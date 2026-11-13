from machine_state import MachineState, derive_state, is_control_enabled
from modbus_client import LiveReading


def _reading(running=False, keep=False, cooling=False, self_tune_upper=False, self_tune_lower=False, self_tune_ir=False):
    return LiveReading(
        upper_temp=100.0, lower_temp=100.0, infrared_temp=50.0, probe_temp=50.0,
        running=running, running_mirror=running, vacuum=False, cooling=cooling, keep=keep,
        self_tune_upper=self_tune_upper, self_tune_lower=self_tune_lower, self_tune_ir=self_tune_ir,
    )


def test_fault_wins_over_everything():
    assert derive_state(_reading(running=True), is_connected=True, active_fault=True) == MachineState.FAULT


def test_disconnected_when_not_connected():
    assert derive_state(None, is_connected=False) == MachineState.DISCONNECTED


def test_connecting_takes_precedence_over_disconnected():
    assert derive_state(None, is_connected=False, connecting=True) == MachineState.CONNECTING


def test_preflight_active():
    assert derive_state(_reading(), is_connected=True, preflight_active=True) == MachineState.PREFLIGHT


def test_no_reading_yet_is_unknown_not_idle():
    """Changed 2026-09-07 per external feature-gap review: this used to return IDLE, which is
    exactly the "state disagrees with observed data" fabrication machine_state.py's own docstring
    warns against - "no reading has ever arrived" is not the same fact as "genuinely idle", and
    conflating them hid real communication problems behind a falsely-reassuring IDLE label."""
    assert derive_state(None, is_connected=True) == MachineState.UNKNOWN


def test_running_and_keep_is_paused_not_running():
    assert derive_state(_reading(running=True, keep=True), is_connected=True) == MachineState.PAUSED


def test_running_without_keep():
    assert derive_state(_reading(running=True), is_connected=True) == MachineState.RUNNING


def test_cooling_when_not_running():
    assert derive_state(_reading(cooling=True), is_connected=True) == MachineState.COOLING


def test_idle_when_nothing_active():
    assert derive_state(_reading(), is_connected=True) == MachineState.IDLE


def test_enabled_controls_only_allow_listed_names():
    assert is_control_enabled(MachineState.IDLE, "start") is True
    assert is_control_enabled(MachineState.RUNNING, "start") is False
    assert is_control_enabled(MachineState.FAULT, "acknowledge_fault") is True
    assert is_control_enabled(MachineState.DISCONNECTED, "start") is False


# ---- self-tune (2026-09-07): confirmed live that COIL_RUNNING stays False the whole time a
# real self-tune runs, which used to be silently reported as IDLE even though real heating was
# happening - see machine_state.py's own comments on MachineState.SELF_TUNING for the incident.

def test_self_tune_ir_reported_instead_of_idle():
    assert derive_state(_reading(self_tune_ir=True), is_connected=True) == MachineState.SELF_TUNING


def test_self_tune_upper_reported_instead_of_idle():
    assert derive_state(_reading(self_tune_upper=True), is_connected=True) == MachineState.SELF_TUNING


def test_self_tune_lower_reported_instead_of_idle():
    assert derive_state(_reading(self_tune_lower=True), is_connected=True) == MachineState.SELF_TUNING


def test_fault_still_wins_over_self_tune():
    assert derive_state(_reading(self_tune_ir=True), is_connected=True, active_fault=True) == MachineState.FAULT


def test_self_tune_has_no_enabled_controls():
    """Deliberately conservative - see machine_state.py's ENABLED_CONTROLS comment for why."""
    for control in ("start", "stop", "send_profile", "send_pid", "vacuum", "cooling", "reconnect"):
        assert is_control_enabled(MachineState.SELF_TUNING, control) is False


# ---- UNKNOWN state (2026-09-07, from an external feature-gap review): a stale reading must not
# keep reporting whatever it last said, and a missing freshness signal must not change behavior
# for callers/tests that don't care about it.

def test_stale_reading_reported_as_unknown_not_whatever_it_last_said():
    from machine_state import STALE_READING_THRESHOLD_S
    reading = _reading(running=True)  # would be RUNNING if trusted
    assert derive_state(reading, is_connected=True, seconds_since_temps_update=STALE_READING_THRESHOLD_S + 1) == MachineState.UNKNOWN


def test_fresh_reading_within_threshold_is_trusted_normally():
    from machine_state import STALE_READING_THRESHOLD_S
    reading = _reading(running=True)
    assert derive_state(reading, is_connected=True, seconds_since_temps_update=STALE_READING_THRESHOLD_S - 1) == MachineState.RUNNING


def test_omitting_staleness_parameter_does_not_change_existing_behavior():
    """Backward compatibility: a caller (or test) that has no opinion on staleness must get
    exactly the pre-existing behavior, not a forced UNKNOWN - only a caller that actually measures
    and passes a real age gets the new protection."""
    assert derive_state(_reading(running=True), is_connected=True) == MachineState.RUNNING
    assert derive_state(_reading(), is_connected=True) == MachineState.IDLE


def test_fault_wins_over_unknown():
    assert derive_state(None, is_connected=True, active_fault=True) == MachineState.FAULT


def test_disconnected_wins_over_unknown():
    """DISCONNECTED is more specific/actionable than UNKNOWN (the port itself isn't even open,
    vs. open but untrustworthy) and must still take precedence."""
    assert derive_state(None, is_connected=False) == MachineState.DISCONNECTED


def test_unknown_only_allows_reconnect():
    for control in ("start", "stop", "send_profile", "send_pid", "vacuum", "cooling"):
        assert is_control_enabled(MachineState.UNKNOWN, control) is False
    assert is_control_enabled(MachineState.UNKNOWN, "reconnect") is True
