"""Crash-safe file writes (review finding #14, 2026-11-12).

`Path.write_text` truncates the destination and then writes, so a reader that runs in between sees a
partial file, and a crash or power loss mid-write destroys the previous contents - for a saved profile that
means its whole embedded version history. Writing a temp file beside the destination, flushing it to disk and
then `os.replace`-ing it over the destination makes the swap atomic: readers see either the old complete file
or the new complete file, never a mix."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def quarantine_corrupt(path: Path) -> Path | None:
    """Copies an unreadable file aside (never deletes it) so a later save cannot silently overwrite the only
    remaining evidence/recovery copy. Returns the copy's path, or None if it could not be made."""
    path = Path(path)
    dest = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        dest.write_bytes(path.read_bytes())
        return dest
    except OSError:
        return None
