"""Formal machine state model, shared by both frontends so neither one independently guesses
what state the machine is in (a real risk before this existed - the desktop app and web app each
derived "is it safe to edit the profile right now" from their own ad-hoc checks).

State is DERIVED FRESH from observed data every time - not tracked as internal "what we think
happened" bookkeeping that could drift from reality. This matches the project's own hard-learned
rule (see docs/06-modbus-register-map.md throughout): never fabricate machine state, never
collapse "we wrote it" and "it's confirmed" into one fact. A state machine that remembered its own
transitions instead of re-deriving them every tick would be exactly that kind of fabrication - if
the machine's actual coils disagree with what we last commanded, the derived state must reflect
the coils, not our expectation.

No EMERGENCY_STOP state exists here on purpose: this board has no independently-verified hardware
e-stop mechanism distinct from the ordinary Stop/Running coil (COIL_RUNNING). Inventing a separate
"emergency" state that behaves differently from Stop would be exactly the kind of fabricated
register/behavior meaning this project has repeatedly had to walk back elsewhere (temp
compensation, curve reads). A critical fault while running triggers the same COIL_RUNNING=False
write Stop already uses - see fault_manager.py - just with more urgency in the UI and a required
acknowledgement, not a different physical action.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from modbus_client import LiveReading


class MachineState(Enum):
    DISCONNECTED = "disconnected"     # bus.is_connected is False
    CONNECTING = "connecting"          # a connection attempt is in progress
    IDLE = "idle"                      # connected, not running, no active fault
    PREFLIGHT = "preflight"            # the pre-Start check sequence is actively running
    RUNNING = "running"                # COIL_RUNNING observed True, KEEP not engaged
    PAUSED = "paused"                  # COIL_RUNNING True and KEEP engaged (holding current step)
    COOLING = "cooling"                # COIL_COOLING True while not running (post-run cooldown)
    FAULT = "fault"                    # an unacknowledged critical/emergency fault is active
    # Confirmed live 2026-09-07: a self-tune (PID autotune, COIL_SELFTUNE_*) leaves COIL_RUNNING
    # False the whole time - a real heating action that used to be silently reported as IDLE,
    # which is exactly the kind of "state disagrees with observed coils" fabrication this module's
    # docstring warns against. Genuinely observed data (the self-tune coils), same as every other
    # state here - not a guess.
    SELF_TUNING = "self_tuning"
    # Added 2026-09-07 per external feature-gap review: derive_state() used to return IDLE both
    # when genuinely idle AND when reading is None (the port is open but no reading has EVER
    # arrived since connecting) - the exact "state disagrees with observed data" fabrication this
    # module's own docstring warns against, just not previously caught because "no data yet" looks
    # a lot like "idle" at a glance. UNKNOWN is for exactly this: the port is open (otherwise it'd
    # be DISCONNECTED, which is more specific and still wins) but there is either no reading yet,
    # or the last one is old enough that it no longer describes what the machine is doing right
    # now - communication can silently die (adapter unseated, bus jammed) without the port itself
    # ever closing, and check_communication_outage() only runs while a profile run is active, so
    # nothing else would ever catch a stale reading while idle/preflight/self-tuning.
    UNKNOWN = "unknown"


# Which named UI controls are meaningful to enable in each state. Both apps look up their own
# button IDs against this set instead of each inventing their own enable/disable rules - this is
# the actual point of having a shared state machine at all (see module docstring).
ENABLED_CONTROLS: dict[MachineState, frozenset[str]] = {
    MachineState.DISCONNECTED: frozenset({"reconnect"}),
    MachineState.CONNECTING: frozenset(),
    MachineState.IDLE: frozenset({"start", "send_profile", "send_pid", "reconnect", "vacuum", "cooling"}),
    MachineState.PREFLIGHT: frozenset(),
    MachineState.RUNNING: frozenset({"stop", "keep", "vacuum", "cooling"}),
    MachineState.PAUSED: frozenset({"stop", "keep", "vacuum", "cooling"}),
    MachineState.COOLING: frozenset({"stop", "vacuum", "cooling"}),
    MachineState.FAULT: frozenset({"stop", "acknowledge_fault"}),
    # Deliberately conservative (nothing enabled, like PREFLIGHT/CONNECTING) - this app has no
    # tested "cancel self-tune" flow, and starting a profile or sending PID/vacuum/cooling changes
    # while the board is mid-autotune risks corrupting the tuning process. Reconnect isn't included
    # either since it isn't actually needed here (the bus is already connected - that's how we're
    # observing the self-tune coils at all).
    MachineState.SELF_TUNING: frozenset(),
    # Same reasoning as DISCONNECTED - the one meaningful action when the data can't be trusted
    # is to re-establish a connection that actually produces fresh readings.
    MachineState.UNKNOWN: frozenset({"reconnect"}),
}

# How old the last temps reading is allowed to be before derive_state() stops trusting it and
# reports UNKNOWN instead of whatever the stale reading would otherwise imply. Deliberately a
# fixed structural constant, not a SafetyLimits field - this is "do we know what's happening at
# all", a different concern from the user-tunable enforcement-grade limits in safety_limits.py
# (see that module's own docstring for the advisory-vs-enforcement split this mirrors). 30s
# matches SafetyLimits.max_communication_outage_s's own default, for a consistent "this is how
# stale is too stale" feel across the app, but is intentionally independent of it.
STALE_READING_THRESHOLD_S = 30.0
# Review finding #9 (2026-11-12): the COIL snapshot (running / self-tune / cooling) is a separate reply from the
# temperatures. get_live_reading() fills coils nobody has seen with False, so a fresh temperature reply with an
# absent or stale status reply used to look exactly like an idle machine. More generous than the temperature
# limit because coil replies are polled less often and often need several retries on this contended bus.
COILS_STALE_THRESHOLD_S = 60.0


def is_control_enabled(state: MachineState, control: str) -> bool:
    return control in ENABLED_CONTROLS.get(state, frozenset())


def derive_state(
    reading: Optional[LiveReading],
    is_connected: bool,
    connecting: bool = False,
    active_fault: bool = False,
    preflight_active: bool = False,
    seconds_since_temps_update: Optional[float] = None,
    seconds_since_coils_update: Optional[float] = None,
) -> MachineState:
    """Pure derivation, no side effects and no memory of prior calls - see module docstring for
    why that matters here. Precedence: a fault always wins (safety-relevant, and it's confirmed
    knowledge, not a guess), then connection state (DISCONNECTED is more specific than UNKNOWN and
    still wins), then preflight (also confirmed activity in progress), then - new as of 2026-09-07
    - UNKNOWN if there's no reading yet or the last one is too old to trust (see
    MachineState.UNKNOWN and STALE_READING_THRESHOLD_S), and only then whatever the live coils say.

    `seconds_since_temps_update` should be MachineBus.get_temps_timestamp()-derived real elapsed
    time (`time.monotonic() - get_temps_timestamp()`) whenever `reading` is not None - a real
    LiveReading always implies that timestamp is already nonzero (they're set together in
    MachineBus), so this is never "never" in that case, only "how old". Deliberately the same
    temps-specific freshness signal the ramp-rate safety check already relies on, not a new
    mechanism. Left as the default None, staleness is simply not checked (treated as fresh) - this
    keeps every existing/simpler caller (and test) that doesn't care about staleness working
    unchanged; only a caller that actually passes a real age gets the new protection. The same goes for
    `seconds_since_coils_update` (MachineBus.coil_snapshot_age(); float('inf') = never observed)."""
    if active_fault:
        return MachineState.FAULT
    if connecting:
        return MachineState.CONNECTING
    if not is_connected:
        return MachineState.DISCONNECTED
    if preflight_active:
        return MachineState.PREFLIGHT
    if reading is None:
        return MachineState.UNKNOWN
    if seconds_since_temps_update is not None and seconds_since_temps_update > STALE_READING_THRESHOLD_S:
        return MachineState.UNKNOWN
    if seconds_since_coils_update is not None and seconds_since_coils_update > COILS_STALE_THRESHOLD_S:
        return MachineState.UNKNOWN  # never observed (inf) or too old: Running/Self-tune are NOT known
    if reading.self_tune_upper or reading.self_tune_lower or reading.self_tune_ir:
        return MachineState.SELF_TUNING
    if reading.running and reading.keep:
        return MachineState.PAUSED
    if reading.running:
        return MachineState.RUNNING
    if reading.cooling:
        return MachineState.COOLING
    return MachineState.IDLE
