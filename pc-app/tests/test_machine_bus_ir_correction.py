"""Tests for MachineBus's IR reading correction (2026-09-07) - the software-side top-up for the
part of the confirmed live-IR-vs-profile-target gap that the hardware Temperature Compensation
register can't reach on its own (confirmed live: that register hard-caps at +10C, while the
observed gap runs ~20-24C). Applied in get_live_reading() - the one place every consumer
(display, logging, alerts, safety checks) reads a live reading through - rather than patched into
each of that method's many call sites individually. Never opens a real serial port."""
from machine_bus import MachineBus


def _stub_temps(bus, upper, lower, ir, probe):
    bus._send_active_read = lambda func_code, addr, count, desc, max_attempts=20, on_progress=None: [
        int(upper * 10), int(lower * 10), int(ir * 10), int(probe * 10), 0, 0, 0, 0,
    ]
    bus.read_temps_active()


def test_ir_correction_defaults_to_zero_no_change_to_raw_reading():
    bus = MachineBus("COM_TEST_ONLY")
    _stub_temps(bus, upper=200.0, lower=210.0, ir=150.0, probe=180.0)
    reading = bus.get_live_reading()
    assert reading.infrared_temp == 150.0


def test_ir_correction_is_added_to_every_subsequent_reading():
    bus = MachineBus("COM_TEST_ONLY")
    bus.set_ir_reading_correction(13.0)
    _stub_temps(bus, upper=200.0, lower=210.0, ir=150.0, probe=180.0)
    reading = bus.get_live_reading()
    assert reading.infrared_temp == 163.0
    # Other zones must be completely unaffected - this is an IR-specific correction only.
    assert reading.upper_temp == 200.0
    assert reading.lower_temp == 210.0
    assert reading.probe_temp == 180.0


def test_ir_correction_can_be_changed_and_takes_effect_on_the_next_reading():
    bus = MachineBus("COM_TEST_ONLY")
    bus.set_ir_reading_correction(10.0)
    _stub_temps(bus, upper=200.0, lower=210.0, ir=100.0, probe=180.0)
    assert bus.get_live_reading().infrared_temp == 110.0

    bus.set_ir_reading_correction(23.0)
    _stub_temps(bus, upper=200.0, lower=210.0, ir=100.0, probe=180.0)
    assert bus.get_live_reading().infrared_temp == 123.0


def test_ir_correction_survives_across_repeated_reads_without_being_reapplied_cumulatively():
    """The correction must be applied fresh from the raw reading each time - not accumulated
    on top of an already-corrected value from a prior get_live_reading() call."""
    bus = MachineBus("COM_TEST_ONLY")
    bus.set_ir_reading_correction(13.0)
    _stub_temps(bus, upper=200.0, lower=210.0, ir=150.0, probe=180.0)
    first = bus.get_live_reading().infrared_temp
    second = bus.get_live_reading().infrared_temp
    assert first == second == 163.0


def test_ir_correction_can_be_negative():
    bus = MachineBus("COM_TEST_ONLY")
    bus.set_ir_reading_correction(-5.0)
    _stub_temps(bus, upper=200.0, lower=210.0, ir=150.0, probe=180.0)
    assert bus.get_live_reading().infrared_temp == 145.0


def test_fresh_machine_bus_always_starts_at_zero_correction():
    """A new MachineBus (e.g. after a reconnect creates a new instance) must not silently inherit
    a nonzero correction from nowhere - callers are responsible for re-applying the configured
    value after every reconnect, see main_window.py/web_server.py's set_ir_reading_correction()
    calls right after each `MachineBus(...)` construction."""
    bus = MachineBus("COM_TEST_ONLY")
    assert bus.ir_reading_correction_c == 0.0
