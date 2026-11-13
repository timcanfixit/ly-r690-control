"""Coverage for /api/fan-timing/read and /api/temp-comp/read - 2026-09-09.

Regression guard for a real, confirmed-live finding: a user report traced "No response from the
board after repeated attempts" on these two routes to the touchscreen's Advanced Parameters screen
never having been opened since the machine's controller was last powered on - a real PLC-side
behavior (opening it once is enough; it does not need to stay open), not a bug in this app's read
logic. The previous generic message ("if the bus is very busy, try again") actively misled the
operator into retrying instead of doing the one thing that actually helps. See
web_server.FAN_TIMING_TEMP_COMP_NO_RESPONSE_MESSAGE and machine_bus.read_fan_timing_active's
docstring for the full finding.
"""
import web_server


class StubBus:
    def __init__(self, fan_timing_result=None, temp_comp_result=None):
        self._fan_timing_result = fan_timing_result
        self._temp_comp_result = temp_comp_result

    def read_fan_timing_active(self, on_progress=None):
        return self._fan_timing_result

    def read_temp_comp_active(self):
        return self._temp_comp_result


def test_fan_timing_read_success():
    web_server.bus = StubBus(fan_timing_result=[120, 2000, 5, 10])
    client = web_server.app.test_client()
    resp = client.post("/api/fan-timing/read")
    assert resp.status_code == 200
    assert resp.get_json()["vacuum_time"] == 120


def test_fan_timing_read_no_response_gives_the_real_cause_not_a_generic_retry_hint():
    web_server.bus = StubBus(fan_timing_result=None)
    client = web_server.app.test_client()
    resp = client.post("/api/fan-timing/read")
    assert resp.status_code == 404
    error = resp.get_json()["error"]
    assert "Advanced Parameters screen" in error
    assert "power" in error.lower()
    # regression guard: must not silently revert to the old misleading generic wording
    assert "bus is very busy" not in error


def test_temp_comp_read_success():
    web_server.bus = StubBus(temp_comp_result=[33, 77, 0, -1 & 0xFFFF])
    client = web_server.app.test_client()
    resp = client.post("/api/temp-comp/read")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["upper"] == 3.3
    assert data["lower"] == 7.7


def test_temp_comp_read_no_response_gives_the_real_cause_not_a_generic_retry_hint():
    web_server.bus = StubBus(temp_comp_result=None)
    client = web_server.app.test_client()
    resp = client.post("/api/temp-comp/read")
    assert resp.status_code == 404
    error = resp.get_json()["error"]
    assert "Advanced Parameters screen" in error
    assert "power" in error.lower()
    assert "bus is very busy" not in error


def test_fan_timing_and_temp_comp_share_the_exact_same_message():
    """Both routes are the same underlying 10-register active read (see
    machine_bus.read_fan_timing_active's docstring) - their failure message must stay identical,
    not drift into two different explanations of the same board behavior."""
    web_server.bus = StubBus(fan_timing_result=None, temp_comp_result=None)
    client = web_server.app.test_client()
    fan_error = client.post("/api/fan-timing/read").get_json()["error"]
    temp_comp_error = client.post("/api/temp-comp/read").get_json()["error"]
    assert fan_error == temp_comp_error
