"""Stage 14 diagnostics counters - these tests never open a real serial port. The bookkeeping
tests monkeypatch the low-level retry primitive directly; the end-to-end test drives the real
write_coil() call path against a fake in-memory serial object."""
from machine_bus import MachineBus, _build_frame


def test_diagnostics_bookkeeping_counts_reads_and_writes_separately():
    bus = MachineBus("COM_TEST_ONLY")  # __init__ only - no serial port opened

    bus._transmit_and_retry_locked = lambda frame, validate, desc, max_attempts, on_progress=None: ([1, 2, 3], 1)
    bus._transmit_and_retry(b"", lambda r: None, "read A", 20, kind="read")
    bus._transmit_and_retry_locked = lambda frame, validate, desc, max_attempts, on_progress=None: ([4, 5], 2)
    bus._transmit_and_retry(b"", lambda r: None, "read B", 20, kind="read")
    bus._transmit_and_retry_locked = lambda frame, validate, desc, max_attempts, on_progress=None: (None, 20)
    bus._transmit_and_retry(b"", lambda r: None, "read C (fails)", 20, kind="read")
    bus._transmit_and_retry_locked = lambda frame, validate, desc, max_attempts, on_progress=None: (True, 3)
    bus._transmit_and_retry(b"", lambda r: None, "write A", 20, kind="write")

    diag = bus.get_diagnostics()
    assert diag["read_attempts"] == 3
    assert diag["read_successes"] == 2
    assert diag["read_failures"] == 1
    assert diag["write_attempts"] == 1
    assert diag["write_successes"] == 1
    # retries = (1-1) + (2-1) + (20-1) + (3-1) = 0+1+19+2 = 22
    assert diag["retry_count"] == 22


def test_response_time_average_and_reset():
    bus = MachineBus("COM_TEST_ONLY")
    bus._record_response_time(0.02)
    bus._record_response_time(0.04)
    diag = bus.get_diagnostics()
    assert abs(diag["avg_response_time_s"] - 0.03) < 1e-9
    assert diag["last_response_time_s"] == 0.04
    assert diag["response_time_sample_count"] == 2

    bus.reset_diagnostics()
    diag2 = bus.get_diagnostics()
    assert diag2["read_attempts"] == 0
    assert diag2["avg_response_time_s"] is None


class _FakeSerial:
    """Mimics just enough of pyserial's interface for _transmit_and_retry_locked: read(256) is
    the quiet-gap poll (empty = line is quiet), read(300) is the real response read."""

    def __init__(self, ack_frame: bytes):
        self.ack_frame = ack_frame
        self.timeout = 0.02

    def read(self, n):
        return b"" if n == 256 else self.ack_frame

    def write(self, data):
        pass

    def flush(self):
        pass


def test_real_write_coil_path_drives_diagnostics_end_to_end():
    addr = 100
    pdu = bytes([5, (addr >> 8) & 0xFF, addr & 0xFF, 0xFF, 0x00])
    frame = _build_frame(pdu)  # FC5's success response is a verbatim echo of the request

    bus = MachineBus("COM_TEST_ONLY")
    bus._serial = _FakeSerial(ack_frame=frame)
    bus._paused_ack.set()  # simulate the (absent) background reader thread having already paused

    result = bus.write_coil(addr, True, max_passes=1, attempts_per_pass=3)
    assert result.ok is True

    diag = bus.get_diagnostics()
    assert diag["write_attempts"] == 1
    assert diag["write_successes"] == 1
    assert diag["response_time_sample_count"] == 1
