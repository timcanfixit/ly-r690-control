"""Heater response monitoring - detects if a zone's actual temperature behavior looks abnormal
given what the machine has been told to do (Running/Stop), without ever needing the profile's own
"rate" field. That field's real-world units were never independently confirmed against live
hardware (same caveat already documented in alerts.py and safety_limits.py) - a check built on an
unverified unit assumption would be worse than no check at all. Everything here compares OBSERVED
temperature over OBSERVED real time against either a fixed absolute value in real degrees C (a
zone's own profile target, or a configurable tolerance) or the zone's own recent history - never
against an assumed rate.

Deliberately does NOT attempt "abnormally slow/fast heating vs. expected rate" - that needs either
the profile's ambiguous rate field or a learned baseline from historical runs (the latter could be
built later from data_logger.list_runs() once enough real history accumulates - not attempted
here). Wording is deliberately hedged ("response abnormal", not "heater is defective") per this
project's established practice of not asserting more than the data actually supports - these are
pattern-based signals that can have false positives (a genuinely slow zone, a profile with an
intentional long plateau), not certainties, which is also why they're raised as WARNING faults
(shown, require acknowledgement) rather than CRITICAL (which auto-stops the run) - see
fault_manager.py's severity semantics.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

from modbus_client import LiveReading, Profile

ZONES = ("upper", "lower", "ir")
_ZONE_TEMP_ATTR = {"upper": "upper_temp", "lower": "lower_temp", "ir": "ir_temp"}


@dataclass
class HeaterIssue:
    zone: str
    kind: str  # "not_responding" | "overshoot" | "stuck_on"
    description: str


class HeaterMonitor:
    def __init__(
        self,
        not_responding_window_s: float = 120.0,
        not_responding_min_delta_c: float = 2.0,
        overshoot_tolerance_c: float = 10.0,
        stuck_on_window_s: float = 90.0,
        stuck_on_min_rise_c: float = 3.0,
    ):
        self.not_responding_window_s = not_responding_window_s
        self.not_responding_min_delta_c = not_responding_min_delta_c
        self.overshoot_tolerance_c = overshoot_tolerance_c
        self.stuck_on_window_s = stuck_on_window_s
        self.stuck_on_min_rise_c = stuck_on_min_rise_c
        self._history: dict[str, deque] = {z: deque(maxlen=1200) for z in ZONES}
        self._was_running = False
        self._stopped_at: Optional[float] = None
        self._stopped_temps: dict[str, float] = {}
        self._active_issue_keys: set[str] = set()

    def reset(self) -> None:
        """Call at the start of each run - a stale history/stopped-at from a PREVIOUS run must
        never leak into this one's checks."""
        for z in ZONES:
            self._history[z].clear()
        self._was_running = False
        self._stopped_at = None
        self._stopped_temps = {}
        self._active_issue_keys = set()

    def update(self, reading: LiveReading, profile: Optional[Profile], now: float) -> list[HeaterIssue]:
        """Returns newly-appearing issues only (dedup against what's still ongoing), same pattern
        as machine_bus/safety_limits use elsewhere - a persisting condition raises one fault, not
        one per poll tick."""
        zone_temps = {z: getattr(reading, {"upper": "upper_temp", "lower": "lower_temp", "ir": "infrared_temp"}[z]) for z in ZONES}
        for z, t in zone_temps.items():
            self._history[z].append((now, t))

        if reading.running and not self._was_running:
            self._stopped_at = None
        if not reading.running and self._was_running:
            self._stopped_at = now
            self._stopped_temps = dict(zone_temps)

        issues: list[HeaterIssue] = []
        if reading.running:
            issues += self._check_not_responding(zone_temps, profile, now)
            issues += self._check_overshoot(zone_temps, profile)
        elif self._stopped_at is not None:
            issues += self._check_stuck_on(zone_temps, now)

        self._was_running = reading.running

        current_keys = {f"{i.zone}:{i.kind}" for i in issues}
        new_issues = [i for i in issues if f"{i.zone}:{i.kind}" not in self._active_issue_keys]
        self._active_issue_keys = current_keys
        return new_issues

    def _zone_max_target(self, zone: str, profile: Optional[Profile]) -> Optional[float]:
        """None means "not applicable to this zone" - either no profile to compare against, or
        the profile never gives this zone a nonzero target anywhere (e.g. an IR-only rework
        profile that never uses Upper/Lower at all). A max of exactly 0 is not a real target
        ceiling to check against - without this, a zone the profile doesn't use at all would get
        flagged as "overshooting" the instant it read anything above ambient, which is not a real
        fault."""
        if profile is None or not profile.sections:
            return None
        attr = _ZONE_TEMP_ATTR[zone]
        max_val = max(getattr(s, attr) for s in profile.sections)
        return max_val if max_val > 0 else None

    def _check_not_responding(self, zone_temps, profile, now) -> list[HeaterIssue]:
        issues = []
        for z, actual in zone_temps.items():
            target_max = self._zone_max_target(z, profile)
            if target_max is None or actual >= target_max - 1.0:
                continue  # no profile to compare against, or already near its ceiling - holding, not stalled
            history = self._history[z]
            if not history or (now - history[0][0]) < self.not_responding_window_s:
                continue  # not enough history yet to judge a window this size
            window = [t for ts, t in history if now - ts <= self.not_responding_window_s]
            if len(window) < 2:
                continue
            if actual - window[0] < self.not_responding_min_delta_c:
                issues.append(HeaterIssue(
                    z, "not_responding",
                    f"{z.capitalize()} heater response abnormal: temperature has risen less than "
                    f"{self.not_responding_min_delta_c:.1f}C in the last {self.not_responding_window_s:.0f}s "
                    f"while running and below its profile's target (currently {actual:.1f}C, target up to {target_max}C).",
                ))
        return issues

    def _check_overshoot(self, zone_temps, profile) -> list[HeaterIssue]:
        issues = []
        for z, actual in zone_temps.items():
            target_max = self._zone_max_target(z, profile)
            if target_max is None:
                continue
            if actual > target_max + self.overshoot_tolerance_c:
                issues.append(HeaterIssue(
                    z, "overshoot",
                    f"{z.capitalize()} temperature ({actual:.1f}C) exceeds its profile's highest "
                    f"target ({target_max}C) by more than the configured tolerance "
                    f"({self.overshoot_tolerance_c:.1f}C).",
                ))
        return issues

    def _check_stuck_on(self, zone_temps, now) -> list[HeaterIssue]:
        issues = []
        if now - self._stopped_at > self.stuck_on_window_s:
            return issues  # only meaningful shortly after Stop - a slow natural cooldown lag afterward is expected, not a fault
        for z, actual in zone_temps.items():
            stopped_temp = self._stopped_temps.get(z)
            if stopped_temp is None:
                continue
            if actual - stopped_temp > self.stuck_on_min_rise_c:
                issues.append(HeaterIssue(
                    z, "stuck_on",
                    f"{z.capitalize()} temperature kept rising ({stopped_temp:.1f}C -> {actual:.1f}C) "
                    f"in the {now - self._stopped_at:.0f}s after Stop was issued - heater may still be energized.",
                ))
        return issues
