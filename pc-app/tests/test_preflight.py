from preflight import run_preflight, curve_send_status, profile_curve_regs
from safety_limits import SafetyLimits
from modbus_client import LiveReading, Profile, CurveSection


class FakeBus:
    def __init__(self, reading=None, connected=True):
        self._reading = reading
        self.is_connected = connected

    def get_live_reading(self):
        return self._reading


def _good_reading():
    return LiveReading(
        upper_temp=150.0, lower_temp=140.0, infrared_temp=90.0, probe_temp=80.0,
        running=False, running_mirror=False, vacuum=False, cooling=False, keep=False,
    )


def _good_profile():
    return Profile(sections=[CurveSection(upper_temp=200) for _ in range(8)])


def test_passes_with_good_reading_and_profile():
    bus = FakeBus(reading=_good_reading())
    result = run_preflight(bus, _good_profile(), SafetyLimits())
    assert result.passed is True


def test_fails_communication_check_when_disconnected():
    bus = FakeBus(reading=None, connected=False)
    result = run_preflight(bus, _good_profile(), SafetyLimits())
    assert result.passed is False
    comm_check = next(c for c in result.checks if c.name == "Communication")
    assert comm_check.passed is False


def test_fails_sensor_check_on_implausible_reading():
    bad_reading = LiveReading(
        upper_temp=-3276.8, lower_temp=140.0, infrared_temp=90.0, probe_temp=80.0,
        running=False, running_mirror=False, vacuum=False, cooling=False, keep=False,
    )
    bus = FakeBus(reading=bad_reading)
    result = run_preflight(bus, _good_profile(), SafetyLimits())
    assert result.passed is False


def test_fails_profile_check_on_wrong_section_count():
    bus = FakeBus(reading=_good_reading())
    bad_profile = Profile(sections=[CurveSection()])
    result = run_preflight(bus, bad_profile, SafetyLimits())
    assert result.passed is False


def test_fails_profile_check_when_exceeding_safety_limits():
    bus = FakeBus(reading=_good_reading())
    limits = SafetyLimits(max_upper_temp_c=100.0)
    result = run_preflight(bus, _good_profile(), limits)
    assert result.passed is False


# --- pre-Start "was this curve actually sent?" reminder (curve_send_status) ---

def test_curve_send_status_empty_when_all_zero():
    empty = Profile(sections=[CurveSection() for _ in range(8)])
    assert curve_send_status(profile_curve_regs(empty), None) == "empty"
    assert curve_send_status(profile_curve_regs(empty), [0] * 72) == "empty"


def test_curve_send_status_unsent_when_nothing_sent_this_session():
    p = _good_profile()
    assert curve_send_status(profile_curve_regs(p), None) == "unsent"


def test_curve_send_status_sent_when_matches_last_send():
    p = _good_profile()
    regs = profile_curve_regs(p)
    assert curve_send_status(regs, list(regs)) == "sent"


def test_curve_send_status_unsent_when_edited_since_last_send():
    p = _good_profile()
    last_sent = profile_curve_regs(p)
    p.sections[0].upper_temp = 205  # one cell changed after the send
    assert curve_send_status(profile_curve_regs(p), last_sent) == "unsent"


def test_curve_send_status_sent_again_after_editing_back_to_sent_values():
    p = _good_profile()
    last_sent = profile_curve_regs(p)
    p.sections[0].upper_temp = 205
    p.sections[0].upper_temp = 200  # reverted - curve is once again exactly what was sent
    assert curve_send_status(profile_curve_regs(p), last_sent) == "sent"
