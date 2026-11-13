"""Local save/load for profiles, independent of the machine. Profiles are stored as JSON in
pc-app/profiles/ so they can be versioned, backed up, and edited without needing the machine
connected."""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

from atomic_io import atomic_write_text, quarantine_corrupt
from modbus_client import CurveSection, Profile

# When frozen by PyInstaller, __file__ resolves inside the ephemeral per-run extraction dir
# (onefile) or the install folder (onedir) - either way, not a sensible place for persistent user
# data. Use the .exe's own location instead so profiles survive across runs and app moves.
if getattr(sys, "frozen", False):
    PROFILES_DIR = Path(sys.executable).resolve().parent / "profiles"
else:
    PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"

# Profile names are free-form user text (e.g. from an imported spreadsheet: "PS5 HDMI ENCODER /
# SMALL IC REFLOW") and may contain characters that are illegal - or worse, silently meaningful -
# in a filename. '/' in particular gets interpreted as a path separator by pathlib, causing
# save_profile to try writing into a non-existent subdirectory and throw FileNotFoundError
# (confirmed live 2026-08-19 - crashed the desktop app's import feature repeatedly). Sanitize the
# filename only; the real name is preserved as-is in the JSON content and in Profile.name.
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Guards save_profile's read-archive-write - see pid_history.py's identical lock for why. One
# global lock (not per-name) since saves are rare, deliberate user actions, never a hot path.
_save_lock = threading.Lock()


def _safe_filename(name: str) -> str:
    safe = _UNSAFE_FILENAME_CHARS.sub("-", name).strip().rstrip(".")
    return safe or "Untitled"


def list_profiles() -> list[str]:
    """Returns display names (the real Profile.name from each file's content), not filenames -
    those may differ now that filenames are sanitized for filesystem safety."""
    PROFILES_DIR.mkdir(exist_ok=True)
    names = []
    for path in sorted(PROFILES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            names.append(data.get("name", path.stem))
        except (json.JSONDecodeError, OSError):
            names.append(path.stem)
    return names


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        # Corrupt (as opposed to missing): keep a copy so the next save can't erase the only evidence.
        quarantine_corrupt(path)
        return {}
    except OSError:
        return {}


def _find_case_variant(name: str, ignore_names: tuple = ()) -> str | None:
    """Returns the name of an existing saved profile that differs from `name` ONLY by letter case
    (e.g. "XBOX 360 XCGPU REFLOW" vs "Xbox 360 XCGPU Reflow"), or None.

    2026-11-12: on the Pi's case-sensitive filesystem those two names get two different files, so
    nothing stopped a bulk backup restore or spreadsheet import from quietly re-creating stale
    ALL-CAPS copies next to the real profiles - which is exactly what happened (13 duplicates came
    back in one restore, hours after being cleaned up). An exact-name match is NOT a variant (that's
    just a normal re-save of the same profile); neither is anything in `ignore_names` (the profile
    currently being renamed away from)."""
    key = name.strip().lower()
    for path in PROFILES_DIR.glob("*.json"):
        other = _read_json(path).get("name", path.stem)
        if other != name and other not in ignore_names and other.strip().lower() == key:
            return other
    return None


def _case_variant_error(name: str, variant: str) -> ValueError:
    return ValueError(
        f"Cannot save '{name}': a profile named '{variant}' already exists and differs only by "
        f"letter case. Saving both would leave a confusing near-duplicate (this is how stale copies "
        f"kept coming back after a backup restore) - rename one of them, or delete the other first."
    )


def save_profile(profile: Profile, metadata: dict | None = None, _replacing_name: str | None = None) -> Path:
    """Every save archives whatever was previously stored under this name into "history" and
    bumps an incrementing version number - "never silently overwrite a profile when versioning is
    enabled" (this project's version of that is always on, not a toggle, since there's no real
    cost to keeping the history). metadata is optional free-form documentation (process type,
    solder, notes, etc.) separate from the machine curve data itself - pass None to leave
    whatever was already stored unchanged.

    2026-09-08 audit finding: _safe_filename() only guarantees a FILESYSTEM-SAFE name, not a
    UNIQUE one - two genuinely different profile names can sanitize to the identical filename
    (e.g. "PS5 HDMI ENCODER / SMALL IC REFLOW" and "PS5 HDMI ENCODER - SMALL IC REFLOW" both
    become "PS5 HDMI ENCODER - SMALL IC REFLOW.json"). Before this check, saving the second one
    would silently treat the first one's file as "a prior version of the SAME profile" - archiving
    its sections under the wrong name, inheriting its metadata by accident, and making it vanish
    from list_profiles() entirely. Raises ValueError instead of guessing; the caller (a real user
    action) is in a far better position to just rename one of them than this layer is to silently
    pick a "safe" resolution that would then be unfindable by its own name on the next load.

    `_replacing_name` is for rename_profile()'s own use only: a rename computes the SAME path for
    both its old and new name whenever they sanitize identically (e.g. "Test:1" -> "Test?1" both
    become "Test-1.json") - that's a legitimate in-place rename of one profile, not a collision
    with some unrelated second profile, so the existing file's name is allowed to match either the
    new name or this."""
    PROFILES_DIR.mkdir(exist_ok=True)
    path = PROFILES_DIR / f"{_safe_filename(profile.name)}.json"
    with _save_lock:
        # Checked FIRST: on a case-insensitive filesystem (Windows) a case-only variant also trips
        # the filename-collision check below, and on a case-sensitive one (the Pi) it trips nothing
        # else at all - this gives the same specific, actionable message on both.
        variant = _find_case_variant(profile.name, ignore_names=(_replacing_name,) if _replacing_name else ())
        if variant is not None:
            raise _case_variant_error(profile.name, variant)
        existing = _read_json(path)
        if existing and existing.get("name") not in (profile.name, _replacing_name):
            raise ValueError(
                f"Cannot save '{profile.name}': a different existing profile "
                f"('{existing.get('name')}') already uses the same filename once unsafe "
                f"characters are replaced ('{path.name}'). Rename one of them so they no longer "
                f"collide."
            )
        history = existing.get("history", [])
        if "sections" in existing:
            history.append({
                "version": existing.get("version", 1),
                "saved_at": time.time(),
                "sections": existing["sections"],
            })
        data = {
            "name": profile.name,
            "sections": [asdict(s) for s in profile.sections],
            "version": existing.get("version", 0) + 1,
            "metadata": metadata if metadata is not None else existing.get("metadata", {}),
            "history": history,
        }
        atomic_write_text(path, json.dumps(data, indent=2))
    return path


def load_profile(name: str) -> Profile:
    path = PROFILES_DIR / f"{_safe_filename(name)}.json"
    data = json.loads(path.read_text())
    sections = [CurveSection(**s) for s in data["sections"]]
    return Profile(name=data["name"], sections=sections)


def get_profile_metadata(name: str) -> dict:
    return _read_json(PROFILES_DIR / f"{_safe_filename(name)}.json").get("metadata", {})


def get_profile_version(name: str) -> int:
    return _read_json(PROFILES_DIR / f"{_safe_filename(name)}.json").get("version", 1)


def get_profile_history(name: str) -> list[dict]:
    """Returns [{version, saved_at, sections}, ...] for every version PRIOR to the current one,
    oldest first - the current version lives in the main file itself (load_profile), not here."""
    return _read_json(PROFILES_DIR / f"{_safe_filename(name)}.json").get("history", [])


def restore_profile_version(name: str, version: int) -> Profile:
    """Restores a past version's curve data as the new current version - itself recorded as a new
    save (see save_profile), not a rewrite of history. Raises ValueError if that version isn't in
    this profile's history."""
    path = PROFILES_DIR / f"{_safe_filename(name)}.json"
    data = _read_json(path)
    entry = next((h for h in data.get("history", []) if h["version"] == version), None)
    if entry is None:
        raise ValueError(f"No version {version} found in history for '{name}'.")
    sections = [CurveSection(**s) for s in entry["sections"]]
    restored = Profile(name=name, sections=sections)
    save_profile(restored, metadata=data.get("metadata", {}))
    return restored


def delete_profile(name: str) -> None:
    path = PROFILES_DIR / f"{_safe_filename(name)}.json"
    if path.exists():
        path.unlink()


def rename_profile(old_name: str, new_name: str) -> Path:
    """Renames by loading under the old name and saving under the new one - simplest correct
    implementation given filenames are already a sanitized derivative of the name, not the name
    itself (see _safe_filename). Raises FileNotFoundError if old_name doesn't exist. Raises
    ValueError if new_name collides (after sanitization) with a DIFFERENT, unrelated existing
    profile - see save_profile()'s own docstring for why that's never silently resolved."""
    old_path = PROFILES_DIR / f"{_safe_filename(old_name)}.json"
    if not old_path.exists():
        raise FileNotFoundError(old_path)
    new_path = PROFILES_DIR / f"{_safe_filename(new_name)}.json"
    with _save_lock:
        record = _read_json(old_path)
        if record.get("name") != old_name:
            raise ValueError(f"'{old_name}' does not match the profile stored in {old_path.name}.")
        variant = _find_case_variant(new_name, ignore_names=(old_name,))
        if variant is not None:
            raise _case_variant_error(new_name, variant)
        existing = _read_json(new_path)
        if existing and old_path != new_path and existing.get("name") != new_name:
            raise ValueError(
                f"Cannot rename to '{new_name}': a different existing profile ('{existing.get('name')}') "
                f"already uses the same filename once unsafe characters are replaced ('{new_path.name}')."
            )
        if existing and old_path != new_path:
            raise ValueError(f"A profile named '{new_name}' already exists.")
        # Audit R03 (2026-11-11): the old implementation re-saved only the curve under the new
        # name, silently dropping metadata, version history and version number on every rename.
        # Carry the WHOLE stored record across; write the new file before removing the old one.
        record["name"] = new_name
        atomic_write_text(new_path, json.dumps(record, indent=2))
        if old_path != new_path:
            old_path.unlink()
    return new_path
