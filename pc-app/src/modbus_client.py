"""
Modbus RTU client for the LY-R690 control board (YL302V12), talking over the RS485 tap on
pins 7/8 of the board's DB9 connector. All addresses below are confirmed against live traffic
- see docs/06-modbus-register-map.md in the repo root for the full derivation and verification
history. Station address 1, 9600 baud, 8N1 (confirmed 2026-08-18).

Safety: read operations (get_*) are always safe. Write operations (start_machine, stop_machine,
write_curve_section) actually command the physical machine - callers must never invoke them
without an explicit, in-the-moment user action (a button press in the UI), never automatically
or speculatively.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from pymodbus.client import ModbusSerialClient


def to_signed16(v: int) -> int:
    """Modbus registers are unsigned 16-bit on the wire; most values in this map (temps, hold
    times, rates) are naturally non-negative and can just be used as-is, but temperature
    compensation offsets can be negative and are stored as two's complement - convert before
    scaling/displaying."""
    return v - 65536 if v > 32767 else v


def to_unsigned16(v: int) -> int:
    """Inverse of to_signed16 - use before writing a possibly-negative value back to a register."""
    return v + 65536 if v < 0 else v

logger = logging.getLogger(__name__)

T = TypeVar("T")

# The touchscreen is a second, independent, continuously-polling Modbus master sharing this
# bus - there is no arbitration, so our own requests/responses can collide with its ongoing
# traffic. Retrying with a randomized backoff lets our request eventually land in a gap.
MAX_RETRIES = 8
RETRY_DELAY_RANGE = (0.05, 0.25)

STATION_ADDRESS = 1
BAUD_RATE = 9600

# --- Confirmed register/coil addresses (docs/06-modbus-register-map.md) ---

# Temperatures: FC3, x10 fixed-point (292 = 29.2 degC)
ADDR_TEMP_UPPER = 20
ADDR_TEMP_LOWER = 21
ADDR_TEMP_INFRARED = 22
ADDR_TEMP_OUTSIDE_PROBE = 23

# Status/control coils: FC1 read, FC5/FC15 write
COIL_RUNNING = 150       # M151 - busy/running status; also the Start trigger (write 1 to start)
COIL_RUNNING_MIRROR = 157  # tracks COIL_RUNNING identically in every test so far - purpose unclear
COIL_VACUUM = 171        # M172
COIL_COOLING = 172       # M173
COIL_KEEP = 173          # M174 - "hold current profile step at current temps". CONFIRMED via
# direct on-screen label adjacency 2026-08-19 (status screen block: literal "KEEP" immediately
# followed by "M174" in the decompressed project) - not yet independently live-toggled during
# an active run, but the label match itself is solid, same method that identified Vacuum/Cooling.

# Self-tuning (PID autotune) triggers: FC1 coils, found directly labeled on the Advanced
# Parameters screen 2026-08-19 ("upper/Lower Hot air self-tuning", "IR self-tuning"). Starts a
# real autotune routine that perturbs the actual heating elements to characterize response -
# treat with the same explicit-permission discipline as Start, never trigger speculatively.
COIL_SELFTUNE_UPPER = 151  # M152
COIL_SELFTUNE_LOWER = 152  # M153
COIL_SELFTUNE_IR = 153     # M154

# PID gains: FC3, 33 registers starting at 200 (VW201 .. VW233). Upper/Lower use 5-segment gain
# scheduling by temperature range; IR has a single (non-segmented) gain set. Order within the
# 33-register block: Upper P(5) I(5) D(5), Lower P(5) I(5) D(5), IR P(1) I(1) D(1).
ADDR_PID_BASE = 200
PID_SEGMENTS = 5
PID_COUNT = 33
ADDR_PID_UPPER_P = 200
ADDR_PID_UPPER_I = 205
ADDR_PID_UPPER_D = 210
ADDR_PID_LOWER_P = 215
ADDR_PID_LOWER_I = 220
ADDR_PID_LOWER_D = 225
ADDR_PID_IR_P = 230
ADDR_PID_IR_I = 231
ADDR_PID_IR_D = 232

# Fan/vacuum timing: FC3. The touchscreen's real poll covers qty=10 (172-181).
ADDR_VACUUM_TIME = 172       # VW173, seconds
ADDR_FAN_STOP_TEMP = 173     # VW174, x10 fixed-point degC
ADDR_FAN_STOP_DELAY = 174    # VW175, seconds
ADDR_ADVANCE_WARNING_TIME = 175  # VW176, seconds
FAN_TIMING_COUNT = 4
FAN_TIMING_POLL_COUNT = 10  # the actual on-wire poll shape, for passive-read recognition

# Temperature compensation offsets. UPPER and LOWER are now CONFIRMED live (2026-09-07): a real
# touchscreen edit of Up to 3.3 and Low to 7.7, captured passively on the wire, landed at exactly
# 176=33 and 177=77 (x10 fixed-point, matching the scale used everywhere else in this map) and
# held steady rather than reverting - see machine_bus.py's handling of this range for the capture
# detail. IR and PROBE are a different story: a real touchscreen edit of IR consistently reverted
# to 0 within a couple of seconds on the touchscreen's own display, with NO corresponding change
# ever observed on the wire for register 178 across two separate live attempts. Leading theory,
# not itself independently confirmed: the "Calibrate IR" auto-routine (see
# docs/02-hmi-screen-flow-proposal.md's back-mounted-reference-thermocouple finding) continuously
# re-asserts its own computed value over this register, fighting any manual entry - not that 178
# is the wrong address. Practical consequence either way: IR temperature compensation cannot
# currently be sustained through this register by any means (touchscreen or Modbus) - see
# calibration.py and the planned HMI-side correction offset in main_window.py/web_server.py for
# the actual fix. PROBE (179) remains unconfirmed - one ambiguous, likely-noise blip during the
# IR test, otherwise never observed to change.
#
# Original hypothesis basis for this whole block, before the above live test (kept for context):
# no adjacent tag-name string found for these in the firmware dump, unlike every other field.
# Registers 176-179 were previously captured live reading the constant 0xFFFF, dismissed at the
# time as "almost certainly an unused sentinel" - revisited 2026-08-19 after a real touchscreen
# photo showed a 4-field "Temperature compensation" section (Up/Low/IR/+1 more) all reading
# "-0.1C". 0xFFFF as a SIGNED 16-bit int is -1, which at the x10 fixed-point scale used everywhere
# else in this map is exactly -0.1 - matched all 4 photographed values exactly. Order (Up, Low,
# IR, Probe) was assumed by analogy with the temperature registers' own order; Up/Low are now
# independently confirmed correct, which validates that assumed order rather than undermining it.
# Values here are SIGNED, unlike every other x10 temperature field in this file, which are
# unsigned - do not reuse plain "v / 10.0" scaling without the sign conversion (see
# machine_bus.py's handling of this range).
ADDR_TEMP_COMP_UPPER = 176   # VW177 - CONFIRMED 2026-09-07
ADDR_TEMP_COMP_LOWER = 177   # VW178 - CONFIRMED 2026-09-07
ADDR_TEMP_COMP_IR = 178      # VW179 - address likely correct, but see note above: not writable/sustainable
ADDR_TEMP_COMP_PROBE = 179   # VW180 - still unconfirmed
TEMP_COMP_COUNT = 4

# Confirmed live 2026-09-09 (user report): an active read of this range (172-181, shared with fan/
# vacuum timing - see machine_bus.read_fan_timing_active) gets no response at all until the
# touchscreen's own Advanced Parameters screen has been opened at least once since the machine's
# controller was last powered on - one visit is enough for the rest of that power cycle.
# Investigated 2026-09-10 (docs/08-advanced-params-register-priming.md): the touchscreen sends NO
# write / handshake on that screen - it just adds a `FC3 172/10` read to its poll loop and the
# controller answers. Suspected controller-side lazy-init on first read of the block; if that's
# all it is, fully removing the touchscreen should let this app do that first read itself
# (unverified - see the doc's decisive test).

# Curve/profile data: FC3 read, FC16 write. 9 attribute rows x 8 sections = 72 registers at 100..171
ADDR_CURVE_BASE = 100
CURVE_SECTIONS = 8
CURVE_ATTRIBUTES = [
    "upper_temp", "upper_rate", "upper_hold_time",
    "lower_temp", "lower_rate", "lower_hold_time",
    "ir_temp", "ir_rate", "ir_hold_time",
]


@dataclass
class LiveReading:
    upper_temp: float
    lower_temp: float
    infrared_temp: float
    probe_temp: float
    running: bool
    running_mirror: bool
    vacuum: bool
    cooling: bool
    keep: bool
    # Self-tune (PID autotune) coils - a REAL heating action separate from a profile run (running
    # stays False the whole time), confirmed live 2026-09-07: a real IR self-tune produced
    # legitimate large/fast IR ramps that our ramp-rate safety check had no way to distinguish
    # from a runaway, since it only knew about profile-run section transitions, not self-tune.
    # Defaults keep every existing LiveReading(...) call site (tests included) working unchanged.
    self_tune_upper: bool = False
    self_tune_lower: bool = False
    self_tune_ir: bool = False


@dataclass
class CurveSection:
    upper_temp: int = 0
    upper_rate: int = 0
    upper_hold_time: int = 0
    lower_temp: int = 0
    lower_rate: int = 0
    lower_hold_time: int = 0
    ir_temp: int = 0
    ir_rate: int = 0
    ir_hold_time: int = 0


@dataclass
class Profile:
    name: str = "Untitled"
    sections: list[CurveSection] = field(default_factory=lambda: [CurveSection() for _ in range(CURVE_SECTIONS)])


class MachineClient:
    """Thin wrapper around pymodbus for this specific machine. Read-only methods are always
    safe to call freely. Methods documented as WRITE actually command the physical machine."""

    def __init__(self, com_port: str):
        self.com_port = com_port
        self._client = ModbusSerialClient(
            port=com_port,
            baudrate=BAUD_RATE,
            parity="N",
            stopbits=1,
            bytesize=8,
            timeout=1.0,
        )

    def connect(self) -> bool:
        return self._client.connect()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MachineClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _with_retry(self, description: str, call: Callable[[], T], validate: Callable[[T], bool]) -> T:
        """Runs `call`, retrying with a randomized backoff if the result errors or fails
        `validate` - which happens routinely on this bus because the touchscreen is a second,
        independent Modbus master and our request/response can collide with its own traffic."""
        last_result = None
        for attempt in range(1, MAX_RETRIES + 1):
            result = call()
            if not result.isError() and validate(result):
                return result
            last_result = result
            logger.debug("%s: attempt %d/%d got unexpected response (%r), retrying", description, attempt, MAX_RETRIES, result)
            time.sleep(random.uniform(*RETRY_DELAY_RANGE))
        raise IOError(f"{description}: no valid response after {MAX_RETRIES} attempts (last: {last_result!r})")

    # ---------------- reads (always safe) ----------------

    def read_live(self) -> LiveReading:
        temps = self._with_retry(
            "read temperatures",
            lambda: self._client.read_holding_registers(address=ADDR_TEMP_UPPER, count=4, device_id=STATION_ADDRESS),
            lambda r: len(r.registers) == 4,
        )
        coils = self._with_retry(
            "read status coils",
            lambda: self._client.read_coils(
                address=COIL_RUNNING, count=(COIL_KEEP - COIL_RUNNING + 1), device_id=STATION_ADDRESS
            ),
            lambda r: len(r.bits) >= (COIL_KEEP - COIL_RUNNING + 1),
        )
        bits = coils.bits
        return LiveReading(
            upper_temp=temps.registers[0] / 10.0,
            lower_temp=temps.registers[1] / 10.0,
            infrared_temp=temps.registers[2] / 10.0,
            probe_temp=temps.registers[3] / 10.0,
            running=bool(bits[COIL_RUNNING - COIL_RUNNING]),
            running_mirror=bool(bits[COIL_RUNNING_MIRROR - COIL_RUNNING]),
            vacuum=bool(bits[COIL_VACUUM - COIL_RUNNING]),
            cooling=bool(bits[COIL_COOLING - COIL_RUNNING]),
            keep=bool(bits[COIL_KEEP - COIL_RUNNING]),
        )

    def read_curve(self) -> Profile:
        expected_count = len(CURVE_ATTRIBUTES) * CURVE_SECTIONS
        result = self._with_retry(
            "read curve data",
            lambda: self._client.read_holding_registers(
                address=ADDR_CURVE_BASE, count=expected_count, device_id=STATION_ADDRESS
            ),
            lambda r: len(r.registers) == expected_count,
        )
        profile = Profile()
        regs = result.registers
        for attr_idx, attr_name in enumerate(CURVE_ATTRIBUTES):
            for section_idx in range(CURVE_SECTIONS):
                value = regs[attr_idx * CURVE_SECTIONS + section_idx]
                setattr(profile.sections[section_idx], attr_name, value)
        return profile

    def read_pid(self) -> list[int]:
        result = self._with_retry(
            "read PID registers",
            lambda: self._client.read_holding_registers(address=ADDR_PID_BASE, count=33, device_id=STATION_ADDRESS),
            lambda r: len(r.registers) == 33,
        )
        return list(result.registers)

    # ---------------- writes (COMMAND THE PHYSICAL MACHINE - never call speculatively) ----------------

    def start_machine(self) -> None:
        """Writes coil 150 (M151) = 1, the same write the touchscreen's on-screen Run button
        sends. This WILL start an actual heating cycle. Only call this in direct response to an
        explicit user action (a button press in this app), never automatically."""
        logger.warning("WRITE: starting machine (coil %d = 1)", COIL_RUNNING)
        self._with_retry(
            "start machine",
            lambda: self._client.write_coil(address=COIL_RUNNING, value=True, device_id=STATION_ADDRESS),
            lambda r: r.address == COIL_RUNNING and bool(r.value) is True,
        )

    def stop_machine(self) -> None:
        """Writes coil 150 (M151) = 0. Stops the machine."""
        logger.warning("WRITE: stopping machine (coil %d = 0)", COIL_RUNNING)
        self._with_retry(
            "stop machine",
            lambda: self._client.write_coil(address=COIL_RUNNING, value=False, device_id=STATION_ADDRESS),
            lambda r: r.address == COIL_RUNNING and bool(r.value) is False,
        )

    def write_curve(self, profile: Profile) -> None:
        """Writes a full 72-register curve/profile to the machine. This overwrites whatever
        curve is currently loaded on the control board. Only call in direct response to an
        explicit, confirmed user action (e.g. a "Send to machine" button, ideally with a
        confirmation dialog first)."""
        regs = [0] * (len(CURVE_ATTRIBUTES) * CURVE_SECTIONS)
        for attr_idx, attr_name in enumerate(CURVE_ATTRIBUTES):
            for section_idx in range(CURVE_SECTIONS):
                regs[attr_idx * CURVE_SECTIONS + section_idx] = getattr(profile.sections[section_idx], attr_name)
        logger.warning("WRITE: sending curve '%s' to machine (%d registers at %d)", profile.name, len(regs), ADDR_CURVE_BASE)
        self._with_retry(
            "write curve data",
            lambda: self._client.write_registers(address=ADDR_CURVE_BASE, values=regs, device_id=STATION_ADDRESS),
            lambda r: r.address == ADDR_CURVE_BASE and r.count == len(regs),
        )
