from safety_limits import (
    SafetyLimits, RampTracker, check_reading, check_run_duration, check_communication_outage,
    check_profile_against_limits,
)
from modbus_client import LiveReading, Profile, CurveSection
from alerts import AlertConfig, RunMonitor


def _reading(upper=100.0, lower=100.0, ir=50.0, probe=50.0):
    return LiveReading(
        upper_temp=upper, lower_temp=lower, infrared_temp=ir, probe_temp=probe,
        running=True, running_mirror=True, vacuum=False, cooling=False, keep=False,
    )


def test_temperature_ceiling_violation():
    limits = SafetyLimits(max_upper_temp_c=200.0)
    violations = check_reading(limits, _reading(upper=250.0), None, None)
    assert any(v.limit_name == "max_upper_temp_c" for v in violations)


def test_no_violation_within_limits():
    limits = SafetyLimits()
    violations = check_reading(limits, _reading(), None, None)
    assert violations == []


def test_ramp_rate_violations_are_tracked_per_zone_independently():
    limits = SafetyLimits(max_observed_ramp_c_per_min=60.0)
    prior = _reading(upper=100.0, lower=100.0)
    current = _reading(upper=130.0, lower=140.0)  # both zones ramp too fast in the same tick
    violations = check_reading(limits, current, prior, seconds_since_prior=10.0)
    names = {v.limit_name for v in violations if "ramp" in v.limit_name}
    assert names == {"max_observed_ramp_c_per_min_upper", "max_observed_ramp_c_per_min_lower"}


def test_ramp_check_skipped_without_prior_reading():
    limits = SafetyLimits(max_observed_ramp_c_per_min=1.0)
    violations = check_reading(limits, _reading(upper=999.0), None, None)
    assert not any("ramp" in v.limit_name for v in violations)


def test_skip_ramp_check_suppresses_ramp_violations_but_not_absolute_max():
    """Confirmed live 2026-09-07: a real IR self-tune (PID autotune) produced a legitimate fast
    IR ramp that got flagged as a runaway, since the ramp check had no way to know a self-tune -
    not a profile run - was intentionally perturbing the heater. skip_ramp_check is the fix; it
    must NOT also suppress the absolute temperature ceiling, which stays safety-critical
    regardless of whether a change was commanded."""
    limits = SafetyLimits(max_observed_ramp_c_per_min=60.0, max_ir_temp_c=200.0)
    prior = _reading(ir=50.0)
    current = _reading(ir=350.0)  # both a fast ramp AND over the absolute ceiling
    violations = check_reading(limits, current, prior, seconds_since_prior=10.0, skip_ramp_check=True)
    names = {v.limit_name for v in violations}
    assert "max_observed_ramp_c_per_min_infrared" not in names, "ramp violation must be suppressed during self-tune"
    assert "max_infrared_temp_c" in names, "absolute max-temperature must still be enforced during self-tune"


def test_skip_probe_ramp_check_suppresses_only_the_probe_ramp():
    """Confirmed live 2026-09-10 (run_20260910_165928): the operator engaged VACUUM to pick the
    chip up at the end of a completed reflow, the probe thermocouple (riding the part) fell
    241->203C in one sample, and that got flagged as 552C/min and auto-stopped the run.
    skip_probe_ramp_check suppresses ONLY the probe's ramp check, and only during extraction -
    the driven heaters (Upper/Lower/IR) keep full runaway protection, and the probe's absolute
    ceiling stays enforced."""
    limits = SafetyLimits(max_observed_ramp_c_per_min=60.0, max_probe_temp_c=300.0)
    prior = _reading(upper=100.0, probe=250.0)
    current = _reading(upper=200.0, probe=100.0)  # Upper ramps fast up; probe crashes fast down
    violations = check_reading(
        limits, current, prior, seconds_since_prior=10.0, skip_probe_ramp_check=True,
    )
    names = {v.limit_name for v in violations}
    assert "max_observed_ramp_c_per_min_probe" not in names, "probe ramp must be suppressed during extraction"
    assert "max_observed_ramp_c_per_min_upper" in names, "heater ramp protection must stay active"


def test_skip_probe_ramp_check_still_enforces_probe_absolute_ceiling():
    limits = SafetyLimits(max_observed_ramp_c_per_min=60.0, max_probe_temp_c=300.0)
    prior = _reading(probe=50.0)
    current = _reading(probe=400.0)  # fast ramp AND over the absolute ceiling
    violations = check_reading(
        limits, current, prior, seconds_since_prior=10.0, skip_probe_ramp_check=True,
    )
    names = {v.limit_name for v in violations}
    assert "max_observed_ramp_c_per_min_probe" not in names
    assert "max_probe_temp_c" in names, "absolute probe ceiling must still be enforced during extraction"


def test_probe_ramp_still_faults_when_not_extracting():
    limits = SafetyLimits(max_observed_ramp_c_per_min=60.0)
    prior = _reading(probe=100.0)
    current = _reading(probe=250.0)
    violations = check_reading(limits, current, prior, seconds_since_prior=10.0)  # skip flag defaults False
    assert any(v.limit_name == "max_observed_ramp_c_per_min_probe" for v in violations)


def test_ramp_tracker_first_observation_returns_no_prior():
    tracker = RampTracker()
    prior_reading, seconds_since_prior = tracker.observe(_reading(upper=100.0), temps_timestamp=1000.0)
    assert prior_reading is None
    assert seconds_since_prior is None


def test_ramp_tracker_reproduces_and_fixes_the_reported_false_positive():
    """Regression test (2026-09): a real false-positive auto-stop reported "Lower changed
    1641C/min" while the operator could see actual temps were not ramping anywhere near that
    fast. Root cause: MachineBus's passive listener is paused for the whole duration of a
    contended coil write (20-40+ seconds under bus contention), during which get_live_reading()
    kept returning the same stale temps every 0.5s outer poll tick. The naive approach used
    "seconds since I was last called" as the elapsed time - so when a genuinely fresh reading
    finally arrived reflecting a normal ~14C change that had actually accumulated over the whole
    multi-second stale window, dividing by only the last 0.5s poll tick produced exactly this
    kind of impossible rate (14 / 0.5 * 60 = 1680, in the same ballpark as the reported 1641).

    This test proves the fix: RampTracker must use the real gap between two DISTINCT
    get_temps_timestamp() values, not wall-clock time at the caller, and must not treat a run of
    identical/stale timestamps as if a fresh sample had arrived every time it's called."""
    tracker = RampTracker()
    limits = SafetyLimits(max_observed_ramp_c_per_min=120.0)

    # t=0: first real, fresh sample.
    reading_1 = _reading(lower=200.0)
    prior, secs = tracker.observe(reading_1, temps_timestamp=1000.0)
    check_reading(limits, reading_1, prior, secs)  # no violation possible - no prior yet

    # The caller (outer poll loop) keeps ticking every 0.5s, but the underlying temps reading is
    # STALE the whole time (e.g. a contended coil write has paused the passive listener) - the
    # timestamp from get_temps_timestamp() does NOT advance across these ticks, even though the
    # caller keeps calling observe() every 0.5s like normal.
    for _ in range(20):  # 10 real seconds of stale polling
        prior, secs = tracker.observe(reading_1, temps_timestamp=1000.0)  # same stale timestamp
        violations = check_reading(limits, reading_1, prior, secs)
        assert not any("ramp" in v.limit_name for v in violations), "a stale-but-repeated reading must never trigger a ramp violation"

    # t=10.5s real time later: a genuinely fresh reading finally arrives. The real temperature
    # rose by a physically normal 14C over that real ~10.5s gap (80C/min - well under the 120
    # limit) - but the OLD, broken approach would have computed this against only the last poll
    # tick (0.5s), giving 14/0.5*60 = 1680C/min, a false CRITICAL violation matching the reported
    # bug almost exactly.
    reading_2 = _reading(lower=214.0)
    prior, secs = tracker.observe(reading_2, temps_timestamp=1010.5)
    assert prior is reading_1
    assert secs == 10.5
    violations = check_reading(limits, reading_2, prior, secs)
    assert not any("ramp" in v.limit_name for v in violations), "a normal real ramp rate must not false-trigger"

    # Sanity check the fix doesn't just suppress ALL ramp violations: a genuinely fast real ramp,
    # sustained over at least the default min_interval_s (5s), must still be caught.
    tracker2 = RampTracker()
    reading_a = _reading(lower=200.0)
    tracker2.observe(reading_a, temps_timestamp=2000.0)
    reading_b = _reading(lower=250.0)  # 50C over 6 real seconds = 500C/min - genuinely too fast
    prior, secs = tracker2.observe(reading_b, temps_timestamp=2006.0)
    violations = check_reading(limits, reading_b, prior, secs)
    assert any(v.limit_name == "max_observed_ramp_c_per_min_lower" for v in violations)


def test_ramp_tracker_ignores_noisy_short_interval_samples_reported_bug_2():
    """Regression test (2026-09, reported immediately after the first fix landed): once stale
    readings stopped false-triggering, a NEW false-positive appeared from readings that were
    genuinely fresh but only ~1-2 seconds apart (MachineBus's active poll loop requests fresh
    temps roughly every ACTIVE_POLL_INTERVAL_S=1.0, and the passive listener can be even faster).
    A couple of degrees of ordinary short-term noise, extrapolated from a ~1s gap to a per-minute
    figure, looks like a runaway ramp even though it isn't one - reported live, the operator kept
    raising the ramp limit (120 -> 200 -> 380 C/min) and kept getting caught by essentially the
    same few degrees of per-tick noise, because the real problem was the too-short baseline, not
    the limit value.

    This proves the fix: a fresh sample arriving less than min_interval_s after the current
    baseline must not advance the baseline or produce a rate at all - it should wait, and once
    enough real time has accumulated, compute the rate across the WHOLE accumulated window
    (using the original baseline), which correctly averages out the noise instead of amplifying
    it 60x."""
    tracker = RampTracker()
    limits = SafetyLimits(max_observed_ramp_c_per_min=120.0)

    # t=0: baseline.
    r0 = _reading(upper=200.0)
    prior, secs = tracker.observe(r0, temps_timestamp=100.0, min_interval_s=5.0)
    assert prior is None and secs is None

    # t=1,2,3,4: fresh samples every ~1s, each wobbling by a few degrees of ordinary noise - a
    # naive per-tick rate would read a few hundred C/min each time (e.g. 3C in 1s = 180C/min,
    # matching the first reported number, 179, almost exactly). None of these must fire, and the
    # baseline must NOT advance to any of them (still < 5s since the real baseline at t=0).
    noisy_readings = [
        _reading(upper=203.0),  # +3C in 1s -> naive 180C/min
        _reading(upper=200.5),  # -2.5C in 1s -> naive -150C/min
        _reading(upper=204.0),  # +3.5C in 1s -> naive 210C/min
        _reading(upper=201.0),  # -3C in 1s -> naive -180C/min
    ]
    for i, r in enumerate(noisy_readings, start=1):
        prior, secs = tracker.observe(r, temps_timestamp=100.0 + i, min_interval_s=5.0)
        assert prior is None and secs is None, f"tick {i}: a <5s-old sample must not produce a rate at all"
        violations = check_reading(limits, r, prior, secs)
        assert not any("ramp" in v.limit_name for v in violations), f"tick {i}: noisy short-interval sample must not fault"

    # t=5: finally >= min_interval_s since the t=0 baseline. The real net change over that whole
    # 5s window is 201.0 - 200.0 = 1C -> 12C/min, correctly unremarkable - NOT computed against
    # the immediately-preceding noisy tick (201.0 vs 204.0, which would itself look like a fast
    # drop over 1s).
    r5 = _reading(upper=201.0)
    prior, secs = tracker.observe(r5, temps_timestamp=105.0, min_interval_s=5.0)
    assert prior is r0, "must compute against the ORIGINAL baseline, not the most recent noisy tick"
    assert secs == 5.0
    violations = check_reading(limits, r5, prior, secs)
    assert not any("ramp" in v.limit_name for v in violations)


def test_ramp_tracker_still_catches_a_sustained_fast_ramp_across_the_minimum_window():
    tracker = RampTracker()
    limits = SafetyLimits(max_observed_ramp_c_per_min=120.0)
    r0 = _reading(upper=200.0)
    tracker.observe(r0, temps_timestamp=100.0, min_interval_s=5.0)
    r1 = _reading(upper=280.0)  # +80C over 5s = 960C/min - a real, sustained, dangerous ramp
    prior, secs = tracker.observe(r1, temps_timestamp=105.0, min_interval_s=5.0)
    assert prior is r0 and secs == 5.0
    violations = check_reading(limits, r1, prior, secs)
    assert any(v.limit_name == "max_observed_ramp_c_per_min_upper" for v in violations)


def test_ramp_tracker_min_interval_s_is_configurable_per_call():
    """The caller passes SafetyLimits.min_ramp_sample_interval_s fresh on every call (not baked
    into the tracker at construction), so an operator's live threshold edit takes effect
    immediately - same pattern as every other SafetyLimits field."""
    tracker = RampTracker()
    r0 = _reading(upper=200.0)
    tracker.observe(r0, temps_timestamp=100.0, min_interval_s=10.0)
    r1 = _reading(upper=203.0)
    # Only 3s elapsed - insufficient for a 10s minimum.
    prior, secs = tracker.observe(r1, temps_timestamp=103.0, min_interval_s=10.0)
    assert prior is None and secs is None
    # Same 3s gap, but a caller with a looser (shorter) configured minimum accepts it.
    tracker2 = RampTracker()
    tracker2.observe(r0, temps_timestamp=100.0, min_interval_s=2.0)
    prior2, secs2 = tracker2.observe(r1, temps_timestamp=103.0, min_interval_s=2.0)
    assert prior2 is r0 and secs2 == 3.0


def test_ramp_tracker_reset_clears_baseline():
    tracker = RampTracker()
    tracker.observe(_reading(), temps_timestamp=100.0)
    assert tracker.prior_timestamp == 100.0
    tracker.reset()
    assert tracker.prior_reading is None
    assert tracker.prior_timestamp == 0.0
    # After reset, the next observe() is treated as the very first sample again - no rate yet.
    prior, secs = tracker.observe(_reading(), temps_timestamp=200.0)
    assert prior is None and secs is None


def test_ramp_tracker_reset_on_detected_section_transition_avoids_false_positive_from_intended_jump():
    """Regression test (2026-09, third related fix): a real, large, but ENTIRELY INTENDED target
    jump at a section transition (reported live: "Infrared changed 488C/min" at a section 7->8
    transition, the profile's final/peak section) must not fire a ramp violation. The fix
    integrates RunMonitor.estimate_current_section() (observed-data-only section detection, no
    profile "rate" field involved) with RampTracker.reset(): when the caller detects the
    estimated section has advanced, it resets the ramp baseline right at that boundary - the
    next window starts fresh AFTER the transition, so the jump itself is never measured as a
    rate at all. This reproduces the exact caller pattern used in main_window.py/web_server.py's
    _check_safety_limits()."""
    # Upper/Lower stay constant across every section (so estimate_current_section's matching is
    # driven entirely by IR, isolating exactly the zone the real report was about) with a real
    # +40C jump into the final section - close to the reported 488C/min over a 5s window.
    ir_targets = [100, 120, 140, 160, 170, 172, 174, 214]
    profile = Profile(name="Test", sections=[
        CurveSection(upper_temp=200, lower_temp=200, ir_temp=t) for t in ir_targets
    ])
    monitor = RunMonitor(AlertConfig())
    monitor.reset(profile=profile)
    tracker = RampTracker()
    limits = SafetyLimits(max_observed_ramp_c_per_min=320.0)
    last_section = [None]

    def tick(ir, ts):
        r = _reading(upper=200.0, lower=200.0, ir=ir)
        current_section = monitor.estimate_current_section(r)
        if current_section is not None and last_section[0] is not None and current_section != last_section[0]:
            tracker.reset()
        last_section[0] = current_section
        prior, secs = tracker.observe(r, temps_timestamp=ts, min_interval_s=5.0)
        return current_section, check_reading(limits, r, prior, secs)

    # Walk forward through the profile in real, well-spaced (>= min_interval_s) ticks until
    # settled in section 6 (the second-to-last section, IR target 174).
    t = 1000.0
    for ir in (172.0, 173.0, 174.0):
        _, violations = tick(ir, t)
        assert not any("ramp" in v.limit_name for v in violations)
        t += 5.0
    assert last_section[0] == 6

    # The transition: IR jumps 174 -> 214 (the final section's target), a real +40C change,
    # detected as a section advance in the SAME tick the jump is observed. Must not fault.
    sec, violations = tick(214.0, t)
    assert sec == 7, "should have detected the move into the final (peak) section"
    assert not any("ramp" in v.limit_name for v in violations), "an intended section-boundary jump must not fault"
    t += 5.0

    # Confirm the check is still alive: a genuine additional runaway WITHIN the new section (no
    # further section to advance to - already at the last one) must still be caught.
    sec, violations = tick(300.0, t)  # +86C over 5s = 1032C/min, genuinely dangerous
    assert sec == 7, "no section left to advance to - this is a real within-section runaway"
    assert any(v.limit_name == "max_observed_ramp_c_per_min_infrared" for v in violations), "a real within-section runaway must still be caught"


def test_ramp_tracker_zero_timestamp_never_treated_as_fresh():
    """get_temps_timestamp() returns 0.0 when temps have never been read at all - must never be
    treated as a valid sample to compare against."""
    tracker = RampTracker()
    prior, secs = tracker.observe(_reading(), temps_timestamp=0.0)
    assert prior is None and secs is None
    prior, secs = tracker.observe(_reading(), temps_timestamp=0.0)
    assert prior is None and secs is None  # still no valid prior - 0.0 was never accepted as fresh


def test_run_duration_violation():
    limits = SafetyLimits(max_run_duration_s=60.0)
    assert check_run_duration(limits, 90.0) is not None
    assert check_run_duration(limits, 30.0) is None


def test_communication_outage_violation():
    limits = SafetyLimits(max_communication_outage_s=10.0)
    assert check_communication_outage(limits, 15.0) is not None
    assert check_communication_outage(limits, 5.0) is None


def test_profile_against_limits_flags_over_limit_section():
    limits = SafetyLimits(max_upper_temp_c=200.0)
    profile = Profile(sections=[CurveSection(upper_temp=250) if i == 0 else CurveSection() for i in range(8)])
    violations = check_profile_against_limits(limits, profile)
    assert any(v.limit_name == "max_upper_temp_c" for v in violations)


def test_profile_against_limits_flags_negative_temperature():
    limits = SafetyLimits()
    profile = Profile(sections=[CurveSection(upper_temp=-5) if i == 0 else CurveSection() for i in range(8)])
    violations = check_profile_against_limits(limits, profile)
    assert any(v.limit_name == "negative_temperature" for v in violations)
