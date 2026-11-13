"""Maintenance tracking - 2026-09-07, external feature-gap review.

Deliberately built entirely from data this app already has (run history, calibration records,
the new self_tune_log.py) rather than inventing new hardware counters (heater-on hours from a
current sensor, SSR cycle counts, etc.) this project has no way to actually measure. Everything
reported here is either a real, already-logged fact (run count, total logged run time, fault
counts, self-tune session timestamps, calibration record timestamps) or a derived "days since"/
"due status" computed from those facts - never a fabricated hardware-wear estimate.

Due/overdue thresholds are configurable and persisted (MaintenanceConfig), same
load/save-JSON-with-per-field-sanity-bounds pattern as safety_limits.py - see
load_maintenance_config()/save_maintenance_config().
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

import calibration
import self_tune_log
from data_logger import list_runs

if getattr(sys, "frozen", False):
    MAINTENANCE_CONFIG_PATH = Path(sys.executable).resolve().parent / "maintenance_config.json"
else:
    MAINTENANCE_CONFIG_PATH = Path(__file__).resolve().parent.parent / "maintenance_config.json"

CALIBRATION_ZONES = ("upper", "lower", "ir", "probe")
SECONDS_PER_DAY = 86400.0


@dataclass
class MaintenanceConfig:
    calibration_interval_days: float = 90.0
    self_tune_review_interval_days: float = 180.0


_SANE_BOUNDS = {
    "calibration_interval_days": (1.0, 3650.0),
    "self_tune_review_interval_days": (1.0, 3650.0),
}


def load_maintenance_config() -> MaintenanceConfig:
    """Same per-field sanity validation as safety_limits.load_safety_limits() - a corrupt or
    hand-edited value (e.g. a 0 or negative interval, which would report everything as eternally
    overdue) falls back to the factory default for that field instead of being trusted as-is."""
    config = MaintenanceConfig()
    if not MAINTENANCE_CONFIG_PATH.exists():
        return config
    try:
        data = json.loads(MAINTENANCE_CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return config
    if not isinstance(data, dict):
        return config
    valid_names = {f.name for f in fields(MaintenanceConfig)}
    for name, value in data.items():
        if name not in valid_names:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        lo, hi = _SANE_BOUNDS.get(name, (float("-inf"), float("inf")))
        if lo <= value <= hi:
            setattr(config, name, value)
    return config


def save_maintenance_config(config: MaintenanceConfig) -> None:
    MAINTENANCE_CONFIG_PATH.write_text(json.dumps(asdict(config), indent=2))


def _days_since(timestamp: Optional[float], now: float) -> Optional[float]:
    if timestamp is None:
        return None
    return (now - timestamp) / SECONDS_PER_DAY


def _due_status(days_since: Optional[float], interval_days: float) -> str:
    """"never" if it's never been done at all, "overdue" past the interval, "due_soon" within the
    last 20% of the interval, else "ok". Deliberately coarse (four states, not a raw countdown
    the UI has to interpret) so both frontends render the same judgment identically."""
    if days_since is None:
        return "never"
    if days_since >= interval_days:
        return "overdue"
    if days_since >= interval_days * 0.8:
        return "due_soon"
    return "ok"


def _latest_calibration_timestamp_by_zone() -> dict[str, Optional[float]]:
    """calibration records cover multiple zones per entry (one wizard save = several
    CalibrationPoints, all sharing that record's single timestamp) - flatten to the single most
    recent timestamp actually observed for each zone, independent of which record it came from."""
    latest: dict[str, Optional[float]] = {zone: None for zone in CALIBRATION_ZONES}
    for record in calibration.get_calibration_history():
        ts = record.get("timestamp")
        for point in record.get("points", []):
            zone = point.get("zone")
            if zone in latest and (latest[zone] is None or ts > latest[zone]):
                latest[zone] = ts
    return latest


def compute_maintenance_status(config: Optional[MaintenanceConfig] = None, now: Optional[float] = None) -> dict:
    """The one function both frontends call for the Maintenance panel. `now`/`config` are
    injectable purely for testability (real callers omit both) - see this module's own docstring
    for why nothing here is a fabricated hardware-wear estimate."""
    config = config or load_maintenance_config()
    now = now if now is not None else time.time()

    runs = list_runs()
    total_runs = len(runs)
    total_run_hours = sum(r.get("duration_s") or 0.0 for r in runs) / 3600.0
    total_faults = sum(r.get("fault_count") or 0 for r in runs if r.get("fault_count") is not None)
    total_warnings = sum(r.get("warning_count") or 0 for r in runs if r.get("warning_count") is not None)
    runs_with_finalization_data = sum(1 for r in runs if r.get("fault_count") is not None)

    calibration_zones = {}
    latest_calibration = _latest_calibration_timestamp_by_zone()
    for zone in CALIBRATION_ZONES:
        days_since = _days_since(latest_calibration[zone], now)
        calibration_zones[zone] = {
            "last_calibrated_at": latest_calibration[zone],
            "days_since": days_since,
            "status": _due_status(days_since, config.calibration_interval_days),
        }

    self_tune_zones = {}
    for zone in self_tune_log.ZONES:
        last = self_tune_log.get_last_self_tune(zone)
        ended_at = last["ended_at"] if last else None
        days_since = _days_since(ended_at, now)
        session_count = sum(1 for s in self_tune_log.get_self_tune_history() if s["zone"] == zone)
        self_tune_zones[zone] = {
            "last_self_tuned_at": ended_at,
            "days_since": days_since,
            "status": _due_status(days_since, config.self_tune_review_interval_days),
            "session_count": session_count,
        }

    return {
        "config": asdict(config),
        "runs": {
            "total_runs": total_runs,
            "total_run_hours": round(total_run_hours, 2),
            "total_faults": total_faults,
            "total_warnings": total_warnings,
            "runs_with_finalization_data": runs_with_finalization_data,
        },
        "calibration": calibration_zones,
        "self_tune": self_tune_zones,
    }
