"""Coverage for _check_run_ended_externally() - a real gap reported live 2026-09-14: neither a
profile completing on its own (the controller's own curve-sequencer reaches the end and the
running coil drops by itself) nor the touchscreen's own physical Stop button were ever detected -
only this app's own /api/stop and a fault auto-stop were. No "run completed" notification, the CSV
logger never stopped, and run_recovery's marker was never cleared for this third, more common way
a run ends.
"""
import time

import pytest

import run_recovery
import web_server
from modbus_client import LiveReading, Profile, CurveSection
from run_finalization import RunFinalizationTracker
from profile_search import OllamaConfig


@pytest.fixture(autouse=True)
def _reset_shared_monitor():
    """web_server.monitor is a real module-level singleton (not something monkeypatch can swap
    out, since active_profile has no setter) - reset() it back to a clean slate after each test so
    a profile set here can't leak into some unrelated test elsewhere in the suite."""
    yield
    web_server.monitor.reset(profile=None)


class _Bus:
    def get_diagnostics(self):
        return {}


class _Logger:
    def __init__(self):
        self.stopped = False

    def stop_run(self):
        self.stopped = True


def _reading(running, upper=200.0):
    return LiveReading(
        upper_temp=upper, lower_temp=upper, infrared_temp=upper, probe_temp=upper,
        running=running, running_mirror=running, vacuum=False, cooling=False, keep=False,
    )


def _setup(monkeypatch, tmp_path, csv_rows=2, n_sections=8):
    marker = tmp_path / "active_run.json"
    monkeypatch.setattr(run_recovery, "MARKER_PATH", marker)
    csv_path = tmp_path / "run_test.csv"
    header = "timestamp,elapsed_s,upper_temp,lower_temp,infrared_temp,probe_temp,running,vacuum,cooling,profile_name\n"
    rows = "".join(f"{i}.0,{i * 0.5},200,200,200,200,True,False,False,Test\n" for i in range(csv_rows))
    csv_path.write_text(header + rows)
    run_recovery.mark_run_started("Test", csv_path.name)

    monkeypatch.setattr(web_server, "bus", _Bus(), raising=False)
    monkeypatch.setattr(web_server, "logger_", _Logger())
    monkeypatch.setattr(web_server, "_run_tracker", RunFinalizationTracker())
    monkeypatch.setattr(web_server, "_current_log_path", csv_path)
    monkeypatch.setattr(web_server, "_logging_active", True)
    monkeypatch.setattr(web_server, "_run_process_limits", None)
    monkeypatch.setattr(web_server, "_run_start_time", time.monotonic() - 100.0)
    monkeypatch.setattr(web_server, "_active_profile_name", "Test")
    monkeypatch.setattr(web_server, "_run_end_watch_was_running", False)
    monkeypatch.setattr(web_server, "_last_active_section_index", None)
    monkeypatch.setattr(web_server, "_last_active_section_total", None)
    monkeypatch.setattr(web_server, "_last_active_run_start_time", None)
    monkeypatch.setattr(web_server, "ollama_config", OllamaConfig(enabled=False))
    web_server.monitor.reset(profile=Profile(name="Test", sections=[CurveSection() for _ in range(n_sections)]))

    notified = []
    monkeypatch.setattr(web_server.notifications, "notify_async", lambda config, event, message: notified.append((event, message)))
    return marker, csv_path, notified


def test_natural_completion_is_detected_and_finalized(tmp_path, monkeypatch):
    marker, csv_path, notified = _setup(tmp_path=tmp_path, monkeypatch=monkeypatch, n_sections=8)
    monkeypatch.setattr(web_server.monitor, "estimate_current_section", lambda reading: 7)  # final section (0-based)

    web_server._check_run_ended_externally(_reading(running=True))  # establishes the "was running" edge + section
    web_server._check_run_ended_externally(_reading(running=False))  # the drop

    assert not marker.exists(), "run_recovery marker must be cleared - this is a KNOWN ending"
    assert web_server._run_tracker is None
    assert web_server._logging_active is False
    assert web_server.logger_.stopped is True
    assert len(notified) == 1
    event, message = notified[0]
    assert event == "run_completed"
    assert "reached the end of the profile" in message
    assert "Test" in message


def test_early_end_is_flagged_as_such_not_claimed_complete(tmp_path, monkeypatch):
    marker, csv_path, notified = _setup(tmp_path=tmp_path, monkeypatch=monkeypatch, n_sections=8)
    monkeypatch.setattr(web_server.monitor, "estimate_current_section", lambda reading: 2)  # section 3 of 8, not final

    web_server._check_run_ended_externally(_reading(running=True))
    web_server._check_run_ended_externally(_reading(running=False))

    assert len(notified) == 1
    event, message = notified[0]
    assert "ended early" in message
    assert "section 3 of 8" in message
    assert "reached the end of the profile" not in message


def test_no_section_estimate_available_is_honest_about_it(tmp_path, monkeypatch):
    """No active profile / no section match at all - must not fabricate a claim either way."""
    marker, csv_path, notified = _setup(tmp_path=tmp_path, monkeypatch=monkeypatch, n_sections=8)
    monkeypatch.setattr(web_server.monitor, "estimate_current_section", lambda reading: None)

    web_server._check_run_ended_externally(_reading(running=True))
    web_server._check_run_ended_externally(_reading(running=False))

    assert len(notified) == 1
    message = notified[0][1]
    assert "ended (not stopped from this app)" in message


def test_no_op_when_no_run_was_active(tmp_path, monkeypatch):
    marker, csv_path, notified = _setup(tmp_path=tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(web_server, "_run_tracker", None)
    monkeypatch.setattr(web_server, "_logging_active", False)
    monkeypatch.setattr(web_server.monitor, "estimate_current_section", lambda reading: None)

    web_server._check_run_ended_externally(_reading(running=True))
    web_server._check_run_ended_externally(_reading(running=False))

    assert notified == []


def test_no_op_while_still_running(tmp_path, monkeypatch):
    marker, csv_path, notified = _setup(tmp_path=tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(web_server.monitor, "estimate_current_section", lambda reading: 4)

    web_server._check_run_ended_externally(_reading(running=True))
    web_server._check_run_ended_externally(_reading(running=True))

    assert notified == []
    assert web_server._run_tracker is not None
    assert web_server._logging_active is True


def test_message_includes_real_zone_peaks(tmp_path, monkeypatch):
    # zone_statistics is computed from the run's own CSV rows (see run_finalization.py), not from
    # the LiveReading objects passed to the check - write a real peak into the CSV to assert on.
    marker, csv_path, notified = _setup(tmp_path=tmp_path, monkeypatch=monkeypatch, n_sections=8)
    csv_path.write_text(
        "timestamp,elapsed_s,upper_temp,lower_temp,infrared_temp,probe_temp,running,vacuum,cooling,profile_name\n"
        "0.0,0.0,200,200,200,200,True,False,False,Test\n"
        "1.0,0.5,277.6,200,200,200,True,False,False,Test\n"
    )
    monkeypatch.setattr(web_server.monitor, "estimate_current_section", lambda reading: 7)

    web_server._check_run_ended_externally(_reading(running=True))
    web_server._check_run_ended_externally(_reading(running=False))

    message = notified[0][1]
    assert "Upper 277.6C" in message


def test_defers_when_run_control_lock_is_held_by_a_concurrent_stop(tmp_path, monkeypatch):
    """Regression test for a real race found in review (2026-09-15): /api/stop holds
    _run_control_lock for its ENTIRE write_coil()+finalize sequence, which can legitimately run
    for many seconds under real bus contention. write_coil() updates its coil cache the instant
    ANY attempt or the readback confirms - well before /api/stop reaches its own
    _logging_active=False/finalize code - so a poll tick landing in that window used to see
    reading.running already False with _run_tracker/_logging_active still set, and would finalize
    (and notify about) the SAME run a second time. Must defer instead of racing it."""
    marker, csv_path, notified = _setup(tmp_path=tmp_path, monkeypatch=monkeypatch, n_sections=8)
    monkeypatch.setattr(web_server.monitor, "estimate_current_section", lambda reading: 7)
    web_server._check_run_ended_externally(_reading(running=True))

    web_server._run_control_lock.acquire()  # simulates /api/stop mid-flight, holding the lock
    try:
        web_server._check_run_ended_externally(_reading(running=False))
        assert notified == [], "must not finalize while /api/stop could be concurrently doing so"
        assert web_server._run_tracker is not None, "must not have finalized yet"
        assert web_server._run_end_watch_was_running is True, "edge must be preserved for a retry next tick"
    finally:
        web_server._run_control_lock.release()

    # Next tick, lock now free (as if /api/stop's own write finished) - must finalize normally.
    web_server._check_run_ended_externally(_reading(running=False))
    assert len(notified) == 1
    assert web_server._run_tracker is None


def test_no_duplicate_notification_when_stop_finalized_while_lock_was_held(tmp_path, monkeypatch):
    """The other half of the same race: if /api/stop finished its OWN finalize+notify while this
    function was waiting its turn, it must recognize there's nothing left to do and must NOT send
    a second 'run completed' notification for the same run."""
    marker, csv_path, notified = _setup(tmp_path=tmp_path, monkeypatch=monkeypatch, n_sections=8)
    monkeypatch.setattr(web_server.monitor, "estimate_current_section", lambda reading: 7)
    web_server._check_run_ended_externally(_reading(running=True))

    web_server._run_control_lock.acquire()
    try:
        # Simulates /api/stop's own finalize+notify completing while holding the lock.
        notified.append(("run_completed", "Run stopped: Test (from /api/stop)"))
        web_server._run_tracker = None
        web_server._logging_active = False
    finally:
        web_server._run_control_lock.release()

    web_server._check_run_ended_externally(_reading(running=False))

    assert notified == [("run_completed", "Run stopped: Test (from /api/stop)")], "must not add a second notification"
    assert web_server._run_end_watch_was_running is False


def test_duration_survives_run_start_time_being_cleared_before_this_runs(tmp_path, monkeypatch):
    """Regression test for a real bug caught in review before it ever shipped: _check_safety_limits
    (called earlier in the same poll tick, in _run_poll_tick) clears the module-level
    _run_start_time to None the instant reading.running goes False - BEFORE this function ever
    runs on that same tick. Reading _run_start_time directly here would always compute
    duration_s=0 for every naturally/externally-ended run. This asserts the fix: a private
    snapshot (_last_active_run_start_time) taken every tick while still running, consumed here."""
    marker, csv_path, notified = _setup(tmp_path=tmp_path, monkeypatch=monkeypatch, n_sections=8)
    monkeypatch.setattr(web_server, "_run_start_time", time.monotonic() - 45.0)
    monkeypatch.setattr(web_server.monitor, "estimate_current_section", lambda reading: 7)

    web_server._check_run_ended_externally(_reading(running=True))
    # Mirror what _check_safety_limits actually does on the same tick the drop is observed, before
    # _check_run_ended_externally gets a turn.
    monkeypatch.setattr(web_server, "_run_start_time", None)
    web_server._check_run_ended_externally(_reading(running=False))

    message = notified[0][1]
    assert "Duration: 0s" not in message
    assert "Duration: 45s" in message
