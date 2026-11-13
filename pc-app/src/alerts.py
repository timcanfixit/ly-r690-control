"""
Rule-based live monitoring for issues during a run. Deliberately rule-based, not ML - see
project decision 2026-08-18: build explainable rules now, structure logging (data_logger.py)
so a real model can be trained on accumulated run history later.

Note on scope: the curve/profile "rate" column's exact physical units were never independently
confirmed against live hardware (we confirmed which register is which, not what a value like
"rate=2" means in real degC/time units - see docs/06-modbus-register-map.md). Because of that,
this deliberately does NOT try to simulate an expected temperature-vs-time curve and compare
against it point-by-point, which would silently encode an unverified assumption. Instead it uses
checks that don't depend on knowing the rate units at all.

estimate_current_section() (2026-08-23, explicitly requested) is built to the same constraint: it
never touches "rate" or elapsed time. It only compares real live temperature readings against each
section's real target temperatures (both already-confirmed plain degC), picking whichever
not-yet-passed section's targets the current readings resemble most closely, and only ever moving
forward through the section list (since sections always execute in order - a momentary reading
that happens to be closer to an earlier target, e.g. mid-ramp, must not make the indicator jump
backward). This is a best-effort ESTIMATE surfaced to the operator as such, not a claim of ground
truth from the machine - the machine does not expose a "current section" register.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from modbus_client import LiveReading, Profile


class Severity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Alert:
    severity: Severity
    message: str
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class AlertConfig:
    stuck_sensor_seconds: float = 20.0       # flag if a temp reading is bit-identical this long while running
    stuck_sensor_epsilon: float = 0.05       # degC - readings within this are "identical" (float noise)
    max_jump_per_reading: float = 15.0       # degC - larger single-step jump than this is suspicious
    wetting_temp_c: float = 217.0            # SAC305 lead-free liquidus - configurable in the UI
    section_deviation_c: float = 20.0        # degC - how far actual can drift from nearest section target


class RunMonitor:
    """Feed it every new LiveReading while a run is active via check(); it returns any new
    alerts. Call reset() at the start of each run."""

    def __init__(self, config: AlertConfig | None = None):
        self.config = config or AlertConfig()
        self._last_reading: LiveReading | None = None
        self._stuck_since: dict[str, float] = {}
        self._wetting_alerted = False
        self._active_profile: Profile | None = None
        self._current_section_index: int | None = None

    def reset(self, profile: Profile | None = None) -> None:
        self._last_reading = None
        self._stuck_since.clear()
        self._wetting_alerted = False
        self._active_profile = profile
        self._current_section_index = None

    @property
    def active_profile(self) -> Profile | None:
        """Whatever reset() was last called with - exposed so other monitors (heater_monitor.py)
        can compare against the same profile without each frontend needing its own separate
        "what are we currently monitoring against" bookkeeping."""
        return self._active_profile

    def estimate_current_section(self, reading: LiveReading) -> int | None:
        """Best-effort ESTIMATE of the 0-based profile section currently executing - see module
        docstring for the technique and why it's safe with respect to the unconfirmed "rate"
        units. Returns None if not running, no active profile, or the profile has no sections."""
        if not reading.running or self._active_profile is None:
            return None
        sections = self._active_profile.sections
        if not sections:
            return None

        start = self._current_section_index if self._current_section_index is not None else 0
        best_index, best_score = None, None
        for i in range(start, len(sections)):
            s = sections[i]
            diffs = [
                abs(getattr(s, attr) - actual)
                for actual, attr in (
                    (reading.upper_temp, "upper_temp"),
                    (reading.lower_temp, "lower_temp"),
                    (reading.infrared_temp, "ir_temp"),
                )
                if getattr(s, attr) > 0
            ]
            if not diffs:
                continue
            score = sum(diffs) / len(diffs)
            if best_score is None or score < best_score:
                best_index, best_score = i, score

        if best_index is not None:
            self._current_section_index = best_index
        return self._current_section_index

    def check(self, reading: LiveReading) -> list[Alert]:
        now = time.monotonic()
        alerts: list[Alert] = []

        alerts += self._check_stuck_sensors(reading, now)
        alerts += self._check_sudden_jumps(reading)
        alerts += self._check_wetting_temp(reading)
        if self._active_profile is not None:
            alerts += self._check_section_deviation(reading)

        self._last_reading = reading
        return alerts

    # ---------------- individual rules ----------------

    def _check_stuck_sensors(self, reading: LiveReading, now: float) -> list[Alert]:
        alerts: list[Alert] = []
        if self._last_reading is None or not reading.running:
            self._stuck_since.clear()
            return alerts
        eps = self.config.stuck_sensor_epsilon
        for name, current, previous in (
            ("Upper", reading.upper_temp, self._last_reading.upper_temp),
            ("Lower", reading.lower_temp, self._last_reading.lower_temp),
            ("Infrared", reading.infrared_temp, self._last_reading.infrared_temp),
            ("Probe", reading.probe_temp, self._last_reading.probe_temp),
        ):
            if abs(current - previous) < eps:
                started = self._stuck_since.setdefault(name, now)
                if now - started >= self.config.stuck_sensor_seconds:
                    alerts.append(Alert(
                        Severity.WARNING,
                        f"{name} sensor reading hasn't changed in {self.config.stuck_sensor_seconds:.0f}s "
                        f"while running ({current:.1f}C) - possible stuck sensor or heater not responding.",
                    ))
            else:
                self._stuck_since.pop(name, None)
        return alerts

    def _check_sudden_jumps(self, reading: LiveReading) -> list[Alert]:
        alerts: list[Alert] = []
        if self._last_reading is None:
            return alerts
        for name, current, previous in (
            ("Upper", reading.upper_temp, self._last_reading.upper_temp),
            ("Lower", reading.lower_temp, self._last_reading.lower_temp),
            ("Infrared", reading.infrared_temp, self._last_reading.infrared_temp),
            ("Probe", reading.probe_temp, self._last_reading.probe_temp),
        ):
            if abs(current - previous) > self.config.max_jump_per_reading:
                alerts.append(Alert(
                    Severity.WARNING,
                    f"{name} temperature jumped {current - previous:+.1f}C in one reading "
                    f"({previous:.1f}C -> {current:.1f}C) - possible sensor glitch or bad connection.",
                ))
        return alerts

    def _check_wetting_temp(self, reading: LiveReading) -> list[Alert]:
        alerts: list[Alert] = []
        if not reading.running or self._wetting_alerted:
            if not reading.running:
                self._wetting_alerted = False
            return alerts
        # Probe is the most representative of actual board/joint temperature if it's attached
        # to the workpiece; Upper is the fallback if probe isn't in use (reads near ambient).
        indicator = reading.probe_temp if reading.probe_temp > 40.0 else reading.upper_temp
        if indicator >= self.config.wetting_temp_c:
            self._wetting_alerted = True
            alerts.append(Alert(
                Severity.INFO,
                f"Wetting temperature reached ({indicator:.1f}C >= {self.config.wetting_temp_c:.0f}C) - "
                f"solder should be wetting now.",
            ))
        return alerts

    def _check_section_deviation(self, reading: LiveReading) -> list[Alert]:
        """Flags if the actual temp is far from EVERY section's target, which is a much weaker
        (but unit-assumption-free) signal than "far from the currently-expected section" would
        be - see module docstring for why we don't try to determine "currently expected"."""
        alerts: list[Alert] = []
        if not reading.running or self._active_profile is None:
            return alerts
        for zone_name, actual, attr in (
            ("Upper", reading.upper_temp, "upper_temp"),
            ("Lower", reading.lower_temp, "lower_temp"),
            ("Infrared", reading.infrared_temp, "ir_temp"),
        ):
            # Curve/profile registers are RAW degrees C, not x10 fixed-point - see
            # docs/06-modbus-register-map.md and the main_window.py/app.js/profile_import.py fix
            # from the same bug (2026-08-19). This /10.0 was the same mistake, just missed here -
            # it would have made every target look 10x too cold, so real running temperatures
            # would always appear "off track" and section-deviation alerts would fire constantly.
            targets = [getattr(s, attr) for s in self._active_profile.sections if getattr(s, attr) > 0]
            if not targets:
                continue
            nearest = min(targets, key=lambda t: abs(t - actual))
            if abs(nearest - actual) > self.config.section_deviation_c:
                alerts.append(Alert(
                    Severity.WARNING,
                    f"{zone_name} temp ({actual:.1f}C) is more than {self.config.section_deviation_c:.0f}C "
                    f"from every target temperature in the loaded profile - run may be off track.",
                ))
        return alerts
