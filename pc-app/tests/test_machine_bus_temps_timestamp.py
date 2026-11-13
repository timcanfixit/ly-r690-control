"""Tests for MachineBus.get_temps_timestamp() - deliberately distinct from seconds_since_update(),
which reflects ANY bus activity (coil reads/writes too). See its docstring and
safety_limits.RampTracker for the real false-positive auto-stop this was built to fix (reported
live 2026-09: "Lower changed 1641C/min" while actual temps were not ramping anywhere near that
fast). Never opens a real serial port."""
from machine_bus import MachineBus, _modbus_crc16
from modbus_client import STATION_ADDRESS


def test_temps_timestamp_is_zero_before_any_temps_read():
    bus = MachineBus("COM_TEST_ONLY")
    assert bus.get_temps_timestamp() == 0.0


def test_temps_timestamp_updates_after_active_temps_read():
    bus = MachineBus("COM_TEST_ONLY")
    bus._send_active_read = lambda func_code, addr, count, desc, max_attempts=20, on_progress=None: [200, 210, 220, 230, 0, 0, 0, 0]
    bus.read_temps_active()
    assert bus.get_temps_timestamp() > 0.0


def test_temps_timestamp_unaffected_by_a_coil_read():
    """The core of the bug this fixes: a coil read/write refreshes _last_update_time (used by
    seconds_since_update(), the comms-outage check) but must NOT be mistaken for a fresh
    temperature sample - coils and temps are read independently."""
    bus = MachineBus("COM_TEST_ONLY")
    bus._send_active_read = lambda func_code, addr, count, desc, max_attempts=20, on_progress=None: [200, 210, 220, 230, 0, 0, 0, 0]
    bus.read_temps_active()
    temps_ts_before = bus.get_temps_timestamp()

    # A coil read refreshes _last_update_time (generic bus activity)...
    bus._send_active_read = lambda func_code, addr, count, desc, max_attempts=20, on_progress=None: [False] * 29
    bus.read_coils_active()

    assert bus.get_temps_timestamp() == temps_ts_before, "a coil read must not touch the temps-specific timestamp"
    assert bus.seconds_since_update() < 1.0, "but generic activity tracking should still see the coil read as recent activity"


def test_temps_timestamp_updates_via_passive_listener_status_poll():
    """The passive listener parses overheard Modbus responses directly (not through
    read_temps_active()) - confirms that path also updates the temps-specific timestamp."""
    bus = MachineBus("COM_TEST_ONLY")
    assert bus.get_temps_timestamp() == 0.0

    # A real FC3 response for an 8-register status-screen poll (registers 20-27): byte_count=16.
    regs = [2000, 2100, 2200, 2300, 0, 0, 0, 0]  # x10 fixed-point -> 200.0, 210.0, 220.0, 230.0
    payload = bytearray()
    for r in regs:
        payload += r.to_bytes(2, "big")
    header = bytes([STATION_ADDRESS, 3, 16]) + bytes(payload)
    crc = _modbus_crc16(header)
    frame = header + bytes([crc & 0xFF, (crc >> 8) & 0xFF])

    bus._handle_holding_register_response(frame, byte_count=16)
    assert bus.get_temps_timestamp() > 0.0
