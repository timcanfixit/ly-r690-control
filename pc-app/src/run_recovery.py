"""Run State Recovery - detects a run that was still active when the application disappeared
(crash, Windows restart, Raspberry Pi power loss, an unhandled exception) rather than staying
silent about it. A small marker file is written the instant a run starts and removed the instant
it ends through any KNOWN path (a manual Stop, or a fault-triggered auto-stop - both are cases
where this process knows exactly what happened and why). If that marker still exists the next
time the app starts, the previous run never reached one of those known endings - the process
itself vanished while it thought a run was active, and that must be reported, not silently
assumed to be fine.

This does not, and cannot, independently verify whether the physical machine is still actually
running - it only reports that the SOFTWARE'S OWN RECORD of the run never got a clean ending, so a
recovered run is marked INTERRUPTED in its summary, never COMPLETE.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

if getattr(sys, "frozen", False):
    MARKER_PATH = Path(sys.executable).resolve().parent / "active_run.json"
else:
    MARKER_PATH = Path(__file__).resolve().parent.parent / "active_run.json"


@dataclass
class InterruptedRun:
    profile_name: str
    started_at: float
    log_filename: str


def mark_run_started(profile_name: str, log_filename: str) -> None:
    MARKER_PATH.write_text(json.dumps({
        "profile_name": profile_name,
        "started_at": time.time(),
        "log_filename": log_filename,
    }))


def mark_run_ended() -> None:
    """Call this from every KNOWN run-ending path (manual Stop, fault-triggered auto-stop) - not
    just the happy path. Both of those are cases this process understands and has already
    recorded (the fault itself is in fault_manager's history); only a genuine disappearance should
    ever leave the marker behind for the next startup to find."""
    if MARKER_PATH.exists():
        MARKER_PATH.unlink()


def check_for_interrupted_run() -> Optional[InterruptedRun]:
    """Call once at startup, before any new run can begin. Returns the leftover marker's info if
    the previous run never reached mark_run_ended(), else None."""
    if not MARKER_PATH.exists():
        return None
    try:
        data = json.loads(MARKER_PATH.read_text())
        return InterruptedRun(
            profile_name=data.get("profile_name", "unknown"),
            started_at=data.get("started_at", 0.0),
            log_filename=data.get("log_filename", ""),
        )
    except (json.JSONDecodeError, OSError):
        return None


def mark_run_as_interrupted(log_filename: str) -> None:
    """Flags the recovered run's own summary sidecar (see run_finalization.py) as INTERRUPTED,
    not COMPLETE - imported lazily to avoid a hard import-time dependency for callers that only
    need the marker-file half of this module (e.g. a lightweight startup check)."""
    from data_logger import LOGS_DIR
    from run_finalization import load_summary

    if not log_filename:
        return
    csv_path = LOGS_DIR / log_filename
    existing = load_summary(csv_path) or {}
    existing["interrupted"] = True
    csv_path.with_suffix(".summary.json").write_text(json.dumps(existing, indent=2))


def acknowledge_interrupted_run(log_filename: str) -> None:
    """Call once the operator has seen the interrupted-run notice - flags that run's summary and
    clears the marker so it isn't reported again on the next startup."""
    mark_run_as_interrupted(log_filename)
    mark_run_ended()
