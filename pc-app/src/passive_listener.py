"""
Passive Modbus RTU listener for the LY-R690 bus. The touchscreen is a continuously-polling
Modbus master already reading everything this app needs (temps, status coils) - trying to
also be an active master on the same half-duplex bus causes severe collisions (confirmed:
8/8 retry attempts failed against real hardware, 2026-08-18).

Instead of sending our own requests, this listens to the touchscreen's own ongoing traffic
and extracts values from responses meant for it. This is the same technique proven reliable
throughout the original reverse-engineering work (see docs/06-modbus-register-map.md) - never
transmits anything, so it cannot collide with or disrupt the touchscreen's own communication.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

import serial

from modbus_client import (
    ADDR_TEMP_UPPER,
    ADDR_CURVE_BASE,
    COIL_RUNNING,
    COIL_RUNNING_MIRROR,
    COIL_VACUUM,
    COIL_COOLING,
    COIL_KEEP,
    CURVE_ATTRIBUTES,
    CURVE_SECTIONS,
    STATION_ADDRESS,
    BAUD_RATE,
    LiveReading,
    Profile,
)

logger = logging.getLogger(__name__)


def _modbus_crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def _crc_valid(frame: bytes) -> bool:
    if len(frame) < 4:
        return False
    calc = _modbus_crc16(frame[:-2])
    expected = frame[-2] | (frame[-1] << 8)
    return calc == expected


class PassiveListener:
    """Runs a background thread that reads raw bytes off the serial port, reconstructs Modbus
    RTU frames by validating CRC16 at structurally-plausible lengths (never just accepting the
    first CRC match - see the false-positive lesson in docs/06-modbus-register-map.md), and
    updates the latest known values whenever it recognizes a response for a register/coil range
    this app cares about. Never transmits anything."""

    def __init__(self, com_port: str):
        self.com_port = com_port
        self._serial: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self._latest_temps: Optional[tuple[float, float, float, float]] = None
        self._latest_coils: dict[int, bool] = {}
        self._latest_curve: Optional[Profile] = None
        self._last_update_time: float = 0.0

    def start(self) -> None:
        self._serial = serial.Serial(
            port=self.com_port, baudrate=BAUD_RATE, parity="N", stopbits=1, bytesize=8, timeout=0.2
        )
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._serial:
            self._serial.close()

    def get_live_reading(self) -> Optional[LiveReading]:
        with self._lock:
            if self._latest_temps is None:
                return None
            upper, lower, ir, probe = self._latest_temps
            return LiveReading(
                upper_temp=upper,
                lower_temp=lower,
                infrared_temp=ir,
                probe_temp=probe,
                running=self._latest_coils.get(COIL_RUNNING, False),
                running_mirror=self._latest_coils.get(COIL_RUNNING_MIRROR, False),
                vacuum=self._latest_coils.get(COIL_VACUUM, False),
                cooling=self._latest_coils.get(COIL_COOLING, False),
                keep=self._latest_coils.get(COIL_KEEP, False),
            )

    def get_latest_curve(self) -> Optional[Profile]:
        with self._lock:
            return self._latest_curve

    def seconds_since_update(self) -> float:
        if self._last_update_time == 0.0:
            return float("inf")
        return time.monotonic() - self._last_update_time

    # ---------------- internal ----------------

    def _run(self) -> None:
        buf = bytearray()
        while not self._stop_event.is_set():
            chunk = self._serial.read(256)
            if chunk:
                buf.extend(chunk)
                buf = self._process_buffer(buf)
            # trim runaway buffer if nothing recognizable is accumulating
            if len(buf) > 4096:
                buf = buf[-256:]

    def _process_buffer(self, buf: bytearray) -> bytearray:
        """Consumes recognized frames from the front of buf, returns the remainder."""
        pos = 0
        while pos < len(buf) - 4:
            if buf[pos] != STATION_ADDRESS:
                pos += 1
                continue
            consumed = self._try_parse_at(buf, pos)
            if consumed:
                pos += consumed
            else:
                pos += 1
        return buf[pos:]

    def _try_parse_at(self, buf: bytearray, pos: int) -> int:
        """Returns the number of bytes consumed if a recognized, CRC-valid frame starts at pos,
        else 0. Only recognizes the specific response shapes this app needs - deliberately
        conservative to avoid the false-positive problem seen during initial reverse-engineering."""
        func = buf[pos + 1]

        if func == 3:  # read holding registers response: addr, func, byteCount, data..., crc(2)
            if pos + 3 > len(buf):
                return 0
            byte_count = buf[pos + 2]
            frame_len = 3 + byte_count + 2
            if byte_count == 0 or byte_count > 250 or pos + frame_len > len(buf):
                return 0
            frame = bytes(buf[pos:pos + frame_len])
            if not _crc_valid(frame):
                return 0
            self._handle_holding_register_response(frame, byte_count)
            return frame_len

        if func == 1:  # read coils response: addr, func, byteCount, data..., crc(2)
            if pos + 3 > len(buf):
                return 0
            byte_count = buf[pos + 2]
            frame_len = 3 + byte_count + 2
            if byte_count == 0 or byte_count > 16 or pos + frame_len > len(buf):
                return 0
            frame = bytes(buf[pos:pos + frame_len])
            if not _crc_valid(frame):
                return 0
            self._handle_coil_response(frame, byte_count)
            return frame_len

        return 0

    def _handle_holding_register_response(self, frame: bytes, byte_count: int) -> None:
        # We can't know the request's start address from the response alone (RTU doesn't echo
        # it), so we disambiguate purely by register count, matching the known poll shapes.
        count = byte_count // 2
        regs = [int.from_bytes(frame[3 + i * 2 : 5 + i * 2], "big") for i in range(count)]
        with self._lock:
            if count == 8:
                # Status-screen poll: start=20, qty=8 (addresses 20-27). Only the first 4
                # (20-23) are the real x10-precision temps - see register-map doc for why
                # 24-27 are a redundant rounded-integer duplicate, not needed here.
                self._latest_temps = tuple(v / 10.0 for v in regs[:4])
                self._last_update_time = time.monotonic()
            elif count == 72:
                # Curve data poll (start=100, qty=82 actually returns 82; a pure 72-count read
                # would be a different/narrower poll - kept for forward compatibility)
                self._update_curve(regs)
            elif count == 82:
                # start=100, qty=82: first 72 are curve data, remaining 10 are the
                # advanced-parameters block (172-181) - only the first 72 matter here.
                self._update_curve(regs[:72])

    def _update_curve(self, regs: list[int]) -> None:
        profile = Profile()
        for attr_idx, attr_name in enumerate(CURVE_ATTRIBUTES):
            for section_idx in range(CURVE_SECTIONS):
                value = regs[attr_idx * CURVE_SECTIONS + section_idx]
                setattr(profile.sections[section_idx], attr_name, value)
        self._latest_curve = profile

    def _handle_coil_response(self, frame: bytes, byte_count: int) -> None:
        # Known poll: start=150, qty=29 -> byte_count=4. Bit i (LSB-first within each byte)
        # corresponds to coil 150+i.
        if byte_count != 4:
            return
        with self._lock:
            for byte_idx in range(byte_count):
                byte_val = frame[3 + byte_idx]
                for bit_idx in range(8):
                    coil_addr = COIL_RUNNING + byte_idx * 8 + bit_idx
                    self._latest_coils[coil_addr] = bool(byte_val & (1 << bit_idx))
            self._last_update_time = time.monotonic()
