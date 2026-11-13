"""Regression coverage for two related fixes to write_coil() (2026-08-22):

1. on_progress feedback across passes - a heavily-contended coil write can legitimately take
   20-40+ seconds across multiple retry passes, which looked identical to a hang with nothing but
   a static "Sending..." the whole time.
2. Active read-back fallback when no ACK is ever seen - confirmed live that a coil write can
   actually take effect on the physical machine while the ACK itself is lost to a collision on the
   return trip, which used to be reported as a flat failure even though the write worked. An
   independent read-back after the ACK budget is exhausted catches this instead of a false
   failure.

Never opens a real serial port."""
from machine_bus import MachineBus, _build_frame, _modbus_crc16, _COIL_READBACK_MAX_ATTEMPTS
from modbus_client import COIL_RUNNING, COIL_VACUUM, STATION_ADDRESS


class _AlwaysReadyEvent:
    def set(self): pass
    def clear(self): pass
    def wait(self, timeout=None): return True


def _coil_write_frame(address, value):
    pdu = bytes([5, (address >> 8) & 0xFF, address & 0xFF, 0xFF if value else 0x00, 0x00])
    return _build_frame(pdu)


def _coil_read_response(address, value, base=COIL_RUNNING, qty=29):
    """Builds a valid FC1 response for a `qty`-coil read starting at `base`, with only
    `address`'s bit set according to `value`."""
    byte_count = (qty + 7) // 8
    bits = [False] * (byte_count * 8)
    bits[address - base] = value
    data = bytearray(byte_count)
    for i, b in enumerate(bits):
        if b:
            data[i // 8] |= 1 << (i % 8)
    header = bytes([STATION_ADDRESS, 1, byte_count]) + bytes(data)
    crc = _modbus_crc16(header)
    return header + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


class _NeverAcksButReadbackMatchesSerial:
    """Never returns a valid ACK for the coil WRITE frame (simulating a lost ACK), but DOES
    answer a coil READ request correctly, showing the coil already at the requested value - the
    scenario where the write actually worked but its own ACK never made it back."""

    def __init__(self, write_frame, read_response):
        self.write_frame = write_frame
        self.read_response = read_response
        self.last_write = None
        self.timeout = 0.02

    def write(self, data):
        self.last_write = data

    def flush(self):
        pass

    def read(self, n):
        if n == 256:
            return b""  # quiet gap always available
        if self.last_write == self.write_frame:
            return b""  # the coil write's own ACK is always "lost"
        return self.read_response  # any other request (the read-back) succeeds


def _patch_bus_for_no_thread(bus):
    bus._paused_ack = _AlwaysReadyEvent()
    bus._pause_event = _AlwaysReadyEvent()
    bus._resume_event = _AlwaysReadyEvent()


def test_on_progress_fires_across_multiple_passes_with_pass_number():
    bus = MachineBus("COM_TEST_ONLY")
    _patch_bus_for_no_thread(bus)
    write_frame = _coil_write_frame(COIL_VACUUM, True)
    bus._serial = _NeverAcksButReadbackMatchesSerial(write_frame, b"")  # readback also fails here

    progress_log = []
    result = bus.write_coil(COIL_VACUUM, True, max_passes=2, attempts_per_pass=3,
                             on_progress=lambda a, m, p: progress_log.append((a, m, p)))

    assert result.ok is False  # readback returned garbage too, so this stays a real failure
    # 3 attempts in pass 1, 3 in pass 2 = 6 progress calls total
    assert len(progress_log) == 6
    assert progress_log[0] == (1, 3, 1)
    assert progress_log[-1] == (3, 3, 2)


def test_lost_ack_but_readback_confirms_the_write_actually_worked():
    bus = MachineBus("COM_TEST_ONLY")
    _patch_bus_for_no_thread(bus)
    write_frame = _coil_write_frame(COIL_VACUUM, True)
    read_resp = _coil_read_response(COIL_VACUUM, True)
    bus._serial = _NeverAcksButReadbackMatchesSerial(write_frame, read_resp)

    result = bus.write_coil(COIL_VACUUM, True, max_passes=1, attempts_per_pass=2)

    assert result.ok is True
    assert result.independently_verified is True
    assert "read-back confirms" in result.detail
    # the local cache should reflect the confirmed real state
    assert bus._latest_coils.get(COIL_VACUUM) is True


def test_readback_short_circuits_after_first_failed_pass_not_after_all_passes():
    """Regression test for a real reported latency bug (2026-08-22): the original fix only tried
    read-back once, after ALL passes were exhausted - meaning a coil that actually toggled
    correctly on pass 1 (just with its ACK lost) still had to sit through 2 more full retry
    passes before anything confirmed it, making an already-successful toggle look like it took
    20-40+ seconds to "notice". Read-back must be tried right after the FIRST failed pass, and
    must short-circuit out - pass 2 should never even start."""
    bus = MachineBus("COM_TEST_ONLY")
    _patch_bus_for_no_thread(bus)
    write_frame = _coil_write_frame(COIL_VACUUM, True)
    read_resp = _coil_read_response(COIL_VACUUM, True)  # readback will confirm success
    serial = _NeverAcksButReadbackMatchesSerial(write_frame, read_resp)
    bus._serial = serial

    progress_log = []
    result = bus.write_coil(COIL_VACUUM, True, max_passes=3, attempts_per_pass=5,
                             on_progress=lambda a, m, p: progress_log.append((a, m, p)))

    assert result.ok is True
    assert result.independently_verified is True
    # only pass 1's attempts (5) should have run - pass 2 and 3 never started
    assert result.attempts == 5
    assert all(p == 1 for _, _, p in progress_log), f"pass 2/3 should never have started: {progress_log}"


def test_between_passes_readback_uses_a_small_attempt_budget_not_the_default():
    """Regression test for a real reported latency bug (2026-08-22, a follow-up to the previous
    one): the between-passes read-back called read_coils_active() with no override, silently
    inheriting the same max_attempts=20 default used for a deliberate, patient "read coil state"
    request - meaning the gap BETWEEN passes could itself take up to ~100 seconds under
    contention, defeating the whole point of checking early. The read-back is a quick sanity
    check and must use a small, fast budget instead."""
    bus = MachineBus("COM_TEST_ONLY")
    _patch_bus_for_no_thread(bus)
    write_frame = _coil_write_frame(COIL_VACUUM, True)
    bus._serial = _NeverAcksButReadbackMatchesSerial(write_frame, b"")  # readback also never answers

    captured_max_attempts = []
    original = bus.read_coils_active

    def spy(max_attempts=20):
        captured_max_attempts.append(max_attempts)
        return original(max_attempts=max_attempts)

    bus.read_coils_active = spy
    bus.write_coil(COIL_VACUUM, True, max_passes=1, attempts_per_pass=2)

    assert captured_max_attempts, "read_coils_active was never called"
    assert all(m <= _COIL_READBACK_MAX_ATTEMPTS for m in captured_max_attempts), (
        f"between-passes read-back used too large a budget: {captured_max_attempts} "
        f"(this is what caused the 'taking too long between passes' report)"
    )


def test_lost_ack_and_readback_disagrees_stays_a_real_failure():
    bus = MachineBus("COM_TEST_ONLY")
    _patch_bus_for_no_thread(bus)
    write_frame = _coil_write_frame(COIL_VACUUM, True)
    read_resp = _coil_read_response(COIL_VACUUM, False)  # board says it's still off
    bus._serial = _NeverAcksButReadbackMatchesSerial(write_frame, read_resp)

    result = bus.write_coil(COIL_VACUUM, True, max_passes=1, attempts_per_pass=2)

    assert result.ok is False
    assert result.independently_verified is False
