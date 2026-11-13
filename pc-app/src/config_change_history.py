"""Safety-limit change history - pre-change snapshots + undo - 2026-09-08, external feature-gap
review, second pass.

Mirrors pid_history.py's exact pattern (every SAVE is logged with old/new values; restoring is
itself a new logged change, never a silent rewrite of history) extended to safety_limits.
SafetyLimits - the one other piece of "config that governs how the machine is protected" that
previously had no way to undo a mistaken edit short of restoring an entire daily automatic backup
(see automatic_backup.py), which also brings back every saved profile and PID send history along
with it - overkill for "I fat-fingered one safety limit field".

Deliberately scoped to safety_limits only, not every persisted config in this app - alerts.
AlertConfig has no persistence at all yet (a separate, pre-existing gap this doesn't attempt to
close), and maintenance.MaintenanceConfig/AI assistant settings are informational/preference
settings, not safety-relevant values where an undo actually matters.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Optional

if getattr(sys, "frozen", False):
    CONFIG_CHANGE_HISTORY_PATH = Path(sys.executable).resolve().parent / "config_change_history.json"
else:
    CONFIG_CHANGE_HISTORY_PATH = Path(__file__).resolve().parent.parent / "config_change_history.json"

# Guards the read-modify-write below - same reasoning as pid_history.py's identical lock.
_lock = threading.Lock()

# A rolling window is enough for "undo my last mistake, or the one before it" - not a full audit
# trail (that's what the daily automatic backups are for).
_MAX_HISTORY = 100


def _load() -> list[dict]:
    if not CONFIG_CHANGE_HISTORY_PATH.exists():
        return []
    try:
        return json.loads(CONFIG_CHANGE_HISTORY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def record_config_change(old_values: Optional[dict], new_values: dict, note: str = "") -> None:
    """old_values=None for the very first change this history has ever seen (nothing to diff
    against) - same convention as pid_history.record_pid_change. `note` is the same idea too -
    used to mark a restore's own new entry (e.g. "Restored from 2026-09-08 01:00:00")."""
    with _lock:
        entries = _load()
        entries.append({"timestamp": time.time(), "old_values": old_values, "new_values": new_values, "note": note})
        entries = entries[-_MAX_HISTORY:]
        CONFIG_CHANGE_HISTORY_PATH.write_text(json.dumps(entries, indent=2))


def get_config_change_history() -> list[dict]:
    return _load()


def find_history_entry(timestamp: float) -> Optional[dict]:
    """Same purpose as pid_history.find_history_entry() - lets a caller (the web app's restore
    route in particular) address one entry by timestamp rather than a fragile array index."""
    for entry in _load():
        if entry["timestamp"] == timestamp:
            return entry
    return None


def diff_config_values(old_values: Optional[dict], new_values: dict) -> list[str]:
    """Per-field before/after diff, sorted by field name for a stable, readable order (unlike
    pid_history's diff, these are named dict keys, not a flat positional register list)."""
    if old_values is None:
        return []
    return [
        f"{key}: {old_values.get(key)} -> {new_values[key]}"
        for key in sorted(new_values.keys())
        if old_values.get(key) != new_values[key]
    ]
