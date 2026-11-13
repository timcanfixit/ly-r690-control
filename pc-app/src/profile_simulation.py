"""Profile simulation - a summary of the profile's own TARGET values, not a physical prediction
of actual board/heater temperature (that would need real thermal modeling this project doesn't
have and shouldn't pretend to). Deliberately limited to what's computable without the profile's
own "rate" field - see safety_limits.py's module docstring for the recurring reason (its real-
world units were never confirmed against live hardware). Total duration is reported as a
hold-time-only LOWER BOUND, explicitly labeled as such - it excludes ramp time between sections,
so a real run will always take longer than this number.
"""
from __future__ import annotations

from dataclasses import dataclass

from modbus_client import Profile


@dataclass
class SimulationResult:
    section_count: int
    total_hold_time_s: int
    peak_upper_c: float
    peak_lower_c: float
    peak_ir_c: float
    time_above_wetting_s: int


def simulate_profile(profile: Profile, wetting_temp_c: float) -> SimulationResult:
    sections = profile.sections
    total_hold = sum(max(s.upper_hold_time, s.lower_hold_time, s.ir_hold_time) for s in sections)
    peak_upper = max((s.upper_temp for s in sections), default=0)
    peak_lower = max((s.lower_temp for s in sections), default=0)
    peak_ir = max((s.ir_temp for s in sections), default=0)

    # Same indicator logic as alerts.py's wetting check: Upper is the representative zone unless
    # a section's own target suggests the probe/board itself would be at or above wetting - since
    # we only have TARGET values here (no live probe data), Upper's own target is the only
    # unit-safe stand-in available.
    time_above_wetting = sum(
        max(s.upper_hold_time, s.lower_hold_time, s.ir_hold_time)
        for s in sections
        if s.upper_temp >= wetting_temp_c
    )

    return SimulationResult(
        section_count=len(sections),
        total_hold_time_s=total_hold,
        peak_upper_c=peak_upper,
        peak_lower_c=peak_lower,
        peak_ir_c=peak_ir,
        time_above_wetting_s=time_above_wetting,
    )
