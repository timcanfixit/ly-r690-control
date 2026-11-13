"""Regression tests for the external code review of 2026-11-12 (finding numbers in the test names).
Each of the reviewer's 10 reproductions is asserted here with the CORRECTED behavior. No serial port,
network or real config files are touched."""
import threading
import time
from dataclasses import asdict
from unittest.mock import Mock

import pytest

import web_server as w
import config_backup
import profile_search
from alerts import AlertConfig, RunMonitor
from fault_manager import FaultManager
from machine_bus import MachineBus, WriteResult
from machine_state import MachineState, derive_state
from modbus_client import LiveReading, Profile, COIL_RUNNING, COIL_COOLING
from safety_limits import SafetyLimits, RampTracker, check_reading


def reading(**changes):
    values = dict(upper_temp=25., lower_temp=25., infrared_temp=25., probe_temp=25.,
                  running=False, running_mirror=False, vacuum=False, cooling=False, keep=False)
    values.update(changes)
    return LiveReading(**values)


@pytest.fixture
def env(monkeypatch, tmp_path):
    bus = Mock()
    bus.is_connected = True
    bus.get_live_reading.return_value = reading()
    bus.get_temps_timestamp.return_value = time.monotonic()
    bus.seconds_since_update.return_value = 0
    bus.coil_snapshot_age.return_value = 0.0
    bus.config_write_in_progress.return_value = False
    bus.config_incomplete.return_value = []
    bus.reader_failure = ""
    bus.get_diagnostics.return_value = {}
    bus.write_coil.return_value = WriteResult(True, 1)
    log = Mock()
    log.start_run.return_value = tmp_path / "run.csv"
    for name, value in dict(bus=bus, logger_=log, safety_limits=SafetyLimits(),
                            monitor=RunMonitor(AlertConfig()), fault_manager=FaultManager(),
                            _logging_active=False, _run_tracker=None, _current_log_path=None,
                            _run_process_limits=None, _active_violation_names=set(), _prior_reading=None,
                            _run_start_time=None, _last_ramp_section_index=None, _was_extracting=False,
                            _active_write_progress=None, _ramp_tracker=RampTracker(),
                            _preflight_active=False).items():
        monkeypatch.setattr(w, name, value, raising=False)
    monkeypatch.setattr(w.notifications, "notify_async", Mock())
    monkeypatch.setattr(w.run_recovery, "mark_run_started", Mock())
    monkeypatch.setattr(w.profile_store, "get_profile_metadata", lambda name: {})
    return bus, log, w.app.test_client()


# ---- #1 --------------------------------------------------------------------------------------
def test_1_failed_manual_stop_does_not_close_the_run(env, monkeypatch):
    bus, log, client = env
    bus.get_live_reading.return_value = reading(running=True)
    bus.write_coil.return_value = WriteResult(False, 36, "no ack")
    monkeypatch.setattr(w, "_logging_active", True)
    finalize = Mock(return_value=None)
    monkeypatch.setattr(w, "_finalize_current_run", finalize)
    body = client.post("/api/stop", json={}).get_json()
    assert body["ok"] is False and "NOT CONFIRMED" in body["detail"]
    assert w._logging_active is True
    log.stop_run.assert_not_called()
    finalize.assert_not_called()


def test_1_critical_stop_failure_is_retried_and_not_reported_as_success(env, monkeypatch):
    bus, _, _ = env
    hot = reading(running=True, upper_temp=999)
    bus.get_live_reading.return_value = hot
    bus.write_coil.return_value = WriteResult(False, 36)
    finalize = Mock()
    monkeypatch.setattr(w, "_finalize_current_run", finalize)
    w._check_safety_limits(hot)
    faults = list(w.fault_manager.active_faults)
    assert faults and all("Automatically stopped" not in f.auto_action_taken for f in faults)
    assert any("NOT CONFIRMED" in f.auto_action_taken for f in faults)
    assert w._protective_stop_pending is True
    finalize.assert_not_called()
    # next retry window: the stop is sent again
    bus.write_coil.reset_mock()
    monkeypatch.setattr(w, "_protective_retry_at", 0.0)
    w._check_safety_limits(hot)
    assert any(c.args == (COIL_RUNNING, False) for c in bus.write_coil.call_args_list)
    # machine finally observed stopped -> pending clears
    w._check_safety_limits(reading(running=False, upper_temp=900))
    assert w._protective_stop_pending is False


def test_1_confirmed_critical_stop_still_closes_run_and_says_so(env, monkeypatch):
    bus, _, _ = env
    monkeypatch.setattr(w, "_logging_active", True)
    finalize = Mock(return_value=None)
    monkeypatch.setattr(w, "_finalize_current_run", finalize)
    w._check_safety_limits(reading(running=True, upper_temp=999))
    assert any("Automatically stopped" in f.auto_action_taken for f in w.fault_manager.active_faults)
    finalize.assert_called_once()


# ---- #2 --------------------------------------------------------------------------------------
def test_2_overtemperature_during_selftune_sends_the_selftune_off(env):
    bus, _, _ = env
    w._check_safety_limits(reading(self_tune_ir=True, infrared_temp=999))
    assert w.fault_manager.has_unacknowledged_critical()
    assert any(c.args == (w.SELFTUNE_COILS["ir"], False) for c in bus.write_coil.call_args_list)
    # honest about what is NOT verified
    assert any("NOT been verified" in f.auto_action_taken for f in w.fault_manager.active_faults)


# ---- #3 / #6 ---------------------------------------------------------------------------------
def test_3_start_refused_when_machine_already_running(env):
    bus, _, client = env
    bus.get_live_reading.return_value = reading(running=True)
    r = client.post("/api/start", json={"profile": asdict(Profile(name="review"))})
    assert r.status_code == 409 and r.get_json()["ok"] is False
    assert not any(c.args == (COIL_RUNNING, True) for c in bus.write_coil.call_args_list)


def test_3_start_refused_when_data_is_stale(env):
    bus, _, client = env
    bus.get_temps_timestamp.return_value = time.monotonic() - 3600
    r = client.post("/api/start", json={"profile": asdict(Profile(name="review"))})
    assert r.status_code == 409


def test_3_curve_send_refused_mid_run(env):
    bus, _, client = env
    bus.get_live_reading.return_value = reading(running=True)
    assert client.post("/api/curve/send", json=asdict(Profile(name="x"))).status_code == 409
    bus.write_registers.assert_not_called()


def test_3_selftune_start_refused_unless_idle_but_off_always_allowed(env):
    bus, _, client = env
    bus.get_live_reading.return_value = reading(running=True)
    assert client.post("/api/selftune/ir", json={"value": True}).status_code == 409
    assert client.post("/api/selftune/ir", json={"value": False}).status_code == 200


def test_6_no_command_is_accepted_after_a_restart_is_scheduled(env, monkeypatch):
    _, _, client = env
    monkeypatch.setenv("INVOCATION_ID", "review")
    scheduled = Mock()
    monkeypatch.setattr(w, "_restart_process_soon", scheduled)
    assert client.post("/api/admin/restart", json={"confirm": True}).status_code == 200
    scheduled.assert_called_once()
    r = client.post("/api/start", json={"profile": asdict(Profile(name="review"))})
    assert r.status_code == 409 and "restart" in r.get_json()["detail"].lower()


def test_6_restart_refused_while_a_command_is_in_flight(env, monkeypatch):
    _, _, client = env
    monkeypatch.setenv("INVOCATION_ID", "review")
    monkeypatch.setattr(w, "_inflight_ops", 1)
    monkeypatch.setattr(w, "_restart_process_soon", Mock())
    assert client.post("/api/admin/restart", json={"confirm": True}).status_code == 409


# ---- #4 --------------------------------------------------------------------------------------
def test_4_logging_failure_does_not_prevent_the_safety_check(env, monkeypatch):
    bus, log, _ = env
    bus.get_curve_status.return_value = (0, 0)
    bus.get_pid_status.return_value = (0, 0)
    bus.get_fan_timing_status.return_value = (0, 0)
    bus.get_temp_comp_status = Mock(return_value=(0, 0))
    for name in ("_check_disk_space", "_check_config_drift", "_check_system_health", "_check_automatic_backup"):
        monkeypatch.setattr(w, name, Mock())
    monkeypatch.setattr(w, "_self_tune_tracker", Mock())
    monkeypatch.setattr(w, "_logging_active", True)
    log.log.side_effect = OSError("disk full")
    safety = Mock()
    monkeypatch.setattr(w, "_check_safety_limits", safety)
    try:
        w._run_poll_tick(time.monotonic())
    except Exception as exc:  # later steps of the tick (state dict) may need more stubbing; not the point here
        assert not isinstance(exc, OSError)
    safety.assert_called_once()


def test_4_a_step_that_keeps_failing_raises_a_visible_fault(env):
    boom = Mock(side_effect=RuntimeError("x"))
    for _ in range(3):
        w._guarded("unit-test step", boom)
    assert any("unit-test step" in f.description for f in w.fault_manager.active_faults)


# ---- #5 --------------------------------------------------------------------------------------
def test_5_concurrent_chunked_writes_are_serialized_not_interleaved(monkeypatch):
    bus = MachineBus("NO-HARDWARE")
    registers = {}
    first_chunk_done = threading.Event()
    release_first = threading.Event()
    lock = threading.Lock()

    def send(frame, *args, **kwargs):
        address = int.from_bytes(frame[2:4], "big")
        count = int.from_bytes(frame[4:6], "big")
        with lock:
            for i in range(count):
                registers[address + i] = int.from_bytes(frame[7 + 2 * i:9 + 2 * i], "big")
        return WriteResult(True, 1)

    monkeypatch.setattr(bus, "_send_verified", send)

    def pause_after_first_chunk(confirmed, total, pass_num):
        if confirmed == 1:
            first_chunk_done.set()
            release_first.wait(2)

    results = {}
    t1 = threading.Thread(target=lambda: results.setdefault("a", bus.write_registers(100, [1] * 16, on_progress=pause_after_first_chunk)))
    t2 = threading.Thread(target=lambda: results.setdefault("b", bus.write_registers(100, [2] * 16)))
    t1.start()
    assert first_chunk_done.wait(2)
    assert bus.config_write_in_progress()
    t2.start()
    time.sleep(0.2)  # second sender must be waiting, not interleaving
    release_first.set()
    t1.join(3)
    t2.join(3)
    assert results["a"].ok and results["b"].ok
    values = [registers[i] for i in range(100, 116)]
    assert values in ([1] * 16, [2] * 16)  # one whole request won; never a mix


def test_5_failed_write_marks_config_incomplete_until_redone(monkeypatch):
    bus = MachineBus("NO-HARDWARE")
    monkeypatch.setattr(bus, "_send_verified", lambda *a, **k: WriteResult(False, 3, "no ack"))
    assert not bus.write_registers(100, [1, 2, 3]).ok
    assert bus.config_incomplete() == [100]
    monkeypatch.setattr(bus, "_send_verified", lambda *a, **k: WriteResult(True, 1))
    assert bus.write_registers(100, [1, 2, 3]).ok
    assert bus.config_incomplete() == []


def test_5_start_refused_while_curve_only_partly_applied(env):
    bus, _, client = env
    bus.config_incomplete.return_value = [w.ADDR_CURVE_BASE]
    r = client.post("/api/start", json={"profile": asdict(Profile(name="review"))})
    assert r.status_code == 409 and "PARTLY" in r.get_json()["detail"]


# ---- #7 --------------------------------------------------------------------------------------
def test_7_invalid_backup_is_rejected_before_anything_is_changed():
    limits = SafetyLimits()
    before = asdict(limits)
    with pytest.raises(ValueError):
        config_backup.restore_backup({"schema": config_backup.SCHEMA,
                                      "safety_limits": {"max_upper_temp_c": "disabled"}}, limits, AlertConfig())
    assert asdict(limits) == before
    assert limits.max_upper_temp_c == 320.0  # still the real number, not the string


def test_7_bad_profile_in_backup_leaves_limits_untouched():
    limits = SafetyLimits()
    before = asdict(limits)
    with pytest.raises(ValueError):
        config_backup.restore_backup({"schema": config_backup.SCHEMA,
                                      "safety_limits": {"max_upper_temp_c": 280},
                                      "profiles": [{"name": "ok"}]}, limits, AlertConfig())
    assert asdict(limits) == before


# ---- #8 --------------------------------------------------------------------------------------
def test_8_dead_temperature_feed_trips_outage_even_if_coil_traffic_continues(env, monkeypatch):
    bus, _, _ = env
    bus.get_temps_timestamp.return_value = time.monotonic() - 600  # temps long dead
    bus.seconds_since_update.return_value = 0                      # ...but coils keep arriving
    monkeypatch.setattr(w, "_run_start_time", time.monotonic())
    w._check_safety_limits(reading(running=True))
    assert any("no fresh reading" in f.description.lower() for f in w.fault_manager.active_faults)


# ---- #9 --------------------------------------------------------------------------------------
def test_9_unseen_or_stale_status_coils_are_unknown_not_idle():
    fresh = reading()
    assert derive_state(fresh, True, seconds_since_temps_update=1, seconds_since_coils_update=float("inf")) == MachineState.UNKNOWN
    assert derive_state(fresh, True, seconds_since_temps_update=1, seconds_since_coils_update=3600) == MachineState.UNKNOWN
    assert derive_state(fresh, True, seconds_since_temps_update=1, seconds_since_coils_update=1) == MachineState.IDLE


def test_9_bus_tracks_coil_freshness_separately():
    bus = MachineBus("NO-HARDWARE")
    assert bus.coil_snapshot_age() == float("inf")
    bus._handle_coil_response if hasattr(bus, "_handle_coil_response") else None
    bus._last_coils_update_time = time.monotonic()
    assert bus.coil_snapshot_age() < 5


# ---- #10 -------------------------------------------------------------------------------------
def test_10_stop_does_not_call_the_ai_inline(env, monkeypatch):
    bus, log, client = env
    bus.get_live_reading.return_value = reading(running=True)
    monkeypatch.setattr(w, "_logging_active", True)
    monkeypatch.setattr(w, "_finalize_current_run", Mock(return_value={"x": 1}))
    ai_called_on = []
    started = threading.Event()
    main = threading.current_thread()

    def fake_ai(*a, **k):
        ai_called_on.append(threading.current_thread())
        started.set()
        return None

    monkeypatch.setattr(w.run_summary_ai, "generate_run_summary_text", fake_ai)
    monkeypatch.setattr(w.run_summary_ai, "format_run_summary_notification", lambda *a, **k: "msg")
    assert client.post("/api/stop", json={}).get_json()["ok"]
    assert started.wait(2)
    assert ai_called_on[0] is not main


# ---- #11 -------------------------------------------------------------------------------------
def test_11_finalize_is_idempotent_per_run(env, monkeypatch, tmp_path):
    path = tmp_path / "run.csv"
    path.write_text("timestamp,elapsed_s,upper_temp,lower_temp,infrared_temp,probe_temp,running,vacuum,cooling,profile_name\n")
    monkeypatch.setattr(w, "_current_log_path", path)
    monkeypatch.setattr(w, "_run_tracker", Mock())
    monkeypatch.setattr(w, "_run_process_limits", None)
    calls = []
    monkeypatch.setattr(w, "finalize_run", lambda *a, **k: calls.append(1) or {"ok": 1})
    monkeypatch.setattr(w.run_recovery, "mark_run_ended", Mock())
    results = []
    threads = [threading.Thread(target=lambda: results.append(w._finalize_current_run())) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(3) for t in threads]
    assert len(calls) == 1
    assert sum(r is not None for r in results) == 1


def test_11_logger_survives_concurrent_log_and_stop(tmp_path, monkeypatch):
    import data_logger
    monkeypatch.setattr(data_logger, "LOGS_DIR", tmp_path)
    lg = data_logger.RunLogger()
    lg.start_run("p")
    errors = []

    def spam():
        try:
            for _ in range(300):
                lg.log(reading())
        except Exception as exc:  # noqa
            errors.append(exc)

    t = threading.Thread(target=spam)
    t.start()
    time.sleep(0.01)
    lg.stop_run()
    t.join(5)
    assert errors == []


# ---- #12 -------------------------------------------------------------------------------------
def test_12_desktop_error_callback_does_not_raise_nameerror(monkeypatch):
    main_window = pytest.importorskip("main_window")
    event = threading.Event()
    callbacks = []
    fake_window = Mock()

    def emit(callback, result):
        callbacks.append(callback)
        event.set()

    fake_window._write_done.emit.side_effect = emit
    monkeypatch.setattr(main_window.QMessageBox, "critical", Mock())
    on_done = Mock()

    def fail():
        raise OSError("adapter unplugged")

    main_window.MainWindow._run_write(fake_window, fail, on_done)
    assert event.wait(2)
    for thread in threading.enumerate():
        if thread.name.endswith("(worker)") or thread is not threading.current_thread():
            thread.join(0.2)
    callbacks[0](None)  # used to raise NameError
    main_window.QMessageBox.critical.assert_called_once()
    on_done.assert_called_once()
    assert on_done.call_args.args[0].ok is False


# ---- #14 -------------------------------------------------------------------------------------
def test_14_atomic_write_replaces_whole_file_and_leaves_no_temp(tmp_path):
    from atomic_io import atomic_write_text
    target = tmp_path / "p.json"
    target.write_text("old")
    atomic_write_text(target, "new")
    assert target.read_text() == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["p.json"]


def test_14_failed_atomic_write_keeps_the_previous_file(tmp_path, monkeypatch):
    import atomic_io
    target = tmp_path / "p.json"
    target.write_text("old")
    monkeypatch.setattr(atomic_io.os, "replace", Mock(side_effect=OSError("boom")))
    with pytest.raises(OSError):
        atomic_io.atomic_write_text(target, "new")
    assert target.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["p.json"]


def test_14_corrupt_profile_file_is_quarantined_not_silently_forgotten(tmp_path, monkeypatch):
    import profile_store
    monkeypatch.setattr(profile_store, "PROFILES_DIR", tmp_path)
    bad = tmp_path / "x.json"
    bad.write_text("{not json")
    assert profile_store._read_json(bad) == {}
    assert any(".corrupt-" in p.name for p in tmp_path.iterdir())


# ---- #15 -------------------------------------------------------------------------------------
def test_15_generic_quote_with_invented_numbers_is_not_marked_verified(monkeypatch):
    monkeypatch.setattr(profile_search, "_call_ollama", lambda *a: ({
        "found": True, "profile_name": "review", "machine_match": True,
        "sections": [{"upper_temp": 300}], "source_quotes": ["BGA repair guide"]}, ""))
    candidate, warnings = profile_search.extract_profile_from_page(
        "review", "BGA repair guide. No temperatures specified.", profile_search.OllamaConfig())
    assert not candidate.quotes_verified
    assert any("300" in w_ for w_ in warnings)
    assert candidate.machine_match is False  # the page never names this machine


def test_15_numbers_present_in_the_page_stay_verified(monkeypatch):
    page = "For the LY-R690, run the top heater at 240 C. BGA repair guide."
    monkeypatch.setattr(profile_search, "_call_ollama", lambda *a: ({
        "found": True, "profile_name": "review", "machine_match": True,
        "sections": [{"upper_temp": 240}], "source_quotes": ["BGA repair guide"]}, ""))
    candidate, _ = profile_search.extract_profile_from_page("review", page, profile_search.OllamaConfig())
    assert candidate.quotes_verified and candidate.machine_match


# ---- #17 -------------------------------------------------------------------------------------
def test_17_bus_with_a_failed_reader_reports_disconnected_and_fails_fast():
    bus = MachineBus("NO-HARDWARE")
    bus._serial = Mock(is_open=True)
    assert bus.is_connected
    bus._mark_reader_failed("passive reader", OSError("unplugged"))
    assert not bus.is_connected
    assert "unplugged" in bus.reader_failure
    t0 = time.monotonic()
    result = bus.write_coil(COIL_COOLING, True)
    assert not result.ok and result.attempts == 0 and time.monotonic() - t0 < 10
