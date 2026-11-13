"""Pre-Start reminder: /api/preflight reports whether the editor's curve has actually been sent
to the machine from this app, so the frontend can prompt before Start. 2026-09-10.

The machine runs whatever curve is on its control board and this app can't reliably read that
back (docs/06-modbus-register-map.md), so this is provenance only - "did /api/curve/send succeed
for this exact curve this process" - never a claim about the board's real state.
"""
import web_server
from machine_bus import WriteResult
from modbus_client import LiveReading


class StubBus:
    is_connected = True

    def __init__(self, send_ok=True):
        self._send_ok = send_ok

    def get_live_reading(self):
        return LiveReading(
            upper_temp=25.0, lower_temp=25.0, infrared_temp=25.0, probe_temp=25.0,
            running=False, running_mirror=False, vacuum=False, cooling=False, keep=False,
        )

    def get_diagnostics(self):
        return {}

    def write_registers(self, start, values, on_progress=None):
        return WriteResult(ok=self._send_ok, attempts=1, detail="stub", independently_verified=self._send_ok)

    def read_curve_active(self):
        return None

    def get_temps_timestamp(self):
        import time
        return time.monotonic()

    def coil_snapshot_age(self):
        return 0.0

    def config_write_in_progress(self):
        return False

    def config_incomplete(self):
        return []


def _profile_json(upper_temp=200, name="Test Profile"):
    return {"name": name, "sections": [{"upper_temp": upper_temp} for _ in range(8)]}


def _reset():
    web_server._last_sent_curve_regs = None
    # sends are now refused while a fault is unacknowledged; other tests' faults must not leak in
    from fault_manager import FaultManager
    web_server.fault_manager = FaultManager()
    web_server._preflight_active = False
    web_server._restart_pending = False
    web_server._inflight_ops = 0


def test_preflight_reports_unsent_before_any_send(monkeypatch):
    _reset()
    web_server.bus = StubBus()
    client = web_server.app.test_client()
    resp = client.post("/api/preflight", json={"profile": _profile_json()})
    assert resp.status_code == 200
    assert resp.get_json()["curve_send_status"] == "unsent"


def test_preflight_reports_empty_for_all_zero_curve():
    _reset()
    web_server.bus = StubBus()
    client = web_server.app.test_client()
    resp = client.post("/api/preflight", json={"profile": _profile_json(upper_temp=0)})
    assert resp.get_json()["curve_send_status"] == "empty"


def test_send_curve_then_preflight_same_profile_reports_sent():
    _reset()
    web_server.bus = StubBus(send_ok=True)
    client = web_server.app.test_client()
    client.post("/api/curve/send", json=_profile_json())
    resp = client.post("/api/preflight", json={"profile": _profile_json()})
    assert resp.get_json()["curve_send_status"] == "sent"


def test_send_curve_then_preflight_different_profile_reports_unsent():
    _reset()
    web_server.bus = StubBus(send_ok=True)
    client = web_server.app.test_client()
    client.post("/api/curve/send", json=_profile_json(upper_temp=200))
    resp = client.post("/api/preflight", json={"profile": _profile_json(upper_temp=210)})
    assert resp.get_json()["curve_send_status"] == "unsent"


def test_failed_send_does_not_mark_curve_as_sent():
    _reset()
    web_server.bus = StubBus(send_ok=False)
    client = web_server.app.test_client()
    client.post("/api/curve/send", json=_profile_json())
    resp = client.post("/api/preflight", json={"profile": _profile_json()})
    assert resp.get_json()["curve_send_status"] == "unsent"
