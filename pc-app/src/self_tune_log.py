"""Self-tune (PID autotune) session history - 2026-09-07, external feature-gap review (the
maintenance-tracking item).

Confirmed live earlier this same session: a real self-tune leaves COIL_RUNNING False the whole
time, so it isn't part of any profile run, gets no CSV log, no summary sidecar, nothing - once it
ends, there is no record anywhere that it ever happened. That's a real gap for maintenance
tracking, which wants to answer "when did I last self-tune the IR zone?" - this closes it.

Sessions are detected from raw per-tick self-tune coil observations (see SelfTuneTracker) using
the same False->True / True->False transition technique machine_bus.py's own coil-change
diagnostic already uses - a session is only ever recorded once fully observed start-to-end, never
guessed at from a single snapshot. Stored as a flat JSON list in pc-app/self_tune_history.json,
oldest first - same pattern as calibration.py/pid_history.py.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

if getattr(sys, "frozen", False):
    SELF_TUNE_LOG_PATH = Path(sys.executable).resolve().parent / "self_tune_history.json"
else:
    SELF_TUNE_LOG_PATH = Path(__file__).resolve().parent.parent / "self_tune_history.json"

# See pid_history.py's _lock for why - same read-modify-write-a-JSON-file pattern, same race.
_lock = threading.Lock()

ZONES = ("upper", "lower", "ir")


def _load() -> list[dict]:
    if not SELF_TUNE_LOG_PATH.exists():
        return []
    try:
        return json.loads(SELF_TUNE_LOG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def record_self_tune_session(zone: str, started_at: float, ended_at: float) -> None:
    """Called once a self-tune session for `zone` ("upper"/"lower"/"ir") has been observed to
    fully end (a True->False coil transition) - see SelfTuneTracker for how start/end pairing is
    actually detected. Timestamps are real wall-clock time (time.time()), not monotonic - this
    needs to still mean something correctly days or months later."""
    with _lock:
        sessions = _load()
        sessions.append({
            "zone": zone, "started_at": started_at, "ended_at": ended_at,
            "duration_s": ended_at - started_at,
        })
        SELF_TUNE_LOG_PATH.write_text(json.dumps(sessions, indent=2))


def get_self_tune_history() -> list[dict]:
    return _load()


def get_last_self_tune(zone: str) -> dict | None:
    """Most recent completed session for `zone`, or None if it's never been self-tuned (at least
    not since this logging existed) - never fabricates a fake "long ago" answer for that case."""
    sessions = [s for s in _load() if s["zone"] == zone]
    if not sessions:
        return None
    return max(sessions, key=lambda s: s["ended_at"])


class SelfTuneTracker:
    """Feed it the 3 current self-tune coil booleans on every live reading (see
    LiveReading.self_tune_upper/lower/ir) - detects a False->True transition per zone as a
    session start and a True->False transition as the session end, recording the completed
    session via record_self_tune_session(). One instance shared for the whole app's lifetime
    (module-level in web_server.py, an instance attribute in main_window.py) - self-tune isn't
    tied to a profile run the way RunFinalizationTracker is, so it has no natural "per-run"
    lifetime to reset against.

    A session still in progress when the app closes/crashes is simply never recorded (no
    started-but-never-ended entry is ever written) - an honest "we don't know how long it ran",
    not a guessed or truncated duration."""

    def __init__(self):
        self._active_since: dict[str, float] = {}  # zone -> wall-clock start time, while active

    def observe(self, self_tune_upper: bool, self_tune_lower: bool, self_tune_ir: bool) -> None:
        now = time.time()
        for zone, active in (("upper", self_tune_upper), ("lower", self_tune_lower), ("ir", self_tune_ir)):
            was_active = zone in self._active_since
            if active and not was_active:
                self._active_since[zone] = now
            elif not active and was_active:
                started_at = self._active_since.pop(zone)
                record_self_tune_session(zone, started_at, now)
