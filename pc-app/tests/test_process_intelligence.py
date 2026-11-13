from process_intelligence import compute_zone_deviations
from modbus_client import LiveReading, Profile, CurveSection


def _reading():
    return LiveReading(
        upper_temp=150.0, lower_temp=140.0, infrared_temp=100.0, probe_temp=90.0,
        running=True, running_mirror=True, vacuum=False, cooling=False, keep=False,
    )


def test_no_profile_means_no_target_for_any_zone():
    devs = compute_zone_deviations(_reading(), None)
    assert all(d.deviation_c is None for d in devs)
    assert {d.zone for d in devs} == {"Upper", "Lower", "Infrared"}


def test_deviation_is_distance_to_nearest_target_not_first_or_last():
    sections = [CurveSection() for _ in range(8)]
    sections[0].upper_temp = 160
    sections[1].upper_temp = 220
    profile = Profile(sections=sections)
    devs = compute_zone_deviations(_reading(), profile)
    upper = next(d for d in devs if d.zone == "Upper")
    assert upper.nearest_target_c == 160  # 150 is closer to 160 than to 220
    assert abs(upper.deviation_c - 10.0) < 1e-9


def test_zone_never_targeted_by_profile_reports_no_target():
    sections = [CurveSection(upper_temp=200) for _ in range(8)]  # lower/ir never set
    profile = Profile(sections=sections)
    devs = compute_zone_deviations(_reading(), profile)
    lower = next(d for d in devs if d.zone == "Lower")
    assert lower.deviation_c is None
