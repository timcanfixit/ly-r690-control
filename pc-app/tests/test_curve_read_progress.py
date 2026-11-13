"""Regression coverage for read_curve_active()'s on_progress callback (2026-08-22) - this read
tries up to ~100 attempts across 3 fallback strategies before giving up (this address range has
never once succeeded on the real board over any method tried), so phase labels matter here even
more than for the simpler PID/fan-timing reads. Never opens a real serial port."""
from machine_bus import MachineBus, _modbus_crc16, _build_read_frame
from modbus_client import ADDR_CURVE_BASE, CURVE_SECTIONS, CURVE_ATTRIBUTES, STATION_ADDRESS


class _AlwaysReadyEvent:
    def set(self): pass
    def clear(self): pass
    def wait(self, timeout=None): return True


def _fc_response(func_code, values):
    byte_count = len(values) * 2
    header = bytes([STATION_ADDRESS, func_code, byte_count])
    data = b"".join(v.to_bytes(2, "big") for v in values)
    frame = header + data
    crc = _modbus_crc16(frame)
    return frame + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


class _ScriptedSerial:
    """Responds based on which exact request frame was just written - lets a test drive each of
    read_curve_active's 3 strategies (full qty=82, per-row FC3, per-row FC4) independently."""

    def __init__(self, responses_by_frame):
        self.responses_by_frame = responses_by_frame  # {frame_bytes: response_bytes or None}
        self.last_write = None
        self.timeout = 0.02

    def write(self, data):
        self.last_write = data

    def flush(self):
        pass

    def read(self, n):
        if n == 256:
            return b""  # quiet gap always available
        resp = self.responses_by_frame.get(self.last_write)
        return resp if resp else b""


def _patch_no_thread(bus):
    bus._paused_ack = _AlwaysReadyEvent()
    bus._pause_event = _AlwaysReadyEvent()
    bus._resume_event = _AlwaysReadyEvent()


def test_progress_reports_full_read_phase_then_per_row_fc3_phase():
    bus = MachineBus("COM_TEST_ONLY")
    _patch_no_thread(bus)

    full_read_frame = _build_read_frame(3, ADDR_CURVE_BASE, 82)
    responses = {full_read_frame: None}  # phase 1 never responds
    for row in range(len(CURVE_ATTRIBUTES)):
        addr = ADDR_CURVE_BASE + row * CURVE_SECTIONS
        row_frame = _build_read_frame(3, addr, CURVE_SECTIONS)
        # plausible curve values (small ints) so _curve_regs_look_plausible accepts them
        responses[row_frame] = _fc_response(3, [2] * CURVE_SECTIONS)

    bus._serial = _ScriptedSerial(responses)

    progress_log = []
    profile = bus.read_curve_active(on_progress=lambda phase, a, m: progress_log.append((phase, a, m)))

    assert profile is not None
    phases_seen = {p for p, a, m in progress_log}
    assert "Full read (qty=82)" in phases_seen
    assert "Row 1/9 (FC3)" in phases_seen
    assert not any("FC4" in p for p in phases_seen)  # FC3 succeeded every row - no fallback needed


def test_progress_falls_back_to_fc4_phase_when_fc3_fails_for_a_row():
    bus = MachineBus("COM_TEST_ONLY")
    _patch_no_thread(bus)

    full_read_frame = _build_read_frame(3, ADDR_CURVE_BASE, 82)
    responses = {full_read_frame: None}
    for row in range(len(CURVE_ATTRIBUTES)):
        addr = ADDR_CURVE_BASE + row * CURVE_SECTIONS
        fc3_frame = _build_read_frame(3, addr, CURVE_SECTIONS)
        fc4_frame = _build_read_frame(4, addr, CURVE_SECTIONS)
        if row == 0:
            responses[fc3_frame] = None  # row 0's FC3 never responds
            responses[fc4_frame] = _fc_response(4, [2] * CURVE_SECTIONS)  # FC4 fallback succeeds
        else:
            responses[fc3_frame] = _fc_response(3, [2] * CURVE_SECTIONS)

    bus._serial = _ScriptedSerial(responses)

    progress_log = []
    profile = bus.read_curve_active(on_progress=lambda phase, a, m: progress_log.append((phase, a, m)))

    assert profile is not None
    phases_seen = {p for p, a, m in progress_log}
    assert "Row 1/9 (FC3)" in phases_seen
    assert "Row 1/9 (FC4 fallback)" in phases_seen
