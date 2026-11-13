"""POST /api/admin/restart - 2026-11-12. Restarts the service without sudo by exiting nonzero so
systemd's Restart=on-failure brings it back. The guards matter more than the mechanism: a restart
discards in-memory safety state and live-run logging, so it must be refused during a run, cooldown,
self-tune, fault, an in-flight write, or when nothing would restart the process afterward.
The process is never actually ended in these tests - _restart_process_soon is replaced.
"""
import threading

import pytest

import web_server
from machine_state import MachineState


class StubBus:
    is_connected = True

    def get_live_reading(self):
        return None


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web_server, "bus", StubBus(), raising=False)
    monkeypatch.setenv("INVOCATION_ID", "test-invocation")  # as systemd sets for a service it launched
    monkeypatch.setattr(web_server, "_logging_active", False)
    monkeypatch.setattr(web_server, "_active_write_progress", None)
    monkeypatch.setattr(web_server, "_current_machine_state", lambda reading: MachineState.IDLE)
    restarts = []
    monkeypatch.setattr(web_server, "_restart_process_soon", lambda: restarts.append(1))
    c = web_server.app.test_client()
    c.restarts = restarts
    return c


def _post(client, body=None):
    return client.post("/api/admin/restart", json={"confirm": True} if body is None else body)


def test_restarts_when_idle_under_systemd(client):
    resp = _post(client)
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert client.restarts == [1]


@pytest.mark.parametrize("state", [MachineState.DISCONNECTED, MachineState.UNKNOWN])
def test_allowed_when_machine_state_cannot_be_read(client, monkeypatch, state):
    """Recovering a stuck connection is a main reason to restart, so these must be allowed."""
    monkeypatch.setattr(web_server, "_current_machine_state", lambda reading: state)
    assert _post(client).status_code == 200
    assert client.restarts == [1]


@pytest.mark.parametrize("state", [
    MachineState.RUNNING, MachineState.PAUSED, MachineState.COOLING, MachineState.PREFLIGHT,
    MachineState.SELF_TUNING, MachineState.FAULT, MachineState.CONNECTING,
])
def test_refused_in_every_state_where_something_is_physically_happening(client, monkeypatch, state):
    monkeypatch.setattr(web_server, "_current_machine_state", lambda reading: state)
    resp = _post(client)
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["ok"] is False
    assert state.value in body["detail"]
    assert client.restarts == []


def test_refused_without_explicit_confirmation(client):
    for body in ({}, {"confirm": False}, {"confirm": 0}):
        resp = client.post("/api/admin/restart", json=body)
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False
    assert client.post("/api/admin/restart").status_code == 400  # no body at all
    assert client.restarts == []


def test_refused_when_not_running_under_systemd(client, monkeypatch):
    """Started by hand there is no supervisor to bring the process back, so exiting would just kill
    the app."""
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    resp = _post(client)
    assert resp.status_code == 501
    assert "systemd" in resp.get_json()["detail"]
    assert client.restarts == []


def test_refused_while_a_run_is_being_logged_even_if_state_looks_idle(client, monkeypatch):
    monkeypatch.setattr(web_server, "_logging_active", True)
    resp = _post(client)
    assert resp.status_code == 409
    assert "logged" in resp.get_json()["detail"]
    assert client.restarts == []


def test_refused_while_a_machine_write_is_in_flight(client, monkeypatch):
    monkeypatch.setattr(web_server, "_active_write_progress", {"label": "Starting", "attempt": 1})
    assert _post(client).status_code == 409
    assert client.restarts == []


def test_refused_while_a_start_or_stop_holds_the_run_control_lock(client):
    assert web_server._run_control_lock.acquire(blocking=False)
    try:
        resp = _post(client)
    finally:
        web_server._run_control_lock.release()
    assert resp.status_code == 409
    assert client.restarts == []


def test_the_exit_status_is_nonzero_or_systemd_would_not_restart_the_service():
    """Restart=on-failure only restarts on a NONZERO exit - a clean exit(0) would just stop the
    service for good."""
    assert web_server._RESTART_EXIT_CODE != 0


def test_only_states_with_nothing_physically_happening_are_restartable():
    assert web_server._RESTARTABLE_STATES == {
        MachineState.IDLE, MachineState.DISCONNECTED, MachineState.UNKNOWN,
    }


def test_restart_process_soon_closes_the_bus_then_exits_nonzero(monkeypatch):
    """The real helper, with the process-ending call replaced: the serial port must be closed
    first (so the next instance can open it) and the exit status must be the nonzero one."""
    calls = []

    class Bus:
        def stop(self):
            calls.append("bus.stop")

    monkeypatch.setattr(web_server, "bus", Bus(), raising=False)
    monkeypatch.setattr(web_server.time, "sleep", lambda s: None)
    done = threading.Event()

    def fake_exit(code):
        calls.append(("exit", code))
        done.set()

    monkeypatch.setattr(web_server.os, "_exit", fake_exit)
    web_server._restart_process_soon()
    assert done.wait(timeout=5)
    assert calls == ["bus.stop", ("exit", web_server._RESTART_EXIT_CODE)]


def test_restart_process_soon_still_exits_if_closing_the_bus_fails(monkeypatch):
    class Bus:
        def stop(self):
            raise OSError("port already gone")

    monkeypatch.setattr(web_server, "bus", Bus(), raising=False)
    monkeypatch.setattr(web_server.time, "sleep", lambda s: None)
    done = threading.Event()
    codes = []
    monkeypatch.setattr(web_server.os, "_exit", lambda code: (codes.append(code), done.set()))
    web_server._restart_process_soon()
    assert done.wait(timeout=5)
    assert codes == [web_server._RESTART_EXIT_CODE]
