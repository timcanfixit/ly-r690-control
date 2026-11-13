"""Process limits - per-recipe quality windows, deliberately separate from safety_limits.py.

safety_limits.py answers "is this profile/reading safe to run at all" - hard ceilings that block a
Start or auto-stop a run. This module answers a different question: "did THIS run meet the process
quality window a specific recipe actually needs" - e.g. "time above wetting must be between 45 and
90 seconds", "peak Upper zone must not exceed 235C" - numbers that vary per board/profile and have
nothing to do with machine safety. A profile can be perfectly safe (within safety_limits.py's
ceilings the whole time) and still fail its own process window, or vice versa.

Stored inside a profile's own metadata dict (profile_store.py) under the "process_limits" key,
alongside the process_type/solder/notes/status fields already there - not a separate file, since
these numbers only ever mean anything in the context of one specific profile.

None (of the whole dataclass, or of any individual field) means "not specified" - never a
fabricated default. run_finalization.classify_process_result() stays the conservative fallback
used whenever a run's profile has no process_limits configured; classify_with_limits() below is
only used when they are, and produces a REAL pass/fail against the window instead of the
deliberately-ambiguous three-way guess.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ProcessLimits:
    min_time_above_wetting_s: Optional[float] = None
    max_time_above_wetting_s: Optional[float] = None
    max_peak_upper_c: Optional[float] = None
    max_peak_lower_c: Optional[float] = None
    max_peak_ir_c: Optional[float] = None

    def is_empty(self) -> bool:
        return all(getattr(self, f) is None for f in self.__dataclass_fields__)


def parse_process_limits(metadata: Optional[dict]) -> Optional[ProcessLimits]:
    """None if `metadata` has no "process_limits" key, the key is empty/falsy, or every field in
    it is None/absent - all of those mean "this recipe hasn't defined a quality window", not
    "everything is zero"."""
    if not metadata:
        return None
    raw = metadata.get("process_limits")
    if not raw:
        return None
    known = {f: raw.get(f) for f in ProcessLimits.__dataclass_fields__ if raw.get(f) is not None}
    if not known:
        return None
    return ProcessLimits(**known)


_PEAK_ZONE_CHECKS = (
    ("upper", "max_peak_upper_c", "Upper"),
    ("lower", "max_peak_lower_c", "Lower"),
    ("infrared", "max_peak_ir_c", "Infrared"),
)


def classify_with_limits(fault_count: int, time_above_wetting_s: float,
                          zone_statistics: dict, limits: ProcessLimits) -> tuple[str, list[str]]:
    """Returns (result, violations). result is always "PASS" or "FAIL" - never "INCOMPLETE" - a
    configured window turns "not enough happened" into a real, namable failure (e.g. "TAL 0.0s
    below required minimum 45.0s") instead of the ambiguous case classify_process_result() has to
    leave open when no window is known. `violations` is empty exactly when result == "PASS"."""
    violations: list[str] = []
    if fault_count > 0:
        violations.append(f"{fault_count} fault(s) raised during the run.")

    if limits.min_time_above_wetting_s is not None and time_above_wetting_s < limits.min_time_above_wetting_s:
        violations.append(
            f"Time above wetting ({time_above_wetting_s:.1f}s) below required minimum "
            f"({limits.min_time_above_wetting_s:.1f}s)."
        )
    if limits.max_time_above_wetting_s is not None and time_above_wetting_s > limits.max_time_above_wetting_s:
        violations.append(
            f"Time above wetting ({time_above_wetting_s:.1f}s) exceeds allowed maximum "
            f"({limits.max_time_above_wetting_s:.1f}s)."
        )

    for zone_key, field_name, label in _PEAK_ZONE_CHECKS:
        max_peak = getattr(limits, field_name)
        if max_peak is None:
            continue
        zone_stats = zone_statistics.get(zone_key)
        if not zone_stats:
            continue
        peak = zone_stats.get("max_c")
        if peak is not None and peak > max_peak:
            violations.append(f"{label} peak ({peak:.1f}C) exceeds process limit ({max_peak:.1f}C).")

    return ("FAIL" if violations else "PASS"), violations
