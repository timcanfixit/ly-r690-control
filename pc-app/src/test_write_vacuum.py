"""Tests the quiet-gap write mechanism using the Vacuum coil (safe, reversible, already
established this session) - deliberately NOT Start/Stop. Toggles Vacuum on, confirms via
passive read, toggles it back off, confirms again."""
import sys
import time

from machine_bus import MachineBus
from modbus_client import COIL_VACUUM

COM_PORT = sys.argv[1] if len(sys.argv) > 1 else "COM4"


def main() -> None:
    bus = MachineBus(COM_PORT)
    bus.start()
    print(f"Bus started on {COM_PORT}. Waiting for initial passive reading...")
    time.sleep(2)

    reading = bus.get_live_reading()
    print(f"Before: vacuum={reading.vacuum if reading else 'unknown'}")

    print("Writing coil (Vacuum) = ON...")
    result = bus.write_coil(COIL_VACUUM, True)
    print(f"  Write result: ok={result.ok} attempts={result.attempts} detail={result.detail}")

    time.sleep(1)
    reading = bus.get_live_reading()
    print(f"After ON write: vacuum={reading.vacuum if reading else 'unknown'}")

    time.sleep(2)
    print("Writing coil (Vacuum) = OFF...")
    result = bus.write_coil(COIL_VACUUM, False)
    print(f"  Write result: ok={result.ok} attempts={result.attempts} detail={result.detail}")

    time.sleep(1)
    reading = bus.get_live_reading()
    print(f"After OFF write: vacuum={reading.vacuum if reading else 'unknown'}")

    bus.stop()


if __name__ == "__main__":
    main()
