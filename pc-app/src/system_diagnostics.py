"""System self-test / Run Diagnostics - 2026-09-08, external feature-gap review, second pass.

An on-demand, comprehensive health check the operator can run any time - NOT the same thing as
preflight.py, which only gates whether it's safe to Start ONE SPECIFIC PROFILE right now (comms,
sensor sanity, that profile's own values against safety limits). This is broader and doesn't gate
anything: communication depth (does the board answer register reads, not just the temps poll),
disk space, host system health, configuration drift against any saved known-good reference,
calibration/self-tune staleness, automatic backup freshness, and USB adapter identity - everything
this project has already built a dedicated check for, pulled into one report so an operator can
ask "is everything actually OK" without visiting five different tabs.

Deliberately a PURE function over already-gathered data (run_diagnostics() below), not something
that touches the bus itself - each app's own orchestration (see main_window.py's
_on_run_diagnostics / web_server.py's api_run_diagnostics) is responsible for actually fetching
live reads, disk/system status, etc. first. This keeps the actual judgment logic here fully unit-
testable without a real bus or filesystem, matching every other "compute_..." module in this
project (compute_zone_statistics, compute_maintenance_status, compute_drift, ...).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

# Same bounds as preflight.py's own sensor-sanity check (not imported directly - duplicating two
# named constants is simpler and clearer than reaching into another module's private names).
_PLAUSIBLE_MIN_C = -10.0
_PLAUSIBLE_MAX_C = 400.0

# An automatic backup older than this many multiples of its own scheduled interval is flagged -
# see automatic_backup.py's _AUTO_BACKUP_INTERVAL_S (24h) - 2x gives real headroom for an app
# that wasn't running at exactly the right moment, without silently tolerating a backup that's
# actually stopped happening.
_STALE_BACKUP_INTERVAL_MULTIPLE = 2.0
_AUTO_BACKUP_INTERVAL_S = 24 * 3600.0


@dataclass
class DiagnosticCheck:
    name: str
    status: str  # "ok" | "warning" | "critical" | "info"
    detail: str


_STATUS_RANK = {"ok": 0, "info": 0, "warning": 1, "critical": 2}


def overall_status(checks: list[DiagnosticCheck]) -> str:
    worst = max((_STATUS_RANK.get(c.status, 0) for c in checks), default=0)
    return {0: "ok", 1: "warning", 2: "critical"}[worst]


def run_diagnostics(
    *,
    connected: bool,
    live_reading,  # modbus_client.LiveReading or None
    disk_status: dict,
    system_health_status: dict,
    maintenance_status: dict,
    automatic_backups: list[dict],
    remembered_adapter: Optional[dict],
    config_drift: Optional[dict],
    pid_regs: Optional[list[int]],
    fan_timing_regs: Optional[list[int]],
) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []

    # -- Communication --
    if connected and live_reading is not None:
        checks.append(DiagnosticCheck("Communication", "ok", "Connected, live data available."))
    elif connected:
        checks.append(DiagnosticCheck("Communication", "warning", "Connected, but no live reading observed yet."))
    else:
        checks.append(DiagnosticCheck("Communication", "critical", "Not connected."))

    # -- Sensors --
    if live_reading is not None:
        implausible = []
        for name, value in (
            ("Upper", live_reading.upper_temp), ("Lower", live_reading.lower_temp),
            ("Infrared", live_reading.infrared_temp), ("Probe", live_reading.probe_temp),
        ):
            if not (_PLAUSIBLE_MIN_C <= value <= _PLAUSIBLE_MAX_C):
                implausible.append(f"{name} ({value:.1f}C)")
        if implausible:
            checks.append(DiagnosticCheck("Sensors", "critical", "Implausible reading(s): " + ", ".join(implausible)))
        else:
            checks.append(DiagnosticCheck("Sensors", "ok", "All 4 sensors within plausible range."))
    else:
        checks.append(DiagnosticCheck("Sensors", "info", "No live reading to check yet."))

    # -- Register-level readability (PID / fan timing) --
    if pid_regs is not None and fan_timing_regs is not None:
        checks.append(DiagnosticCheck("Register Readability", "ok", "PID and fan-timing/compensation registers both read successfully."))
    else:
        missing = []
        if pid_regs is None:
            missing.append("PID")
        if fan_timing_regs is None:
            missing.append("fan-timing/compensation")
        checks.append(DiagnosticCheck("Register Readability", "critical", f"Could not read: {', '.join(missing)}."))

    # -- Disk space --
    level = disk_status.get("level", "unknown")
    if level == "ok":
        checks.append(DiagnosticCheck("Disk Space", "ok", f"{disk_status.get('free_pct', 0):.1f}% free."))
    elif level == "unknown":
        checks.append(DiagnosticCheck("Disk Space", "info", disk_status.get("error", "Could not check disk space.")))
    else:
        checks.append(DiagnosticCheck("Disk Space", level, f"{disk_status.get('free_pct', 0):.1f}% free."))

    # -- Host system health --
    if not system_health_status.get("available"):
        checks.append(DiagnosticCheck("Host System Health", "info", system_health_status.get("error", "Not available.")))
    else:
        hs_level = system_health_status["level"]
        checks.append(DiagnosticCheck(
            "Host System Health", hs_level,
            f"Load average {system_health_status['load_avg'][0]:.2f} "
            f"({system_health_status['cpu_count']} core(s)), memory {system_health_status['mem_used_pct']:.1f}% used.",
        ))

    # -- Configuration drift --
    if config_drift is None:
        checks.append(DiagnosticCheck("Machine Configuration", "info", "No known-good configuration saved yet."))
    elif config_drift["has_drift"]:
        all_diffs = config_drift["pid_diffs"] + config_drift["fan_timing_diffs"] + config_drift["temp_comp_diffs"]
        checks.append(DiagnosticCheck("Machine Configuration", "warning", "Drifted from known-good: " + "; ".join(all_diffs)))
    else:
        checks.append(DiagnosticCheck("Machine Configuration", "ok", "Matches saved known-good reference."))

    # -- Calibration / self-tune staleness (reusing maintenance.compute_maintenance_status()'s
    # own per-zone due_status - never a second judgment about what "overdue" means) --
    cal_overdue = [zone for zone, info in maintenance_status.get("calibration", {}).items() if info["status"] == "overdue"]
    if cal_overdue:
        checks.append(DiagnosticCheck("Calibration", "warning", f"Overdue for: {', '.join(z.capitalize() for z in cal_overdue)}."))
    else:
        checks.append(DiagnosticCheck("Calibration", "ok", "No zone overdue for calibration."))

    st_overdue = [zone for zone, info in maintenance_status.get("self_tune", {}).items() if info["status"] == "overdue"]
    if st_overdue:
        checks.append(DiagnosticCheck("Self-Tune Review", "warning", f"Overdue for: {', '.join(z.capitalize() for z in st_overdue)}."))
    else:
        checks.append(DiagnosticCheck("Self-Tune Review", "ok", "No zone overdue for self-tune review."))

    # -- Automatic backups --
    if not automatic_backups:
        checks.append(DiagnosticCheck("Automatic Backups", "warning", "No automatic backup has ever been created."))
    else:
        newest = automatic_backups[0]
        age_s = time.time() - (newest.get("created_at") or 0)
        if age_s > _AUTO_BACKUP_INTERVAL_S * _STALE_BACKUP_INTERVAL_MULTIPLE:
            checks.append(DiagnosticCheck("Automatic Backups", "warning", f"Newest backup is {age_s / 3600:.1f} hours old - expected roughly daily."))
        else:
            checks.append(DiagnosticCheck("Automatic Backups", "ok", f"Newest backup is {age_s / 3600:.1f} hours old ({len(automatic_backups)} kept)."))

    # -- USB adapter identity --
    if remembered_adapter is None:
        checks.append(DiagnosticCheck("USB Adapter Identity", "info", "No adapter identity remembered yet."))
    elif remembered_adapter.get("currently_resolves_to"):
        checks.append(DiagnosticCheck("USB Adapter Identity", "ok", f"Resolves to {remembered_adapter['currently_resolves_to']}."))
    else:
        checks.append(DiagnosticCheck("USB Adapter Identity", "warning", "Remembered adapter not currently found - check it's plugged in."))

    return checks
