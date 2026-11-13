"""Automatic backups - 2026-09-08, external feature-gap review, second pass.

config_backup.py already builds a full backup (safety limits, alert thresholds, every saved
profile, PID send history) and restores from one - but only ever on explicit operator action (an
Export/Import button). Nothing previously ran that on a schedule, so a Pi that quietly runs for
months with nobody remembering to click Export could lose everything to a bad SD card with zero
recent backup to restore from.

This wraps that existing mechanism with scheduling and retention, nothing more: once a day (see
_AUTO_BACKUP_INTERVAL_S), write a new timestamped backup file, then prune down to the most recent
_MAX_AUTO_BACKUPS so this directory doesn't grow forever. "Due" is determined by the newest
existing backup's own timestamp - no separate state file needed, and a fresh install with zero
backups is immediately "due" rather than waiting a full day for the first one.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

import config_backup

if getattr(sys, "frozen", False):
    BACKUPS_DIR = Path(sys.executable).resolve().parent / "backups"
else:
    BACKUPS_DIR = Path(__file__).resolve().parent.parent / "backups"

_AUTO_BACKUP_INTERVAL_S = 24 * 3600.0
_MAX_AUTO_BACKUPS = 30


def list_automatic_backups() -> list[dict]:
    """Newest first. Reads just enough of each file to report created_at/size - never the full
    profile list, which nobody needs just to see what backups exist."""
    if not BACKUPS_DIR.exists():
        return []
    backups = []
    for path in BACKUPS_DIR.glob("backup_*.json"):
        try:
            data = json.loads(path.read_text())
            backups.append({
                "filename": path.name,
                "created_at": data.get("created_at"),
                "size_bytes": path.stat().st_size,
                "profile_count": len(data.get("profiles", [])),
            })
        except (json.JSONDecodeError, OSError):
            continue
    backups.sort(key=lambda b: b["created_at"] or 0, reverse=True)
    return backups


def _prune_old_backups() -> None:
    backups = list_automatic_backups()
    for stale in backups[_MAX_AUTO_BACKUPS:]:
        try:
            (BACKUPS_DIR / stale["filename"]).unlink()
        except OSError:
            pass


def is_backup_due() -> bool:
    backups = list_automatic_backups()
    if not backups:
        return True
    newest_created_at = backups[0]["created_at"] or 0
    return (time.time() - newest_created_at) >= _AUTO_BACKUP_INTERVAL_S


def create_automatic_backup(safety_limits, alert_config) -> Path:
    BACKUPS_DIR.mkdir(exist_ok=True)
    backup = config_backup.create_backup(safety_limits, alert_config)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(backup["created_at"]))
    path = BACKUPS_DIR / f"backup_{stamp}.json"
    path.write_text(json.dumps(backup, indent=2))
    _prune_old_backups()
    return path


def run_automatic_backup_if_due(safety_limits, alert_config) -> Optional[Path]:
    """The one function callers actually need - checks is_backup_due() and creates+prunes if so,
    else does nothing. Safe to call as often as a caller likes; the check itself is cheap (a
    directory listing), but callers should still throttle their own call frequency (see
    _check_automatic_backup() in main_window.py/web_server.py) rather than calling this every poll
    tick just to avoid the directory listing cost for no reason."""
    if not is_backup_due():
        return None
    return create_automatic_backup(safety_limits, alert_config)
