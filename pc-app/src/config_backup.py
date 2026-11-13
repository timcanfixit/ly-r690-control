"""Full configuration backup/restore - bundles everything this app has locally, into one JSON
file: safety limits, alert thresholds, every saved local profile (current version only - see
profile_store.py for full version history, not duplicated here to keep backups a reasonable
size), and PID send history.

Explicitly does NOT claim to include "profile values read from the machine" - the curve/profile
registers aren't reliably readable over Modbus at all on this board (see
docs/06-modbus-register-map.md's "Curve/profile active-read investigation"), so profiles in a
backup are always this application's own local library, never a live machine dump. Likewise PID
gains here are the SEND history (what this app told the machine), not a guaranteed-current machine
readback - PID reads work, but a backup is a point-in-time snapshot of app state, not a live poll.
"""
from __future__ import annotations

import time
from dataclasses import asdict

from modbus_client import Profile, CurveSection
from safety_limits import SafetyLimits, parse_safety_limit_updates
from alerts import AlertConfig
import profile_store
import pid_history

SCHEMA = "ly-r690-backup-v1"


def create_backup(safety_limits: SafetyLimits, alert_config: AlertConfig) -> dict:
    profiles = []
    for name in profile_store.list_profiles():
        try:
            profile = profile_store.load_profile(name)
        except (FileNotFoundError, KeyError):
            continue
        profiles.append({
            "name": profile.name,
            "sections": [asdict(s) for s in profile.sections],
            "metadata": profile_store.get_profile_metadata(name),
            "version": profile_store.get_profile_version(name),
        })
    return {
        "schema": SCHEMA,
        "created_at": time.time(),
        "safety_limits": asdict(safety_limits),
        "alert_config": asdict(alert_config),
        "profiles": profiles,
        "pid_history": pid_history.get_pid_history(),
    }


def restore_backup(data: dict, safety_limits: SafetyLimits, alert_config: AlertConfig) -> dict:
    """Applies a backup's safety_limits/alert_config fields onto the given live objects (mutated
    in place, since both are already shared/referenced throughout each app), and restores every
    profile into the local library (each restore is itself a normal save - see
    profile_store.save_profile - so it's versioned like any other change, not a silent overwrite).
    Returns a summary of what was restored, not the full data back."""
    if data.get("schema") != SCHEMA:
        raise ValueError(f"Unrecognized backup schema: {data.get('schema')!r}")

    # Review finding #7 (2026-11-12): VALIDATE EVERYTHING FIRST, then apply. Before this a backup holding
    # e.g. "max_upper_temp_c": "disabled" was set straight onto the live limits (a later numeric compare
    # then raised on every reading), and a bad profile halfway through left the limits already changed.
    # Any ValueError below happens before a single live object has been touched.
    raw_limits = data.get("safety_limits", {}) or {}
    if not isinstance(raw_limits, dict):
        raise ValueError("safety_limits: must be an object")
    limit_updates = parse_safety_limit_updates(raw_limits)  # same bounds the settings UI enforces

    raw_alerts = data.get("alert_config", {}) or {}
    if not isinstance(raw_alerts, dict):
        raise ValueError("alert_config: must be an object")
    alert_updates: dict[str, float] = {}
    import math
    for field_name, value in raw_alerts.items():
        if not hasattr(alert_config, field_name):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError(f"alert_config.{field_name}: must be a non-negative finite number")
        alert_updates[field_name] = float(value)

    parsed_profiles = []
    for p in data.get("profiles", []) or []:
        try:
            name = p["name"]
            if not isinstance(name, str) or not name.strip():
                raise ValueError("name must be a non-empty string")
            sections = [CurveSection(**s) for s in p["sections"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"profile {p.get('name', '?') if isinstance(p, dict) else '?'!r}: malformed ({exc})")
        parsed_profiles.append((p, Profile(name=name, sections=sections)))

    for field_name, value in limit_updates.items():
        setattr(safety_limits, field_name, value)
    for field_name, value in alert_updates.items():
        setattr(alert_config, field_name, value)

    restored_profiles = []
    skipped_profiles = []
    for p, profile in parsed_profiles:
        try:
            profile_store.save_profile(profile, metadata=p.get("metadata"))
        except ValueError as exc:
            # 2026-09-08 audit finding: save_profile() now rejects a genuine filename collision
            # (see its own docstring) rather than silently corrupting an unrelated profile - one
            # bad profile in a backup must not abort restoring every profile after it in the same
            # backup, the same "a single bad entry can't take down the whole batch" rule
            # profile_import.py's own bulk-import already follows.
            skipped_profiles.append({"name": p["name"], "reason": str(exc)})
            continue
        restored_profiles.append(p["name"])

    return {
        "profiles_restored": restored_profiles,
        "profiles_skipped": skipped_profiles,
        "safety_limits_restored": bool(data.get("safety_limits")),
        "alert_config_restored": bool(data.get("alert_config")),
        "backup_created_at": data.get("created_at"),
    }
