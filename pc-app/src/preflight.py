"""Pre-Start safety check sequence. Deliberately lightweight - this is NOT the full profile
validator the original roadmap describes as a separate subsystem (ramp-rate sanity, duplicate
sections, warnings vs errors, etc.) - that's out of scope for this pass. This only checks what's
needed to safely gate the Start button: is there live communication, do the sensors look sane, and
does the profile about to be sent avoid the same hard temperature limits safety_limits.py already
enforces during a run. See safety_limits.py for why ramp-rate/duration aren't checked here either
(the profile's own "rate" field has never-confirmed real-world units).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from machine_bus import MachineBus
from modbus_client import CURVE_ATTRIBUTES, Profile
from safety_limits import SafetyLimits, check_profile_against_limits


def profile_curve_regs(profile: Profile) -> list[int]:
    """The 72 curve registers in the exact order both frontends send them
    (write_registers(ADDR_CURVE_BASE, ...)) - attribute-major, 8 sections per attribute."""
    return [getattr(s, attr) for attr in CURVE_ATTRIBUTES for s in profile.sections]


def curve_send_status(current_regs: list[int], last_sent_regs: Optional[list[int]]) -> str:
    """Whether the curve currently in the editor has been sent to the machine FROM THIS APP
    SESSION. Deliberately NOT a claim about what's actually on the control board - this machine's
    curve read-back is unreliable (see docs/06-modbus-register-map.md and machine_state.py's
    "never fabricate machine state" rule), so the app genuinely cannot verify the board. This only
    tracks provenance: was Send-to-machine confirmed for this exact curve since it was loaded/edited
    here?

      "empty"  - the editor curve is all zeros (nothing meaningfully loaded)
      "sent"   - current curve is byte-identical to the last Send-to-machine that succeeded this session
      "unsent" - a real curve is loaded but it has not been sent from here (or differs from the last send)
    """
    if not any(current_regs):
        return "empty"
    if last_sent_regs is not None and list(current_regs) == list(last_sent_regs):
        return "sent"
    return "unsent"


@dataclass
class PreflightCheck:
    name: str
    passed: bool
    detail: str


@dataclass
class PreflightResult:
    checks: list[PreflightCheck]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


# A live sensor reading this far outside plausible ambient-to-operating range indicates a sensor
# fault (disconnected thermocouple, bad register read) rather than a real temperature - generous
# bounds since ambient conditions vary a lot more than the trusted operating range in
# safety_limits.py, which is specifically about the hot end during a run.
_PLAUSIBLE_MIN_C = -10.0
_PLAUSIBLE_MAX_C = 400.0


def run_preflight(bus: MachineBus, profile: Profile, limits: SafetyLimits) -> PreflightResult:
    checks = []

    reading = bus.get_live_reading()
    comm_ok = bus.is_connected and reading is not None
    checks.append(PreflightCheck(
        "Communication",
        comm_ok,
        "Connected, live data available." if comm_ok else "Not connected, or no live reading observed yet.",
    ))

    if reading is not None:
        for name, value in (
            ("Upper", reading.upper_temp), ("Lower", reading.lower_temp),
            ("Infrared", reading.infrared_temp), ("Probe", reading.probe_temp),
        ):
            ok = _PLAUSIBLE_MIN_C <= value <= _PLAUSIBLE_MAX_C
            checks.append(PreflightCheck(
                f"{name} sensor",
                ok,
                f"{value:.1f}C" if ok else f"Implausible reading ({value:.1f}C) - check the sensor/connection.",
            ))
    else:
        checks.append(PreflightCheck("Sensors", False, "No live reading to check yet."))

    if profile is None:
        checks.append(PreflightCheck("Profile", False, "No profile selected."))
    elif len(profile.sections) != 8:
        checks.append(PreflightCheck("Profile", False, f"Expected 8 sections, got {len(profile.sections)}."))
    else:
        violations = check_profile_against_limits(limits, profile)
        checks.append(PreflightCheck(
            "Profile",
            not violations,
            "Within configured safety limits." if not violations else "; ".join(v.description for v in violations),
        ))

    return PreflightResult(checks=checks)
