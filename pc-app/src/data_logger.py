"""
Structured logging of every reading during a run, to CSV files under pc-app/logs/. Kept
deliberately simple and tabular (timestamp, all 4 temps, status bits, active profile name) so
that once enough real run history has accumulated, a proper ML model could be trained on it -
per the project decision to build rule-based alerting now and structure for ML later.
"""
from __future__ import annotations

import csv
import threading
import shutil
import sys
import time
from pathlib import Path
from typing import Optional, TextIO

from modbus_client import LiveReading
from run_finalization import load_summary, set_run_golden, set_run_notes, set_repair_outcome

# See profile_store.py for why this branches on sys.frozen - __file__ is not a stable location
# for persistent data once bundled into a PyInstaller exe.
if getattr(sys, "frozen", False):
    LOGS_DIR = Path(sys.executable).resolve().parent / "logs"
else:
    LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

FIELDNAMES = [
    "timestamp", "elapsed_s", "upper_temp", "lower_temp", "infrared_temp", "probe_temp",
    "running", "vacuum", "cooling", "profile_name",
]

# Thresholds for check_disk_space() - external feature-gap review, 2026-09-07: every run's
# telemetry lands under LOGS_DIR forever (nothing currently prunes it), and a filling disk was a
# real, previously-unwatched failure mode - on the Pi specifically, a full SD card can also affect
# the OS itself, not just this app. These are free-space PERCENTAGE thresholds, not absolute size,
# so they scale sensibly across a small SD card and a much larger drive alike.
DISK_SPACE_WARNING_FREE_PCT = 20.0
DISK_SPACE_CRITICAL_FREE_PCT = 10.0


def check_disk_space() -> dict:
    """Real disk-usage check on whatever filesystem LOGS_DIR lives on. Returns a plain dict (not
    a dataclass) so it serializes straight to JSON for an API/SSE payload with no extra step.
    Never raises - a health check that itself crashes the polling loop would be worse than no
    check at all. `level` is one of "ok"/"warning"/"critical"/"unknown" (the last only if the
    filesystem call itself failed, e.g. LOGS_DIR's mount was pulled)."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(LOGS_DIR)
    except OSError as exc:
        return {"ok": False, "error": str(exc), "free_pct": None, "free_bytes": None, "total_bytes": None, "level": "unknown"}
    free_pct = (usage.free / usage.total * 100.0) if usage.total else 0.0
    if free_pct < DISK_SPACE_CRITICAL_FREE_PCT:
        level = "critical"
    elif free_pct < DISK_SPACE_WARNING_FREE_PCT:
        level = "warning"
    else:
        level = "ok"
    return {
        "ok": True, "free_pct": round(free_pct, 1), "free_bytes": usage.free,
        "total_bytes": usage.total, "level": level,
    }


class RunLogger:
    def __init__(self):
        self._file: Optional[TextIO] = None
        self._writer: Optional[csv.DictWriter] = None
        self._run_start: Optional[float] = None
        self._profile_name: str = ""
        # Review finding #11 (2026-11-12): log() runs on the poll thread while stop_run()/start_run() run on
        # request threads or the fault path; unguarded, a close between log()'s None-check and its write
        # raised "I/O operation on closed file". Reentrant because start_run() calls stop_run().
        self._lock = threading.RLock()

    def start_run(self, profile_name: str) -> Path:
        with self._lock:
            return self._start_run_locked(profile_name)

    def _start_run_locked(self, profile_name: str) -> Path:
        # A second start_run() before stop_run() (e.g. a duplicate/racing Start request) would
        # otherwise silently orphan the previous file handle, open and never flushed/closed again
        # for the life of the process - close it first so there's never more than one open at once.
        if self._file is not None:
            self.stop_run()
        LOGS_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = LOGS_DIR / f"run_{stamp}.csv"
        # The timestamp alone only has 1-second resolution - two runs started within the same
        # second (a duplicate/racing Start request, or just two runs started back-to-back fast)
        # would otherwise collide on the same filename and silently overwrite each other's data.
        suffix = 1
        while path.exists():
            path = LOGS_DIR / f"run_{stamp}_{suffix}.csv"
            suffix += 1
        self._file = open(path, "w", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=FIELDNAMES)
        self._writer.writeheader()
        self._run_start = time.monotonic()
        self._profile_name = profile_name
        return path

    def log(self, reading: LiveReading) -> None:
        with self._lock:
            self._log_locked(reading)

    def _log_locked(self, reading: LiveReading) -> None:
        if self._writer is None or self._run_start is None:
            return
        self._writer.writerow({
            "timestamp": time.time(),
            "elapsed_s": round(time.monotonic() - self._run_start, 2),
            "upper_temp": reading.upper_temp,
            "lower_temp": reading.lower_temp,
            "infrared_temp": reading.infrared_temp,
            "probe_temp": reading.probe_temp,
            "running": reading.running,
            "vacuum": reading.vacuum,
            "cooling": reading.cooling,
            "profile_name": self._profile_name,
        })
        self._file.flush()

    def stop_run(self) -> None:
        with self._lock:
            try:
                if self._file:
                    self._file.close()
            finally:
                self._file = None
                self._writer = None
                self._run_start = None


def _num(value) -> Optional[float]:
    """Float or None - a corrupt/truncated cell must be skipped, not abort the whole listing
    (audit R06, 2026-11-11: one bad CSV used to make list_runs() raise for every consumer)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def list_runs() -> list[dict]:
    """Summarizes every past run log, most recent first - filename, profile name, start time
    (parsed from the run_YYYYMMDD_HHMMSS.csv filename), duration, and peak temps. Every run has
    been logged since day one, but nothing surfaced this back to either app's UI until now -
    added 2026-08-19 as a run-history viewer. Read-only, never touches the machine."""
    runs = []
    if not LOGS_DIR.exists():
        return runs
    for path in sorted(LOGS_DIR.glob("run_*.csv"), reverse=True):
        try:
            with open(path, newline="") as f:
                rows = list(csv.DictReader(f))
        except (OSError, csv.Error, UnicodeDecodeError):
            continue
        if not rows:
            continue
        stamp = path.stem[len("run_"):]
        try:
            started_str = time.strftime("%Y-%m-%d %H:%M:%S", time.strptime(stamp, "%Y%m%d_%H%M%S"))
        except ValueError:
            started_str = stamp

        def _peak(field: str) -> float:
            values = [v for v in (_num(r.get(field)) for r in rows) if v is not None]
            return max(values) if values else 0.0

        def _time_to_peak(field: str, peak: float) -> float:
            for r in rows:
                v = _num(r.get(field))
                if v is not None and v >= peak:
                    return _num(r.get("elapsed_s")) or 0.0
            return 0.0

        peak_upper = _peak("upper_temp")
        # Fault/warning counts and communication stats aren't in the CSV itself (they aren't
        # per-reading columns) - they live in a companion sidecar written at run stop (see
        # run_finalization.py). Older runs logged before that existed simply have none - reported
        # as None/not-available rather than a fabricated 0.
        summary = load_summary(path) or {}
        runs.append({
            "filename": path.name,
            "profile_name": rows[0].get("profile_name", ""),
            "started": started_str,
            "duration_s": _num(rows[-1].get("elapsed_s")) or 0.0,
            "peak_upper": peak_upper,
            "peak_lower": _peak("lower_temp"),
            "peak_ir": _peak("infrared_temp"),
            "peak_probe": _peak("probe_temp"),
            "time_to_peak_upper_s": _time_to_peak("upper_temp", peak_upper),
            "fault_count": summary.get("fault_count"),
            "warning_count": summary.get("warning_count"),
            "time_above_wetting_s": summary.get("time_above_wetting_s"),
            "cooling_rate_c_per_min": summary.get("cooling_rate_c_per_min"),
            "communication": summary.get("communication"),
            "interrupted": summary.get("interrupted", False),
            "row_count": len(rows),
            # Added 2026-09-07 per external feature-gap review - see run_finalization.py's
            # compute_zone_statistics()/classify_process_result() for what these actually are.
            "zone_statistics": summary.get("zone_statistics"),
            "process_result": summary.get("process_result"),
            "golden": summary.get("golden", False),
            # Operator run notes (2026-09-07, external feature-gap review, second pass).
            "notes": summary.get("notes", ""),
            # Repair outcome tracking (2026-09-07, external feature-gap review, second pass) - see
            # run_finalization.set_repair_outcome()'s docstring for why this stays deliberately
            # minimal (one status + one free-text reason, no CRM).
            "repair_outcome": summary.get("repair_outcome", ""),
            "repair_failure_reason": summary.get("repair_failure_reason", ""),
        })
    return runs


def _validate_run_filename(filename: str) -> None:
    """`filename` may come straight from an HTTP request (the web app's /api/runs/<filename>/...
    routes) - reject anything that isn't a plain "run_*.csv" name (matching list_runs()'s own glob
    pattern) before it's ever used to build a path, the same protection api_download_run() already
    gets for free from send_from_directory(). Shared by every filename-addressed function below."""
    if not (filename.startswith("run_") and filename.endswith(".csv") and "/" not in filename and "\\" not in filename):
        raise ValueError(f"Not a valid run filename: {filename!r}")


def mark_run_golden(filename: str, golden: bool) -> dict:
    """Filename-addressed convenience wrapper over run_finalization.set_run_golden() - the two
    apps' run-history UIs (and the web API) address runs by their CSV filename, same as every
    other per-run endpoint."""
    _validate_run_filename(filename)
    return set_run_golden(LOGS_DIR / filename, golden)


def mark_run_notes(filename: str, notes: str) -> dict:
    """Filename-addressed wrapper over run_finalization.set_run_notes() - same pattern as
    mark_run_golden() above."""
    _validate_run_filename(filename)
    return set_run_notes(LOGS_DIR / filename, notes)


def mark_repair_outcome(filename: str, outcome: str, failure_reason: str = "") -> dict:
    """Filename-addressed wrapper over run_finalization.set_repair_outcome() - same pattern as
    mark_run_golden()/mark_run_notes() above."""
    _validate_run_filename(filename)
    return set_repair_outcome(LOGS_DIR / filename, outcome, failure_reason)


def delete_run(filename: str) -> dict:
    """Permanently removes a run's CSV log and its .summary.json sidecar (if one exists) - the
    only two files any per-run function in this module ever touches (golden/notes/repair-outcome
    all live inside the one sidecar - see run_finalization.py). 2026-09-16, real reported gap: no
    way to delete a run from Run History at all. Irreversible, unlike every other per-run
    mutation here (which are all simple field edits) - there is no undo/trash. Returns {"ok":
    False, "detail": ...} rather than silently "succeeding" if the CSV doesn't exist at all."""
    _validate_run_filename(filename)
    csv_path = LOGS_DIR / filename
    if not csv_path.exists():
        return {"ok": False, "detail": f"No such run: {filename}"}
    deleted = []
    summary_path = csv_path.with_suffix(".summary.json")
    if summary_path.exists():
        summary_path.unlink()
        deleted.append(summary_path.name)
    csv_path.unlink()
    deleted.append(csv_path.name)
    return {"ok": True, "deleted": deleted}


def get_run_summary(filename: str) -> Optional[dict]:
    """Filename-addressed wrapper over run_finalization.load_summary() - the FULL sidecar
    (events, config_snapshot, zone_statistics, everything), unlike list_runs()'s cherry-picked
    subset meant for the run history table. Added 2026-09-07 (external feature-gap review) so the
    event timeline and config snapshot - computed and saved all along, never surfaced anywhere -
    have somewhere to actually be read from. None if there's no sidecar at all for this run."""
    _validate_run_filename(filename)
    return load_summary(LOGS_DIR / filename)
