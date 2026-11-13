"""Host system health monitoring - 2026-09-08, external feature-gap review, second pass.

The Pi runs this app 24/7, unattended, as a systemd service (see docs/00-project-status-and-scope.
md for its operational role) - if IT runs low on memory or gets pegged on CPU (a runaway process,
a full-disk log spiral, whatever), nothing previously would have noticed until something else
broke first. data_logger.check_disk_space() already covers disk; this covers CPU load and memory.

Deliberately stdlib-only, reading directly from /proc (Linux only) rather than adding a psutil
dependency - this project has consistently avoided adding dependencies/complexity it doesn't
need (see the project's own "keep the architecture simple" philosophy), and /proc gives everything
needed here in a few lines. On a non-Linux host (the Windows dev PC this app also occasionally
runs on) this honestly reports "not available on this platform" rather than faking a number or
requiring a platform-specific dependency just for that occasional use case.
"""
from __future__ import annotations

import os
import platform
from typing import Optional

# A Pi (or the Linux host this typically runs on) doing real work should rarely peg its 1-minute
# load average much above its core count - these ratios are generous on purpose (this is a small,
# fixed-purpose device running one Flask app + a modbus poll loop, not a general server).
_LOAD_WARNING_RATIO = 1.5
_LOAD_CRITICAL_RATIO = 3.0
_MEM_WARNING_PCT = 85.0
_MEM_CRITICAL_PCT = 95.0


def _read_proc_loadavg() -> Optional[tuple[float, float, float]]:
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except (OSError, ValueError, IndexError):
        return None


def _read_proc_meminfo() -> Optional[dict]:
    try:
        values = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    values[key] = int(rest.strip().split()[0])  # kB
        if "MemTotal" not in values or "MemAvailable" not in values:
            return None
        return values
    except (OSError, ValueError, IndexError):
        return None


def _read_proc_uptime() -> Optional[float]:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def get_system_health() -> dict:
    """Returns {"available": False, "error": ...} on any non-Linux host or read failure - never a
    fabricated number. On success: load_avg (1/5/15 min), cpu_count, mem_total_mb,
    mem_available_mb, mem_used_pct, uptime_s, and an overall "level" (ok/warning/critical) taken as
    the worse of the CPU-load level and the memory level."""
    if platform.system() != "Linux":
        return {"available": False, "error": f"Not available on {platform.system()} - reads /proc, Linux only."}

    load_avg = _read_proc_loadavg()
    meminfo = _read_proc_meminfo()
    uptime_s = _read_proc_uptime()
    if load_avg is None or meminfo is None or uptime_s is None:
        return {"available": False, "error": "Could not read /proc/loadavg, /proc/meminfo, or /proc/uptime."}

    cpu_count = os.cpu_count() or 1
    mem_total_mb = meminfo["MemTotal"] / 1024.0
    mem_available_mb = meminfo["MemAvailable"] / 1024.0
    mem_used_pct = 100.0 * (1.0 - meminfo["MemAvailable"] / meminfo["MemTotal"]) if meminfo["MemTotal"] else 0.0

    load_ratio = load_avg[0] / cpu_count
    load_level = "critical" if load_ratio >= _LOAD_CRITICAL_RATIO else "warning" if load_ratio >= _LOAD_WARNING_RATIO else "ok"
    mem_level = "critical" if mem_used_pct >= _MEM_CRITICAL_PCT else "warning" if mem_used_pct >= _MEM_WARNING_PCT else "ok"
    # Overall `level` (the worse of the two) is unchanged for existing consumers (system_diagnostics.
    # py, the disk-space-style fault check in main_window.py/web_server.py) - load_level/mem_level
    # are additive, added 2026-09-15 for the System Health tab so each metric's own card can be
    # colored by ITS OWN state instead of borrowing whichever of the two happens to be worse.
    level = "critical" if "critical" in (load_level, mem_level) else "warning" if "warning" in (load_level, mem_level) else "ok"

    return {
        "available": True,
        "load_avg": load_avg,
        "cpu_count": cpu_count,
        "mem_total_mb": mem_total_mb,
        "mem_available_mb": mem_available_mb,
        "mem_used_pct": mem_used_pct,
        "uptime_s": uptime_s,
        "level": level,
        "load_level": load_level,
        "mem_level": mem_level,
    }
