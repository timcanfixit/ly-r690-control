"""
Combined passive listener + coordinated writer for the LY-R690 Modbus RTU bus.

The touchscreen is a continuously-polling master with essentially no idle gaps (confirmed
2026-08-18: 8/8 blind retry attempts at writing failed against real hardware, and raw capture
showed near-continuous back-to-back traffic). Reads are handled purely passively (see
PassiveListener docstring) - this class adds coordinated writes on top of that, using a single
shared serial handle (Windows only allows one open handle per COM port) and a real quiet-gap
detector instead of blind retries:

1. The background thread continuously tracks the timestamp of the last received byte and
   whether it is currently mid-frame (inside a plausible, not-yet-fully-consumed frame) or
   between frames.
2. A write request signals the background thread to pause after its current read cycle,
   waits for an observed inter-frame gap of at least MIN_QUIET_GAP, then takes exclusive
   control of the port: transmits the write frame, reads the response, validates it, and
   hands the port back to the background thread.

Writes are still not guaranteed collision-free (the touchscreen could start transmitting a
few microseconds after our gap check passes), so callers get a validated ACK or a clear
failure - never a silent maybe.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import serial

from modbus_client import (
    COIL_RUNNING,
    COIL_RUNNING_MIRROR,
    COIL_VACUUM,
    COIL_COOLING,
    COIL_KEEP,
    COIL_SELFTUNE_UPPER,
    COIL_SELFTUNE_LOWER,
    COIL_SELFTUNE_IR,
    CURVE_ATTRIBUTES,
    CURVE_SECTIONS,
    STATION_ADDRESS,
    BAUD_RATE,
    ADDR_PID_BASE,
    PID_COUNT,
    ADDR_VACUUM_TIME,
    FAN_TIMING_COUNT,
    FAN_TIMING_POLL_COUNT,
    ADDR_TEMP_COMP_UPPER,
    TEMP_COMP_COUNT,
    ADDR_TEMP_UPPER,
    ADDR_CURVE_BASE,
    LiveReading,
    Profile,
)

logger = logging.getLogger(__name__)

MIN_QUIET_GAP_S = 0.015  # ~15ms - comfortably longer than the ~4ms RTU inter-frame silence spec
WRITE_WAIT_FOR_GAP_TIMEOUT_S = 5.0
ACTIVE_POLL_INTERVAL_S = 1.0  # how often to actively re-request temps/coils (see _active_poll_loop)

# A fixed retry delay can fall into lockstep with the touchscreen's own polling cycle - a chunk
# that collides once keeps colliding on every retry at the same phase offset, which is
# consistent with what was observed live 2026-08-19 (specific chunks failing all 8 attempts
# in a row, while which chunks failed changed between separate full-curve send attempts -
# random collision, not a blocked register). Randomized jitter breaks that synchronization.
WRITE_RETRY_JITTER_RANGE_S = (0.05, 0.2)

# write_coil()'s between-PASSES delay needs the exact same randomization, for the exact same
# reason - confirmed live 2026-09-15 (real machine, real touchscreen attached): a coil write can
# fail EVERY attempt of BOTH passes (16 low-level tries total, each already jittered - see
# WRITE_RETRY_JITTER_RANGE_S above) with the coil never actually changing state (independently
# confirmed by the passive coil-change logger staying silent throughout, ruling out "it worked but
# we lost the ACK"). Before this fix the gap between passes was a flat time.sleep(1.0) - exactly
# the "fixed delay can fall into lockstep" hazard this file's own jitter comment already warns
# about, just never applied here. Randomizing it gives a second, independent chance to fall out of
# sync with the touchscreen's polling cycle between passes, on top of the per-attempt jitter within
# each pass.
COIL_WRITE_PASS_GAP_RANGE_S = (0.8, 1.8)


def _modbus_crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _build_frame(pdu: bytes) -> bytes:
    frame = bytes([STATION_ADDRESS]) + pdu
    crc = _modbus_crc16(frame)
    return frame + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def _crc_valid(frame: bytes) -> bool:
    if len(frame) < 4:
        return False
    calc = _modbus_crc16(frame[:-2])
    expected = frame[-2] | (frame[-1] << 8)
    return calc == expected


def _build_write_registers_frame(start_address: int, values: list[int]) -> bytes:
    count = len(values)
    byte_count = count * 2
    pdu = bytearray([16, (start_address >> 8) & 0xFF, start_address & 0xFF, (count >> 8) & 0xFF, count & 0xFF, byte_count])
    for v in values:
        pdu += bytes([(v >> 8) & 0xFF, v & 0xFF])
    return _build_frame(bytes(pdu))


def _build_write_registers_ack(start_address: int, count: int) -> bytes:
    """The board's real FC16 success response - confirmed live 2026-08-18 by computing the
    CRC of this exact 6-byte header and finding it matched bytes captured immediately after
    a real write (addr, func=0x10, startHi, startLo, qtyHi, qtyLo, then CRC)."""
    header = bytes([STATION_ADDRESS, 16, (start_address >> 8) & 0xFF, start_address & 0xFF, (count >> 8) & 0xFF, count & 0xFF])
    crc = _modbus_crc16(header)
    return header + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def _build_read_frame(func_code: int, start_address: int, count: int) -> bytes:
    """FC3 (read holding registers) or FC1 (read coils) request - same header shape either way:
    addr, func, startHi, startLo, qtyHi, qtyLo, CRC."""
    pdu = bytes([func_code, (start_address >> 8) & 0xFF, start_address & 0xFF, (count >> 8) & 0xFF, count & 0xFF])
    return _build_frame(pdu)


def _parse_read_response(resp: bytes, func_code: int, expected_byte_count: int) -> Optional[list]:
    """Scans `resp` for a valid response to our own active read request - unlike the passive
    listener (which has to guess what a frame means purely from its byte count, since RTU
    responses don't echo the request), we know exactly what function code and byte count to
    expect because we made the request ourselves, so this validates against that directly rather
    than guessing. Returns parsed register ints (FC3) or coil bools (FC1) in address order, or
    None if no matching, CRC-valid frame is found anywhere in the captured window (it may contain
    leading/trailing noise from other bus traffic caught in the same read)."""
    n = len(resp)
    for pos in range(max(0, n - 4)):
        if resp[pos] != STATION_ADDRESS or resp[pos + 1] != func_code:
            continue
        byte_count = resp[pos + 2]
        if byte_count != expected_byte_count:
            continue
        frame_len = 3 + byte_count + 2
        if pos + frame_len > n:
            continue
        frame = bytes(resp[pos:pos + frame_len])
        if not _crc_valid(frame):
            continue
        data = frame[3:3 + byte_count]
        if func_code in (3, 4):
            count = byte_count // 2
            return [int.from_bytes(data[i * 2:i * 2 + 2], "big") for i in range(count)]
        bits = []
        for byte_val in data:
            for bit_idx in range(8):
                bits.append(bool(byte_val & (1 << bit_idx)))
        return bits
    return None


# Real captured curve data (multiple profiles, multiple sessions) has Rate always a single digit
# (2 or 3) and Hold time always tens of seconds (24-56). A "curve" capture with either field
# wildly outside that isn't real curve data, regardless of how clean its CRC is - see
# _update_curve_locked for why this check exists at all (Modbus RTU responses carry no address
# info, so a same-byte-count response from something else entirely on the bus can and did get
# mistaken for curve data - confirmed live 2026-08-19). Deliberately generous bounds (not tight
# to the exact observed range) to avoid rejecting a genuine profile that just has unusual values.
_MAX_PLAUSIBLE_RATE = 30
_MAX_PLAUSIBLE_HOLD_TIME = 250


def _curve_regs_look_plausible(regs: list[int]) -> bool:
    for attr_idx, attr_name in enumerate(CURVE_ATTRIBUTES):
        if attr_name.endswith("_rate"):
            limit = _MAX_PLAUSIBLE_RATE
        elif attr_name.endswith("_hold_time"):
            limit = _MAX_PLAUSIBLE_HOLD_TIME
        else:
            continue
        section_values = regs[attr_idx * CURVE_SECTIONS:(attr_idx + 1) * CURVE_SECTIONS]
        if any(v > limit for v in section_values):
            return False
    return True


# A 72-register FC16 frame is ~151 bytes - at 9600 baud (~1.04ms/byte) that's ~157ms on the
# wire, far longer than the ~8ms coil writes that are proven reliable. The bus has
# near-continuous traffic (see module docstring), so the 15ms pre-write quiet-gap check only
# guarantees the line was clear *before* transmission starts, not for the full frame duration -
# a 157ms frame gives the touchscreen's own polling ample opportunity to start transmitting
# mid-frame, corrupting it (silently dropped by the PLC's CRC check, no error visible to us).
# Confirmed live 2026-08-18: curve writes reported "transmitted 3/3" but never took effect, even
# after forcing the touchscreen to reload the curve editor screen. Splitting any large write into
# chunks this size (~26ms/frame - still short) keeps each individual frame's collision window
# small. Originally curve-specific, generalized 2026-08-19 for the PID write path (33 registers -
# unchunked that's ~78ms, in the same risky territory as the original curve write).
WRITE_CHUNK_SIZE = 8
# Writes at or under this many registers are short enough (<= ~9 registers, ~24ms) to send as one
# frame without meaningfully risking collision - matches the coil-write precedent (8 bytes, ~8ms)
# that's proven reliable unchunked. Above this, chunk.
CHUNK_THRESHOLD = 10

# write_coil()'s between-passes read-back is a quick sanity check ("did the write already work
# despite a lost ACK"), not a patient hunt for an answer - see its call site for why this must
# stay small (confirmed live 2026-08-22 that the full default active-read budget of 20 turned
# this quick check into a potential ~100-second wait of its own). Raised 3 -> 6 on 2026-09-15: with
# the readback now confirmed live to be the path that resolves the large majority of real coil
# toggles (the ACK itself is lost far more often than the physical write actually fails - see
# write_coil's own docstring), doubling this budget meaningfully raises the odds this quick check
# lands inside a real quiet window, while staying far short of the ~100-second regression above.
_COIL_READBACK_MAX_ATTEMPTS = 6


@dataclass
class WriteResult:
    ok: bool
    attempts: int
    detail: str = ""
    independently_verified: bool = True  # False = "we believe it was sent, but couldn't
    # confirm via a follow-up read" - true for coil writes (which poll continuously and so
    # confirm quickly), not reliably achievable for curve writes (see write_registers).


class MachineBus:
    """Owns the one serial handle for this COM port. Use get_live_reading()/get_latest_curve()
    for passive reads (always safe, never transmits). Use write_coil()/write_curve() for writes
    - these command the physical machine and must only be called in direct response to an
    explicit user action, never automatically or speculatively."""

    def __init__(self, com_port: str):
        self.com_port = com_port
        self._serial: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._active_poll_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()   # set by a writer to ask the reader to pause
        self._paused_ack = threading.Event()    # set by the reader once it has actually paused
        self._resume_event = threading.Event()  # set by a writer to let the reader resume
        self._port_lock = threading.Lock()       # serializes access to self._serial itself
        self._active_request_lock = threading.Lock()  # serializes whole pause/transmit/resume
        # sequences against EACH OTHER - _pause_event/_paused_ack/_resume_event were only ever
        # designed for one caller at a time (originally true by construction, since writes only
        # ever came from a single GUI action at once). Adding the background active-poll loop
        # (2026-08-19) broke that assumption: its periodic read_temps_active()/read_coils_active()
        # calls could now race against an explicit user-triggered read/write, both trying to
        # pause/resume the same reader concurrently - confirmed live, caused near-total failure
        # (one caller's `finally` block resuming the reader mid-flight for the other). This lock
        # makes "one active operation on the bus at a time" true again.

        self._data_lock = threading.Lock()
        # HMI-side IR reading correction (2026-09-07) - see get_live_reading() for the full story.
        # Confirmed live: the machine's own ADDR_TEMP_COMP_IR register caps at +10C and can't
        # cover the full ~20-24C bias found between live IR and its own profile targets. This is
        # the software-side top-up for whatever the hardware register can't reach - deliberately
        # applied here, in the one place every consumer (display/logging/alerts/safety) reads
        # through, rather than patched into each of the ~13 call sites individually. Set via
        # set_ir_reading_correction() - the caller (main_window.py/web_server.py) owns persisting
        # the configured value (SafetyLimits.ir_reading_correction_c) and re-applying it here after
        # every reconnect, since a fresh MachineBus always starts at the 0.0 default below.
        self.ir_reading_correction_c: float = 0.0
        self._latest_temps: Optional[tuple] = None
        self._latest_coils: dict[int, bool] = {}
        self._latest_curve: Optional[Profile] = None
        self._last_update_time: float = 0.0
        # Separate from _last_update_time, which reflects ANY bus activity (coil reads/writes
        # too) - see get_temps_timestamp() for why a temps-specific freshness timestamp is
        # needed for the ramp-rate safety check.
        self._last_temps_update_time: float = 0.0
        # Review finding #9 (2026-11-12): when the COIL snapshot (running / self-tune / cooling...) was last
        # actually observed. get_live_reading() fills coils nobody has seen with False, so without this a
        # fresh temperature reply plus an absent or stale status reply looked exactly like an idle machine.
        self._last_coils_update_time: float = 0.0
        # Review finding #5: serializes a WHOLE register update (all chunks), not just each chunk's
        # transaction - two senders interleaving chunks could both be told "success" while the registers
        # ended up half one request, half the other.
        self._register_write_lock = threading.Lock()
        # start addresses of register writes that failed (possibly part-applied) and have not been
        # successfully redone since - see config_incomplete().
        self._incomplete_writes: dict[int, float] = {}
        # Review finding #17: set when a serial worker dies on an I/O error (adapter unplugged...).
        self._reader_failed: str = ""
        self._last_byte_time: float = 0.0
        self._curve_seen_count: int = 0
        self._last_curve_time: float = 0.0
        self._latest_pid: Optional[list[int]] = None
        self._pid_seen_count: int = 0
        self._last_pid_time: float = 0.0
        self._latest_fan_timing: Optional[list[int]] = None
        self._fan_timing_seen_count: int = 0
        self._last_fan_timing_time: float = 0.0
        self._latest_temp_comp: Optional[list[int]] = None  # signed - see modbus_client.ADDR_TEMP_COMP_*

        # Communication diagnostics (Stage 14) - purely observational counters bolted onto the
        # existing retry mechanism, never changing what it does or how it decides to retry. Read
        # and write are counted separately since they have very different call volume (reads
        # happen continuously via _active_poll_loop; writes only on explicit user action).
        # "retry_count" is attempts beyond the first successful-or-exhausted try, summed across
        # every read and write operation - a rising number here means the bus is contended, even
        # if every individual operation still eventually succeeds.
        self._diag_lock = threading.Lock()
        self._diag_read_attempts = 0
        self._diag_read_successes = 0
        self._diag_read_failures = 0
        self._diag_write_attempts = 0
        self._diag_write_successes = 0
        self._diag_write_failures = 0
        self._diag_retry_count = 0
        self._diag_response_times: deque[float] = deque(maxlen=50)

    def start(self) -> None:
        self._serial = serial.Serial(
            port=self.com_port, baudrate=BAUD_RATE, parity="N", stopbits=1, bytesize=8, timeout=0.02
        )
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._active_poll_thread = threading.Thread(target=self._active_poll_loop, daemon=True)
        self._active_poll_thread.start()

    def get_coils_timestamp(self) -> float:
        """Monotonic time the coil snapshot was last actually OBSERVED (active read or overheard
        reply) - 0.0 if never. Deliberately separate from the temps timestamp: the two arrive in
        different replies and either can go stale alone."""
        with self._data_lock:
            return self._last_coils_update_time

    def coil_snapshot_age(self) -> float:
        ts = self.get_coils_timestamp()
        return float("inf") if ts == 0.0 else time.monotonic() - ts

    def config_write_in_progress(self) -> bool:
        """True while a register update (curve / PID / fan timing / temp comp) is mid-send."""
        return self._register_write_lock.locked()

    def config_incomplete(self) -> list:
        """Start addresses of register writes that FAILED (so the range may be partly updated) and
        haven't been successfully redone since. A run must not be started on a half-written curve."""
        with self._data_lock:
            return sorted(self._incomplete_writes)

    @property
    def reader_failure(self) -> str:
        return self._reader_failed

    @property
    def is_connected(self) -> bool:
        """True if the serial port is currently open - i.e. start() succeeded and stop() hasn't
        been called since. Doesn't guarantee live traffic (the adapter could be unplugged after a
        successful open); just reflects whether this MachineBus currently holds an open handle.
        Added 2026-08-19 for the web app's /api/connection status and reconnect support."""
        return self._serial is not None and not self._reader_failed and getattr(self._serial, "is_open", True)

    def stop(self) -> None:
        self._stop_event.set()
        self._resume_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._active_poll_thread:
            self._active_poll_thread.join(timeout=2.0)
        if self._serial:
            try:
                self._serial.close()
            finally:
                self._serial = None  # review finding #17: is_connected must not keep saying True after stop()

    def _active_poll_loop(self) -> None:
        """Keeps temps/status fresh via active requests, independent of whatever the passive
        listener happens to overhear - see the "active reads" section below for why. Curve/PID/
        fan-timing aren't polled continuously here (they're large, infrequently-needed reads -
        left as on-demand read_*_active() calls instead, e.g. from a "Read from machine" button)."""
        while not self._stop_event.is_set():
            try:
                self.read_temps_active()
                if self._stop_event.wait(timeout=ACTIVE_POLL_INTERVAL_S):
                    break
                self.read_coils_active()
                if self._stop_event.wait(timeout=ACTIVE_POLL_INTERVAL_S):
                    break
            except (serial.SerialException, OSError) as exc:
                self._mark_reader_failed("active poll", exc)
                break
            except Exception:
                logger.exception("active poll loop: unexpected error - continuing")
                if self._stop_event.wait(timeout=ACTIVE_POLL_INTERVAL_S):
                    break

    def _mark_reader_failed(self, which: str, exc: BaseException) -> None:
        """A serial worker hit an I/O error (adapter unplugged, port closed under it). Record it so
        is_connected turns False and every pending/later request fails fast instead of waiting for a
        pause acknowledgement that a dead reader will never send."""
        self._reader_failed = f"{which}: {type(exc).__name__}: {exc}"
        logger.error("serial %s worker stopped on an I/O error - bus marked disconnected: %s", which, self._reader_failed)
        self._pause_event.clear()
        self._paused_ack.set()

    # ---------------- passive reads (always safe) ----------------

    def set_ir_reading_correction(self, value: float) -> None:
        """See ir_reading_correction_c's definition in __init__ for the full story. Call this
        after every connect/reconnect (a fresh MachineBus always starts at 0.0) and whenever the
        operator changes the configured value."""
        with self._data_lock:
            self.ir_reading_correction_c = value

    def get_live_reading(self) -> Optional[LiveReading]:
        with self._data_lock:
            if self._latest_temps is None:
                return None
            upper, lower, ir, probe = self._latest_temps
            ir += self.ir_reading_correction_c
            return LiveReading(
                upper_temp=upper, lower_temp=lower, infrared_temp=ir, probe_temp=probe,
                running=self._latest_coils.get(COIL_RUNNING, False),
                running_mirror=self._latest_coils.get(COIL_RUNNING_MIRROR, False),
                vacuum=self._latest_coils.get(COIL_VACUUM, False),
                cooling=self._latest_coils.get(COIL_COOLING, False),
                keep=self._latest_coils.get(COIL_KEEP, False),
                # Same coil range as COIL_RUNNING (150-178, byte_count==4 in _handle_coil_response)
                # - already captured passively whenever the touchscreen's own status poll crosses
                # the wire, same as running/vacuum/cooling/keep above. No extra active read needed.
                self_tune_upper=self._latest_coils.get(COIL_SELFTUNE_UPPER, False),
                self_tune_lower=self._latest_coils.get(COIL_SELFTUNE_LOWER, False),
                self_tune_ir=self._latest_coils.get(COIL_SELFTUNE_IR, False),
            )

    def get_latest_curve(self) -> Optional[Profile]:
        with self._data_lock:
            return self._latest_curve

    def get_curve_status(self) -> tuple[int, float]:
        """Returns (how many curve reads we've observed, seconds since the most recent one).
        The touchscreen only emits the full curve poll on certain screen-load events, not
        continuously - the UI surfaces this so the user can see exactly when fresh curve data
        actually arrives, instead of guessing at the timing."""
        with self._data_lock:
            if self._last_curve_time == 0.0:
                return self._curve_seen_count, float("inf")
            return self._curve_seen_count, time.monotonic() - self._last_curve_time

    def seconds_since_update(self) -> float:
        if self._last_update_time == 0.0:
            return float("inf")
        return time.monotonic() - self._last_update_time

    def get_temps_timestamp(self) -> float:
        """Monotonic time the live temperature reading was last ACTUALLY refreshed (active read
        or passive listener) - 0.0 if never. Deliberately separate from seconds_since_update(),
        which reflects ANY bus activity (a coil read/write refreshes it too) and can look "fresh"
        even when temps specifically have gone stale.

        Needed so a ramp-rate safety check can be computed against the real elapsed time between
        two genuinely different temperature samples, not against however often the caller happens
        to poll get_live_reading() - confirmed live 2026-09 that the passive listener is paused
        for the entire duration of a contended coil write (already known this session to take
        20-40+ seconds under bus contention), during which get_live_reading() keeps returning the
        same stale temps every outer poll tick. When a genuinely fresh reading finally arrives,
        reflecting however much the temperature actually changed over that whole stale window,
        computing its rate against only the last poll interval (POLL_INTERVAL_S, a fraction of a
        second) instead of the real elapsed time produces a wildly inflated, physically-impossible
        rate (reported live: a normal ~14C real change divided by a 0.5s tick instead of the true
        multi-second window computed as 1641C/min) and an unwarranted safety auto-stop."""
        with self._data_lock:
            return self._last_temps_update_time

    def get_latest_pid(self) -> Optional[list[int]]:
        """Returns the raw 33-register PID block (see modbus_client.ADDR_PID_* for layout), if
        we've ever passively observed one - like curve data, only emitted when the touchscreen
        loads the Advanced Parameters screen, not continuously."""
        with self._data_lock:
            return list(self._latest_pid) if self._latest_pid is not None else None

    def get_pid_status(self) -> tuple[int, float]:
        with self._data_lock:
            if self._last_pid_time == 0.0:
                return self._pid_seen_count, float("inf")
            return self._pid_seen_count, time.monotonic() - self._last_pid_time

    def get_latest_fan_timing(self) -> Optional[list[int]]:
        """Returns [vacuum_time, fan_stop_temp_x10, fan_stop_delay, advance_warning_time] if
        we've ever passively observed the Advanced Parameters screen's fan-timing poll."""
        with self._data_lock:
            return list(self._latest_fan_timing) if self._latest_fan_timing is not None else None

    def get_fan_timing_status(self) -> tuple[int, float]:
        with self._data_lock:
            if self._last_fan_timing_time == 0.0:
                return self._fan_timing_seen_count, float("inf")
            return self._fan_timing_seen_count, time.monotonic() - self._last_fan_timing_time

    def get_latest_temp_comp(self) -> Optional[list[int]]:
        """Returns [upper, lower, ir, probe] compensation offsets (SIGNED x10 fixed-point - see
        modbus_client.ADDR_TEMP_COMP_*) if ever observed - same poll/freshness as fan timing,
        since they're read together in one request."""
        with self._data_lock:
            return list(self._latest_temp_comp) if self._latest_temp_comp is not None else None

    def _record_response_time(self, seconds: float) -> None:
        with self._diag_lock:
            self._diag_response_times.append(seconds)

    def get_diagnostics(self) -> dict:
        """Stage 14: a read-only snapshot of the counters described at their declaration in
        __init__. Response times are the wire round-trip (write -> validated response) of the
        most recent up-to-50 SUCCESSFUL attempts only - failed/timed-out attempts don't have a
        real response to time, and mixing them in would understate how fast the bus actually is
        when it isn't contended."""
        with self._diag_lock:
            times = list(self._diag_response_times)
            return {
                "read_attempts": self._diag_read_attempts,
                "read_successes": self._diag_read_successes,
                "read_failures": self._diag_read_failures,
                "write_attempts": self._diag_write_attempts,
                "write_successes": self._diag_write_successes,
                "write_failures": self._diag_write_failures,
                "retry_count": self._diag_retry_count,
                "last_response_time_s": times[-1] if times else None,
                "avg_response_time_s": (sum(times) / len(times)) if times else None,
                "response_time_sample_count": len(times),
            }

    def reset_diagnostics(self) -> None:
        with self._diag_lock:
            self._diag_read_attempts = 0
            self._diag_read_successes = 0
            self._diag_read_failures = 0
            self._diag_write_attempts = 0
            self._diag_write_successes = 0
            self._diag_write_failures = 0
            self._diag_retry_count = 0
            self._diag_response_times.clear()

    # ---------------- active reads (ask the board directly, don't just wait to overhear it) ----------------
    #
    # Everything above this point only ever learns values by passively overhearing the
    # touchscreen's own traffic - fine while it's present, but it means removing the touchscreen
    # entirely (2026-08-19: user considering permanently disconnecting it now that writes are
    # reliable without it) leaves every read frozen forever, since there'd be nothing left to
    # overhear. These methods request data directly instead, using the exact same collision-
    # avoidance mechanism already proven for writes - the SENDING side of that (this app's own
    # code, its collision avoidance) is safe to call whether or not the touchscreen is present,
    # full stop. They update the same _latest_* state the get_*() methods above read from, so
    # nothing else in the app needs to change to benefit from this.
    #
    # CORRECTION 2026-09-09: that "safe to call either way" claim is about this app's own request
    # logic - it does NOT mean the board is guaranteed to actually answer every one of these
    # without the touchscreen. Confirmed live: read_fan_timing_active() (and therefore
    # read_temp_comp_active(), the same request) gets no response at all until the touchscreen's
    # own Advanced Parameters screen has been opened at least once since the controller was last
    # powered on - so despite being called exactly like every other active read here, this
    # specific one is NOT currently touchscreen-free in practice, even briefly at every power-on.
    # read_temps_active(), read_coils_active(), and read_pid_active() have no such requirement
    # confirmed against them so far - this caveat is specific to the fan-timing/temp-comp range,
    # not a blanket walk-back of active reads in general. See that method's own docstring for the
    # full finding.

    def read_temps_active(self) -> Optional[tuple]:
        """Requests qty=8 at ADDR_TEMP_UPPER - the touchscreen's own exact, extensively-verified
        status-poll shape (see docs/06-modbus-register-map.md) - not the smaller qty=4 that would
        be enough on its own. Modbus RTU read responses carry no address information (only
        function code + byte count + data), so a smaller/different-shaped request risks getting
        confused with some OTHER read of the same byte count that's also on the bus (confirmed
        live 2026-08-19: a naive qty=4 request returned values matching the unrelated 24-27
        rounded-duplicate poll instead of the real x10-precision temps - an easy mistake given
        RTU's lack of address echo, worth flagging clearly since it's not obvious). Matching the
        touchscreen's own proven shape avoids that ambiguity as much as possible."""
        values = self._send_active_read(3, ADDR_TEMP_UPPER, 8, "read_temps_active")
        if values is None:
            return None
        temps = tuple(v / 10.0 for v in values[:4])
        with self._data_lock:
            self._latest_temps = temps
            self._last_update_time = time.monotonic()
            self._last_temps_update_time = self._last_update_time
        return temps

    def read_coils_active(self, max_attempts: int = 20) -> Optional[dict[int, bool]]:
        """Requests qty=29 at COIL_RUNNING - the touchscreen's own exact, proven coil-poll shape
        (byte_count=4) - see read_temps_active for why matching a known-good shape matters here,
        not just picking the minimum count actually needed. `max_attempts` defaults to the same
        generous budget as any other active read (for an explicit, deliberate "read coil state"
        call), but write_coil()'s own quick between-passes sanity check passes a much smaller
        value - see write_coil for why."""
        bits = self._send_active_read(1, COIL_RUNNING, 29, "read_coils_active", max_attempts=max_attempts)
        if bits is None:
            return None
        with self._data_lock:
            for i, b in enumerate(bits):
                self._latest_coils[COIL_RUNNING + i] = b
            self._last_update_time = time.monotonic()
            self._last_coils_update_time = self._last_update_time
            return dict(self._latest_coils)

    def read_curve_active(self, on_progress=None) -> Optional[Profile]:
        """2026-09-13: the standalone "Read from machine" button that called this directly was
        removed from both frontends - confirmed non-functional (see below), not a temporary gap,
        so exposing it as a primary action just taught operators to distrust a "Reading..." spinner
        that would burn through ~100 attempts and never once succeed. Still called internally, on
        a best-effort basis, as the opportunistic read-back check right after a curve Send (see
        api_curve_send/_on_send_to_machine) - that caller already treats None as "unconfirmed, not
        a failure," which is the only way this method's near-permanent None is safe to consume.

        Tries every plausible shape found for reading this address range, in order of how well
        each is historically grounded, before giving up:

        1. FC3, qty=82 (not 72), single shot - docs/06-modbus-register-map.md documents BOTH 72
           and 82 as real observed byte-shapes for this address range ("82 = the curve range plus
           the trailing advanced-params block ... 72 = a narrower read of just the curve range").
           Only the first 72 registers are curve data; the rest is discarded. This exact category
           of bug already happened once this session for PID and fan-timing - requesting the
           logically-sufficient count instead of the touchscreen's real exact poll size got
           silence both times, fixed by matching the real shape. 82, not 72, may be that shape
           here too - never tried before now.
        2. FC3, chunked (8 registers/row, 9 requests) - ruled out as a fix on its own (confirmed
           live 2026-08-19, both the original 72-register single request and this chunked version
           failed 100% of the time), kept as a fallback since smaller chunks are still strictly
           safer if the range responds to *something*.
        3. FC4 (read input registers) per chunk - a genuine but currently-unconfirmed guess: no
           evidence in the reverse-engineered firmware dump points at it specifically, it's just
           the next thing to rule out given every other explanation (request size, touchscreen
           screen state including the one screen documented as doing a live PLC read, a full
           machine power cycle) has already failed to produce a single response, and the passive
           listener - independent of this class's own read logic - has never observed one either.

        Writes to this exact address range are independently confirmed working (real captured
        ACKs - see write_registers), ruling out the address itself being wrong; every other range
        on this bus (temps, PID, fan-timing, coils) reads back fine over FC3. If nothing here
        works, the honest conclusion is that this register range may simply not be readable over
        Modbus by any method tried so far, on this board, at all.

        `on_progress`, if given, is called as `on_progress(phase, attempt, max_attempts)` after
        each attempt in any of the 3 strategies above - `phase` is a short human-readable label
        ("Full read (qty=82)", "Row 3/9 (FC3)", "Row 3/9 (FC4 fallback)") since this read can
        legitimately burn through nearly 100 attempts across every fallback before giving up
        (confirmed live: this range has never once succeeded on this board over any method tried),
        which is a very long time to show nothing but a static "Reading..." the whole time."""
        def phase_progress(phase):
            if on_progress is None:
                return None
            return lambda attempt, max_attempts: on_progress(phase, attempt, max_attempts)

        full = self._send_active_read(
            3, ADDR_CURVE_BASE, 82, "read_curve_active(FC3 qty=82)", max_attempts=8,
            on_progress=phase_progress("Full read (qty=82)"),
        )
        if full is not None:
            with self._data_lock:
                if self._update_curve_locked(full[:72]):
                    return self._latest_curve

        regs: list[int] = []
        total_rows = len(CURVE_ATTRIBUTES)
        for row in range(total_rows):
            addr = ADDR_CURVE_BASE + row * CURVE_SECTIONS
            chunk = self._send_active_read(
                3, addr, CURVE_SECTIONS, f"read_curve_active(row {row}, FC3)", max_attempts=5,
                on_progress=phase_progress(f"Row {row + 1}/{total_rows} (FC3)"),
            )
            if chunk is None:
                chunk = self._send_active_read(
                    4, addr, CURVE_SECTIONS, f"read_curve_active(row {row}, FC4)", max_attempts=5,
                    on_progress=phase_progress(f"Row {row + 1}/{total_rows} (FC4 fallback)"),
                )
            if chunk is None:
                return None
            regs.extend(chunk)
        with self._data_lock:
            if not self._update_curve_locked(regs):
                return None
            return self._latest_curve

    def read_pid_active(self, on_progress=None) -> Optional[list[int]]:
        values = self._send_active_read(3, ADDR_PID_BASE, PID_COUNT, "read_pid_active", on_progress=on_progress)
        if values is None:
            return None
        with self._data_lock:
            self._latest_pid = values
            self._pid_seen_count += 1
            self._last_pid_time = time.monotonic()
        return list(values)

    def read_fan_timing_active(self, on_progress=None) -> Optional[list[int]]:
        """Requests qty=FAN_TIMING_POLL_COUNT (10), the touchscreen's own real poll shape for
        this range, not the smaller qty=FAN_TIMING_COUNT (4) that's actually meaningful - see
        read_temps_active for why matching a known-good shape matters (RTU read responses carry
        no address information to disambiguate against a same-sized read of something else). This
        range also covers temperature compensation (registers 176-179) - both are updated from
        the one request; read_temp_comp_active() is a thin wrapper around this same call.

        Confirmed live 2026-09-09 (user report): this active read fails with no response at all
        until the touchscreen's own Advanced Parameters screen has been opened at least once since
        the machine's controller was last powered on - it does NOT need to stay on that screen
        afterward; one visit is enough for the rest of that power cycle.

        Investigated 2026-09-10 (see docs/08-advanced-params-register-priming.md): decompiled the
        HMI's MCGS project and CRC-decoded the bus captures. The touchscreen does NOT write
        anything or send any handshake on that screen - it simply adds a `FC3 start=172 qty=10`
        read to its poll loop, and the controller answers it immediately. There is no telegram to
        replicate. Best-supported theory: the controller lazy-initializes this register block on
        its first read since power-on, and the app's identical request currently can't land
        cleanly against the touchscreen's gap-free poll stream to trigger that. If so, physically
        removing the touchscreen (no contention) should let this method perform that first read
        itself - UNVERIFIED, needs the decisive test in that doc. Until then, treat "one visit to
        the Advanced Parameters screen per power cycle" as a real requirement for this range in a
        touchscreen-present setup. See main_window.py's/web_server.py's "no response" message for
        the operator-facing version."""
        regs = self._send_active_read(3, ADDR_VACUUM_TIME, FAN_TIMING_POLL_COUNT, "read_fan_timing_active", on_progress=on_progress)
        if regs is None:
            return None
        comp_offset = ADDR_TEMP_COMP_UPPER - ADDR_VACUUM_TIME
        with self._data_lock:
            self._latest_fan_timing = regs[:FAN_TIMING_COUNT]
            self._latest_temp_comp = regs[comp_offset:comp_offset + TEMP_COMP_COUNT]
            self._fan_timing_seen_count += 1
            self._last_fan_timing_time = time.monotonic()
        return regs[:FAN_TIMING_COUNT]

    def read_temp_comp_active(self) -> Optional[list[int]]:
        """See read_fan_timing_active - same underlying request, this just returns the other
        slice of it for callers that only care about compensation."""
        if self.read_fan_timing_active() is None:
            return None
        return self.get_latest_temp_comp()

    def read_raw_active(self, func_code: int, start_address: int, count: int, on_progress=None) -> Optional[list]:
        """Register/coil viewer - 2026-09-08, external feature-gap review, second pass. A thin
        public wrapper over _send_active_read() for arbitrary (func_code, start_address, count),
        unlike every other read_*_active() method above which targets one specific, already-
        understood register range. Doesn't update any of this class's own cached state (
        _latest_pid etc.) - this is a one-off diagnostic look, not a new source of ongoing truth
        the rest of the app should start relying on. Callers must validate func_code/count
        themselves first (see register_viewer.validate_register_read_request) - this method
        trusts its arguments and will pass through to the wire whatever it's given.

        Unlike the passive listener's shape-matching concern (see read_temps_active's docstring -
        RTU read RESPONSES carry no address info, so passively OVERHEARD traffic has to be
        disambiguated by shape), that ambiguity doesn't apply here: this is a synchronous request
        this code itself sent, so the response is unambiguously the answer to THIS call."""
        return self._send_active_read(func_code, start_address, count, "read_raw_active", on_progress=on_progress)

    # ---------------- coordinated writes (COMMAND THE PHYSICAL MACHINE) ----------------

    def write_coil(self, address: int, value: bool, max_passes: int = 3, attempts_per_pass: int = 12, on_progress=None) -> WriteResult:
        """Writes a single coil (FC5) and verifies it against the board's own direct ACK - FC5's
        success response is a verbatim echo of the request per the Modbus spec, so `frame` IS
        the expected ack here. Previously verified by watching the passive listener for the
        resulting status poll instead, which is proven reliable while the touchscreen is
        present - but that meant a temporarily touchscreen-off bus (see docs/06-modbus-
        register-map.md, "temporarily power off for profile sends") always reported false
        failures even when the write worked fine, since nothing polls status without it. Direct-
        ACK verification works the same regardless of whether the touchscreen is present. Only
        call this in direct response to an explicit, in-the-moment user action.

        Retries across up to `max_passes` passes, each pass separated by a randomized pause if it
        failed outright, rather than one big attempt budget crammed into a continuous burst -
        confirmed live 2026-08-19 for chunked register writes that spreading retries across
        passes with real wall-clock gaps between them succeeds more often than the same total
        attempt count run back-to-back (contention gets a genuine chance to clear between
        passes); the same reasoning applies here. `max_passes`/`attempts_per_pass` raised from
        2/8 to 3/12 on 2026-09-15 after catching a real live failure (reported directly: Vacuum/
        Cooling/Stop/Start/Keep needing more than one full press-and-wait cycle to actually take
        effect) - a real machine + touchscreen capture that day showed one coil write losing the
        ACK on EVERY attempt of BOTH passes, with the coil's actual state independently confirmed
        (via the passive coil-change logger) to have never changed - a genuine, if intermittent,
        RS485 collision window with the touchscreen's own traffic, not a false alarm. See
        COIL_WRITE_PASS_GAP_RANGE_S below for the other half of this fix.

        `on_progress`, if given, is called as `on_progress(attempt, attempts_per_pass, pass_num)`
        after each attempt.

        After EVERY failed pass (not just once at the very end), this tries one more thing before
        either giving up or starting another pass: an independent active read-back of the coil's
        actual current state. Losing the ACK on the return trip (e.g. to a collision with the
        touchscreen's own traffic arriving a moment later) doesn't mean the write itself failed -
        the board may well have already applied it, and confirmed live 2026-08-22 that it
        typically has (the physical relay audibly/visibly changed on the first real transmission
        well over 90% of the time even when its own ACK never made it back). A single quick
        read-back after one failed pass is a much faster path to a confirmed answer than blindly
        grinding through a second and third full ACK-hunting pass on the hope of eventually
        catching one - confirmed live 2026-08-22 that the original 3-pass/15-attempt budget
        (up to 45 attempts) made an already-successful toggle look like it was taking 20-40+
        seconds to "notice", when a read-back moments after pass 1 would have confirmed it almost
        immediately. Read-back is a genuinely independent check (a fresh request/response, not
        just re-checking the same lost ACK) - if it shows the coil already matches what was
        requested, that's real confirmation the write worked, not a guess, and reporting it as a
        failure would be the same "collapse observed/confirmed into one boolean" mistake this
        project has already had to walk back elsewhere (see machine_state.py's module docstring)."""
        pdu = bytes([5, (address >> 8) & 0xFF, address & 0xFF, 0xFF if value else 0x00, 0x00])
        frame = _build_frame(pdu)
        result = None
        total_attempts = 0
        for pass_num in range(1, max_passes + 1):
            pass_progress = (lambda attempt, mx, p=pass_num: on_progress(attempt, mx, p)) if on_progress else None
            result = self._send_verified(
                frame, frame,
                description=f"write_coil({address}, {value}) pass {pass_num}",
                max_attempts=attempts_per_pass,
                on_progress=pass_progress,
            )
            total_attempts += result.attempts
            if result.ok:
                break

            # A quick sanity check, not a patient hunt for an answer - this used to call
            # read_coils_active() with its full default budget (max_attempts=20, the same as any
            # other deliberate "read coil state" request), which meant the gap BETWEEN passes
            # could itself take up to ~100 seconds under contention (confirmed live 2026-08-22:
            # "taking too long between passes") - worse than the retry budget it was meant to
            # shortcut. A handful of quick attempts is enough to catch the common case (the write
            # already worked); if the bus is too contended even for that, the next write pass
            # will get its own chance to try again anyway.
            actual = self.read_coils_active(max_attempts=_COIL_READBACK_MAX_ATTEMPTS)
            if actual is not None and actual.get(address) == value:
                result = WriteResult(
                    ok=True, attempts=total_attempts,
                    detail=(
                        f"No ACK was seen after {total_attempts} attempt(s) (likely lost to a "
                        f"collision on the return trip), but an independent read-back confirms "
                        f"the coil is now {value} - the write did take effect."
                    ),
                    independently_verified=True,
                )
                break

            if pass_num < max_passes:
                time.sleep(random.uniform(*COIL_WRITE_PASS_GAP_RANGE_S))
        result.attempts = total_attempts
        if result.ok:
            # The ACK (or the read-back above) IS confirmation this value was applied - update
            # the local cache immediately instead of waiting for the next passive/active poll to
            # catch up (up to ~1s away, longer if the touchscreen isn't currently polling coils).
            # Every toggle-style button handler (Vacuum/Cooling/Keep) reads this cache via
            # get_live_reading() to decide which direction to flip - a stale read here meant a
            # second click landing inside that window toggled FROM the pre-write state, silently
            # undoing the first click even on writes that succeeded immediately.
            with self._data_lock:
                self._latest_coils[address] = value
        return result

    def write_registers(self, start_address: int, values: list[int], on_progress=None) -> WriteResult:
        """Writes multiple holding registers (FC16), verified against the board's own direct
        ACK (see write_coil for why - same reasoning applies here).

        Writes larger than CHUNK_THRESHOLD registers are split into short chunks rather than
        sent as one long frame - see WRITE_CHUNK_SIZE for why - each chunk verified the same
        way, with multiple passes over whatever is still outstanding - see
        _send_chunked_registers. Applies to curve writes (72 registers) and PID writes (33
        registers) alike.

        `on_progress`, if given, is called as `on_progress(confirmed_count, total_chunks,
        pass_num)` after each chunk attempt (chunked writes only) - lets a slow send under heavy
        bus contention show real progress instead of going dark for however long the whole write
        takes (confirmed live 2026-08-19: a badly-contended curve send took 2-3 minutes before
        reporting failure, with zero feedback in the meantime). Called on whatever thread
        write_registers runs on - callers on a GUI thread need to marshal it back themselves,
        same as _run_write's result."""
        count = len(values)
        with self._register_write_lock:  # one WHOLE update at a time (review finding #5)
            if count > CHUNK_THRESHOLD:
                result = self._send_chunked_registers(start_address, values, on_progress=on_progress)
            else:
                frame = _build_write_registers_frame(start_address, values)
                expected_ack = _build_write_registers_ack(start_address, count)
                result = self._send_verified(frame, expected_ack, description=f"write_registers({start_address}, {count} values)")
            with self._data_lock:
                if result.ok:
                    self._incomplete_writes.pop(start_address, None)
                else:
                    self._incomplete_writes[start_address] = time.monotonic()
        return result

    def _send_chunked_registers(self, start_address: int, values: list[int], chunk_size: int = WRITE_CHUNK_SIZE,
                                 on_progress=None, max_passes: int = 5, attempts_per_pass: int = 8) -> WriteResult:
        """Sends a large register write as a series of short per-chunk FC16 frames (see
        WRITE_CHUNK_SIZE), each individually verified against the board's real write-ACK and
        retried on its own until confirmed - not fired blind. Live testing 2026-08-18 (curve
        writes) showed blind chunked sends still lost most chunks to collisions (only 2/9 rows
        actually stuck, confirmed against a known baseline) even though the transport-level
        "transmitted" count looked fine - the missing piece was never checking for the real
        per-chunk ACK, so failures were invisible. This does.

        Even with that fix, real production sends on 2026-08-19 confirmed a single pass (up to
        `attempts_per_pass` x 20 tries each, back-to-back) does not reliably confirm every chunk -
        two separate real attempts landed at 8/9 and 5/9, both requiring the user to manually
        notice the partial failure and click Send again to clean up the rest. This automates
        exactly that: up to `max_passes` full sweeps, each pass only re-attempting chunks not yet
        confirmed (already-confirmed ones are left alone, not needlessly re-verified), instead of
        relying on the user to notice and retry by hand. Each individual attempt within a pass
        uses a smaller `attempts_per_pass` (8, not the old single-pass 20) so a genuinely stuck
        chunk still surfaces a result in bounded time rather than the whole operation being
        dominated by one hard-luck chunk exhausting a huge budget in a single stretch - spreading
        the same total attempt budget (8 x 5 = 40, more than the old 20) across multiple passes
        separated by real wall-clock time gives contention a chance to genuinely clear between
        attempts, rather than all 20 tries landing in the same bad window."""
        logger.warning("chunked write requested: %d values at %d = %s", len(values), start_address, values)
        total_chunks = (len(values) + chunk_size - 1) // chunk_size
        chunk_specs = [
            (start_address + offset, values[offset:offset + chunk_size])
            for offset in range(0, len(values), chunk_size)
        ]
        confirmed = [False] * total_chunks

        for pass_num in range(1, max_passes + 1):
            outstanding = [i for i in range(total_chunks) if not confirmed[i]]
            if not outstanding:
                break
            logger.warning("chunked write pass %d: %d/%d chunks still outstanding", pass_num, len(outstanding), total_chunks)
            for i in outstanding:
                chunk_start, chunk_values = chunk_specs[i]
                frame = _build_write_registers_frame(chunk_start, chunk_values)
                expected_ack = _build_write_registers_ack(chunk_start, len(chunk_values))
                result = self._send_verified(
                    frame, expected_ack,
                    description=f"write chunk ({chunk_values} at {chunk_start}) pass {pass_num}",
                    max_attempts=attempts_per_pass,
                )
                confirmed[i] = result.ok
                if on_progress is not None:
                    on_progress(sum(confirmed), total_chunks, pass_num)

        chunks_ok = sum(confirmed)
        failed_at = [chunk_specs[i][0] for i in range(total_chunks) if not confirmed[i]]
        if chunks_ok == total_chunks:
            logger.warning("chunked write (%d values at %d): all %d chunks ACK-confirmed after %d pass(es)", len(values), start_address, total_chunks, pass_num)
            return WriteResult(ok=True, attempts=total_chunks, detail=f"all {total_chunks} chunks ACK-confirmed by the board (took {pass_num} pass(es))", independently_verified=True)
        return WriteResult(
            ok=False, attempts=total_chunks,
            detail=f"only {chunks_ok}/{total_chunks} chunks confirmed after {max_passes} passes - failed at register(s) {failed_at}. Range is now partially updated - retry.",
            independently_verified=False,
        )

    def _send_verified(self, frame: bytes, expected_ack: bytes, description: str, max_attempts: int = 20, on_progress=None) -> WriteResult:
        """Transmits `frame` and scans the immediate response window for `expected_ack` - the
        board's own direct write-ACK, confirmed live 2026-08-18 (see _build_write_registers_ack)
        and the general mechanism every write (coil or register) now uses instead of relying on
        the passive listener overhearing a later status poll - that only works while the
        touchscreen is present generating that traffic. Retries the SAME write - not just
        fire-and-hope - until the ACK is actually seen or max_attempts is exhausted.
        `on_progress`, if given, is called as `on_progress(attempt, max_attempts)` after each
        attempt - see _transmit_and_retry_locked."""
        result, attempts = self._transmit_and_retry(
            frame, lambda resp: True if expected_ack in resp else None, description, max_attempts,
            kind="write", on_progress=on_progress,
        )
        if result is not None:
            logger.warning("%s: ACK confirmed on attempt %d", description, attempts)
            return WriteResult(ok=True, attempts=attempts, detail="ACK confirmed", independently_verified=True)
        logger.warning("%s: FAILED - no confirming ACK after %d attempts", description, attempts)
        return WriteResult(ok=False, attempts=attempts, detail="never saw a confirming ACK", independently_verified=False)

    def _send_active_read(self, func_code: int, start_address: int, count: int, description: str, max_attempts: int = 20, on_progress=None) -> Optional[list]:
        """Actively requests data directly from the board (FC3 holding registers or FC1 coils)
        instead of waiting to passively overhear the touchscreen's own polls - works whether or
        not the touchscreen is present, using the exact same proven collision-avoidance mechanism
        as writes (2026-08-19: the touchscreen's power may be removed entirely, at which point
        passive listening has nothing left to overhear at all). Returns parsed register ints (FC3)
        or coil bools (FC1/FC4), or None if no valid response arrived within max_attempts.
        `on_progress`, if given, is called as `on_progress(attempt, max_attempts)` after each
        attempt - see _transmit_and_retry_locked."""
        expected_byte_count = count * 2 if func_code in (3, 4) else (count + 7) // 8
        frame = _build_read_frame(func_code, start_address, count)
        values, attempts = self._transmit_and_retry(
            frame, lambda resp: _parse_read_response(resp, func_code, expected_byte_count), description, max_attempts,
            kind="read", on_progress=on_progress,
        )
        if values is None:
            logger.warning("%s: FAILED - no valid response after %d attempts", description, attempts)
        return values

    def _transmit_and_retry(self, frame: bytes, validate_response, description: str, max_attempts: int, kind: str = "read", on_progress=None):
        """Shared low-level mechanism behind both verified writes and active reads: pause the
        passive reader, wait for a quiet gap, transmit `frame`, capture whatever comes back, and
        hand it to `validate_response(resp_bytes)` - a callable returning a non-None result on
        success or None to keep retrying. Retries with randomized jitter (see
        WRITE_RETRY_JITTER_RANGE_S - a fixed delay can fall into lockstep with the touchscreen's
        own polling cycle) until validated or max_attempts is exhausted. Returns
        (result_or_None, attempts_used).

        Holds _active_request_lock for the whole call (all attempts) - the pause/resume signaling
        below is only safe for one caller at a time; see the lock's own comment for why that
        stopped being automatically true once the active-poll loop started calling this too.

        `kind` ("read" or "write") only feeds the Stage 14 diagnostic counters below - it has no
        effect on the retry/collision-avoidance behavior itself."""
        with self._active_request_lock:
            result, attempts = self._transmit_and_retry_locked(frame, validate_response, description, max_attempts, on_progress=on_progress)
        with self._diag_lock:
            if kind == "write":
                self._diag_write_attempts += 1
                if result is not None:
                    self._diag_write_successes += 1
                else:
                    self._diag_write_failures += 1
            else:
                self._diag_read_attempts += 1
                if result is not None:
                    self._diag_read_successes += 1
                else:
                    self._diag_read_failures += 1
            self._diag_retry_count += max(0, attempts - 1)
        return result, attempts

    def _transmit_and_retry_locked(self, frame: bytes, validate_response, description: str, max_attempts: int, on_progress=None):
        """`on_progress`, if given, is called as `on_progress(attempt, max_attempts)` after every
        attempt (success or failure) - lets a slow read under heavy bus contention show real
        progress instead of a static "Reading..." with zero feedback for however long the whole
        call takes (same reasoning as write_registers' own on_progress for chunked writes -
        confirmed live 2026-08-22 that an active read stalling for tens of seconds under
        contention looked indistinguishable from a genuine hang without this)."""
        if not self.is_connected:
            return None, 0  # review finding #17: dead reader / closed port - don't burn the retry budget
        for attempt in range(1, max_attempts + 1):
            if self._stop_event.is_set():
                return None, attempt
            self._pause_event.set()
            if not self._paused_ack.wait(timeout=1.0):
                self._pause_event.clear()
                if on_progress is not None:
                    on_progress(attempt, max_attempts)
                continue
            try:
                with self._port_lock:
                    if self._watch_for_live_quiet_gap(WRITE_WAIT_FOR_GAP_TIMEOUT_S):
                        wire_start = time.monotonic()
                        self._serial.write(frame)
                        self._serial.flush()
                        original_timeout = self._serial.timeout
                        self._serial.timeout = 0.3
                        try:
                            resp = self._serial.read(300)
                        finally:
                            self._serial.timeout = original_timeout
                        result = validate_response(resp)
                        if result is not None:
                            self._record_response_time(time.monotonic() - wire_start)
                            if on_progress is not None:
                                on_progress(attempt, max_attempts)
                            return result, attempt
                        logger.debug("%s attempt %d: no valid response (%s)", description, attempt, resp.hex())
            finally:
                self._resume_event.set()
                self._pause_event.clear()
                self._paused_ack.clear()
            if on_progress is not None:
                on_progress(attempt, max_attempts)
            time.sleep(random.uniform(*WRITE_RETRY_JITTER_RANGE_S))
        return None, max_attempts

    def _watch_for_live_quiet_gap(self, timeout_s: float) -> bool:
        """Must be called with the background reader already paused AND self._port_lock held.
        Actively polls the serial port itself for a sustained empty period, rather than trusting
        a timestamp from before the pause - that timestamp stops updating the instant the
        background reader stops reading, so it goes stale immediately and can't tell us anything
        about what's happening on the wire *during* the pause, which is exactly when it matters.
        Returns with the port left quiet (nothing unread) if it returns True."""
        original_timeout = self._serial.timeout
        self._serial.timeout = MIN_QUIET_GAP_S
        try:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                chunk = self._serial.read(256)
                if not chunk:
                    return True  # a full MIN_QUIET_GAP_S passed with nothing arriving
                # bytes arrived - the line is busy, discard and keep watching
            return False
        finally:
            self._serial.timeout = original_timeout

    # ---------------- internal background reader ----------------

    def _run(self) -> None:
        buf = bytearray()
        while not self._stop_event.is_set():
            if self._pause_event.is_set():
                self._paused_ack.set()
                self._resume_event.wait(timeout=5.0)
                self._resume_event.clear()
                buf.clear()
                continue

            try:
                with self._port_lock:
                    chunk = self._serial.read(256)
            except (serial.SerialException, OSError, AttributeError) as exc:
                if not self._stop_event.is_set():
                    self._mark_reader_failed("passive reader", exc)
                break
            if chunk:
                with self._data_lock:
                    self._last_byte_time = time.monotonic()
                buf.extend(chunk)
                buf = self._process_buffer(buf)
            if len(buf) > 4096:
                buf = buf[-256:]

    def _process_buffer(self, buf: bytearray) -> bytearray:
        pos = 0
        while pos < len(buf) - 4:
            if buf[pos] != STATION_ADDRESS:
                pos += 1
                continue
            consumed = self._try_parse_at(buf, pos)
            pos += consumed if consumed else 1
        return buf[pos:]

    def _try_parse_at(self, buf: bytearray, pos: int) -> int:
        func = buf[pos + 1]

        if func in (3, 4):
            # FC4 (read input registers) is included here as a speculative experiment - every
            # other register range on this bus (temps, PID, fan-timing) reads back fine over FC3,
            # but curve/recipe data (100-171) never once produced a response over FC3, even
            # passively, even from the touchscreen's own traffic, even across a full machine
            # power cycle (writes to the same range are confirmed working - see write_registers).
            # Register value layout is identical either way, so routing both through the same
            # handler is safe regardless of which function code turns out to be the real one.
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

        if func == 1:
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
        count = byte_count // 2
        regs = [int.from_bytes(frame[3 + i * 2:5 + i * 2], "big") for i in range(count)]
        with self._data_lock:
            if count == 8:
                # Status-screen poll: registers 20-27. First 4 are the real x10-precision temps.
                self._latest_temps = tuple(v / 10.0 for v in regs[:4])
                self._last_update_time = time.monotonic()
                self._last_temps_update_time = self._last_update_time
            elif count == 4:
                # Seen on non-status screens (e.g. advanced params, curve editor): registers
                # 24-27, the rounded-integer duplicate of the same 4 zones - NOT x10 scaled.
                # Lower precision than the count==8 case, but keeps live temps updating no
                # matter which screen the touchscreen is currently showing.
                self._latest_temps = tuple(float(v) for v in regs)
                self._last_update_time = time.monotonic()
                self._last_temps_update_time = self._last_update_time
            elif count in (72, 82):
                # 82 = the curve range plus the trailing advanced-params block (only the first
                # 72 are curve data); 72 = a narrower read of just the curve range.
                self._update_curve_locked(regs[:72])
            elif count == PID_COUNT:
                # Advanced Parameters screen poll: start=200, qty=33.
                self._latest_pid = regs
                self._pid_seen_count += 1
                self._last_pid_time = time.monotonic()
            elif count == FAN_TIMING_POLL_COUNT:
                # Advanced Parameters screen poll: start=172, qty=10. First 4 are fan/vacuum
                # timing. Of the next 4 (176-179): Upper (176) and Lower (177) are CONFIRMED live
                # 2026-09-07 - a real touchscreen edit of Up to 3.3 and Low to 7.7, captured on
                # this exact poll, landed at 176=33 and 177=77 (the same x10 scale as everywhere
                # else on this machine) and, unlike IR, held steady rather than reverting. IR
                # (178) and the unlabeled/Probe slot (179) are NOT usable the same way: a real
                # touchscreen edit of IR consistently reverted to 0 within a couple of seconds on
                # the touchscreen's own display, with no corresponding change ever seen on this
                # wire for 178 across two separate attempts - most likely explanation is the
                # "Calibrate IR" auto-routine (see docs/02-hmi-screen-flow-proposal.md's back-
                # mounted-thermocouple finding) continuously re-asserts its own computed value
                # over any manual entry, rather than 178 being the wrong address. Practical
                # consequence: IR temperature compensation cannot be sustained via this register
                # by any means (touchscreen or Modbus) - see calibration.py / main_window.py's
                # planned HMI-side correction offset for the actual fix. Last 2 (180-181) still
                # unidentified, discarded.
                self._latest_fan_timing = regs[:FAN_TIMING_COUNT]
                comp_offset = ADDR_TEMP_COMP_UPPER - ADDR_VACUUM_TIME
                self._latest_temp_comp = regs[comp_offset:comp_offset + TEMP_COMP_COUNT]
                self._fan_timing_seen_count += 1
                self._last_fan_timing_time = time.monotonic()

    def _update_curve_locked(self, regs: list[int]) -> bool:
        """Accepts a 72-value curve capture (passive or active) as the new _latest_curve, unless
        it fails a plausibility check - see _curve_regs_look_plausible. Modbus RTU responses carry
        no address information (a running theme throughout this file), and this address range's
        byte-count (144/164 bytes for 72/82 registers) is apparently not unique on this bus:
        confirmed live 2026-08-19, a captured "curve" response with the right byte-count was
        proven NOT to be the machine's actual loaded profile (independently checked against the
        touchscreen's Current Curve screen at the same moment) - Rate and Hold-time rows showed
        values around 290, wildly outside anything a real profile has ever shown (always
        single/low-double digits). Returns True if accepted, False if rejected as implausible -
        callers must not treat a rejected capture as a successful read."""
        if not _curve_regs_look_plausible(regs):
            logger.warning(
                "Curve data REJECTED as implausible (right byte-count, but not real curve data - "
                "likely an unrelated response on the bus caught by coincidence): regs[100-171]=%s",
                regs,
            )
            return False
        profile = Profile()
        for attr_idx, attr_name in enumerate(CURVE_ATTRIBUTES):
            for section_idx in range(CURVE_SECTIONS):
                setattr(profile.sections[section_idx], attr_name, regs[attr_idx * CURVE_SECTIONS + section_idx])
        self._latest_curve = profile
        self._curve_seen_count += 1
        self._last_curve_time = time.monotonic()
        logger.warning("Curve data captured (#%d): regs[100-171]=%s", self._curve_seen_count, regs)
        return True

    def _handle_coil_response(self, frame: bytes, byte_count: int) -> None:
        if byte_count != 4:
            return
        changed = []
        with self._data_lock:
            for byte_idx in range(byte_count):
                byte_val = frame[3 + byte_idx]
                for bit_idx in range(8):
                    coil_addr = COIL_RUNNING + byte_idx * 8 + bit_idx
                    new_val = bool(byte_val & (1 << bit_idx))
                    old_val = self._latest_coils.get(coil_addr)
                    if old_val is not None and old_val != new_val:
                        changed.append((coil_addr, old_val, new_val))
                    self._latest_coils[coil_addr] = new_val
            self._last_update_time = time.monotonic()
            self._last_coils_update_time = self._last_update_time
        # Diagnostic, 2026-08-19: logs every coil bit flip with a wall-clock timestamp so a
        # physical-switch toggle can be correlated against exactly which unidentified coil
        # changed - the same method that identified Vacuum/Cooling, just made continuous
        # instead of a one-off before/after capture comparison.
        for addr, old, new in changed:
            logger.warning(
                "%s COIL CHANGE: address=%d (tag M%d) %s -> %s",
                time.strftime("%H:%M:%S"), addr, addr + 1, old, new,
            )
