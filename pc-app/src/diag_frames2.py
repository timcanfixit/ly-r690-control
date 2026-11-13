"""Diagnostic pass 2: dumps the actual VALUES of the smaller poll shapes (1-register and
4-register FC3 responses), so we can work out which registers they correspond to. The
touchscreen changes its polling pattern per screen, so the app can't rely only on the
8-register status-screen poll for live temperatures."""
from __future__ import annotations

import sys
import time

import serial

BAUD = 9600


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def crc_ok(frame: bytes) -> bool:
    return len(frame) >= 4 and crc16(frame[:-2]) == (frame[-2] | (frame[-1] << 8))


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM4"
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0

    ser = serial.Serial(port=port, baudrate=BAUD, parity="N", stopbits=1, bytesize=8, timeout=0.05)
    print(f"Listening on {port} for {duration:.0f}s.")
    print("Shows REQUESTS (which reveal start addresses) paired with response values.\n")

    buf = bytearray()
    deadline = time.monotonic() + duration
    printed = 0
    try:
        while time.monotonic() < deadline and printed < 60:
            chunk = ser.read(256)
            if chunk:
                buf.extend(chunk)
            pos = 0
            while pos < len(buf) - 4:
                if buf[pos] != 1:
                    pos += 1
                    continue
                func = buf[pos + 1]

                # A request is always exactly 8 bytes: id, func, addrHi, addrLo, cntHi, cntLo, crc, crc
                if func in (1, 3) and pos + 8 <= len(buf):
                    frame = bytes(buf[pos:pos + 8])
                    if crc_ok(frame):
                        start = (frame[2] << 8) | frame[3]
                        count = (frame[4] << 8) | frame[5]
                        # Sanity: a "request" whose count is implausible is really a response
                        if 1 <= count <= 130:
                            print(f"REQ  FC{func} start={start:<5} count={count}")
                            printed += 1
                            pos += 8
                            continue

                if func == 3 and pos + 3 <= len(buf):
                    byte_count = buf[pos + 2]
                    frame_len = 3 + byte_count + 2
                    if 0 < byte_count <= 250 and pos + frame_len <= len(buf):
                        frame = bytes(buf[pos:pos + frame_len])
                        if crc_ok(frame):
                            regs = [
                                int.from_bytes(frame[3 + i * 2:5 + i * 2], "big")
                                for i in range(byte_count // 2)
                            ]
                            print(f"  RESP FC3 {len(regs)} regs: {regs}")
                            printed += 1
                            pos += frame_len
                            continue
                pos += 1
            buf = buf[pos:]
            if len(buf) > 4096:
                buf = buf[-512:]
    finally:
        ser.close()


if __name__ == "__main__":
    main()
