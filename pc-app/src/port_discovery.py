"""COM/serial port auto-detection - previously the port had to be typed in by hand with no way to
see what's actually plugged in. Does not assume any particular port IS the LY-R690 adapter (an
FTDI-based USB-RS485 adapter looks like any other FTDI serial device at this level) - listing is
just a convenience over guessing blindly; use MachineBus.is_connected after actually attempting a
connection (see test_connection()) to know if a given port is genuinely the right one."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import serial.tools.list_ports


@dataclass
class PortInfo:
    device: str
    description: str
    manufacturer: str
    vid: str
    pid: str
    serial_number: str


def list_available_ports() -> list[PortInfo]:
    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append(PortInfo(
            device=p.device,
            description=p.description or "",
            manufacturer=p.manufacturer or "",
            vid=f"{p.vid:04X}" if p.vid else "",
            pid=f"{p.pid:04X}" if p.pid else "",
            serial_number=p.serial_number or "",
        ))
    return ports


def find_port_by_identity(vid: str, pid: str, serial_number: str) -> Optional[str]:
    """Resolves a remembered adapter's hardware identity back to whatever port it currently
    enumerates as - 2026-09-08, external feature-gap review, second pass (see
    connection_identity.py). A USB-serial adapter's OS-assigned device path (COM4, /dev/ttyUSB0)
    isn't stable across reboots or replugging into a different USB port; VID/PID/serial_number
    are the adapter's own hardware identity and don't change.

    Prefers matching by serial_number alone when one was recorded - it's unique per physical
    adapter, so this is reliable even with two identical VID/PID adapters plugged in at once (a
    real FTDI-cable scenario). Falls back to VID+PID together when no serial_number was ever
    recorded (some cheap adapters don't expose one) - weaker (collides with any identical
    adapter), but still far better than a bare hardcoded port path. Returns None if nothing
    currently plugged in matches at all."""
    ports = list_available_ports()
    if serial_number:
        for p in ports:
            if p.serial_number == serial_number:
                return p.device
        return None
    if vid and pid:
        for p in ports:
            if p.vid == vid and p.pid == pid:
                return p.device
    return None


def test_connection(com_port: str, timeout_s: float = 3.0) -> tuple[bool, str]:
    """Opens `com_port` and tries ONE active read (temps) to see if the LY-R690 actually answers -
    a port opening successfully only proves a serial device exists there, not that it's this
    machine. Returns (ok, detail). Always closes the connection it opens, even on failure - never
    leaves a port held open for the caller to clean up."""
    from machine_bus import MachineBus
    bus = MachineBus(com_port)
    try:
        bus.start()
    except Exception as exc:
        return False, f"Could not open {com_port}: {exc}"
    try:
        temps = bus.read_temps_active()
        if temps is None:
            return False, f"{com_port} opened, but the LY-R690 didn't answer a test read - probably not the right port, or the machine is off."
        upper, lower, ir, probe = temps
        return True, f"{com_port}: LY-R690 responded - Upper {upper:.1f}C, Lower {lower:.1f}C, IR {ir:.1f}C, Probe {probe:.1f}C."
    finally:
        bus.stop()
