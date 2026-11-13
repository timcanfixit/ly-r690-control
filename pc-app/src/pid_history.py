"""PID change history - every SEND to the machine is logged (not every read - reads don't change
anything and would just spam the log). Stored as a flat JSON list in pc-app/pid_history.json,
oldest first. Old values are whatever was last read/sent before this change, or None if unknown
(e.g. the very first send this session, before any read happened)."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

if getattr(sys, "frozen", False):
    PID_HISTORY_PATH = Path(sys.executable).resolve().parent / "pid_history.json"
else:
    PID_HISTORY_PATH = Path(__file__).resolve().parent.parent / "pid_history.json"

# Guards the read-modify-write below - the web app's Flask dev server runs threaded, so two PID
# sends landing in the same instant (two browser tabs, a double-click) could otherwise both read
# the same pre-change file and each write back a version missing the other's entry.
_lock = threading.Lock()


def _load() -> list[dict]:
    if not PID_HISTORY_PATH.exists():
        return []
    try:
        return json.loads(PID_HISTORY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def record_pid_change(old_values: list[int] | None, new_values: list[int], note: str = "") -> None:
    with _lock:
        entries = _load()
        entries.append({
            "timestamp": time.time(),
            "old_values": old_values,
            "new_values": new_values,
            "note": note,
        })
        PID_HISTORY_PATH.write_text(json.dumps(entries, indent=2))


def get_pid_history() -> list[dict]:
    return _load()


def find_history_entry(timestamp: float) -> dict | None:
    """Looks up one history entry by its exact timestamp - 2026-09-08, external feature-gap
    review, second pass. Used by the restore feature so a caller (the web app's restore route in
    particular, addressing an entry across an HTTP round trip) can reference one entry without
    relying on array-index math staying valid between a history fetch and a later restore action."""
    for entry in _load():
        if entry["timestamp"] == timestamp:
            return entry
    return None


def diff_pid_values(old_values: list[int] | None, new_values: list[int]) -> list[str]:
    """Per-register before/after diff for one history entry - 2026-09-08, external feature-gap
    review, second pass. The existing history viewer only ever showed a bare "N register(s)
    changed" count; this is what an actual comparison needs. old_values=None (the very first send,
    with no prior read to compare against) produces no diff lines at all - there's nothing to
    compare, not a fabricated "everything changed"."""
    if old_values is None:
        return []
    return [
        f"register {i}: {old} -> {new}"
        for i, (old, new) in enumerate(zip(old_values, new_values))
        if old != new
    ]
