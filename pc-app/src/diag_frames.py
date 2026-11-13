"""Diagnostic: prints every Modbus register/coil response the passive parser recognizes,
with its register count, so we can see exactly what shapes are actually arriving. Run this
INSTEAD of the app (it needs exclusive access to the COM port), navigate around on the
touchscreen, and watch what shows up."""
from __future__ import annotations

import sys
import time
from collections import Counter

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
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0

    ser = serial.Serial(port=port, baudrate=BAUD, parity="N", stopbits=1, bytesize=8, timeout=0.05)
    print(f"Listening on {port} for {duration:.0f}s. Navigate around on the touchscreen now.")
    print("Every recognized response frame is printed with its register/byte count.\n")

    buf = bytearray()
    seen = Counter()
    deadline = time.monotonic() + duration
    try:
        while time.monotonic() < deadline:
            chunk = ser.read(256)
            if chunk:
                buf.extend(chunk)
            pos = 0
            while pos < len(buf) - 4:
                if buf[pos] != 1:
                    pos += 1
                    continue
                func = buf[pos + 1]
                if func in (1, 3):
                    byte_count = buf[pos + 2]
                    frame_len = 3 + byte_count + 2
                    if byte_count == 0 or byte_count > 250 or pos + frame_len > len(buf):
                        pos += 1
                        continue
                    frame = bytes(buf[pos:pos + frame_len])
                    if crc_ok(frame):
                        if func == 3:
                            regs = byte_count // 2
                            key = f"FC3 response: {regs} registers ({byte_count} bytes)"
                        else:
                            key = f"FC1 response: {byte_count} bytes of coils"
                        if seen[key] == 0:
                            print(f"  FIRST SEEN -> {key}")
                        seen[key] += 1
                        pos += frame_len
                        continue
                pos += 1
            buf = buf[pos:]
            if len(buf) > 4096:
                buf = buf[-512:]
    finally:
        ser.close()

    print("\n--- Summary of everything seen ---")
    for key, count in seen.most_common():
        print(f"  {count:6d} x  {key}")
    if not any("72 registers" in k or "82 registers" in k for k in seen):
        print("\n  NOTE: no 72- or 82-register response was seen at all - the curve data")
        print("  poll never happened during this window.")


if __name__ == "__main__":
    main()
