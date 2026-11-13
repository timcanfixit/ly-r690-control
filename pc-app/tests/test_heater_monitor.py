from heater_monitor import HeaterMonitor
from modbus_client import LiveReading, Profile, CurveSection


def _reading(upper=100.0, lower=100.0, ir=50.0, running=True):
    return LiveReading(
        upper_temp=upper, lower_temp=lower, infrared_temp=ir, probe_temp=50.0,
        running=running, running_mirror=running, vacuum=False, cooling=False, keep=False,
    )


def _profile_using_only_upper(target=250):
    # Lower/IR sections are all zero - this profile never uses those zones at all.
    return Profile(sections=[CurveSection(upper_temp=target) for _ in range(8)])


def test_zone_never_used_by_profile_does_not_false_positive_overshoot():
    """Regression test for the real bug found this session: a zone with an all-zero profile
    target (never used by this profile) was flagged as 'overshooting' the instant it read
    anything above 0C."""
    hm = HeaterMonitor()
    profile = _profile_using_only_upper(target=250)
    # Lower reads a plausible ambient-ish value even though the profile never targets it.
    issues = hm.update(_reading(upper=50.0, lower=30.0), profile, now=0.0)
    assert not any(i.zone == "lower" and i.kind == "overshoot" for i in issues)


def test_overshoot_detected_on_zone_the_profile_does_use():
    hm = HeaterMonitor(overshoot_tolerance_c=10.0)
    profile = _profile_using_only_upper(target=200)
    issues = hm.update(_reading(upper=220.0), profile, now=0.0)
    assert any(i.zone == "upper" and i.kind == "overshoot" for i in issues)


def test_not_responding_fires_once_after_flat_window_then_stays_silent():
    """Regression test for a real test-methodology mistake made earlier this session: a loop that
    only checks the LAST iteration's return value misses the actual 'new issue' event, which
    fires once and then dedupes on subsequent identical ticks - this accumulates across the whole
    loop instead."""
    hm = HeaterMonitor(not_responding_window_s=100.0, not_responding_min_delta_c=2.0)
    profile = _profile_using_only_upper(target=250)
    now = 0.0
    all_issues = []
    for _ in range(12):
        all_issues += hm.update(_reading(upper=50.0), profile, now=now)
        now += 10.0
    not_responding_count = sum(1 for i in all_issues if i.kind == "not_responding")
    assert not_responding_count == 1, f"expected exactly one not_responding event, got {not_responding_count}"

    # Continuing flat must not re-raise it (dedup while the same condition persists).
    again = hm.update(_reading(upper=50.0), profile, now=now)
    assert not any(i.kind == "not_responding" for i in again)


def test_not_responding_does_not_fire_while_genuinely_climbing():
    hm = HeaterMonitor(not_responding_window_s=50.0, not_responding_min_delta_c=2.0)
    profile = _profile_using_only_upper(target=250)
    now = 0.0
    all_issues = []
    temp = 50.0
    for _ in range(10):
        all_issues += hm.update(_reading(upper=temp), profile, now=now)
        temp += 5.0
        now += 10.0
    assert not any(i.kind == "not_responding" for i in all_issues)


def test_stuck_on_detected_after_stop():
    hm = HeaterMonitor(stuck_on_window_s=60.0, stuck_on_min_rise_c=3.0)
    profile = _profile_using_only_upper(target=250)
    hm.update(_reading(upper=200.0, running=True), profile, now=0.0)
    hm.update(_reading(upper=200.0, running=False), profile, now=1.0)  # Stop issued
    issues = hm.update(_reading(upper=210.0, running=False), profile, now=10.0)
    assert any(i.kind == "stuck_on" for i in issues)


def test_reset_clears_state_between_runs():
    hm = HeaterMonitor(not_responding_window_s=10.0, not_responding_min_delta_c=2.0)
    profile = _profile_using_only_upper(target=250)
    for i in range(5):
        hm.update(_reading(upper=50.0), profile, now=float(i * 5))
    hm.reset()
    # After reset, history is empty - a single fresh reading must not immediately raise anything.
    issues = hm.update(_reading(upper=50.0), profile, now=0.0)
    assert issues == []
