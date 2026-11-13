"""Regression coverage for the on_progress callback added to active reads (2026-08-22) - without
it, a slow read under real bus contention (confirmed live: PID/fan-timing reads sometimes taking
tens of seconds) looked identical to a hung app, with the "Reading..." button text never changing.
Never opens a real serial port."""
from machine_bus import MachineBus, _modbus_crc16
from modbus_client import PID_COUNT, STATION_ADDRESS


class _AlwaysReadyEvent:
    """Stand-in for the pause/resume events - the real background reader thread continuously
    re-acks every pause request; these tests have no such thread running, so fake it."""
    def set(self): pass
    def clear(self): pass
    def wait(self, timeout=None): return True


class _FlakySerial:
    """Returns no response for the first `fail_count` real reads, then a valid one."""
    def __init__(self, fail_count, good_resp):
        self.fail_count = fail_count
        self.calls = 0
        self.good_resp = good_resp
        self.timeout = 0.02

    def read(self, n):
        if n == 256:
            return b""  # quiet gap always available
        self.calls += 1
        return b"" if self.calls <= self.fail_count else self.good_resp

    def write(self, data):
        pass

    def flush(self):
        pass


def _fake_bus_with_flaky_pid_response(fail_count):
    bus = MachineBus("COM_TEST_ONLY")
    bus._paused_ack = _AlwaysReadyEvent()
    bus._pause_event = _AlwaysReadyEvent()
    bus._resume_event = _AlwaysReadyEvent()

    regs = [0] * PID_COUNT
    byte_count = PID_COUNT * 2
    header = bytes([STATION_ADDRESS, 3, byte_count])
    data = b"".join(v.to_bytes(2, "big") for v in regs)
    frame_body = header + data
    crc = _modbus_crc16(frame_body)
    resp = frame_body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    bus._serial = _FlakySerial(fail_count=fail_count, good_resp=resp)
    return bus


def test_on_progress_fires_once_per_attempt_including_success():
    bus = _fake_bus_with_flaky_pid_response(fail_count=3)
    progress_log = []
    values = bus.read_pid_active(on_progress=lambda a, m: progress_log.append((a, m)))
    assert values is not None
    assert progress_log == [(1, 20), (2, 20), (3, 20), (4, 20)]


def test_on_progress_fires_through_full_budget_on_total_failure():
    bus = _fake_bus_with_flaky_pid_response(fail_count=999)  # never succeeds
    progress_log = []
    values = bus.read_pid_active(on_progress=lambda a, m: progress_log.append((a, m)))
    assert values is None
    assert len(progress_log) == 20
    assert progress_log[-1] == (20, 20)


def test_on_progress_is_optional_and_read_still_works_without_it():
    bus = _fake_bus_with_flaky_pid_response(fail_count=0)
    values = bus.read_pid_active()  # no on_progress passed at all
    assert values is not None
