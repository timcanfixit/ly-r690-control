from profile_simulation import simulate_profile
from modbus_client import Profile, CurveSection


def test_basic_simulation_math():
    sections = [CurveSection() for _ in range(8)]
    sections[0].upper_temp, sections[0].upper_hold_time = 220, 40
    sections[1].upper_temp, sections[1].upper_hold_time = 250, 30
    sections[0].lower_temp, sections[1].lower_temp = 150, 180
    profile = Profile(sections=sections)

    sim = simulate_profile(profile, wetting_temp_c=217.0)
    assert sim.section_count == 8
    assert sim.peak_upper_c == 250
    assert sim.peak_lower_c == 180
    assert sim.total_hold_time_s == 40 + 30  # only sections 0/1 have nonzero hold times
    # both sections' upper target is >= wetting temp, so both hold times count
    assert sim.time_above_wetting_s == 40 + 30


def test_no_section_reaches_wetting_temp():
    sections = [CurveSection(upper_temp=100, upper_hold_time=30) for _ in range(8)]
    profile = Profile(sections=sections)
    sim = simulate_profile(profile, wetting_temp_c=217.0)
    assert sim.time_above_wetting_s == 0


def test_empty_profile_does_not_crash():
    profile = Profile(sections=[])
    sim = simulate_profile(profile, wetting_temp_c=217.0)
    assert sim.section_count == 0
    assert sim.peak_upper_c == 0
    assert sim.total_hold_time_s == 0
