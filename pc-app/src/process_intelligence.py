"""Stage 22: Process Intelligence Dashboard - a live composite view of state already computed
elsewhere (per-zone deviation from the nearest target in the loaded profile, active faults,
elapsed run time), not a new source of machine truth.

compute_zone_deviations() here deliberately stays a "nearest of every target" check, not a
"nearest of the currently-expected section" check - see alerts.py's module docstring for why the
distinction matters: the curve/profile's rate-field units were never independently confirmed
against live hardware, so there's no reliable way to simulate an expected temperature-vs-time
curve. This reuses that exact "nearest of every target" logic (not a reimplementation) so the
number shown here and the alert threshold it's implicitly compared against in alerts.py always
agree.

The separate, coarser question of "which section is probably running right now" - explicitly
requested 2026-08-23 for a live-graph indicator - is answered by RunMonitor.estimate_current_
section() in alerts.py instead, using a technique that avoids the rate-unit problem entirely (see
that module's docstring). It's surfaced as a best-effort estimate, not folded into the deviation
numbers here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from modbus_client import LiveReading, Profile

_ZONE_ATTRS = [
    ("Upper", "upper_temp", "upper_temp"),
    ("Lower", "lower_temp", "lower_temp"),
    ("Infrared", "infrared_temp", "ir_temp"),
]


@dataclass
class ZoneDeviation:
    zone: str
    actual_c: float
    nearest_target_c: Optional[float]
    deviation_c: Optional[float]  # None if the profile has no nonzero target for this zone at all


def compute_zone_deviations(reading: LiveReading, profile: Optional[Profile]) -> list[ZoneDeviation]:
    results = []
    for zone_name, reading_attr, profile_attr in _ZONE_ATTRS:
        actual = getattr(reading, reading_attr)
        targets = [getattr(s, profile_attr) for s in profile.sections if getattr(s, profile_attr) > 0] if profile else []
        if not targets:
            results.append(ZoneDeviation(zone=zone_name, actual_c=actual, nearest_target_c=None, deviation_c=None))
            continue
        nearest = min(targets, key=lambda t: abs(t - actual))
        results.append(ZoneDeviation(zone=zone_name, actual_c=actual, nearest_target_c=nearest, deviation_c=abs(nearest - actual)))
    return results
