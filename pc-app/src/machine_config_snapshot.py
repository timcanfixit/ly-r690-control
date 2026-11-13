"""Known-good machine configuration + drift detection - 2026-09-08, external feature-gap review,
second pass.

The touchscreen can write PID gains, fan/vacuum timing, and temperature-compensation registers
directly - completely bypassing this app (see machine_bus.py's own docstring on why the touchscreen
is a continuously-polling master this app has to coexist with, not something it controls). If
someone changes one of those values from the touchscreen, this app previously had no way to notice
at all. This module lets an operator explicitly save "this is how the machine should be configured
right now" as a reference snapshot (PID gains, fan/vacuum timing, temperature compensation - NOT
the curve/profile, which already has its own versioned history in profile_store.py and isn't
reliably actively-readable from this board anyway - see docs/06-modbus-register-map.md's
"Curve/profile active-read investigation"), then compare the machine's CURRENT values against it on
demand or periodically.

Deliberately does not auto-correct anything - a real difference might be an intentional change the
operator made from the touchscreen for a good reason. This only reports what changed, leaving the
operator to decide what (if anything) to do about it.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

if getattr(sys, "frozen", False):
    KNOWN_GOOD_CONFIG_PATH = Path(sys.executable).resolve().parent / "known_good_config.json"
else:
    KNOWN_GOOD_CONFIG_PATH = Path(__file__).resolve().parent.parent / "known_good_config.json"

_FAN_TIMING_LABELS = ("Vacuum time", "Fan stop temp", "Fan stop delay", "Advance warning time")
_TEMP_COMP_LABELS = ("Upper", "Lower", "Infrared", "Probe")


@dataclass
class ConfigSnapshot:
    pid: list[int]
    fan_timing: list[int]
    temp_comp: list[int]
    captured_at: float = field(default_factory=time.time)


def save_known_good_config(pid: list[int], fan_timing: list[int], temp_comp: list[int]) -> ConfigSnapshot:
    snapshot = ConfigSnapshot(pid=list(pid), fan_timing=list(fan_timing), temp_comp=list(temp_comp))
    KNOWN_GOOD_CONFIG_PATH.write_text(json.dumps({
        "pid": snapshot.pid, "fan_timing": snapshot.fan_timing, "temp_comp": snapshot.temp_comp,
        "captured_at": snapshot.captured_at,
    }, indent=2))
    return snapshot


def load_known_good_config() -> Optional[ConfigSnapshot]:
    if not KNOWN_GOOD_CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(KNOWN_GOOD_CONFIG_PATH.read_text())
        return ConfigSnapshot(
            pid=data["pid"], fan_timing=data["fan_timing"], temp_comp=data["temp_comp"],
            captured_at=data.get("captured_at", 0.0),
        )
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def _diff_list(known: list[int], current: list[int], labels: Optional[tuple] = None) -> list[str]:
    """Compares two register lists position-by-position. A length mismatch (the board reported a
    different count than the known-good snapshot has) is reported as its own single note rather
    than attempted index-by-index, since the two lists wouldn't be comparable at all."""
    if len(known) != len(current):
        return [f"Register count changed ({len(known)} -> {len(current)}) - cannot compare value-by-value."]
    diffs = []
    for i, (old, new) in enumerate(zip(known, current)):
        if old != new:
            label = labels[i] if labels and i < len(labels) else f"register {i}"
            diffs.append(f"{label}: {old} -> {new}")
    return diffs


def compute_drift(known_good: ConfigSnapshot, pid: list[int], fan_timing: list[int],
                   temp_comp: list[int]) -> dict:
    pid_diffs = _diff_list(known_good.pid, pid)
    fan_timing_diffs = _diff_list(known_good.fan_timing, fan_timing, _FAN_TIMING_LABELS)
    temp_comp_diffs = _diff_list(known_good.temp_comp, temp_comp, _TEMP_COMP_LABELS)
    return {
        "pid_diffs": pid_diffs,
        "fan_timing_diffs": fan_timing_diffs,
        "temp_comp_diffs": temp_comp_diffs,
        "has_drift": bool(pid_diffs or fan_timing_diffs or temp_comp_diffs),
    }
