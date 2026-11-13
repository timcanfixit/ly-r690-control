"""Hardware-free demo of the web control panel: a simulated machine instead of a serial port.

    python tools/demo_server.py          # then open http://127.0.0.1:8080  (set DEMO_PORT to change)

For trying the UI and taking screenshots. Nothing is ever sent to a real machine. It runs the REAL web app
with a simulated MachineBus: temperatures warm up toward the active profile while "Running" is on and cool
down otherwise. Use a scratch copy of the repo - the demo writes a sample profile and run logs next to the app."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "pc-app" / "src"
sys.path.insert(0, str(SRC))

import machine_bus  # noqa: E402
import web_server  # noqa: E402
from machine_bus import MachineBus, WriteResult  # noqa: E402
from modbus_client import COIL_RUNNING, COIL_RUNNING_MIRROR, CurveSection, Profile  # noqa: E402

AMBIENT = 25.0


class SimulatedBus(MachineBus):
    """Same public interface as MachineBus; no serial port. Registers/coils live in memory."""

    def start(self) -> None:  # no serial port
        self._serial = _FakePort()
        with self._data_lock:
            self._latest_coils = {}
        self._set_temps(AMBIENT, AMBIENT, AMBIENT, AMBIENT)
        self._sim_thread = threading.Thread(target=self._simulate, daemon=True)
        self._sim_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._serial = None

    def _set_temps(self, upper, lower, ir, probe) -> None:
        with self._data_lock:
            self._latest_temps = (upper, lower, ir, probe)
            now = time.monotonic()
            self._last_update_time = self._last_temps_update_time = self._last_coils_update_time = now

    def _simulate(self) -> None:
        upper = lower = ir = probe = AMBIENT
        started = None
        while not self._stop_event.is_set():
            running = self._latest_coils.get(COIL_RUNNING, False)
            if running and started is None:
                started = time.monotonic()
            if not running:
                started = None
            def step(value, target, up, down=1.0):
                return value + max(-down, min(up, target - value))  # rate-limited, like a real heater

            if running:
                elapsed = time.monotonic() - started
                target_top = min(235.0, 60 + elapsed * 1.1)
                target_bottom = min(225.0, 55 + elapsed * 1.0)
                upper = step(upper, target_top, 1.2)
                lower = step(lower, target_bottom, 1.1)
                ir = step(ir, target_top - 6, 1.1)
                probe = step(probe, target_top - 12, 1.0)
            else:
                upper, lower, ir, probe = (step(v, AMBIENT, 0.5, 0.6) for v in (upper, lower, ir, probe))
            self._set_temps(round(upper, 1), round(lower, 1), round(ir, 1), round(probe, 1))
            time.sleep(1.0)

    def write_coil(self, address, value, max_passes=3, attempts_per_pass=12, on_progress=None) -> WriteResult:
        with self._data_lock:
            self._latest_coils[address] = bool(value)
            if address == COIL_RUNNING:
                self._latest_coils[COIL_RUNNING_MIRROR] = bool(value)
            self._last_coils_update_time = time.monotonic()
        return WriteResult(True, 1, "simulated")

    def write_registers(self, start_address, values, on_progress=None) -> WriteResult:
        return WriteResult(True, 1, "simulated")

    def read_coils_active(self, max_attempts: int = 20):
        with self._data_lock:
            self._last_coils_update_time = time.monotonic()
            return dict(self._latest_coils)

    def read_temps_active(self):
        return self._latest_temps

    def read_curve_active(self, on_progress=None):
        return None

    def read_pid_active(self, on_progress=None):
        return None

    def read_fan_timing_active(self, on_progress=None):
        return None

    def read_temp_comp_active(self):
        return None


class _FakePort:
    is_open = True

    def close(self) -> None:
        self.is_open = False


def seed_demo_profile() -> None:
    import profile_store
    sections = [
        CurveSection(upper_temp=100, upper_rate=2, upper_hold_time=30, lower_temp=100, lower_rate=2, lower_hold_time=30, ir_temp=90, ir_rate=2, ir_hold_time=30),
        CurveSection(upper_temp=150, upper_rate=2, upper_hold_time=60, lower_temp=150, lower_rate=2, lower_hold_time=60, ir_temp=140, ir_rate=2, ir_hold_time=60),
        CurveSection(upper_temp=183, upper_rate=1, upper_hold_time=45, lower_temp=183, lower_rate=1, lower_hold_time=45, ir_temp=175, ir_rate=1, ir_hold_time=45),
        CurveSection(upper_temp=235, upper_rate=1, upper_hold_time=20, lower_temp=225, lower_rate=1, lower_hold_time=20, ir_temp=228, ir_rate=1, ir_hold_time=20),
    ]
    sections += [CurveSection() for _ in range(8 - len(sections))]  # the machine always has 8 sections
    try:
        profile_store.save_profile(Profile(name="Demo lead-free reflow", sections=sections))
    except Exception as exc:  # already exists on a second run
        print("demo profile not (re)created:", exc)


if __name__ == "__main__":
    import os
    port = int(os.environ.get("DEMO_PORT", "8080"))
    web_server.MachineBus = SimulatedBus
    _run = web_server.app.run
    web_server.app.run = lambda **kw: _run(**{**kw, "host": "127.0.0.1", "port": port})  # demo is local-only
    seed_demo_profile()
    print(f"DEMO MODE - simulated machine, no hardware. Open http://127.0.0.1:{port}")
    web_server.main()
