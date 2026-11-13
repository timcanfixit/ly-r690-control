"""Independent software safety limits - deliberately separate from alerts.AlertConfig.

alerts.py's thresholds are advisory ("this looks off, notify the operator") and were designed
around values that are safe to guess loosely, since being wrong just means a spurious notification.
These limits are enforcement-grade ("this is dangerous, stop the process") and a wrong guess here
either fails to catch a real problem or stops a perfectly good run - so they default conservative
and are meant to be tuned to the actual profiles this machine runs, not left as arbitrary numbers.

Deliberately excludes anything that depends on the curve/profile "rate" field's real-world units -
alerts.py's own module docstring already flags that those units were never independently confirmed
against live hardware (only which register is which, not what "rate=2" means physically). A safety
limit built on an unverified unit assumption would be worse than no limit at all - it would either
false-trigger constantly or silently never trigger. Ramp-rate and run-duration limits here are
instead computed from OBSERVED temperature-over-real-time and OBSERVED real elapsed seconds, which
need no unit assumptions at all.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

from modbus_client import LiveReading, Profile

logger = logging.getLogger(__name__)

# Persisted config - confirmed live 2026-09-07 that its absence was a real, standing gap: this
# app was restarted ~6 times in one session for unrelated deploys, and each restart silently
# discarded every custom limit back to the code defaults below (including a ramp-rate limit the
# operator had deliberately raised from 120 to 380 during an earlier live false-positive
# investigation, wiping it without any visible warning). See load_safety_limits()/
# save_safety_limits() - same frozen-aware path pattern as calibration.py.
if getattr(sys, "frozen", False):
    SAFETY_LIMITS_PATH = Path(sys.executable).resolve().parent / "safety_limits_config.json"
else:
    SAFETY_LIMITS_PATH = Path(__file__).resolve().parent.parent / "safety_limits_config.json"

_lock = threading.Lock()


@dataclass
class SafetyLimits:
    max_upper_temp_c: float = 320.0
    max_lower_temp_c: float = 320.0
    max_ir_temp_c: float = 320.0
    max_probe_temp_c: float = 320.0
    max_observed_ramp_c_per_min: float = 120.0   # real measured rate of change, not the profile's own unit-ambiguous "rate" field
    max_run_duration_s: float = 3600.0           # real elapsed wall-clock time for one run, not a profile-side estimate (see module docstring)
    max_communication_outage_s: float = 30.0     # how long a run may continue with no fresh reading before it's treated as unsafe to keep trusting
    min_ramp_sample_interval_s: float = 5.0      # see RampTracker - a rate computed from less real time than this is noise-dominated, not a trustworthy measurement
    # Not really a "limit" - lives here purely to reuse this dataclass's existing settings UI,
    # persistence, and backup/restore plumbing rather than building a parallel path for one field.
    # See MachineBus.get_live_reading()/ir_reading_correction_c for the full story: confirmed live
    # 2026-09-07 that live IR reads ~20-24C below its own profile targets while Upper/Lower track
    # theirs closely, and that the machine's own ADDR_TEMP_COMP_IR register (a real, confirmed,
    # working register - unlike the earlier unconfirmed-hypothesis status of this whole block) is
    # hard-capped at +10C, unable to cover the full gap on its own. Set the hardware register to
    # its max (+10C, via the touchscreen or this app's Temperature Compensation box) FIRST, then
    # set this to the remaining residual - default here assumes +10C hardware compensation is
    # already applied, so this covers roughly the rest of a ~20-24C observed gap. Tune to your own
    # observed gap after a fresh run; this is a starting estimate, not a precise measurement.
    ir_reading_correction_c: float = 13.0


# Sane (min, max) bounds per field, for load_safety_limits()'s per-field validation - see that
# function's docstring for why this exists (external feature-gap review, 2026-09-07): a corrupt
# or hand-edited file that parses as valid JSON but holds a nonsensical value (e.g. a negative or
# zero max-temperature, which would fault on literally the first reading and effectively brick the
# safety system into permanently blocking Start) was previously accepted as-is - only total JSON
# corruption was ever caught. These are deliberately generous (this is a sanity floor/ceiling, not
# a recommendation) - real values should sit well inside them.
_SANE_BOUNDS: dict[str, tuple[float, float]] = {
    "max_upper_temp_c": (50.0, 500.0),
    "max_lower_temp_c": (50.0, 500.0),
    "max_ir_temp_c": (50.0, 500.0),
    "max_probe_temp_c": (50.0, 500.0),
    "max_observed_ramp_c_per_min": (1.0, 2000.0),
    "max_run_duration_s": (10.0, 86400.0),
    "max_communication_outage_s": (1.0, 600.0),
    "min_ramp_sample_interval_s": (0.5, 120.0),
    "ir_reading_correction_c": (-100.0, 100.0),
}


def parse_safety_limit_updates(data: dict) -> dict[str, float]:
    """Validates a whole update payload BEFORE anything is applied (audit C07, 2026-11-11): every
    present field must be a real number (not bool/str/None), finite (NaN compares False against
    everything, so a NaN max-temp would silently disable that check), and inside _SANE_BOUNDS.
    Raises ValueError naming the first bad field; a caller that applies the returned dict only
    after this succeeds can never leave a half-applied update behind."""
    import math
    updates: dict[str, float] = {}
    for name, (lo, hi) in _SANE_BOUNDS.items():
        if name not in data:
            continue
        raw = data[name]
        if isinstance(raw, bool) or raw is None:
            raise ValueError(f"{name}: must be a number")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{name}: must be a number")
        if not math.isfinite(value):
            raise ValueError(f"{name}: must be finite")
        if not lo <= value <= hi:
            raise ValueError(f"{name}: must be between {lo:g} and {hi:g}")
        updates[name] = value
    return updates


def load_safety_limits() -> SafetyLimits:
    """Loads persisted limits from disk, falling back to the code defaults above for anything
    missing, out of range, the wrong type, or if the file doesn't exist or is corrupt entirely -
    never raises, and never lets a bad on-disk value quietly turn into a dangerous or
    self-defeating running configuration. See _SANE_BOUNDS for what "out of range" means per
    field. Call this once at startup instead of bare `SafetyLimits()` (see module-level
    SAFETY_LIMITS_PATH comment for why this exists at all)."""
    limits = SafetyLimits()
    if not SAFETY_LIMITS_PATH.exists():
        return limits
    try:
        data = json.loads(SAFETY_LIMITS_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("safety_limits_config.json unreadable/corrupt (%s) - using factory defaults.", exc)
        return limits
    if not isinstance(data, dict):
        logger.warning("safety_limits_config.json did not contain a JSON object - using factory defaults.")
        return limits
    valid_names = {f.name for f in fields(SafetyLimits)}
    for name, value in data.items():
        if name not in valid_names:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            logger.warning("safety_limits_config.json: %s=%r is not a number - keeping factory default %s.", name, value, getattr(limits, name))
            continue
        lo, hi = _SANE_BOUNDS.get(name, (float("-inf"), float("inf")))
        if not (lo <= value <= hi):
            logger.warning("safety_limits_config.json: %s=%s is outside the sane range [%s, %s] - keeping factory default %s.", name, value, lo, hi, getattr(limits, name))
            continue
        setattr(limits, name, value)
    return limits


def save_safety_limits(limits: SafetyLimits) -> None:
    """Call this after every change to `limits` (see main_window.py's _on_safety_limit_edited/
    _on_ir_reading_correction_changed and web_server.py's api_set_safety_limits for the call
    sites) - not just on the existing manual "backup config" action, which the operator has to
    remember to click and which most people (reasonably) don't expect to also be required just to
    keep a settings change past the next restart."""
    with _lock:
        from atomic_io import atomic_write_text
        atomic_write_text(SAFETY_LIMITS_PATH, json.dumps(asdict(limits), indent=2))


@dataclass
class SafetyViolation:
    limit_name: str
    description: str


@dataclass
class RampTracker:
    """Feeds check_reading() a (prior_reading, seconds_since_prior) pair that reflects the real
    elapsed time between two genuinely different temperature samples - deliberately decoupled
    from however often the caller happens to poll get_live_reading().

    Confirmed live 2026-09 as the fix for a real false-positive auto-stop: MachineBus's passive
    listener is paused for the entire duration of a contended coil write (already known this
    session to take 20-40+ seconds under bus contention), during which get_live_reading() keeps
    returning the same stale temps every outer poll tick. The naive approach - comparing against
    whatever reading was captured on the caller's own last poll tick, using wall-clock time since
    that tick - divides however much the temperature genuinely changed over the WHOLE stale
    window by only the last poll interval (a fraction of a second), producing a wildly inflated,
    physically-impossible rate (reported live: a normal ~14C real change computed as 1641C/min)
    and an unwarranted auto-stop. This tracker only ever compares two samples whose
    MachineBus.get_temps_timestamp() values actually differ, using the real gap between those
    timestamps - not wall-clock time at the caller.

    Second, related fix (also confirmed live 2026-09, immediately after the first): once the
    above stopped comparing against STALE readings, a new false-positive appeared comparing
    against readings that WERE genuinely fresh but only ~1-2 seconds apart (MachineBus's active
    poll loop requests fresh temps roughly every ACTIVE_POLL_INTERVAL_S, and the passive listener
    can refresh even faster). A couple of degrees of perfectly ordinary short-term noise - ADC
    jitter, a slightly-off register read on this known-noisy shared bus, a momentary heater
    output blip - extrapolated from a ~1-2 second delta to a "per-minute" figure looks like a
    dramatic runaway even though it isn't one: reported live, escalating operator-raised limits
    (120 -> 200 -> 380 C/min) kept getting caught by essentially the same few degrees of
    per-tick noise, because the underlying problem was never the limit, it was trusting a rate
    computed over far too short a baseline to be a meaningful measurement. `min_interval_s`
    (see observe()) fixes this the same way a real instrument would: refuse to report a rate
    until enough real time has actually accumulated to make short-term noise negligible, THEN
    measure across that whole accumulated window rather than just the most recent noisy tick -
    this also makes the reported rate more representative of a real sustained trend, not a
    single instantaneous (and noise-dominated) derivative.

    Third, related fix (also confirmed live 2026-09, immediately after the second): a real,
    LARGE, but entirely INTENDED temperature change still false-triggered - reported as
    "Infrared changed 488C/min" at a section 7->8 transition (the final section, typically the
    profile's peak/hold target - see profile_search.py's reference-profile feature). A profile
    section boundary can legitimately command a zone's target to jump by tens of degrees between
    adjacent sections, and the machine driving hard toward that new commanded target looks
    identical to an uncommanded runaway to a check that has no idea a target change was ever
    commanded - which this one deliberately doesn't, since the profile's own "rate" field's units
    were never confirmed (see this module's own docstring) and using it would reintroduce exactly
    that unverified-unit risk. The fix instead uses only what's already independently verified:
    RunMonitor.estimate_current_section() (alerts.py) infers section progress purely from how
    closely live temps match each section's real target values - no profile "rate" field
    involved. When the caller detects that estimate has advanced to a new section, it calls
    reset() here, discarding the current baseline so the next window starts fresh AFTER the
    transition - the ramp check then only ever measures rate WITHIN one section's steady-state
    operation, never across a boundary where a legitimate large jump is expected. See
    main_window.py/web_server.py's _check_safety_limits() for the integration."""
    prior_reading: Optional[LiveReading] = None
    prior_timestamp: float = 0.0

    def reset(self) -> None:
        """Discards the current baseline - see the class docstring's "Third, related fix" for
        why this is called on a detected section transition. The next observe() call establishes
        a fresh baseline with no rate computed until min_interval_s has elapsed again, measured
        entirely within the new section rather than spanning across the transition boundary."""
        self.prior_reading = None
        self.prior_timestamp = 0.0

    def observe(self, reading: LiveReading, temps_timestamp: float, min_interval_s: float = 5.0) -> tuple[Optional[LiveReading], Optional[float]]:
        """Returns (prior_reading, seconds_since_prior) to pass straight into check_reading() -
        both None if there's no trustworthy comparison to make yet: the very first observation,
        temps haven't refreshed since the last call, or a fresh sample arrived but not enough
        real time (min_interval_s) has passed since the current baseline to compute a
        noise-resistant rate from it. In that last case, the existing baseline is deliberately
        NOT advanced to the new sample - the next call keeps accumulating real elapsed time
        against the SAME baseline until min_interval_s is reached, then computes the rate across
        that whole window (not just the most recent tick) and re-baselines from there."""
        is_fresh = temps_timestamp > 0.0 and temps_timestamp != self.prior_timestamp
        if not is_fresh:
            return None, None
        if self.prior_timestamp == 0.0:
            # very first sample ever - nothing to compare against yet, just establish a baseline.
            self.prior_reading = reading
            self.prior_timestamp = temps_timestamp
            return None, None
        elapsed = temps_timestamp - self.prior_timestamp
        if elapsed < min_interval_s:
            # fresh sample, but too soon since the baseline to trust a rate from it - keep the
            # OLD baseline and wait for more real time to accumulate against it.
            return None, None
        result = (self.prior_reading, elapsed)
        self.prior_reading = reading
        self.prior_timestamp = temps_timestamp
        return result


def check_reading(limits: SafetyLimits, reading: LiveReading, prior: Optional[LiveReading],
                   seconds_since_prior: Optional[float], skip_ramp_check: bool = False,
                   skip_probe_ramp_check: bool = False) -> list[SafetyViolation]:
    """Hard-limit checks against a single live reading. `prior`/`seconds_since_prior` are only
    used for the ramp-rate check - pass None for either to skip it (e.g. on the first reading of
    a run). `skip_ramp_check` is for a real, confirmed-live 2026-09-07 case: a self-tune (PID
    autotune) legitimately perturbs the actual heater to characterize its response, producing
    large/fast temperature changes that are intended, not a runaway - the same category of
    problem RampTracker.reset() already solves for profile section transitions, but self-tune
    isn't tied to a profile at all, so that mechanism can't see it. Absolute max-temperature
    checks above are deliberately NOT skipped here - those are safety-critical regardless of
    whether a change was commanded, unlike a rate which is inherently context-dependent.

    `skip_probe_ramp_check` is for a second confirmed-live case (2026-09-10, run_20260910_165928):
    the probe thermocouple is physically attached to the part being reworked, so the instant the
    operator engages VACUUM (chip pickup) or KEEP to lift the chip at the end of a cycle, the
    probe reading stops being a temperature and starts being an artifact of the part moving away
    (that run: probe fell 241->203C in one sample, flagged as 552C/min and auto-stopped a reflow
    that was already complete). The probe is not a driven heater and cannot run away; only its
    ramp check is skipped here, and only during extraction - its absolute max-temp check stays."""
    violations = []
    for zone_name, actual, limit in (
        ("Upper", reading.upper_temp, limits.max_upper_temp_c),
        ("Lower", reading.lower_temp, limits.max_lower_temp_c),
        ("Infrared", reading.infrared_temp, limits.max_ir_temp_c),
        ("Probe", reading.probe_temp, limits.max_probe_temp_c),
    ):
        if actual > limit:
            violations.append(SafetyViolation(
                f"max_{zone_name.lower()}_temp_c",
                f"{zone_name} temperature {actual:.1f}C exceeds the configured hard maximum of {limit:.1f}C.",
            ))

    if prior is not None and seconds_since_prior and seconds_since_prior > 0 and not skip_ramp_check:
        for zone_name, actual, previous in (
            ("Upper", reading.upper_temp, prior.upper_temp),
            ("Lower", reading.lower_temp, prior.lower_temp),
            ("Infrared", reading.infrared_temp, prior.infrared_temp),
            ("Probe", reading.probe_temp, prior.probe_temp),
        ):
            if zone_name == "Probe" and skip_probe_ramp_check:
                continue
            rate_c_per_min = abs(actual - previous) / seconds_since_prior * 60.0
            if rate_c_per_min > limits.max_observed_ramp_c_per_min:
                # limit_name includes the zone so two zones ramping too fast at once are tracked
                # (deduped, raised, cleared) independently - a shared name would let a second
                # zone's violation go unreported just because some other zone already raised one.
                violations.append(SafetyViolation(
                    f"max_observed_ramp_c_per_min_{zone_name.lower()}",
                    f"{zone_name} temperature changed {rate_c_per_min:.0f}C/min, exceeding the "
                    f"configured hard maximum of {limits.max_observed_ramp_c_per_min:.0f}C/min.",
                ))
    return violations


def check_run_duration(limits: SafetyLimits, elapsed_s: float) -> Optional[SafetyViolation]:
    if elapsed_s > limits.max_run_duration_s:
        return SafetyViolation(
            "max_run_duration_s",
            f"Run has been active for {elapsed_s / 60.0:.0f} minutes, exceeding the configured "
            f"hard maximum of {limits.max_run_duration_s / 60.0:.0f} minutes.",
        )
    return None


def check_communication_outage(limits: SafetyLimits, seconds_since_last_reading: float) -> Optional[SafetyViolation]:
    if seconds_since_last_reading > limits.max_communication_outage_s:
        return SafetyViolation(
            "max_communication_outage_s",
            f"No fresh reading for {seconds_since_last_reading:.0f}s while running, exceeding "
            f"the configured hard maximum of {limits.max_communication_outage_s:.0f}s.",
        )
    return None


def check_profile_against_limits(limits: SafetyLimits, profile: Profile) -> list[SafetyViolation]:
    """Profile-side check that needs no rate-unit assumptions at all: does any section's target
    temperature already exceed a hard limit, before the machine ever gets there. Ramp rate and
    total duration are intentionally NOT checked here - see module docstring."""
    violations = []
    for section_idx, section in enumerate(profile.sections, start=1):
        for zone_name, attr, limit in (
            ("Upper", "upper_temp", limits.max_upper_temp_c),
            ("Lower", "lower_temp", limits.max_lower_temp_c),
            ("Infrared", "ir_temp", limits.max_ir_temp_c),
        ):
            value = getattr(section, attr)
            if value > limit:
                violations.append(SafetyViolation(
                    f"max_{zone_name.lower()}_temp_c",
                    f"Section {section_idx} {zone_name} target ({value}C) exceeds the configured "
                    f"hard maximum of {limit:.1f}C.",
                ))
            if value < 0:
                violations.append(SafetyViolation(
                    "negative_temperature",
                    f"Section {section_idx} {zone_name} target is negative ({value}).",
                ))
    return violations
