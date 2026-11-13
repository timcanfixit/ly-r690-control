"""Quick read-only sanity test for MachineClient. Connects, reads live values a few times,
and prints them for visual comparison against the touchscreen. Does not write anything."""
import sys
import time

from modbus_client import MachineClient

COM_PORT = sys.argv[1] if len(sys.argv) > 1 else "COM4"


def main() -> None:
    print(f"Connecting to {COM_PORT}...")
    with MachineClient(COM_PORT) as client:
        for i in range(5):
            reading = client.read_live()
            print(
                f"[{i}] Upper={reading.upper_temp:.1f}C Lower={reading.lower_temp:.1f}C "
                f"IR={reading.infrared_temp:.1f}C Probe={reading.probe_temp:.1f}C | "
                f"running={reading.running} running_mirror={reading.running_mirror} "
                f"vacuum={reading.vacuum} cooling={reading.cooling}"
            )
            time.sleep(1)

        print("\nReading curve data...")
        profile = client.read_curve()
        for idx, section in enumerate(profile.sections, start=1):
            print(f"  Section {idx}: {section}")


if __name__ == "__main__":
    main()
