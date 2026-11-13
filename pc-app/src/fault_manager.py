"""Central fault system - the "hard" counterpart to alerts.py's "soft" advisory alerts.

alerts.py stays exactly as it is (stuck sensor, sudden jump, wetting temp, section deviation) -
those are genuinely advisory, meant to inform an operator who's paying attention, not to act on
their own. Faults raised here are different in kind, not just severity: a CRITICAL or EMERGENCY
fault means the software's own hard safety_limits.SafetyLimits were violated, and the machine gets
stopped automatically (see main_window.py/web_server.py's fault-checking poll-tick logic) rather
than just shown in a list. This mirrors the project's repeated real lesson (documented at length
in docs/06-modbus-register-map.md) that a write's ACK only proves the transaction happened, not
that the result is what was intended - the same distinction applies here: an alert only informs,
a fault acts.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

# History is a record for the operator to scroll back through, not an unbounded audit trail -
# capped so a long-running deployment (the Pi's web app can run for months) doesn't grow this
# forever. Active (unacknowledged) faults are unaffected - only the historical record is capped.
_MAX_HISTORY = 500


class FaultSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"       # stops the run automatically if one is active
    EMERGENCY = "emergency"     # stops the run automatically; also blocks Start until acknowledged


class FaultType(Enum):
    SENSOR_INVALID = "sensor_invalid"
    COMMUNICATION_LOST = "communication_lost"
    TEMPERATURE_HIGH = "temperature_high"
    RAMP_RATE_HIGH = "ramp_rate_high"
    RUN_DURATION_EXCEEDED = "run_duration_exceeded"
    PROFILE_INVALID = "profile_invalid"
    HEATER_NOT_RESPONDING = "heater_not_responding"
    HEATER_OVERSHOOT = "heater_overshoot"
    HEATER_STUCK_ON = "heater_stuck_on"
    SAFETY_LIMIT_EXCEEDED = "safety_limit_exceeded"
    # Added 2026-09-07 per external feature-gap review - every run's telemetry lands on disk
    # (data_logger.py), and nothing previously watched whether that disk was filling up; a full
    # disk degrades or silently breaks logging mid-run. See data_logger.check_disk_space().
    # Deliberately WARNING-only, never CRITICAL/EMERGENCY - low disk space isn't itself physically
    # dangerous, and auto-stopping a real thermal profile mid-run over it would be worse than the
    # problem it's warning about.
    DISK_SPACE_LOW = "disk_space_low"
    # Added 2026-09-08 per external feature-gap review, second pass - see
    # machine_config_snapshot.py. The touchscreen can write PID/fan-timing/temp-compensation
    # registers directly, completely bypassing this app - if that happens, nothing else would ever
    # notice. Deliberately WARNING-only: a configuration change isn't itself dangerous (every value
    # still passes through the board's own control loop and this app's safety_limits checks the
    # next time a profile is sent), it's just something the operator would want to know happened,
    # especially if they didn't do it themselves.
    CONFIG_DRIFT_DETECTED = "config_drift_detected"
    # Added 2026-09-08 per external feature-gap review, second pass - see system_health.py. The
    # Pi runs this app 24/7 unattended as a systemd service; nothing previously watched its own
    # CPU load or memory the way check_disk_space() already watches disk. WARNING-only for the
    # same reason as DISK_SPACE_LOW - host resource pressure isn't itself a physical danger, and
    # auto-stopping a real thermal profile over it would be worse than the problem.
    SYSTEM_HEALTH_DEGRADED = "system_health_degraded"


@dataclass(eq=False)
class Fault:
    fault_type: FaultType
    severity: FaultSeverity
    description: str
    timestamp: float = field(default_factory=time.monotonic)
    reading_snapshot: dict = field(default_factory=dict)
    profile_name: str = ""
    requires_ack: bool = True
    auto_action_taken: str = ""
    acknowledged: bool = False


class FaultManager:
    """Tracks active (unacknowledged) and historical faults. Not thread-safe on its own - callers
    already serialize access to this the same way they serialize access to everything else shared
    with the poll loop (see main_window.py's _poll / web_server.py's _poll_loop, both single-
    threaded with respect to this kind of state)."""

    def __init__(self):
        self._active: list[Fault] = []
        self._history: deque[Fault] = deque(maxlen=_MAX_HISTORY)

    def raise_fault(self, fault: Fault) -> Fault:
        self._active.append(fault)
        self._history.append(fault)
        return fault

    def acknowledge(self, fault: Fault) -> None:
        fault.acknowledged = True
        if fault in self._active:
            self._active.remove(fault)

    def acknowledge_all(self) -> None:
        for fault in list(self._active):
            self.acknowledge(fault)

    @property
    def active_faults(self) -> list[Fault]:
        return list(self._active)

    @property
    def history(self) -> list[Fault]:
        return list(self._history)

    def has_unacknowledged_critical(self) -> bool:
        return any(f.severity in (FaultSeverity.CRITICAL, FaultSeverity.EMERGENCY) for f in self._active)
