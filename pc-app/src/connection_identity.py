"""Remembers which physical USB-serial adapter this app should talk to, by hardware identity
(VID/PID/serial number) rather than by whatever OS-assigned device path (COM4, /dev/ttyUSB0) it
happened to enumerate as - 2026-09-08, external feature-gap review, second pass.

That path is NOT stable: a reboot, replugging into a different USB port, or another USB-serial
device being plugged in first can all shuffle /dev/ttyUSB* numbering on Linux. On the Pi (running
headless as a systemd service - see docs/00-project-status-and-scope.md) this previously meant a
silent connection failure after any of those events, with no clue why, until someone SSHed in to
check. Recording the adapter's own hardware identity (see port_discovery.find_port_by_identity())
and resolving it fresh at every connection attempt fixes that.

remember_connection() is called automatically the moment a connection actually succeeds
(main.py/web_server.py's connect path) - not a separate manual "save" step, since there's nothing
for an operator to deliberately configure here beyond picking a port once, same as today.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

if getattr(sys, "frozen", False):
    CONNECTION_IDENTITY_PATH = Path(sys.executable).resolve().parent / "connection_identity.json"
else:
    CONNECTION_IDENTITY_PATH = Path(__file__).resolve().parent.parent / "connection_identity.json"


@dataclass
class ConnectionIdentity:
    vid: str
    pid: str
    serial_number: str
    description: str
    last_known_port: str


def save_connection_identity(identity: ConnectionIdentity) -> None:
    CONNECTION_IDENTITY_PATH.write_text(json.dumps(asdict(identity), indent=2))


def load_connection_identity() -> Optional[ConnectionIdentity]:
    if not CONNECTION_IDENTITY_PATH.exists():
        return None
    try:
        return ConnectionIdentity(**json.loads(CONNECTION_IDENTITY_PATH.read_text()))
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def remember_connection(port_info) -> None:
    """`port_info` is a port_discovery.PortInfo for the port a connection just succeeded on. Only
    remembers it when there's an actual hardware identity to record (a real VID/PID) - never
    overwrites a good record with a blank one just because a synthetic/virtual port (e.g. a test
    stub with no real USB descriptor) was used."""
    if not port_info.vid and not port_info.serial_number:
        return
    save_connection_identity(ConnectionIdentity(
        vid=port_info.vid, pid=port_info.pid, serial_number=port_info.serial_number,
        description=port_info.description, last_known_port=port_info.device,
    ))


def resolve_remembered_port() -> Optional[str]:
    """Returns the CURRENT port the remembered adapter enumerates as, or None if either nothing
    has ever been remembered, or the remembered adapter isn't currently plugged in."""
    from port_discovery import find_port_by_identity

    identity = load_connection_identity()
    if identity is None:
        return None
    return find_port_by_identity(identity.vid, identity.pid, identity.serial_number)
