"""Tests for RunMonitor.estimate_current_section() (2026-08-23, explicitly requested: "have the
live temp graph show what section of the profile its on"). This deliberately does NOT use the
profile's "rate" field or elapsed time - see alerts.py's module docstring for why those units were
never confirmed against real hardware. It only compares live temps against each section's real
target values, moving forward-only through the section list since sections always execute in
order."""
from alerts import AlertConfig, RunMonitor
from modbus_client import CurveSection, LiveReading, Profile


def _reading(upper=0.0, lower=0.0, ir=0.0, running=True):
    return LiveReading(
        upper_temp=upper, lower_temp=lower, infrared_temp=ir, probe_temp=0.0,
        running=running, running_mirror=running, vacuum=False, cooling=False, keep=False,
    )


def _profile():
    # A simple ramp, mirroring how real saved profiles look (see profile_search.py's reference
    # profile feature): Upper == Lower, IR offset below, monotonically increasing.
    sections = [
        CurveSection(upper_temp=t, lower_temp=t, ir_temp=t - 30, upper_rate=2, lower_rate=2, ir_rate=3)
        for t in (100, 150, 200, 245)
    ]
    return Profile(name="Test Ramp", sections=sections)


def test_no_active_profile_returns_none():
    monitor = RunMonitor(AlertConfig())
    monitor.reset(profile=None)
    assert monitor.estimate_current_section(_reading(100, 100, 70)) is None


def test_not_running_returns_none_even_with_active_profile():
    monitor = RunMonitor(AlertConfig())
    monitor.reset(profile=_profile())
    assert monitor.estimate_current_section(_reading(100, 100, 70, running=False)) is None


def test_picks_the_nearest_section_by_target_values():
    monitor = RunMonitor(AlertConfig())
    monitor.reset(profile=_profile())
    # Closest to section 0 (100/100/70)
    assert monitor.estimate_current_section(_reading(98, 101, 68)) == 0


def test_advances_forward_as_temps_climb_toward_later_sections():
    monitor = RunMonitor(AlertConfig())
    monitor.reset(profile=_profile())
    assert monitor.estimate_current_section(_reading(98, 101, 68)) == 0
    assert monitor.estimate_current_section(_reading(148, 152, 122)) == 1
    assert monitor.estimate_current_section(_reading(243, 246, 213)) == 3


def test_never_reports_an_earlier_section_once_advanced():
    """A momentary reading that looks closer to an earlier target (e.g. a brief dip mid-ramp)
    must not make the indicator jump backward - sections always execute in order."""
    monitor = RunMonitor(AlertConfig())
    monitor.reset(profile=_profile())
    assert monitor.estimate_current_section(_reading(198, 202, 168)) == 2
    # A reading that looks exactly like section 0's target - should still report 2, not regress.
    assert monitor.estimate_current_section(_reading(100, 100, 70)) == 2


def test_resets_to_none_on_a_new_run():
    monitor = RunMonitor(AlertConfig())
    monitor.reset(profile=_profile())
    monitor.estimate_current_section(_reading(198, 202, 168))
    monitor.reset(profile=_profile())  # new run starting
    # Right after reset, the first estimate should be free to land anywhere again - starting a
    # fresh climb from section 0's target proves the forward-only floor was actually cleared.
    assert monitor.estimate_current_section(_reading(98, 101, 68)) == 0


def test_profile_with_no_sections_returns_none():
    monitor = RunMonitor(AlertConfig())
    monitor.reset(profile=Profile(name="Empty", sections=[]))
    assert monitor.estimate_current_section(_reading(100, 100, 70)) is None


def test_zone_with_zero_target_is_ignored_in_the_match():
    """A zone whose target is 0 for every section (unused zone) must not distort the match - only
    zones the profile actually uses should count toward the nearest-section score."""
    sections = [
        CurveSection(upper_temp=100, lower_temp=0, ir_temp=0),
        CurveSection(upper_temp=200, lower_temp=0, ir_temp=0),
    ]
    monitor = RunMonitor(AlertConfig())
    monitor.reset(profile=Profile(name="Upper Only", sections=sections))
    assert monitor.estimate_current_section(_reading(upper=195, lower=999, ir=999)) == 1
