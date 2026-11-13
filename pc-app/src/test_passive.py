"""Test the passive listener against real hardware. Never transmits - purely observes the
touchscreen's own ongoing traffic."""
import sys
import time

from passive_listener import PassiveListener

COM_PORT = sys.argv[1] if len(sys.argv) > 1 else "COM4"


def main() -> None:
    listener = PassiveListener(COM_PORT)
    listener.start()
    print(f"Listening on {COM_PORT} (passive, read-only)...")
    try:
        for i in range(15):
            time.sleep(1)
            reading = listener.get_live_reading()
            age = listener.seconds_since_update()
            if reading is None:
                print(f"[{i}] no data yet")
                continue
            print(
                f"[{i}] Upper={reading.upper_temp:.1f}C Lower={reading.lower_temp:.1f}C "
                f"IR={reading.infrared_temp:.1f}C Probe={reading.probe_temp:.1f}C | "
                f"running={reading.running} vacuum={reading.vacuum} cooling={reading.cooling} "
                f"(age={age:.1f}s)"
            )
    finally:
        listener.stop()


if __name__ == "__main__":
    main()
