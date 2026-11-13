"""
Browser-based UI for the LY-R690 monitor/control app - built to run on a Raspberry Pi (or any
machine) so the control panel is reachable from any phone/laptop browser on the LAN, without
needing a dedicated touchscreen. Reuses machine_bus.py/modbus_client.py/alerts.py/data_logger.py/
profile_store.py exactly as-is - none of that has any Qt dependency, so it's the same proven
collision-avoidance and ACK-verified write logic as the desktop app, just with a web frontend
instead of Qt widgets.

For this to actually deliver the reliability win (no collision workarounds needed), the machine
this runs on should be the ONLY Modbus master on the bus - see docs/06-modbus-register-map.md.
Running it alongside the stock touchscreen still works, just with the same collision-handling
behavior as the desktop app (retries, occasional slowness) rather than the clean instant writes
you get with the touchscreen removed.

Run: python web_server.py COM4          (Windows, for local testing)
     python web_server.py /dev/ttyUSB0  (Raspberry Pi / Linux, once deployed)
Then open http://<this machine's IP>:8080 from any browser on the same network.
"""
from __future__ import annotations

import contextlib
import csv
import functools
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from dataclasses import asdict

from datetime import timedelta

from flask import Flask, Response, jsonify, redirect, request, send_from_directory, session

import web_auth
from werkzeug.exceptions import HTTPException

logger = logging.getLogger(__name__)

from machine_bus import MachineBus
from modbus_client import (
    ADDR_CURVE_BASE, COIL_COOLING, COIL_KEEP, COIL_RUNNING, COIL_VACUUM,
    COIL_SELFTUNE_UPPER, COIL_SELFTUNE_LOWER, COIL_SELFTUNE_IR,
    ADDR_PID_BASE, ADDR_PID_UPPER_P, ADDR_PID_UPPER_I, ADDR_PID_UPPER_D,
    ADDR_PID_LOWER_P, ADDR_PID_LOWER_I, ADDR_PID_LOWER_D,
    ADDR_PID_IR_P, ADDR_PID_IR_I, ADDR_PID_IR_D, PID_SEGMENTS,
    ADDR_VACUUM_TIME, ADDR_FAN_STOP_TEMP, ADDR_FAN_STOP_DELAY, ADDR_ADVANCE_WARNING_TIME,
    ADDR_TEMP_COMP_UPPER, TEMP_COMP_COUNT, to_signed16, to_unsigned16,
    CurveSection, Profile,
)
from alerts import AlertConfig, RunMonitor, Severity
from data_logger import RunLogger, LOGS_DIR, list_runs, check_disk_space, mark_run_golden, mark_run_notes, mark_repair_outcome, get_run_summary, delete_run
from self_tune_log import SelfTuneTracker
import maintenance
import process_assistant
from machine_state import MachineState, derive_state, is_control_enabled, ENABLED_CONTROLS
from safety_limits import SafetyLimits, RampTracker, check_reading, check_run_duration, check_communication_outage, load_safety_limits, save_safety_limits, parse_safety_limit_updates
from fault_manager import FaultManager, Fault, FaultType, FaultSeverity
from preflight import run_preflight, curve_send_status, profile_curve_regs
from heater_monitor import HeaterMonitor
from profile_validator import validate_profile
from profile_qualification import compute_qualification_status, QUALIFICATION_THRESHOLD_RUNS
from machine_config_snapshot import save_known_good_config, load_known_good_config, compute_drift
from system_health import get_system_health
import automatic_backup
from system_diagnostics import run_diagnostics, overall_status
import config_change_history
from register_viewer import validate_register_read_request, format_register_rows, SUPPORTED_FUNC_CODES
from profile_simulation import simulate_profile
from port_discovery import list_available_ports, test_connection
from connection_identity import remember_connection, resolve_remembered_port, load_connection_identity
import pid_history
import config_backup
from run_analysis import compare_runs, recommend_for_profile, compute_repair_outcome_stats
import calibration
import notifications
from process_intelligence import compute_zone_deviations
from run_finalization import RunFinalizationTracker, finalize_run
import run_summary_ai
from process_limits import parse_process_limits
import run_recovery
import profile_search
import thermal_model
import profile_store
import profile_import

POLL_INTERVAL_S = 0.5
SELFTUNE_COILS = {"upper": COIL_SELFTUNE_UPPER, "lower": COIL_SELFTUNE_LOWER, "ir": COIL_SELFTUNE_IR}
ROW_ATTRS = [
    "upper_temp", "upper_rate", "upper_hold_time",
    "lower_temp", "lower_rate", "lower_hold_time",
    "ir_temp", "ir_rate", "ir_hold_time",
]

app = Flask(__name__, static_folder="static", template_folder="templates")

# ---- Access control (review finding #13) - see web_auth.py for the model ----
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_NAME="ly_r690_session",
    PERMANENT_SESSION_LIFETIME=timedelta(days=web_auth.SESSION_DAYS),
)
_access_token: str | None = None   # set by _init_auth() at startup; None = not initialised -> remote requests denied


def _init_auth() -> None:
    global _access_token
    if web_auth.auth_disabled():
        logger.warning("AUTHENTICATION IS DISABLED (LY_R690_AUTH=off) - anyone who can reach this port can control the machine.")
        return
    token, secret, created = web_auth.load_or_create_credentials()
    _access_token = token
    app.secret_key = secret
    if created:
        logger.warning("Created %s - access token (shown once here; also stored in that file): %s", web_auth.AUTH_CONFIG_PATH, token)
    else:
        logger.info("Access control enabled; token is in %s", web_auth.AUTH_CONFIG_PATH)


_PUBLIC_PATHS = ("/login", "/static/login.css")


@app.before_request
def _require_auth():
    if web_auth.auth_disabled() or request.path in _PUBLIC_PATHS:
        return None
    if web_auth.is_local_request(request.remote_addr, request.headers):
        return None
    if _access_token is not None:
        if session.get("auth") is True:
            return None
        if web_auth.token_matches(web_auth.bearer_token(request.headers), _access_token):
            return None
    wants_page = request.method == "GET" and "text/html" in request.headers.get("Accept", "") and not request.path.startswith("/api/")
    if wants_page:
        return redirect("/login")
    return jsonify({"ok": False, "detail": "Authentication required.", "auth_required": True}), 401


@app.route("/login", methods=["GET", "POST"])
def login():
    address = request.remote_addr or "?"
    error = ""
    if request.method == "POST":
        wait = web_auth.locked_out(address)
        supplied = request.form.get("token", "") or (request.get_json(silent=True) or {}).get("token", "")
        if wait > 0:
            error = f"Too many failed attempts - try again in {int(wait) + 1}s."
        elif _access_token is not None and web_auth.token_matches(str(supplied).strip(), _access_token):
            web_auth.record_success(address)
            session.clear()
            session["auth"] = True
            session.permanent = True
            return redirect("/")
        else:
            web_auth.record_failure(address)
            logger.warning("failed login from %s", address)
            error = "Wrong token."
    html = web_auth.LOGIN_PAGE.replace("__ERROR__", f'<p class="err">{error}</p>' if error else "")
    return Response(html, status=401 if error else 200, mimetype="text/html")


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect("/login")


@app.errorhandler(Exception)
def _handle_unhandled_exception(exc):
    """Found during a 2026-09-15 full-webapp bug pass: with no error handler registered, an
    unhandled exception in ANY route fell through to Flask's default HTML error page. Every
    button handler in app.js calls postJson()/fetch(...).then(r => r.json()) expecting a JSON
    body - res.json() on an HTML page throws a SyntaxError, uncaught in nearly every one of those
    .then() chains (only a handful of call sites have their own .catch()). From the operator's
    side that's a button click that silently does nothing at all - no alert, no error, the busy
    state just clears - indistinguishable from the RS485 retry issue this same pass fixed, but
    with a completely different (and previously invisible) cause. This guarantees a JSON body
    on every response instead, whatever throws, so postJson()'s own {ok, detail} handling (used
    by nearly every machine-action button) can show a real error like it does for every other
    failure mode."""
    if isinstance(exc, HTTPException):
        return jsonify({"ok": False, "detail": exc.description}), exc.code
    logger.exception("Unhandled exception in %s %s", request.method, request.path)
    return jsonify({"ok": False, "detail": f"Internal error: {exc}"}), 500


bus: MachineBus
monitor = RunMonitor(AlertConfig())
logger_ = RunLogger()

_state_lock = threading.Lock()
# Flask's dev server runs threaded=True - two /api/start (or /api/start + /api/stop) requests
# arriving close together (a double-tap, a client retry after a slow response) would otherwise
# both mutate logger_/COIL_RUNNING concurrently with no ordering guarantee, including orphaning a
# RunLogger file handle (see data_logger.RunLogger.start_run's own guard for the same class of
# bug). Serializing just these two routes is cheap insurance - they're not on any hot path.
_run_control_lock = threading.Lock()
_finalize_claim_lock = threading.Lock()  # see _finalize_current_run (review finding #11)
_latest_state: dict = {}
_alert_log: deque = deque(maxlen=200)  # (seq, severity, message) - SSE clients tail this
_alert_seq = 0
_logging_active = False
_active_profile_name = "none"
_last_connection_error = ""

# ---- safety foundation (state machine / preflight / hard limits / fault manager) ----
# Shared with the desktop app via machine_state.py/safety_limits.py/fault_manager.py/preflight.py
# - no separate safety logic per frontend, same reasoning throughout this file already (reusing
# machine_bus.py/modbus_client.py/alerts.py rather than reimplementing per frontend).
safety_limits = load_safety_limits()
fault_manager = FaultManager()
heater_monitor = HeaterMonitor()
webhook_config = notifications.load_webhook_config()
ollama_config = profile_search.load_ollama_config()
_active_search_progress = None  # a plain status string while a profile search is in flight -
# same idea as _active_read_progress/_active_write_progress, surfaced through the existing SSE
# stream. A profile search runs several web fetches plus one local-LLM call per page, which can
# genuinely take minutes - the same "looks like a hang without feedback" problem as everything
# else this session, just on a much longer timescale.
_run_tracker = None       # RunFinalizationTracker for whatever run is currently active, if any
_last_sent_curve_regs = None  # the 72 curve registers of the last /api/curve/send that succeeded
# this process - used only for the pre-Start "you never sent this" reminder (see
# preflight.curve_send_status; provenance only, not a claim about the board's actual curve)
_current_log_path = None  # Path returned by logger_.start_run() for the currently active run
_run_process_limits = None  # process_limits.ProcessLimits for the profile actually sent, or None
_interrupted_run_notice = None  # set once at startup if run_recovery finds a leftover marker
_active_read_progress = None  # {"label": str, "attempt": int, "max_attempts": int} while an
# active read (PID/fan-timing) is in flight on some other request's thread - surfaced through the
# existing SSE stream so a slow read under bus contention (confirmed live 2026-08-22: tens of
# seconds under real contention) shows real progress instead of a client left guessing whether
# the request is still alive at all.
_active_write_progress = None  # same idea as _active_read_progress, for coil writes (Start/Stop/
# Vacuum/Cooling) - confirmed live 2026-08-22 these can take 20-40+ seconds across retry passes
# under contention, indistinguishable from a hang without this. {"label", "attempt",
# "max_attempts", "pass_num"}.
_preflight_active = False
_prior_reading = None
_prior_reading_time = None
_run_start_time = None
_active_violation_names: set = set()

# ---- 2026-11-12 code-review fixes: command gating, protective-stop retry, restart reservation ----
# Held only for the few microseconds it takes to decide whether a machine command may begin and to
# register it as in flight - never while talking to the machine.
_command_gate_lock = threading.Lock()
_inflight_ops = 0            # machine-affecting commands (start, config sends, self-tune) currently running
_restart_pending = False     # a restart has been accepted: every other command is refused until it happens
# A protective stop (critical fault while heating) that was NOT confirmed. Retried every tick interval until
# the machine is OBSERVED not heating - see _protective_shutdown / _check_safety_limits.
_protective_stop_pending = False
_protective_retry_at = 0.0
_protective_fault = None
_PROTECTIVE_RETRY_INTERVAL_S = 10.0
# Per-step failure counts for the poll tick (review finding #4) and when the safety check last completed.
_step_failures: dict = {}
_last_safety_check_ok_at = 0.0
_was_extracting = False  # VACUUM/KEEP engaged during a run last tick - see _check_safety_limits
# Separate from _prior_reading/_prior_reading_time (which track every poll tick, used for
# run-start-time bookkeeping) - this tracks only genuinely fresh temperature samples, for the
# ramp-rate check specifically. See safety_limits.RampTracker's docstring for why conflating
# "time since last poll tick" with "time since temps last actually changed" produced a real
# false-positive auto-stop (reported live: 1641C/min for a normal ~14C change).
_ramp_tracker = RampTracker()
# Tracks estimate_current_section()'s last value so _check_safety_limits can detect a section
# transition and reset _ramp_tracker right at that boundary - see RampTracker's docstring
# ("Third, related fix") for why.
_last_ramp_section_index = None
# Real bug found live 2026-09-14: neither a profile completing on its own (the controller's own
# curve-sequencer reaches the end and the running coil drops by itself) nor the touchscreen's own
# physical Stop button were ever detected by this app - only this app's OWN /api/stop and a fault
# auto-stop were. That meant no "run completed" notification, the CSV logger kept appending rows
# forever after the run genuinely ended, and run_recovery's marker was never cleared - the same
# "left in limbo, flagged INTERRUPTED next startup" bug already fixed for faults, just for a
# third, more common way a run ends. See _check_run_ended_externally().
_run_end_watch_was_running = False
# The section estimate from the last tick WHILE running=True - deliberately separate from
# _last_ramp_section_index above, which the ramp tracker resets right on this same running->not-
# running transition (for an unrelated reason - see _check_safety_limits) and would already be
# None by the time _check_run_ended_externally runs this same tick. Used only for an honest
# best-effort guess at HOW a run ended (see that function) - reset back to None once consumed so
# it can never leak into the next run's classification.
_last_active_section_index = None
_last_active_section_total = None
# Snapshot of _run_start_time taken every tick WHILE running=True, for the same reason as the
# section-index fields above: _check_safety_limits (called earlier in the same poll tick) already
# clears the real _run_start_time to None the instant reading.running goes False, so reading it
# directly from _check_run_ended_externally on the drop tick would always compute duration_s=0 -
# a real bug caught in review before this ever shipped. Reset to None once consumed.
_last_active_run_start_time = None
# Disk-space guard (2026-09-07, external feature-gap review) - see data_logger.
# check_disk_space()'s docstring and _check_disk_space() below for why this is throttled and
# only raises a fault on an actual level change.
_last_disk_level = None
_last_disk_check_time = 0.0
_DISK_CHECK_INTERVAL_S = 60.0
# Self-tune session logging (2026-09-07, external feature-gap review, the maintenance-tracking
# item) - see self_tune_log.py's module docstring for why a real self-tune session previously
# left no trace anywhere once it ended.
_self_tune_tracker = SelfTuneTracker()
# Configuration drift detection (2026-09-08, external feature-gap review, second pass) - see
# machine_config_snapshot.py. Uses PASSIVE observation (not an active read every tick), so it only
# ever notices drift once the touchscreen happens to have shown the Advanced Parameters screen
# recently - a deliberate tradeoff against adding load to an already-contended bus just for
# background monitoring; the "Check now" button does a real active read for an immediate answer.
_last_config_drift_state = None
_last_config_drift_check_time = 0.0
_CONFIG_DRIFT_CHECK_INTERVAL_S = 60.0
# System health guard (2026-09-08, external feature-gap review, second pass) - see
# system_health.py. Same throttle/dedup pattern as the disk-space guard.
_last_system_health_level = None
_last_system_health_check_time = 0.0
_SYSTEM_HEALTH_CHECK_INTERVAL_S = 60.0
# Automatic backups (2026-09-08, external feature-gap review, second pass) - see
# automatic_backup.py's module docstring. This throttle is purely to avoid a directory listing
# every poll tick, not a dedup - there's no fault raised here at all.
_last_auto_backup_check_time = 0.0
_AUTO_BACKUP_CHECK_INTERVAL_S = 3600.0


def _set_connection_error(message: str) -> None:
    global _last_connection_error
    _last_connection_error = message


def _profile_from_json(data: dict) -> Profile:
    sections = [CurveSection(**s) for s in data["sections"]]
    return Profile(name=data.get("name", "Untitled"), sections=sections)


def _profile_to_json(profile: Profile) -> dict:
    return {"name": profile.name, "sections": [asdict(s) for s in profile.sections]}


def _reading_to_json(reading) -> dict:
    if reading is None:
        return {}
    return {
        "upper_temp": reading.upper_temp, "lower_temp": reading.lower_temp,
        "infrared_temp": reading.infrared_temp, "probe_temp": reading.probe_temp,
        "running": reading.running, "vacuum": reading.vacuum,
        "cooling": reading.cooling, "keep": reading.keep,
    }


def _pid_regs_to_json(regs: list[int]) -> dict:
    """regs is the raw 33-register block starting at ADDR_PID_BASE (200) - see
    modbus_client.ADDR_PID_* for the layout this slices."""
    def seg(addr):
        i = addr - ADDR_PID_BASE
        return regs[i:i + PID_SEGMENTS]

    return {
        "upper": {"p": seg(ADDR_PID_UPPER_P), "i": seg(ADDR_PID_UPPER_I), "d": seg(ADDR_PID_UPPER_D)},
        "lower": {"p": seg(ADDR_PID_LOWER_P), "i": seg(ADDR_PID_LOWER_I), "d": seg(ADDR_PID_LOWER_D)},
        "ir": {
            "p": regs[ADDR_PID_IR_P - ADDR_PID_BASE],
            "i": regs[ADDR_PID_IR_I - ADDR_PID_BASE],
            "d": regs[ADDR_PID_IR_D - ADDR_PID_BASE],
        },
    }


def _pid_json_to_regs(data: dict) -> list[int]:
    regs = [0] * (ADDR_PID_IR_D - ADDR_PID_BASE + 1)

    def put(addr, values):
        i = addr - ADDR_PID_BASE
        regs[i:i + len(values)] = values

    put(ADDR_PID_UPPER_P, data["upper"]["p"])
    put(ADDR_PID_UPPER_I, data["upper"]["i"])
    put(ADDR_PID_UPPER_D, data["upper"]["d"])
    put(ADDR_PID_LOWER_P, data["lower"]["p"])
    put(ADDR_PID_LOWER_I, data["lower"]["i"])
    put(ADDR_PID_LOWER_D, data["lower"]["d"])
    regs[ADDR_PID_IR_P - ADDR_PID_BASE] = data["ir"]["p"]
    regs[ADDR_PID_IR_I - ADDR_PID_BASE] = data["ir"]["i"]
    regs[ADDR_PID_IR_D - ADDR_PID_BASE] = data["ir"]["d"]
    return regs


def _fan_timing_regs_to_json(regs: list[int]) -> dict:
    return {
        "vacuum_time": regs[0],
        "fan_stop_temp": regs[1] / 10.0,
        "fan_stop_delay": regs[2],
        "advance_warning_time": regs[3],
    }


def _fan_timing_json_to_regs(data: dict) -> list[int]:
    return [
        int(data["vacuum_time"]),
        round(float(data["fan_stop_temp"]) * 10),
        int(data["fan_stop_delay"]),
        int(data["advance_warning_time"]),
    ]


def _temp_comp_regs_to_json(regs: list[int]) -> dict:
    """regs are SIGNED (two's complement) - see modbus_client.ADDR_TEMP_COMP_*."""
    upper, lower, ir, probe = (to_signed16(r) / 10.0 for r in regs)
    return {"upper": upper, "lower": lower, "ir": ir, "probe": probe}


def _temp_comp_json_to_regs(data: dict) -> list[int]:
    return [to_unsigned16(round(float(data[k]) * 10)) for k in ("upper", "lower", "ir", "probe")]


# ---------------- command gating (review findings #3, #6) ----------------

class _CommandRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _command_refusal(control: str, starts_heating: bool) -> str | None:
    """Server-side rule for whether a machine command may begin, checked immediately before it runs.
    Until 2026-11-12 these rules existed only as disabled buttons in the browser, so a second browser tab,
    a stale page or a direct API call could, for example, start a run on a machine already running (a run
    begun at the touchscreen is invisible to _logging_active) or send a new curve mid-run. `control` is a
    name from machine_state.ENABLED_CONTROLS. Stop is deliberately never gated."""
    if _restart_pending:
        return "The service is about to restart - wait for it to come back and try again."
    state = _current_machine_state(bus.get_live_reading())
    if not is_control_enabled(state, control):
        return (f"Refused: the machine state is '{state.value}', which does not allow this command. "
                f"(Fresh temperature AND status readings are required; Start/Send also need the machine idle with no "
                f"unacknowledged fault.)")
    if bus.config_write_in_progress():
        return "A curve/PID/settings send is still in progress - wait for it to finish."
    if starts_heating:
        incomplete = set(bus.config_incomplete()) & {ADDR_CURVE_BASE, ADDR_PID_BASE}
        if incomplete:
            return ("A previous curve/PID send was only PARTLY applied, so the machine's settings are in a mixed state. "
                    "Re-send it successfully before starting a run.")
    return None


@contextlib.contextmanager
def _command(control: str, starts_heating: bool = False):
    """Checks the gate and registers the command as in flight atomically, so a restart can never be
    accepted between a command's check and its first machine write."""
    global _inflight_ops
    with _command_gate_lock:
        reason = _command_refusal(control, starts_heating)
        if reason is not None:
            raise _CommandRefused(reason)
        _inflight_ops += 1
    try:
        yield
    finally:
        with _command_gate_lock:
            _inflight_ops -= 1


def _machine_command(control: str, starts_heating: bool = False):
    """Route decorator form of _command(): refused commands get HTTP 409 in the same JSON shape the page
    already understands for a failed send."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                with _command(control, starts_heating):
                    return fn(*args, **kwargs)
            except _CommandRefused as exc:
                return jsonify({"ok": False, "attempts": 0, "independently_verified": False, "detail": exc.reason}), 409
        return wrapper
    return decorator


# ---------------- safety foundation ----------------

def _current_machine_state(reading) -> MachineState:
    temps_ts = bus.get_temps_timestamp()
    return derive_state(
        reading, bus.is_connected,
        active_fault=fault_manager.has_unacknowledged_critical(),
        preflight_active=_preflight_active,
        seconds_since_temps_update=(time.monotonic() - temps_ts) if temps_ts > 0.0 else None,
        # Review finding #9: unseen/stale coils are NOT "idle" - Running and Self-tune would be unknown.
        seconds_since_coils_update=bus.coil_snapshot_age(),
    )


def _is_self_tuning(reading) -> bool:
    return bool(reading.self_tune_upper or reading.self_tune_lower or reading.self_tune_ir)


def _protective_shutdown(reading) -> tuple[bool, str]:
    """Best-effort stop of EVERYTHING that can be heating, reporting honestly what was and was not
    confirmed. Returns (stop_confirmed, description-for-the-fault).

    Review findings #1/#2 (2026-11-12): the old code wrote the stop, ignored the result, and recorded
    "Automatically stopped the machine" regardless - and it only ever handled the ordinary Running coil,
    so an over-temperature DURING A SELF-TUNE (which leaves Running False) raised a fault and sent nothing.
    "Confirmed" here means the board ACKed the write; the caller keeps retrying until the machine is
    actually OBSERVED not heating.

    Self-tune: writing its coil OFF is the inverse of the trigger this app already uses, and is the only
    cancel this app knows. Whether the controller really aborts an autotune on that write has NOT been
    verified on hardware, so the wording says so rather than claiming a stop."""
    tuning = [zone for zone in SELFTUNE_COILS if getattr(reading, f"self_tune_{zone}", False)]
    stop_ok = True
    if reading.running:
        stop_ok = bool(bus.write_coil(COIL_RUNNING, False).ok)
    for zone in tuning:
        stop_ok = bool(bus.write_coil(SELFTUNE_COILS[zone], False).ok) and stop_ok
    cooling_ok = True
    if not reading.cooling:  # only add, never force off
        cooling_ok = bool(bus.write_coil(COIL_COOLING, True).ok)
    if not stop_ok:
        return False, ("STOP NOT CONFIRMED - the machine did not acknowledge the stop command and may STILL BE HEATING. "
                       "Retrying automatically; stop it at the machine now.")
    cooling_text = "engaged cooling." if cooling_ok else "engaging cooling could NOT be confirmed."
    if tuning and not reading.running:
        return True, (f"Sent a stop to the self-tune coil(s) ({', '.join(tuning)}) and {cooling_text} Whether that really "
                      f"cancels an autotune on this controller has NOT been verified on hardware - confirm the heaters stopped.")
    return True, f"Automatically stopped the machine and {cooling_text}"


def _check_safety_limits(reading) -> None:
    """Mirrors main_window.py's _check_safety_limits exactly - see machine_state.py's module
    docstring for why this logic lives in shared modules rather than being reimplemented per
    frontend. Dedupes so a persisting violation raises one fault, not one per poll tick.

    The ramp-rate check goes through _ramp_tracker (safety_limits.RampTracker) instead of this
    function's own poll cadence - see that class's docstring for the three related fixes this
    went through live in 2026-09, most recently a real-but-INTENDED section-boundary target jump
    being mistaken for a runaway. This function's own contribution to that third fix: detect a
    section transition via monitor.estimate_current_section() (observed-data only, no profile
    "rate" field involved) and reset the ramp baseline right at that boundary, so the check never
    spans across a legitimate commanded jump."""
    global _prior_reading, _prior_reading_time, _run_start_time, _active_violation_names, _logging_active
    global _last_ramp_section_index, _was_extracting
    global _protective_stop_pending, _protective_retry_at, _protective_fault
    now = time.monotonic()
    heating_now = reading.running or _is_self_tuning(reading)
    if _protective_stop_pending:
        # A previous protective stop was not confirmed. Keep trying until the machine is OBSERVED not
        # heating - the fault dedupe below would otherwise never ask again (review finding #1).
        if not heating_now:
            _protective_stop_pending = False
            if _protective_fault is not None:
                _protective_fault.auto_action_taken = "The machine was then OBSERVED stopped (the stop was retried until it took effect)."
                _protective_fault = None
        elif now >= _protective_retry_at:
            _protective_retry_at = now + _PROTECTIVE_RETRY_INTERVAL_S
            confirmed, text = _protective_shutdown(reading)
            logger.error("protective stop retry: %s", text)
            if confirmed and _protective_fault is not None:
                _protective_fault.auto_action_taken = "Stop ACKed on retry - waiting to observe the machine stopped."
    was_running = _prior_reading.running if _prior_reading else False
    if reading.running and not was_running:
        _run_start_time = now
    if not reading.running:
        _run_start_time = None
    if was_running and not reading.running:
        # Run just ended (2026-09-13, real Discord alert: "Infrared temperature changed 538C/min"
        # right as a run finished) - mirrors main_window.py. IR has very low thermal mass and
        # craters fast the instant its heater cuts off / cooling engages, a real expected cooldown
        # artifact, not a runaway - but with no reset here the ramp tracker's last reading is still
        # the final hot in-run sample, and the very next (now-cooling) tick reads as a huge ramp.
        # estimate_current_section() returns None the instant running goes False, so the section-
        # transition reset below never catches this - it only fires between two non-None indices.
        _ramp_tracker.reset()
        _last_ramp_section_index = None

    # Chip extraction phase (2026-09-10, run_20260910_165928) - mirrors main_window.py: once
    # VACUUM (chip pickup) or KEEP is engaged during a run, the probe thermocouple rides the part
    # being lifted away and its reading stops being a temperature. Skip only its ramp check while
    # that's happening, and reset the baseline on the way out. See check_reading's docstring.
    extracting = reading.running and (reading.vacuum or reading.keep)
    if _was_extracting and not extracting:
        _ramp_tracker.reset()
    _was_extracting = extracting

    # Fourth related ramp-tracker case (see safety_limits.RampTracker's docstring for the first
    # three), confirmed live 2026-09-07: a real IR self-tune (PID autotune) produced legitimate
    # large/fast IR ramps with `running` False the whole time - not a profile run, so
    # estimate_current_section()'s section-transition mechanism can't see it either. Keep
    # resetting the baseline every tick while any self-tune coil is active; the first tick after
    # it ends then starts fresh instead of comparing against a stale pre/during-self-tune reading.
    # skip_full_ramp also covers COOLING (2026-09-13, same incident as above) - active cooling is a
    # deliberate, commanded temperature change across every zone, not a runaway, and the fast-IR-
    # cooldown effect can keep tripping the ramp check tick after tick for as long as cooling stays
    # engaged, not just on the one edge tick the reset above covers.
    self_tuning = reading.self_tune_upper or reading.self_tune_lower or reading.self_tune_ir
    skip_full_ramp = self_tuning or reading.cooling
    if skip_full_ramp:
        _ramp_tracker.reset()
        _last_ramp_section_index = None
        ramp_prior_reading, seconds_since_prior = None, None
    else:
        current_section = monitor.estimate_current_section(reading)
        if current_section is not None and _last_ramp_section_index is not None and current_section != _last_ramp_section_index:
            _ramp_tracker.reset()
        _last_ramp_section_index = current_section

        ramp_prior_reading, seconds_since_prior = _ramp_tracker.observe(
            reading, bus.get_temps_timestamp(), min_interval_s=safety_limits.min_ramp_sample_interval_s,
        )
    violations = check_reading(
        safety_limits, reading, ramp_prior_reading, seconds_since_prior,
        skip_ramp_check=skip_full_ramp, skip_probe_ramp_check=extracting,
    )

    if reading.running and _run_start_time is not None:
        duration_violation = check_run_duration(safety_limits, now - _run_start_time)
        if duration_violation:
            violations.append(duration_violation)
        # Review finding #8: age of the TEMPERATURE data specifically. seconds_since_update() also advances on
        # every coil reply, so with coil traffic still flowing a dead temperature feed never tripped this.
        temps_ts = bus.get_temps_timestamp()
        temps_age = (time.monotonic() - temps_ts) if temps_ts > 0.0 else float("inf")
        outage_violation = check_communication_outage(safety_limits, temps_age)
        if outage_violation:
            violations.append(outage_violation)

    current_names = {v.limit_name for v in violations}
    new_violations = [v for v in violations if v.limit_name not in _active_violation_names]
    _active_violation_names = current_names

    for v in new_violations:
        fault = fault_manager.raise_fault(Fault(
            fault_type=FaultType.SAFETY_LIMIT_EXCEEDED,
            severity=FaultSeverity.CRITICAL,
            description=v.description,
            reading_snapshot={
                "upper": reading.upper_temp, "lower": reading.lower_temp,
                "ir": reading.infrared_temp, "probe": reading.probe_temp,
            },
            profile_name=_active_profile_name,
        ))
        # 2026-09-08 audit: capture into a local ONCE before checking - a bare
        # "if _run_tracker is not None: _run_tracker.record_x()" re-reads the global TWICE, and
        # /api/stop (a different thread - Flask's dev server runs threaded) can set
        # _run_tracker = None in between, crashing this poll-loop thread with
        # "'NoneType' object has no attribute 'record_fault'". Same fix applied at every
        # _run_tracker call site in this file.
        tracker = _run_tracker
        if tracker is not None:
            if fault.severity in (FaultSeverity.CRITICAL, FaultSeverity.EMERGENCY):
                tracker.record_fault()
            else:
                tracker.record_warning()
            tracker.record_event(f"[{fault.severity.value.upper()}] {fault.description}")
        if fault.severity in (FaultSeverity.CRITICAL, FaultSeverity.EMERGENCY):
            notifications.notify_async(webhook_config, "critical_fault", fault.description)
            had_active_run = _logging_active or _run_tracker is not None
            if heating_now:
                # Roadmap's own emergency-handling sequence (Section 37): cut heating, then engage cooling.
                stop_confirmed, description = _protective_shutdown(reading)
                fault.auto_action_taken = description
                if stop_confirmed:
                    notifications.notify_async(webhook_config, "run_failed", f"Run auto-stopped: {fault.description}")
                    if reading.running or had_active_run:
                        _logging_active = False
                        logger_.stop_run()
                        _finalize_current_run()
                else:
                    # Do NOT close the run or claim success: the machine may still be heating. Keep recording
                    # and retry every tick interval; the run closes when the machine is observed stopped
                    # (_check_run_ended_externally) - review finding #1.
                    notifications.notify_async(webhook_config, "run_failed",
                                               f"STOP NOT CONFIRMED after: {fault.description} - CHECK THE MACHINE NOW")
                    _protective_stop_pending = True
                    _protective_retry_at = now + _PROTECTIVE_RETRY_INTERVAL_S
                    _protective_fault = fault
            elif had_active_run:
                # A critical fault is a KNOWN run ending - close it out (write the summary, clear
                # run_recovery's marker) even though the running coil reads False, so it's never
                # wrongly flagged INTERRUPTED at the next startup. Bug found live 2026-09-10
                # (run_20260910_162151): gated on reading.running, so a fault while the coil read
                # False left the run in limbo - no summary, marker never cleared.
                fault.auto_action_taken = "Run ended by this fault (the machine was already not in a running state)."
                notifications.notify_async(webhook_config, "run_failed", f"Run ended by fault: {fault.description}")
                _logging_active = False
                logger_.stop_run()
                _finalize_current_run()

    _prior_reading = reading
    _prior_reading_time = now


def _check_heater_response(reading) -> None:
    """Pattern-based, not a hard numeric limit - see heater_monitor.py's module docstring for why
    these are WARNING faults (shown, don't auto-stop) rather than CRITICAL."""
    fault_type_by_kind = {
        "not_responding": FaultType.HEATER_NOT_RESPONDING,
        "overshoot": FaultType.HEATER_OVERSHOOT,
        "stuck_on": FaultType.HEATER_STUCK_ON,
    }
    for issue in heater_monitor.update(reading, monitor.active_profile, time.monotonic()):
        fault_manager.raise_fault(Fault(
            fault_type=fault_type_by_kind[issue.kind], severity=FaultSeverity.WARNING,
            description=issue.description, profile_name=_active_profile_name,
        ))
        tracker = _run_tracker  # see the safety-limits check above for why this is captured first
        if tracker is not None:
            tracker.record_warning()
            tracker.record_event(f"[WARNING] {issue.description}")


def _check_run_ended_externally(reading) -> None:
    """Catches a run ending by anything OTHER than this app's own /api/stop or a fault this same
    tick already handled - see _run_end_watch_was_running's own comment for the real incident this
    fixes. Runs every tick regardless of whether a run is active; it's a no-op unless it actually
    observes the running->not-running edge with a run still open. Uses a non-blocking
    _run_control_lock.acquire() around the actual finalize (2026-09-15, found during a full-webapp
    bug pass - see the comment at that acquire() call) to avoid racing /api/stop to finalize the
    same run twice.

    Can't perfectly distinguish "the profile finished on its own" from "someone pressed Stop on
    the touchscreen" - both look identical on this bus (the running coil just goes False) - so it
    makes an honest best-effort call from the last section estimate seen before the drop: reaching
    the profile's own final section reads as "reached the end of the profile"; anything earlier
    (or no estimate at all) reads as "ended early", never claiming more than what was observed."""
    global _run_end_watch_was_running, _last_active_section_index, _last_active_section_total, _logging_active
    global _last_active_run_start_time
    was_running = _run_end_watch_was_running
    if reading.running:
        _run_end_watch_was_running = True
        section_index = monitor.estimate_current_section(reading)
        if section_index is not None:
            _last_active_section_index = section_index
            _last_active_section_total = len(monitor.active_profile.sections) if monitor.active_profile else None
        if _run_start_time is not None:
            _last_active_run_start_time = _run_start_time
        return
    if not (was_running and (_run_tracker is not None or _logging_active)):
        _run_end_watch_was_running = False
        return  # no edge this tick, or already closed out by the fault path above

    # /api/stop (a different thread - Flask's dev server runs threaded) holds _run_control_lock
    # for its ENTIRE write_coil()+finalize sequence, which can now legitimately run for many
    # seconds under real bus contention (see write_coil's 2026-09-15 retry-budget increase) -
    # write_coil() updates its local coil cache the instant ANY attempt or the readback confirms,
    # well before /api/stop reaches its own _logging_active=False/finalize code. A poll tick
    # landing in that window would otherwise see reading.running already False with _run_tracker/
    # _logging_active still set, and race /api/stop to finalize the SAME run twice - two separate
    # "run completed" notifications for one Stop press. Try the lock WITHOUT blocking: if /api/stop
    # (or /api/start) is genuinely the cause, it's already holding it, so leave
    # _run_end_watch_was_running set and retry this same edge next tick instead of silently losing
    # it. Blocking here would stall this thread's own safety-limit checks for however long that
    # write takes, which is worse than a fraction-of-a-second delay noticing a run ended.
    if not _run_control_lock.acquire(blocking=False):
        return
    try:
        if not (_run_tracker is not None or _logging_active):
            return  # /api/stop's own path already finalized (and notified about) this run while we waited
        _run_end_watch_was_running = False

        section_index, section_total = _last_active_section_index, _last_active_section_total
        _last_active_section_index = None
        _last_active_section_total = None
        profile_name = _active_profile_name
        run_start_time, _last_active_run_start_time = _last_active_run_start_time, None
        duration_s = (time.monotonic() - run_start_time) if run_start_time is not None else 0.0

        _logging_active = False
        logger_.stop_run()
        summary = _finalize_current_run()
    finally:
        _run_control_lock.release()
    if summary is None:
        return
    if section_index is not None and section_total is not None and section_index >= section_total - 1:
        outcome = "reached the end of the profile"
    elif section_index is not None and section_total is not None:
        outcome = f"ended early - last matched section {section_index + 1} of {section_total} (verify this was intentional)"
    else:
        outcome = "ended (not stopped from this app)"
    _send_run_completed_async(f"Run {outcome}: {profile_name}", profile_name, outcome, summary, duration_s)


def _check_disk_space() -> None:
    """Throttled (see _DISK_CHECK_INTERVAL_S) - only raises a fault on an actual LEVEL CHANGE
    (ok/warning/critical/unknown), in either direction. fault_manager.raise_fault() has no dedup,
    and disk space doesn't change fast enough to need per-tick alerting anyway."""
    global _last_disk_level, _last_disk_check_time
    now = time.monotonic()
    if now - _last_disk_check_time < _DISK_CHECK_INTERVAL_S:
        return
    _last_disk_check_time = now
    status = check_disk_space()
    level = status["level"]
    if level == _last_disk_level:
        return
    _last_disk_level = level
    if level == "ok":
        return  # recovered from warning/critical, or the very first check landed on ok - no fault needed
    if level == "unknown":
        description = f"Could not check disk space for run logs: {status.get('error', 'unknown error')}."
    else:
        description = (
            f"Disk space for run logs is {level} - {status['free_pct']:.1f}% free "
            f"({status['free_bytes'] / (1024**3):.2f} GB of {status['total_bytes'] / (1024**3):.2f} GB)."
        )
    fault_manager.raise_fault(Fault(
        fault_type=FaultType.DISK_SPACE_LOW, severity=FaultSeverity.WARNING, description=description,
    ))
    tracker = _run_tracker  # see _check_safety_limits' first _run_tracker use for why this is captured first
    if tracker is not None:
        tracker.record_warning()
        tracker.record_event(f"[WARNING] {description}")


def _check_config_drift() -> None:
    """Throttled (see _CONFIG_DRIFT_CHECK_INTERVAL_S) - only raises a fault on a transition INTO
    drift (dedup, same pattern as _check_disk_space()). No-op entirely if no known-good config has
    ever been saved, or if the passive listener hasn't observed all three register groups yet."""
    global _last_config_drift_state, _last_config_drift_check_time
    now = time.monotonic()
    if now - _last_config_drift_check_time < _CONFIG_DRIFT_CHECK_INTERVAL_S:
        return
    _last_config_drift_check_time = now

    known_good = load_known_good_config()
    if known_good is None:
        return
    pid, fan_timing, temp_comp = bus.get_latest_pid(), bus.get_latest_fan_timing(), bus.get_latest_temp_comp()
    if pid is None or fan_timing is None or temp_comp is None:
        return

    drift = compute_drift(known_good, pid, fan_timing, temp_comp)
    state = drift["has_drift"]
    if state == _last_config_drift_state:
        return
    _last_config_drift_state = state
    if not state:
        return  # recovered (or the config was re-saved as known-good since the last check)
    all_diffs = drift["pid_diffs"] + drift["fan_timing_diffs"] + drift["temp_comp_diffs"]
    description = "Machine configuration has drifted from the saved known-good reference: " + "; ".join(all_diffs)
    fault_manager.raise_fault(Fault(
        fault_type=FaultType.CONFIG_DRIFT_DETECTED, severity=FaultSeverity.WARNING, description=description,
    ))
    tracker = _run_tracker  # see _check_safety_limits' first _run_tracker use for why this is captured first
    if tracker is not None:
        tracker.record_warning()
        tracker.record_event(f"[WARNING] {description}")


def _check_system_health() -> None:
    """Throttled - only raises a fault on an actual LEVEL CHANGE, same pattern as
    _check_disk_space(). A no-op (never "unknown"-faults) on a platform where system_health.py
    reports unavailable (e.g. this app's occasional Windows dev-PC runs) - that's an expected,
    permanent condition there, not a transient problem worth alerting on."""
    global _last_system_health_level, _last_system_health_check_time
    now = time.monotonic()
    if now - _last_system_health_check_time < _SYSTEM_HEALTH_CHECK_INTERVAL_S:
        return
    _last_system_health_check_time = now
    status = get_system_health()
    if not status["available"]:
        return
    level = status["level"]
    if level == _last_system_health_level:
        return
    _last_system_health_level = level
    if level == "ok":
        return
    description = (
        f"Host system resources are {level} - load average {status['load_avg'][0]:.2f} "
        f"({status['cpu_count']} core(s)), memory {status['mem_used_pct']:.1f}% used."
    )
    fault_manager.raise_fault(Fault(
        fault_type=FaultType.SYSTEM_HEALTH_DEGRADED, severity=FaultSeverity.WARNING, description=description,
    ))
    tracker = _run_tracker  # see _check_safety_limits' first _run_tracker use for why this is captured first
    if tracker is not None:
        tracker.record_warning()
        tracker.record_event(f"[WARNING] {description}")


def _check_automatic_backup() -> None:
    """Throttled purely to avoid a directory listing every poll tick - see
    automatic_backup.py's module docstring."""
    global _last_auto_backup_check_time
    now = time.monotonic()
    if now - _last_auto_backup_check_time < _AUTO_BACKUP_CHECK_INTERVAL_S:
        return
    _last_auto_backup_check_time = now
    try:
        automatic_backup.run_automatic_backup_if_due(safety_limits, monitor.config)
    except OSError:
        pass  # a failed automatic backup shouldn't take down the server - manual export still works


# ---------------- background poll loop (single source of truth for reads/alerts) ----------------

def _poll_loop() -> None:
    """Runs forever on a daemon thread (see main()) - this is the ONLY place live readings turn
    into alerts, faults, run logging, and the SSE state every client polls. 2026-09-08 audit
    finding: this loop previously had NO exception handling at all - a single unhandled exception
    on any one tick (confirmed: a real, if narrow, race existed on _run_tracker - see its call
    sites' own comments) would silently kill this entire daemon thread forever, with the Flask
    server still nominally "up" but with alerting/safety-limit-checking/run-logging/SSE updates
    all permanently frozen from that point on - on a headless Pi, nothing in the UI would ever
    indicate this happened. Wrapping each tick means a bad tick gets logged (visible via
    `journalctl` under systemd) and skipped, not fatal to everything after it."""
    global _alert_seq
    start_time = time.monotonic()
    while True:
        time.sleep(POLL_INTERVAL_S)
        try:
            _run_poll_tick(start_time)
        except Exception:
            logger.exception("Unhandled exception in poll loop tick - skipping this tick, monitoring continues.")


def _guarded(label: str, fn, *args, critical: bool = False) -> bool:
    """Runs one poll-tick step so its failure cannot stop the steps after it (review finding #4).
    Before this, a disk-full error in run logging - which ran BEFORE the safety check - aborted the whole
    tick every tick, so safety enforcement silently stopped while the web page stayed up. A step that keeps
    failing (3 ticks in a row) raises a visible fault; a safety-check failure is CRITICAL."""
    try:
        fn(*args)
    except Exception as exc:
        count = _step_failures.get(label, 0) + 1
        _step_failures[label] = count
        if count in (1, 10) or count % 100 == 0:
            logger.exception("poll tick step %r failed (%d in a row)", label, count)
        if count == 3:
            fault_manager.raise_fault(Fault(
                fault_type=FaultType.SAFETY_LIMIT_EXCEEDED,
                severity=FaultSeverity.CRITICAL if critical else FaultSeverity.WARNING,
                description=(f"Monitoring step '{label}' keeps failing ({type(exc).__name__}: {exc}). "
                             + ("Safety limits are NOT being checked - treat the machine as unmonitored."
                                if critical else "Other monitoring continues.")),
                reading_snapshot={},
                profile_name=_active_profile_name,
            ))
        return False
    _step_failures.pop(label, None)
    return True


def _process_alerts(reading) -> None:
    global _alert_seq
    for alert in monitor.check(reading):
        _alert_seq += 1
        _alert_log.append((_alert_seq, alert.severity.value, alert.message))
        tracker = _run_tracker  # see _check_safety_limits' first _run_tracker use for why this is captured first
        if tracker is not None:
            tracker.record_warning()


def _log_reading(reading) -> None:
    if _logging_active:
        logger_.log(reading)


def _run_poll_tick(start_time: float) -> None:
    global _alert_seq, _last_safety_check_ok_at
    reading = bus.get_live_reading()
    if reading is not None:
        # SAFETY FIRST (review finding #4), then everything else - each isolated from the rest.
        if _guarded("safety limits", _check_safety_limits, reading, critical=True):
            _last_safety_check_ok_at = time.monotonic()
        _guarded("heater response", _check_heater_response, reading)
        _guarded("run-end detection", _check_run_ended_externally, reading)
        _guarded("run logging", _log_reading, reading)
        _guarded("alerts", _process_alerts, reading)
        _guarded("self-tune tracking", _self_tune_tracker.observe,
                 reading.self_tune_upper, reading.self_tune_lower, reading.self_tune_ir)
    _guarded("disk space", _check_disk_space)
    _guarded("config drift", _check_config_drift)
    _guarded("system health", _check_system_health)
    _guarded("automatic backup", _check_automatic_backup)
    count, age = bus.get_curve_status()
    pid_count, pid_age = bus.get_pid_status()
    fan_count, fan_age = bus.get_fan_timing_status()

    state = _current_machine_state(reading)

    process_status = None
    if reading is not None:
        elapsed_s = (time.monotonic() - _run_start_time) if (reading.running and _run_start_time is not None) else None
        section_index = monitor.estimate_current_section(reading)
        process_status = {
            "running": reading.running,
            "elapsed_s": elapsed_s,
            "deviations": [
                {"zone": d.zone, "actual_c": d.actual_c, "nearest_target_c": d.nearest_target_c, "deviation_c": d.deviation_c}
                for d in compute_zone_deviations(reading, monitor.active_profile)
            ],
            "active_faults": [f.description for f in fault_manager.active_faults[:3]],
            "active_fault_count": len(fault_manager.active_faults),
            # Best-effort ESTIMATE, not a machine-reported fact - see alerts.py's RunMonitor.
            # estimate_current_section() docstring for the technique and its limits.
            "current_section_index": section_index,
            "current_section_total": len(monitor.active_profile.sections) if monitor.active_profile else None,
            # Target-envelope data (2026-09-07, external feature-gap review) - the current
            # estimated section's own Upper/Lower/IR targets, so the browser can shade a band
            # around each without needing the full profile loaded client-side (the operator
            # may be viewing from a phone with the profile editor closed). 0 means "not
            # controlled this section", same convention as everywhere else in this map.
            "current_section_targets": (
                {
                    "upper_temp": monitor.active_profile.sections[section_index].upper_temp,
                    "lower_temp": monitor.active_profile.sections[section_index].lower_temp,
                    "ir_temp": monitor.active_profile.sections[section_index].ir_temp,
                } if section_index is not None and monitor.active_profile is not None else None
            ),
        }

    with _state_lock:
        _latest_state.clear()
        _latest_state.update({
            "reading": _reading_to_json(reading),
            "process_status": process_status,
            "t": time.monotonic() - start_time,
            "curve_count": count,
            "curve_age": None if age == float("inf") else age,
            "pid_count": pid_count,
            "pid_age": None if pid_age == float("inf") else pid_age,
            "fan_timing_count": fan_count,
            "fan_timing_age": None if fan_age == float("inf") else fan_age,
            # Temp compensation is extracted from the same poll response as fan timing
            # (see machine_bus._handle_holding_register_response), so it shares freshness.
            "temp_comp_count": fan_count,
            "temp_comp_age": None if fan_age == float("inf") else fan_age,
            "active_profile": _active_profile_name,
            "alert_seq": _alert_seq,
            "machine_state": state.value,
            "enabled_controls": sorted(ENABLED_CONTROLS.get(state, frozenset())),
            "active_fault_count": len(fault_manager.active_faults),
            # Health of the monitor itself (review findings #4/#16): a reachable page is not proof the
            # safety checks are running. Ages are seconds since each last succeeded; None = never.
            "monitoring": {
                "last_safety_check_age_s": (time.monotonic() - _last_safety_check_ok_at) if _last_safety_check_ok_at else None,
                "failing_steps": sorted(_step_failures),
                "protective_stop_pending": _protective_stop_pending,
                "restart_pending": _restart_pending,
                "temps_age_s": (time.monotonic() - bus.get_temps_timestamp()) if bus.get_temps_timestamp() > 0 else None,
                "status_age_s": None if bus.coil_snapshot_age() == float("inf") else bus.coil_snapshot_age(),
                "bus_failure": bus.reader_failure,
            },
            "interrupted_run_notice": _interrupted_run_notice,
            "active_read_progress": _active_read_progress,
            "active_write_progress": _active_write_progress,
            "active_search_progress": _active_search_progress,
        })


# ---------------- pages / static ----------------

@app.route("/")
def index():
    return send_from_directory(app.template_folder, "index.html")


# ---------------- connection status / reconnect ----------------
# COM port was previously only settable via a launch argument (main(): sys.argv[1]) baked into
# the systemd service file - no way to change it, or retry after a failed connection, without SSH
# access to edit the service and restart it. A bad port used to crash the whole process before
# app.run() ever started, making the web UI itself unreachable to fix it - a real chicken-and-egg
# problem on a headless Pi. Added 2026-08-19.

@app.route("/api/connection", methods=["GET"])
def api_connection_status():
    # remembered_adapter (2026-09-08, external feature-gap review, second pass) - see
    # connection_identity.py's module docstring.
    identity = load_connection_identity()
    remembered_adapter = None
    if identity is not None:
        remembered_adapter = {
            "description": identity.description, "serial_number": identity.serial_number,
            "last_known_port": identity.last_known_port,
            "currently_resolves_to": resolve_remembered_port(),
        }
    return jsonify({
        "com_port": bus.com_port, "connected": bus.is_connected, "error": _last_connection_error,
        "remembered_adapter": remembered_adapter,
    })


@app.route("/api/ports", methods=["GET"])
def api_list_ports():
    return jsonify([
        {"device": p.device, "description": p.description, "manufacturer": p.manufacturer, "vid": p.vid, "pid": p.pid}
        for p in list_available_ports()
    ])


@app.route("/api/ports/test", methods=["POST"])
def api_test_port():
    port = (request.get_json() or {}).get("port", "").strip()
    if not port:
        return jsonify({"ok": False, "detail": "port is required"}), 400
    ok, detail = test_connection(port)
    return jsonify({"ok": ok, "detail": detail})


@app.route("/api/interrupted-run/acknowledge", methods=["POST"])
def api_acknowledge_interrupted_run():
    global _interrupted_run_notice
    if _interrupted_run_notice is not None:
        run_recovery.acknowledge_interrupted_run(_interrupted_run_notice.get("log_filename", ""))
        _interrupted_run_notice = None
    return jsonify({"ok": True})


@app.route("/api/diagnostics", methods=["GET"])
def api_diagnostics():
    return jsonify(bus.get_diagnostics())


@app.route("/api/diagnostics/reset", methods=["POST"])
def api_diagnostics_reset():
    bus.reset_diagnostics()
    return jsonify(bus.get_diagnostics())


@app.route("/api/registers/read", methods=["POST"])
def api_registers_read():
    """Raw register/coil viewer - 2026-09-08, external feature-gap review, second pass. See
    register_viewer.py's module docstring for why this is deliberately read-only."""
    global _active_read_progress
    data = request.get_json() or {}
    try:
        func_code = int(data.get("func_code"))
        start_address = int(data.get("start_address"))
        count = int(data.get("count"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "detail": "func_code, start_address, and count are required integers."}), 400

    error = validate_register_read_request(func_code, start_address, count)
    if error is not None:
        return jsonify({"ok": False, "detail": error}), 400

    def on_progress(attempt, max_attempts):
        global _active_read_progress
        _active_read_progress = {"label": "Registers (viewer)", "attempt": attempt, "max_attempts": max_attempts}

    try:
        values = bus.read_raw_active(func_code, start_address, count, on_progress=on_progress)
    finally:
        _active_read_progress = None
    if values is None:
        return jsonify({"ok": False, "detail": "No response from the board after repeated attempts."})
    return jsonify({"ok": True, "rows": format_register_rows(func_code, start_address, values)})


@app.route("/api/profiles/simulate", methods=["POST"])
def api_simulate_profile():
    data = request.get_json()
    profile = _profile_from_json(data["profile"])
    wetting_temp_c = float(data.get("wetting_temp_c", monitor.config.wetting_temp_c))
    sim = simulate_profile(profile, wetting_temp_c)
    return jsonify(asdict(sim))


def _remember_current_port(com_port: str) -> None:
    """Called after any successful connection - see connection_identity.py's module docstring for
    why this records the adapter's own hardware identity (VID/PID/serial number), not just the OS-
    assigned port path, so a reboot or replug that shuffles /dev/ttyUSB* numbering doesn't strand
    the app on a stale port. A no-op if the connected port's identity can't be found (e.g. a
    virtual/test port with no real USB descriptor)."""
    for p in list_available_ports():
        if p.device == com_port:
            remember_connection(p)
            return


@app.route("/api/connection", methods=["POST"])
def api_reconnect():
    global bus
    new_port = (request.get_json() or {}).get("com_port", "").strip()
    if not new_port:
        return jsonify({"error": "com_port is required"}), 400
    bus.stop()
    bus = MachineBus(new_port)
    bus.set_ir_reading_correction(safety_limits.ir_reading_correction_c)
    try:
        bus.start()
    except Exception as exc:
        _set_connection_error(str(exc))
        return jsonify({"com_port": new_port, "connected": False, "error": str(exc)})
    _set_connection_error("")
    _remember_current_port(new_port)
    return jsonify({"com_port": new_port, "connected": True, "error": ""})


# ---------------- live state stream (SSE) ----------------

@app.route("/events")
def events():
    def generate():
        last_sent_seq = _alert_seq
        while True:
            time.sleep(POLL_INTERVAL_S)
            with _state_lock:
                state = dict(_latest_state)
            if not state:
                continue
            new_alerts = [
                {"severity": sev, "message": msg}
                for seq, sev, msg in list(_alert_log)
                if seq > last_sent_seq
            ]
            if new_alerts:
                last_sent_seq = state["alert_seq"]
            state["new_alerts"] = new_alerts
            yield f"data: {json.dumps(state)}\n\n"

    return Response(generate(), mimetype="text/event-stream")


# ---------------- safety foundation: state / preflight / faults / limits ----------------

@app.route("/api/state", methods=["GET"])
def api_state():
    reading = bus.get_live_reading()
    state = _current_machine_state(reading)
    return jsonify({
        "machine_state": state.value,
        "enabled_controls": sorted(ENABLED_CONTROLS.get(state, frozenset())),
        "active_fault_count": len(fault_manager.active_faults),
    })


@app.route("/api/preflight", methods=["POST"])
def api_preflight():
    profile = _profile_from_json(request.get_json()["profile"])
    result = run_preflight(bus, profile, safety_limits)
    return jsonify({
        "passed": result.passed,
        "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in result.checks],
        # "empty" | "sent" | "unsent" - whether this exact curve has been sent to the machine from
        # this app since it was loaded/edited. The frontend turns "unsent"/"empty" into a
        # confirm-before-Start prompt (see preflight.curve_send_status for why this is provenance,
        # not a read of the board).
        "curve_send_status": curve_send_status(profile_curve_regs(profile), _last_sent_curve_regs),
    })


@app.route("/api/faults", methods=["GET"])
def api_faults():
    return jsonify([
        {
            "fault_type": f.fault_type.value, "severity": f.severity.value,
            "description": f.description, "timestamp": f.timestamp,
            "profile_name": f.profile_name, "auto_action_taken": f.auto_action_taken,
            "acknowledged": f.acknowledged,
        }
        for f in fault_manager.active_faults
    ])


@app.route("/api/faults/acknowledge", methods=["POST"])
def api_acknowledge_faults():
    global _active_violation_names
    fault_manager.acknowledge_all()
    _active_violation_names = set()
    return jsonify({"ok": True})


@app.route("/api/safety-limits", methods=["GET"])
def api_get_safety_limits():
    return jsonify(asdict(safety_limits))


@app.route("/api/safety-limits", methods=["POST"])
def api_set_safety_limits():
    old_values = asdict(safety_limits)
    data = request.get_json() or {}
    # Validate the whole payload first (audit C07): nothing is applied unless every field passes.
    try:
        updates = parse_safety_limit_updates(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    for field_name, value in updates.items():
        setattr(safety_limits, field_name, value)
    # ir_reading_correction_c isn't read by anything each time it's needed - MachineBus caches its
    # own copy (see get_live_reading()) so it doesn't have to import SafetyLimits. Push a change
    # through immediately rather than waiting for the next reconnect to pick it up.
    if "ir_reading_correction_c" in data:
        bus.set_ir_reading_correction(safety_limits.ir_reading_correction_c)
    save_safety_limits(safety_limits)
    # Pre-change snapshot (2026-09-08, external feature-gap review, second pass) - see
    # config_change_history.py's module docstring for why this exists alongside the daily
    # automatic backup.
    config_change_history.record_config_change(old_values, asdict(safety_limits))
    return jsonify(asdict(safety_limits))


@app.route("/api/safety-limits/history", methods=["GET"])
def api_safety_limits_history():
    return jsonify(config_change_history.get_config_change_history())


@app.route("/api/safety-limits/history/restore", methods=["POST"])
def api_safety_limits_history_restore():
    """Restores a past safety-limits snapshot - 2026-09-08, external feature-gap review, second
    pass. Itself recorded as a new history entry, never a silent rewrite - same convention as
    pid_history's restore."""
    timestamp = (request.get_json() or {}).get("timestamp")
    if timestamp is None:
        return jsonify({"error": "timestamp is required"}), 400
    entry = config_change_history.find_history_entry(timestamp)
    if entry is None:
        return jsonify({"error": f"No history entry found for timestamp {timestamp}"}), 404
    old_values = asdict(safety_limits)
    try:
        updates = parse_safety_limit_updates(entry["new_values"])
    except ValueError as exc:
        return jsonify({"error": f"History entry is invalid and was not restored: {exc}"}), 400
    for field_name, value in updates.items():
        setattr(safety_limits, field_name, value)
    bus.set_ir_reading_correction(safety_limits.ir_reading_correction_c)
    save_safety_limits(safety_limits)
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    config_change_history.record_config_change(old_values, asdict(safety_limits), note=f"Restored from {when}")
    return jsonify(asdict(safety_limits))


# ---------------- machine actions (each blocks its own request thread until done -
# Flask's threaded dev server means this never blocks other requests or the SSE stream) ----------------

def _coil_write_progress(label: str):
    def on_progress(attempt, max_attempts, pass_num):
        global _active_write_progress
        _active_write_progress = {"label": label, "attempt": attempt, "max_attempts": max_attempts, "pass_num": pass_num}
    return on_progress


@app.route("/api/start", methods=["POST"])
@_machine_command("start", starts_heating=True)
def api_start():
    global _logging_active, _active_profile_name, _run_tracker, _current_log_path, _active_write_progress, _run_process_limits
    data = request.get_json()
    profile = _profile_from_json(data["profile"])

    with _run_control_lock:
        # Audit C01 (2026-11-11): a second accepted Start used to close the first run's CSV and
        # replace its tracker. The lock serializes Start/Stop but never checked for a run already
        # open, so a double-tap or client retry orphaned the first run's data.
        if _logging_active:
            return jsonify({
                "ok": False, "attempts": 0, "independently_verified": False,
                "detail": f"A run is already in progress ({_active_profile_name}); Stop it before starting another.",
            }), 409
        preflight = run_preflight(bus, profile, safety_limits)
        if not preflight.passed:
            failed = "; ".join(c.detail for c in preflight.checks if not c.passed)
            return jsonify({"ok": False, "attempts": 0, "detail": f"Preflight failed: {failed}", "independently_verified": False})

        monitor.reset(profile=profile)
        heater_monitor.reset()
        _active_profile_name = profile.name
        _current_log_path = logger_.start_run(profile.name)
        run_recovery.mark_run_started(profile.name, _current_log_path.name)
        # config_snapshot (2026-09-07, external feature-gap review): what actually governed this
        # specific run, captured now rather than reconstructed later from today's settings, which
        # could easily have changed by the time anyone looks back at this run's history.
        _run_tracker = RunFinalizationTracker(
            comm_snapshot_start=bus.get_diagnostics(),
            config_snapshot={
                "safety_limits": asdict(safety_limits),
                "alert_config": asdict(monitor.config),
                "profile": {"name": profile.name, "sections": [asdict(s) for s in profile.sections]},
            },
        )
        # process_limits (2026-09-07, external feature-gap review, second pass) - see
        # main_window.py's identical call site for why this is looked up now, at Start.
        _run_process_limits = parse_process_limits(profile_store.get_profile_metadata(profile.name))
        _logging_active = True
        try:
            result = bus.write_coil(COIL_RUNNING, True, on_progress=_coil_write_progress("Starting"))
        finally:
            _active_write_progress = None
        if not result.ok:
            _logging_active = False
            logger_.stop_run()
        else:
            notifications.notify_async(webhook_config, "run_started", f"Run started: {profile.name}")
    return jsonify(asdict(result))


def _send_run_completed_async(title: str, profile_name: str, outcome: str, summary: dict, duration_s: float) -> None:
    """Completion notification with the optional AI sentence, built on a worker thread. The Ollama
    request can take up to 120 s, and this used to run on the monitoring thread (blocking every safety
    check, fault and SSE update for that long) or while holding the run-control lock (review finding #10).
    The numeric summary is always sent; the AI sentence is a best-effort extra."""
    def _work() -> None:
        try:
            ai_text = run_summary_ai.generate_run_summary_text(profile_name, outcome, summary, duration_s, ollama_config)
        except Exception:
            logger.exception("AI run summary failed - sending the numeric summary only")
            ai_text = None
        message = run_summary_ai.format_run_summary_notification(title, summary, duration_s, ai_text)
        notifications.notify_async(webhook_config, "run_completed", message)
    threading.Thread(target=_work, name="run-summary", daemon=True).start()


def _finalize_current_run() -> dict | None:
    """Returns the computed summary dict (see run_finalization.finalize_run) so a caller can build
    a real completion notification from it - None if there was nothing to finalize, or the CSV
    couldn't be read (already-logged OSError, unchanged from before this returned anything)."""
    global _run_tracker, _current_log_path, _run_process_limits
    # Review finding #11: idempotent per run. The run's context is claimed (snapshotted and cleared) under
    # a tiny lock, so two paths finalizing at once (Stop, the fault path, external-completion detection)
    # cannot both summarize it, nor see a checked-then-reread global turn into None.
    with _finalize_claim_lock:
        path, tracker, limits = _current_log_path, _run_tracker, _run_process_limits
        if path is None or tracker is None:
            return None
        _current_log_path = None
        _run_tracker = None
        _run_process_limits = None
    summary = None
    try:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        summary = finalize_run(
            path, tracker, bus.get_diagnostics(), rows, monitor.config.wetting_temp_c,
            process_limits=limits,
        )
    except OSError:
        pass
    run_recovery.mark_run_ended()
    return summary


@app.route("/api/stop", methods=["POST"])
def api_stop():
    global _logging_active, _active_write_progress
    with _run_control_lock:
        prior = bus.get_live_reading()
        was_running = prior.running if prior else False
        profile_name = _active_profile_name
        duration_s = (time.monotonic() - _run_start_time) if _run_start_time is not None else 0.0
        try:
            result = bus.write_coil(COIL_RUNNING, False, on_progress=_coil_write_progress("Stopping"))
        finally:
            _active_write_progress = None
        if not result.ok:
            # Review finding #1: the stop was NOT acknowledged, so the machine may still be heating. Closing
            # the log and clearing the recovery marker now would make the app believe the run was over.
            # Leave the run recording and tell the operator; it closes when the machine is observed stopped
            # (_check_run_ended_externally), and a retry of Stop works normally.
            body = asdict(result)
            body["detail"] = ("STOP NOT CONFIRMED - the machine did not acknowledge it and may still be running. "
                              "The run is still being recorded. Stop it at the machine, or press Stop again. "
                              f"({result.detail})")
            return jsonify(body)
        _logging_active = False
        logger_.stop_run()
        summary = _finalize_current_run()
    if was_running:
        # Enriched completion notification (2026-09-14, explicit request) - real computed stats always
        # included. Built off this thread and outside the lock (review finding #10).
        if summary is not None:
            _send_run_completed_async(f"Run stopped: {profile_name}", profile_name, "stopped from this app", summary, duration_s)
        else:
            notifications.notify_async(webhook_config, "run_completed", f"Run stopped: {profile_name}")
    return jsonify(asdict(result))


def _toggle_coil(address: int, current_attr: str, label: str = None):
    global _active_write_progress
    reading = bus.get_live_reading()
    new_state = not (getattr(reading, current_attr) if reading else False)

    def on_progress(attempt, max_attempts, pass_num):
        global _active_write_progress
        _active_write_progress = {"label": label, "attempt": attempt, "max_attempts": max_attempts, "pass_num": pass_num}

    try:
        result = bus.write_coil(address, new_state, on_progress=on_progress if label else None)
    finally:
        _active_write_progress = None
    return jsonify(asdict(result))


@app.route("/api/vacuum", methods=["POST"])
def api_vacuum():
    return _toggle_coil(COIL_VACUUM, "vacuum", label="Vacuum")


@app.route("/api/cooling", methods=["POST"])
def api_cooling():
    return _toggle_coil(COIL_COOLING, "cooling", label="Cooling")


@app.route("/api/keep", methods=["POST"])
def api_keep():
    # Missing its label entirely until 2026-09-15 (found during a live button-reliability audit) -
    # Vacuum/Cooling always showed retry progress on the button itself while Keep silently retried
    # with no visible feedback at all, indistinguishable from a hang for however long a contended
    # write took.
    return _toggle_coil(COIL_KEEP, "keep", label="Keep")


@app.route("/api/wetting", methods=["POST"])
def api_wetting():
    monitor.config.wetting_temp_c = float(request.get_json()["value"])
    return jsonify({"ok": True})


@app.route("/api/alert-config", methods=["GET"])
def api_get_alert_config():
    return jsonify(asdict(monitor.config))


@app.route("/api/alert-config", methods=["POST"])
def api_alert_config():
    """The other alert thresholds (alerts.AlertConfig) were previously hardcoded defaults with no
    way to adjust them from either UI - only wetting temp (above) was exposed. Added 2026-08-19."""
    data = request.get_json() or {}
    for field in ("stuck_sensor_seconds", "stuck_sensor_epsilon", "max_jump_per_reading", "section_deviation_c"):
        if field in data:
            setattr(monitor.config, field, float(data[field]))
    return jsonify({"ok": True})


# ---------------- profile editing ----------------

@app.route("/api/profiles", methods=["GET"])
def api_list_profiles():
    return jsonify(profile_store.list_profiles())


@app.route("/api/profiles/<path:name>", methods=["GET"])
def api_load_profile(name):
    try:
        profile = profile_store.load_profile(name)
    except FileNotFoundError:
        return jsonify({"error": f"No local profile named '{name}'."}), 404
    return jsonify(_profile_to_json(profile))


@app.route("/api/profiles", methods=["POST"])
def api_save_profile():
    data = request.get_json()
    profile = _profile_from_json(data)
    # metadata is optional and additive to the existing {name, sections} shape - keeps this
    # endpoint backward compatible with callers that only ever sent name/sections.
    try:
        path = profile_store.save_profile(profile, metadata=data.get("metadata"))
    except ValueError as exc:
        # 2026-09-08 audit finding - see profile_store.save_profile()'s own docstring: a genuine
        # filename collision with a DIFFERENT profile is now a real, surfaced error instead of
        # silently corrupting the other profile.
        return jsonify({"error": str(exc)}), 409
    return jsonify({"path": str(path), "version": profile_store.get_profile_version(profile.name)})


@app.route("/api/profiles/<path:name>/metadata", methods=["GET"])
def api_get_profile_metadata(name):
    return jsonify(profile_store.get_profile_metadata(name))


@app.route("/api/profiles/<path:name>/metadata", methods=["POST"])
def api_set_profile_metadata(name):
    metadata = request.get_json() or {}
    try:
        profile = profile_store.load_profile(name)
    except FileNotFoundError:
        return jsonify({"error": f"No local profile named '{name}'."}), 404
    try:
        profile_store.save_profile(profile, metadata=metadata)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({"ok": True})


@app.route("/api/profiles/<path:name>/history", methods=["GET"])
def api_get_profile_history(name):
    return jsonify({
        "current_version": profile_store.get_profile_version(name),
        "history": profile_store.get_profile_history(name),
    })


@app.route("/api/profiles/<path:name>/restore", methods=["POST"])
def api_restore_profile_version(name):
    version = (request.get_json() or {}).get("version")
    if version is None:
        return jsonify({"error": "version is required"}), 400
    try:
        restored = profile_store.restore_profile_version(name, int(version))
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify(_profile_to_json(restored))


@app.route("/api/profiles/<path:name>", methods=["DELETE"])
def api_delete_profile(name):
    profile_store.delete_profile(name)
    return jsonify({"ok": True})


@app.route("/api/profiles/<path:name>/rename", methods=["POST"])
def api_rename_profile(name):
    new_name = (request.get_json() or {}).get("new_name", "").strip()
    if not new_name:
        return jsonify({"error": "new_name is required"}), 400
    try:
        path = profile_store.rename_profile(name, new_name)
    except FileNotFoundError:
        return jsonify({"error": f"No local profile named '{name}'."}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({"path": str(path)})


@app.route("/api/runs", methods=["GET"])
def api_list_runs():
    """Every run has always been logged to a CSV under pc-app/logs/ (see data_logger.py), but
    nothing surfaced that history back into either app's UI before now. Read-only, never touches
    the machine."""
    return jsonify(list_runs())


@app.route("/api/runs/<filename>", methods=["GET"])
def api_download_run(filename):
    """Serves a run log CSV for download - send_from_directory rejects path traversal
    (../, absolute paths) on its own, so `filename` from the URL is safe to pass through."""
    return send_from_directory(LOGS_DIR, filename, as_attachment=True)


@app.route("/api/runs/<filename>", methods=["DELETE"])
def api_delete_run(filename):
    """2026-09-16, real reported gap: no way to delete a run from Run History at all. Irreversible -
    see data_logger.delete_run()'s own docstring. `filename` is validated there (rejects anything
    that isn't a plain run_*.csv name) before it's ever used to build a path. Audit R08
    (2026-11-11): refuses the run that is currently being logged or awaiting finalization -
    unlinking an open CSV breaks the logger on Windows and silently orphans its data on Linux."""
    active = _current_log_path
    if active is not None and active.name == filename and (_logging_active or _run_tracker is not None):
        return jsonify({"ok": False, "detail": "That run is still active or being finalized."}), 409
    try:
        result = delete_run(filename)
    except ValueError as exc:
        return jsonify({"ok": False, "detail": str(exc)}), 400
    return jsonify(result), (200 if result["ok"] else 404)


@app.route("/api/runs/<filename>/summary", methods=["GET"])
def api_run_summary(filename):
    """Full sidecar (events, config_snapshot, zone_statistics, everything) - 2026-09-07, external
    feature-gap review. Unlike /api/runs (list_runs()'s cherry-picked subset for the table), this
    is what the run-history UI's "Details" view reads - see data_logger.get_run_summary()."""
    try:
        summary = get_run_summary(filename)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if summary is None:
        return jsonify({"error": "no summary available for this run (predates Run Finalization, or it never finalized cleanly)"}), 404
    return jsonify(summary)


@app.route("/api/runs/<filename>/golden", methods=["POST"])
def api_mark_run_golden(filename):
    """Golden-run marking (2026-09-07, external feature-gap review) - see run_finalization.
    set_run_golden()'s docstring for why this deliberately doesn't build a separate comparison
    mechanism (compare_runs() already handles "this run vs the golden one" as an ordinary
    comparison)."""
    golden = bool((request.get_json() or {}).get("golden", True))
    try:
        summary = mark_run_golden(filename, golden)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(summary)


@app.route("/api/runs/<filename>/notes", methods=["POST"])
def api_set_run_notes(filename):
    """Operator run notes (2026-09-07, external feature-gap review, second pass) - free-text,
    attachable to any historical run, cleared by posting notes: ""."""
    notes = (request.get_json() or {}).get("notes", "")
    try:
        summary = mark_run_notes(filename, notes)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(summary)


@app.route("/api/runs/<filename>/repair-outcome", methods=["POST"])
def api_set_repair_outcome(filename):
    """Repair outcome tracking (2026-09-07, external feature-gap review, second pass) - see
    run_finalization.set_repair_outcome()'s docstring for why this deliberately stays a single
    status + free-text reason, not a repair-shop CRM."""
    data = request.get_json() or {}
    outcome = data.get("outcome", "")
    failure_reason = data.get("failure_reason", "")
    try:
        summary = mark_repair_outcome(filename, outcome, failure_reason)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(summary)


@app.route("/api/runs/repair-outcome-stats", methods=["GET"])
def api_repair_outcome_stats():
    profile_name = request.args.get("profile_name") or None
    return jsonify(compute_repair_outcome_stats(profile_name))


@app.route("/api/runs/compare", methods=["POST"])
def api_compare_runs():
    filenames = (request.get_json() or {}).get("filenames", [])
    return jsonify(compare_runs(filenames))


@app.route("/api/profiles/recommend", methods=["POST"])
def api_recommend_profile():
    profile = _profile_from_json(request.get_json()["profile"])
    rec = recommend_for_profile(profile)
    return jsonify(asdict(rec) if rec is not None else None)


@app.route("/api/thermal-model/status", methods=["GET"])
def api_thermal_model_status():
    return jsonify(asdict(thermal_model.get_model_status()))


@app.route("/api/thermal-model/predict", methods=["POST"])
def api_thermal_model_predict():
    """Applies the trained model (thermal_model.py) to whatever profile is currently being edited -
    read-only, no machine interaction. {"available": false} (with the same status message shown
    elsewhere) when there isn't a trained model yet, rather than a partial/fabricated response."""
    data = request.get_json()
    profile = _profile_from_json(data["profile"])
    predictions = thermal_model.predict_profile_response(profile)
    if predictions is None:
        return jsonify({"available": False, "message": thermal_model.get_model_status().message})
    return jsonify({"available": True, "predictions": {zone: asdict(p) for zone, p in predictions.items()}})


@app.route("/api/maintenance", methods=["GET"])
def api_maintenance_status():
    """2026-09-07, external feature-gap review - see maintenance.py's module docstring for why
    everything here is built from data already logged, never a fabricated hardware-wear estimate."""
    return jsonify(maintenance.compute_maintenance_status())


@app.route("/api/maintenance/config", methods=["POST"])
def api_set_maintenance_config():
    data = request.get_json() or {}
    config = maintenance.load_maintenance_config()
    for field_name in ("calibration_interval_days", "self_tune_review_interval_days"):
        if field_name in data:
            setattr(config, field_name, float(data[field_name]))
    maintenance.save_maintenance_config(config)
    return jsonify(maintenance.compute_maintenance_status(config=config))


@app.route("/api/system-health", methods=["GET"])
def api_system_health():
    """2026-09-08, external feature-gap review, second pass - see system_health.py. Bundled with
    disk usage and the Modbus comm-diagnostics counters (2026-09-15, System Health tab) so that
    tab can show Pi health and bus-link health together in one place - both were already computed
    elsewhere (_check_disk_space's check_disk_space(), /api/diagnostics's bus.get_diagnostics()),
    this just gives the tab a single call to poll instead of three. Neither addition involves a
    fresh Modbus transaction - get_diagnostics() reads already-maintained in-process counters and
    check_disk_space() is a local filesystem syscall - so polling this endpoint on an interval
    adds no extra RS485 bus traffic/contention."""
    status = get_system_health()
    status["disk"] = check_disk_space()
    status["comm"] = bus.get_diagnostics()
    return jsonify(status)


@app.route("/api/diagnostics/run", methods=["POST"])
def api_run_diagnostics():
    """System self-test / Run Diagnostics - 2026-09-08, external feature-gap review, second pass.
    See system_diagnostics.py's module docstring for why this is broader than (and doesn't
    replace) preflight.py. Involves real active reads (PID + fan-timing/temp-comp), so this can
    take a few seconds under bus contention, same as any other active-read endpoint."""
    global _active_read_progress

    def on_progress_pid(attempt, max_attempts):
        global _active_read_progress
        _active_read_progress = {"label": "PID (diagnostics)", "attempt": attempt, "max_attempts": max_attempts}

    def on_progress_fan(attempt, max_attempts):
        global _active_read_progress
        _active_read_progress = {"label": "Fan timing / temp comp (diagnostics)", "attempt": attempt, "max_attempts": max_attempts}

    try:
        pid_regs = bus.read_pid_active(on_progress=on_progress_pid)
        fan_timing_regs = bus.read_fan_timing_active(on_progress=on_progress_fan)
    finally:
        _active_read_progress = None
    temp_comp_regs = bus.get_latest_temp_comp() if fan_timing_regs is not None else None

    known_good = load_known_good_config()
    config_drift = None
    if known_good is not None and pid_regs is not None and fan_timing_regs is not None:
        config_drift = compute_drift(known_good, pid_regs, fan_timing_regs, temp_comp_regs or [])

    identity = load_connection_identity()
    remembered_adapter = None
    if identity is not None:
        remembered_adapter = {"currently_resolves_to": resolve_remembered_port()}

    checks = run_diagnostics(
        connected=bus.is_connected,
        live_reading=bus.get_live_reading(),
        disk_status=check_disk_space(),
        system_health_status=get_system_health(),
        maintenance_status=maintenance.compute_maintenance_status(),
        automatic_backups=automatic_backup.list_automatic_backups(),
        remembered_adapter=remembered_adapter,
        config_drift=config_drift,
        pid_regs=pid_regs,
        fan_timing_regs=fan_timing_regs,
    )
    return jsonify({
        "overall": overall_status(checks),
        "checks": [asdict(c) for c in checks],
    })


@app.route("/api/machine-config/known-good", methods=["GET"])
def api_get_known_good_config():
    """2026-09-08, external feature-gap review, second pass - see machine_config_snapshot.py."""
    snapshot = load_known_good_config()
    if snapshot is None:
        return jsonify({"exists": False})
    return jsonify({
        "exists": True, "captured_at": snapshot.captured_at,
        "pid": snapshot.pid, "fan_timing": snapshot.fan_timing, "temp_comp": snapshot.temp_comp,
    })


@app.route("/api/machine-config/save-known-good", methods=["POST"])
def api_save_known_good_config():
    """Actively reads PID + fan-timing/temp-compensation and saves them as the new known-good
    reference - an explicit operator action ("this is how the machine should be configured right
    now"), not something done silently."""
    global _active_read_progress

    def on_progress_pid(attempt, max_attempts):
        global _active_read_progress
        _active_read_progress = {"label": "PID (known-good snapshot)", "attempt": attempt, "max_attempts": max_attempts}

    def on_progress_fan(attempt, max_attempts):
        global _active_read_progress
        _active_read_progress = {"label": "Fan timing / temp comp (known-good snapshot)", "attempt": attempt, "max_attempts": max_attempts}

    try:
        pid = bus.read_pid_active(on_progress=on_progress_pid)
        fan_timing = bus.read_fan_timing_active(on_progress=on_progress_fan)
    finally:
        _active_read_progress = None
    if pid is None or fan_timing is None:
        return jsonify({"ok": False, "detail": "Could not read PID and/or fan-timing/compensation from the board."}), 502
    temp_comp = bus.get_latest_temp_comp()  # populated by the read_fan_timing_active() call above
    snapshot = save_known_good_config(pid, fan_timing, temp_comp)
    return jsonify({"ok": True, "captured_at": snapshot.captured_at})


@app.route("/api/machine-config/check", methods=["POST"])
def api_check_config_drift():
    """On-demand drift check via a real active read - unlike the periodic passive-only background
    check (_check_config_drift), this gives an immediate, reliable answer regardless of what the
    touchscreen has recently shown."""
    global _active_read_progress
    known_good = load_known_good_config()
    if known_good is None:
        return jsonify({"error": "No known-good configuration saved yet."}), 404

    def on_progress_pid(attempt, max_attempts):
        global _active_read_progress
        _active_read_progress = {"label": "PID (checking configuration)", "attempt": attempt, "max_attempts": max_attempts}

    def on_progress_fan(attempt, max_attempts):
        global _active_read_progress
        _active_read_progress = {"label": "Fan timing / temp comp (checking configuration)", "attempt": attempt, "max_attempts": max_attempts}

    try:
        pid = bus.read_pid_active(on_progress=on_progress_pid)
        fan_timing = bus.read_fan_timing_active(on_progress=on_progress_fan)
    finally:
        _active_read_progress = None
    if pid is None or fan_timing is None:
        return jsonify({"error": "Could not read PID and/or fan-timing/compensation from the board."}), 502
    temp_comp = bus.get_latest_temp_comp()
    return jsonify(compute_drift(known_good, pid, fan_timing, temp_comp))


@app.route("/api/profiles/export.<fmt>", methods=["GET"])
def api_export_profiles(fmt):
    """Exports every saved local profile to a single file - the reverse of /api/profiles/import,
    for backup or sharing. See profile_import.export_csv/export_xlsx."""
    if fmt not in ("csv", "xlsx"):
        return jsonify({"error": f"Unsupported format '{fmt}' - expected csv or xlsx"}), 400
    names = profile_store.list_profiles()
    profiles = [profile_store.load_profile(name) for name in names]
    data = profile_import.export_xlsx(profiles) if fmt == "xlsx" else profile_import.export_csv(profiles)
    mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if fmt == "xlsx" else "text/csv"
    return Response(
        data, mimetype=mimetype,
        headers={"Content-Disposition": f"attachment; filename=profiles.{fmt}"},
    )


@app.route("/api/backup/export", methods=["GET"])
def api_export_backup():
    backup = config_backup.create_backup(safety_limits, monitor.config)
    return Response(
        json.dumps(backup, indent=2), mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=ly-r690-backup.json"},
    )


@app.route("/api/backup/import", methods=["POST"])
def api_import_backup():
    if "file" not in request.files:
        return jsonify({"error": "no file uploaded"}), 400
    try:
        backup = json.loads(request.files["file"].read())
    except json.JSONDecodeError as exc:
        return jsonify({"error": f"Not valid JSON: {exc}"}), 400
    try:
        summary = config_backup.restore_backup(backup, safety_limits, monitor.config)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    # Otherwise a restore only lasts until the next restart, same gap this whole persistence
    # mechanism exists to close - see safety_limits.save_safety_limits().
    save_safety_limits(safety_limits)
    bus.set_ir_reading_correction(safety_limits.ir_reading_correction_c)
    return jsonify(summary)


@app.route("/api/backup/automatic", methods=["GET"])
def api_list_automatic_backups():
    """2026-09-08, external feature-gap review, second pass - see automatic_backup.py."""
    return jsonify(automatic_backup.list_automatic_backups())


@app.route("/api/backup/automatic/<filename>/restore", methods=["POST"])
def api_restore_automatic_backup(filename):
    # filename comes straight from the URL path - same path-traversal guard as
    # data_logger._validate_run_filename() for the same reason.
    if "/" in filename or "\\" in filename or not filename.startswith("backup_") or not filename.endswith(".json"):
        return jsonify({"error": f"Not a valid automatic backup filename: {filename!r}"}), 400
    path = automatic_backup.BACKUPS_DIR / filename
    try:
        backup = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return jsonify({"error": f"Could not read {filename}: {exc}"}), 400
    try:
        summary = config_backup.restore_backup(backup, safety_limits, monitor.config)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    save_safety_limits(safety_limits)
    bus.set_ir_reading_correction(safety_limits.ir_reading_correction_c)
    return jsonify(summary)


@app.route("/api/profiles/import", methods=["POST"])
def api_import_profiles():
    """Bulk-import profiles from an uploaded CSV/XLSX - see profile_import.py for the expected
    layout. Purely local file storage, no machine interaction - safe to call freely."""
    if "file" not in request.files:
        return jsonify({"error": "no file uploaded"}), 400
    upload = request.files["file"]
    filename = (upload.filename or "").lower()
    data = upload.read()

    if filename.endswith(".xlsx"):
        result = profile_import.parse_xlsx(data)
    elif filename.endswith(".csv"):
        result = profile_import.parse_csv(data)
    else:
        return jsonify({"error": f"Unsupported file type '{upload.filename}' - expected .csv or .xlsx"}), 400

    imported = []
    warnings = list(result.warnings)
    for profile in result.profiles:
        try:
            profile_store.save_profile(profile)
            imported.append(profile.name)
        except (OSError, ValueError) as exc:
            # A single bad profile name (e.g. containing filesystem-illegal characters, or one
            # that collides with an existing profile after sanitization - see
            # profile_store.save_profile()'s own docstring, added 2026-09-08) must never abort
            # the rest of the batch - see profile_store._safe_filename.
            warnings.append(f"Could not save '{profile.name}': {exc}")

    return jsonify({"imported": imported, "warnings": warnings})


@app.route("/api/curve/latest", methods=["GET"])
def api_curve_latest():
    """Cheap passive peek at whatever was last observed (from either passive listening or the
    opportunistic read-back after a Send - see api_curve_send) - never transmits. There is no
    active "read the curve on demand" route: this address range (100-171) has never once
    responded to an active read on this board over any method tried, across extensive testing -
    see docs/06-modbus-register-map.md's "Curve/profile active-read investigation". A "Read from
    machine" button for profiles was removed 2026-09-13 for exactly this reason (confirmed
    non-functional, not a temporary gap) - do not re-add one without new evidence this range is
    actually readable. Sending a profile TO the machine remains fully reliable; there is simply no
    way to read back what's currently loaded, only what this app itself last observed or sent."""
    curve = bus.get_latest_curve()
    if curve is None:
        return jsonify({"error": "no curve data observed yet"}), 404
    return jsonify(_profile_to_json(curve))


@app.route("/api/profiles/<path:name>/qualification", methods=["GET"])
def api_profile_qualification(name):
    """2026-09-08, external feature-gap review, second pass - see profile_qualification.py.
    Surfaced on its own (not just as a validate_profile warning) so the metadata editor can show
    "2 of 3 needed" progress proactively, before the operator ever tries to mark a profile
    Approved."""
    status = compute_qualification_status(name)
    return jsonify({**asdict(status), "qualified": status.qualified, "threshold": QUALIFICATION_THRESHOLD_RUNS})


@app.route("/api/profiles/validate", methods=["POST"])
def api_validate_profile():
    profile = _profile_from_json(request.get_json())
    result = validate_profile(
        profile, safety_limits, metadata=profile_store.get_profile_metadata(profile.name),
        qualification=compute_qualification_status(profile.name),
    )
    return jsonify({"errors": result.errors, "warnings": result.warnings, "info": result.info, "blocks_send": result.blocks_send})


@app.route("/api/curve/send", methods=["POST"])
@_machine_command("send_profile")
def api_curve_send():
    profile = _profile_from_json(request.get_json())

    validation = validate_profile(
        profile, safety_limits, metadata=profile_store.get_profile_metadata(profile.name),
        qualification=compute_qualification_status(profile.name),
    )
    if validation.blocks_send:
        return jsonify({
            "ok": False, "attempts": 0,
            "detail": "Validation failed: " + "; ".join(validation.errors),
            "independently_verified": False,
        })

    global _last_sent_curve_regs, _active_write_progress
    regs = []
    for attr in ROW_ATTRS:
        for section in profile.sections:
            regs.append(getattr(section, attr))

    def on_progress(confirmed, total, pass_num):
        global _active_write_progress
        _active_write_progress = {"label": "Curve", "attempt": confirmed, "max_attempts": total, "pass_num": pass_num}

    try:
        result = bus.write_registers(ADDR_CURVE_BASE, regs, on_progress=on_progress)
    finally:
        _active_write_progress = None
    if result.ok:
        # Provenance for the pre-Start reminder - see preflight.curve_send_status.
        _last_sent_curve_regs = list(regs)
    # Read-back verification (2026-09-07, external feature-gap review) - see main_window.py's
    # _on_send_to_machine for the full reasoning: result.independently_verified only means the
    # board's write-command ACK was confirmed, not that the value actually stuck (the IR temp-comp
    # register this same session ACK-confirmed a write that reverted moments later). verified_match
    # is null if the write failed or the opportunistic read-back never fired in time (not itself a
    # failure), true/false only when a real comparison was possible.
    verified_match = None
    if result.ok:
        readback = bus.read_curve_active()
        if readback is not None:
            readback_regs = [getattr(section, attr) for attr in ROW_ATTRS for section in readback.sections]
            verified_match = (readback_regs == regs)
    response = asdict(result)
    response["verified_match"] = verified_match
    return jsonify(response)


@app.route("/api/curve/mirror", methods=["POST"])
def api_curve_mirror():
    global _active_profile_name
    profile = _profile_from_json(request.get_json())
    monitor.reset(profile=profile)
    heater_monitor.reset()
    _active_profile_name = profile.name
    return jsonify({"ok": True, "name": profile.name})


# ---------------- PID gains ----------------

@app.route("/api/pid/latest", methods=["GET"])
def api_pid_latest():
    """Cheap passive peek - see /api/curve/latest for the same pattern."""
    regs = bus.get_latest_pid()
    if regs is None:
        return jsonify({"error": "no PID data observed yet"}), 404
    return jsonify(_pid_regs_to_json(regs))


@app.route("/api/pid/read", methods=["POST"])
def api_pid_read():
    """Actively requests PID gains directly from the board - see MachineBus "active reads"."""
    global _active_read_progress

    def on_progress(attempt, max_attempts):
        global _active_read_progress
        _active_read_progress = {"label": "PID", "attempt": attempt, "max_attempts": max_attempts}

    try:
        regs = bus.read_pid_active(on_progress=on_progress)
    finally:
        _active_read_progress = None
    if regs is None:
        return jsonify({"error": "No response from the board after repeated attempts."}), 404
    return jsonify(_pid_regs_to_json(regs))


@app.route("/api/pid/send", methods=["POST"])
@_machine_command("send_pid")
def api_pid_send():
    regs = _pid_json_to_regs(request.get_json())
    old_regs = bus.get_latest_pid()
    result = bus.write_registers(ADDR_PID_BASE, regs)
    if result.ok:
        pid_history.record_pid_change(old_regs, regs)
    return jsonify(asdict(result))


@app.route("/api/pid/history", methods=["GET"])
def api_pid_history():
    return jsonify(pid_history.get_pid_history())


@app.route("/api/pid/history/restore", methods=["POST"])
def api_pid_history_restore():
    """Restores a past PID history entry's values - 2026-09-08, external feature-gap review,
    second pass. Itself recorded as a new history entry (see record_pid_change), never a silent
    rewrite of history - matching profile_store.restore_profile_version()'s identical convention."""
    timestamp = (request.get_json() or {}).get("timestamp")
    if timestamp is None:
        return jsonify({"ok": False, "detail": "timestamp is required"}), 400
    entry = pid_history.find_history_entry(timestamp)
    if entry is None:
        return jsonify({"ok": False, "detail": f"No history entry found for timestamp {timestamp}"}), 404
    restore_values = entry["new_values"]
    old_regs = bus.get_latest_pid()
    result = bus.write_registers(ADDR_PID_BASE, restore_values)
    if result.ok:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
        pid_history.record_pid_change(old_regs, restore_values, note=f"Restored from {when}")
    return jsonify(asdict(result))


# ---------------- calibration (record-keeping only - see calibration.py's module docstring for
# why this never writes to the temperature compensation registers automatically) ----------------

@app.route("/api/calibration", methods=["GET"])
def api_get_calibration_history():
    return jsonify(calibration.get_calibration_history())


@app.route("/api/calibration", methods=["POST"])
def api_add_calibration_record():
    data = request.get_json() or {}
    points = [calibration.CalibrationPoint(zone=p["zone"], reference_c=float(p["reference_c"]), machine_c=float(p["machine_c"])) for p in data.get("points", [])]
    if not points:
        return jsonify({"error": "at least one calibration point is required"}), 400
    record = calibration.CalibrationRecord(
        operator=data.get("operator", ""), reference_instrument=data.get("reference_instrument", ""),
        points=points, note=data.get("note", ""),
    )
    calibration.add_calibration_record(record)
    return jsonify({"ok": True})


# ---------------- webhook notifications (Stage 21) - see notifications.py's module docstring ----

@app.route("/api/webhook", methods=["GET"])
def api_get_webhook_config():
    # The URL IS the credential (audit C06) - never hand it back in full on an ordinary read.
    return jsonify({"url": notifications.mask_webhook_url(webhook_config.url), "enabled": webhook_config.enabled, "events": webhook_config.events})


@app.route("/api/webhook", methods=["POST"])
def api_save_webhook_config():
    global webhook_config
    data = request.get_json() or {}
    events = {name: True for name in notifications.EVENT_TYPES}
    events.update(data.get("events", {}))
    submitted_url = data.get("url", "").strip()
    if submitted_url and submitted_url == notifications.mask_webhook_url(webhook_config.url):
        submitted_url = webhook_config.url  # the form echoed back the masked value - keep the real one
    webhook_config = notifications.WebhookConfig(
        url=submitted_url, enabled=bool(data.get("enabled", False)), events=events,
    )
    notifications.save_webhook_config(webhook_config)
    return jsonify({"ok": True})


@app.route("/api/webhook/test", methods=["POST"])
def api_test_webhook():
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if url and url == notifications.mask_webhook_url(webhook_config.url):
        url = webhook_config.url  # form echoed the masked display value - test the stored one
    if not url:
        return jsonify({"ok": False, "detail": "url is required"}), 400
    test_config = notifications.WebhookConfig(url=url, enabled=True, events={"test": True})
    ok, detail = notifications.send_notification(test_config, "test", "LY-R690 Control: test notification.")
    return jsonify({"ok": ok, "detail": detail})


# ---------------- profile search (Ollama) - see profile_search.py's module docstring for why
# the model is only ever used to EXTRACT from real fetched webpage text, never to invent a
# profile from its own knowledge, and why every result is a candidate for the operator to review
# in the Profile Editor - never auto-saved or auto-sent. ----------------

@app.route("/api/ollama-config", methods=["GET"])
def api_get_ollama_config():
    return jsonify({"host": ollama_config.host, "model": ollama_config.model, "enabled": ollama_config.enabled})


@app.route("/api/ollama-config", methods=["POST"])
def api_save_ollama_config():
    global ollama_config
    data = request.get_json() or {}
    ollama_config = profile_search.OllamaConfig(
        host=data.get("host", "").strip(), model=data.get("model", "").strip(),
        enabled=bool(data.get("enabled", False)),
    )
    profile_search.save_ollama_config(ollama_config)
    return jsonify({"ok": True})


@app.route("/api/ollama-config/models", methods=["POST"])
def api_list_ollama_models():
    host = (request.get_json() or {}).get("host", "").strip()
    if not host:
        return jsonify({"models": [], "error": "host is required"}), 400
    models, error = profile_search.list_ollama_models(host)
    return jsonify({"models": models, "error": error})


# Web search (DuckDuckGo, 2026-09-09 - reverted back from the Google Custom Search JSON API after
# confirming live that API has been closed to new customers since 2025; see profile_search.py's
# module docstring for the full history and the real DuckDuckGo bot-detection risk this carries.
# No config/credentials needed - DuckDuckGo's html results page is keyless - so unlike the Google
# box this replaces, there's nothing to save, only a way to test it actually works right now.

@app.route("/api/websearch/test", methods=["POST"])
def api_test_web_search():
    """Runs one real query against DuckDuckGo (general web + the curated site list, same as a real
    Profile Search would) so the operator can confirm it's actually working right now - DuckDuckGo's
    bot-detection can and does block this app's requests without warning (confirmed live
    2026-09-09), so "did it work a moment ago" is a real, useful question to be able to ask."""
    query = (request.get_json() or {}).get("query", "").strip() or "BGA rework reflow profile"
    hits, error = profile_search.search_web(query, max_results=5)
    return jsonify({"hits": [{"title": h.title, "url": h.url} for h in hits], "error": error,
                    "provider": profile_search.last_search_provider})


def _serper_status() -> dict:
    """Never includes the key itself - only whether one is stored and its last 4 characters."""
    cfg = profile_search.load_serper_config()
    return {"configured": bool(cfg.api_key), "enabled": cfg.enabled,
            "key_hint": ("..." + cfg.api_key[-4:]) if len(cfg.api_key) >= 8 else ""}


@app.route("/api/serper-config", methods=["GET"])
def api_get_serper_config():
    return jsonify(_serper_status())


@app.route("/api/serper-config", methods=["POST"])
def api_set_serper_config():
    """A blank/missing api_key keeps the stored one (the page never receives the key back, so it can
    only send a new key or nothing)."""
    data = request.get_json(silent=True) or {}
    current = profile_search.load_serper_config()
    new_key = str(data.get("api_key") or "").strip()
    if new_key and not (20 <= len(new_key) <= 80 and new_key.isalnum()):
        return jsonify({"ok": False, "error": "That doesn't look like a Serper API key (letters and digits only)."}), 400
    cfg = profile_search.SerperConfig(api_key=new_key or current.api_key, enabled=bool(data.get("enabled", current.enabled)))
    if cfg.enabled and not cfg.api_key:
        return jsonify({"ok": False, "error": "Enter an API key before enabling Serper."}), 400
    profile_search.save_serper_config(cfg)
    return jsonify({"ok": True, **_serper_status()})


def _search_result_to_json(result) -> dict:
    return {
        "candidates": [
            {
                "source_title": c.source_title, "source_url": c.source_url,
                "machine_match": c.machine_match, "quotes_verified": c.quotes_verified,
                "ai_generated": c.ai_generated,
                "notes": c.notes, "profile": _profile_to_json(c.profile),
                # Verbatim excerpts that informed an AI estimate (empty for an extracted candidate).
                "evidence": c.evidence,
            }
            for c in result.candidates
        ],
        "errors": result.errors,
    }


@app.route("/api/profile-search", methods=["POST"])
def api_profile_search():
    global _active_search_progress
    board_name = (request.get_json() or {}).get("board_name", "").strip()
    if not board_name:
        return jsonify({"candidates": [], "errors": ["board_name is required"]})

    def on_progress(message):
        global _active_search_progress
        _active_search_progress = message

    try:
        result = profile_search.search_profile_online(board_name, ollama_config, on_progress=on_progress)
    finally:
        _active_search_progress = None

    return jsonify(_search_result_to_json(result))


@app.route("/api/profile-search/url", methods=["POST"])
def api_profile_search_url():
    """Manual-URL mode (2026-09-09) - see profile_search.extract_profile_from_url's own docstring
    for why this exists: DuckDuckGo's bot-detection can block the automatic search above without
    warning, and this skips the search step entirely - the operator supplies a page they already
    found themselves."""
    global _active_search_progress
    data = request.get_json() or {}
    board_name = data.get("board_name", "").strip()
    url = data.get("url", "").strip()
    if not board_name:
        return jsonify({"candidates": [], "errors": ["board_name is required"]})
    if not url:
        return jsonify({"candidates": [], "errors": ["url is required"]})

    def on_progress(message):
        global _active_search_progress
        _active_search_progress = message

    try:
        result = profile_search.extract_profile_from_url(board_name, url, ollama_config, on_progress=on_progress)
    finally:
        _active_search_progress = None

    return jsonify(_search_result_to_json(result))


@app.route("/api/process-assistant", methods=["POST"])
def api_process_assistant():
    """AI Process Assistant (2026-09-07, external feature-gap review) - see process_assistant.py's
    own module docstring for the evidence-tagging model and the hard read-only architectural rule
    this respects. Reuses the same `ollama_config` already configured for Profile Search above -
    same underlying Ollama connection, a different question being asked of it."""
    question = (request.get_json() or {}).get("question", "").strip()
    answer, error = process_assistant.ask_process_assistant(question, ollama_config)
    return jsonify({"answer": answer, "error": error})


# ---------------- fan/vacuum timing ----------------

# Confirmed live 2026-09-09 (user report) - shared by fan/vacuum timing and temperature
# compensation reads since they're the same 10-register active read (see
# MachineBus.read_fan_timing_active's docstring for the full finding).
FAN_TIMING_TEMP_COMP_NO_RESPONSE_MESSAGE = (
    "No response from the board after repeated attempts. This register range has been observed "
    "to not respond until the touchscreen's Advanced Parameters screen has been opened at least "
    "once since the machine's controller was last powered on - it does not need to stay on that "
    "screen afterward. If you haven't opened that screen yet this power cycle, do that on the "
    "touchscreen once, then try again."
)


@app.route("/api/fan-timing/latest", methods=["GET"])
def api_fan_timing_latest():
    """Cheap passive peek - see /api/curve/latest for the same pattern."""
    regs = bus.get_latest_fan_timing()
    if regs is None:
        return jsonify({"error": "no fan timing data observed yet"}), 404
    return jsonify(_fan_timing_regs_to_json(regs))


@app.route("/api/fan-timing/read", methods=["POST"])
def api_fan_timing_read():
    """Actively requests fan/vacuum timing directly from the board - see MachineBus "active reads"."""
    global _active_read_progress

    def on_progress(attempt, max_attempts):
        global _active_read_progress
        _active_read_progress = {"label": "fan timing", "attempt": attempt, "max_attempts": max_attempts}

    try:
        regs = bus.read_fan_timing_active(on_progress=on_progress)
    finally:
        _active_read_progress = None
    if regs is None:
        return jsonify({"error": FAN_TIMING_TEMP_COMP_NO_RESPONSE_MESSAGE}), 404
    return jsonify(_fan_timing_regs_to_json(regs))


@app.route("/api/fan-timing/send", methods=["POST"])
@_machine_command("send_pid")
def api_fan_timing_send():
    regs = _fan_timing_json_to_regs(request.get_json())
    result = bus.write_registers(ADDR_VACUUM_TIME, regs)
    return jsonify(asdict(result))


# ---------------- temperature compensation ----------------
# Registers 176-179 - inferred from a live photo of the touchscreen's own Advanced Parameters
# screen matching exactly (all 4 read -0.1C at the x10 scale used everywhere else, interpreted
# as signed), not independently confirmed the way most of this app is. See modbus_client.py.

@app.route("/api/temp-comp/latest", methods=["GET"])
def api_temp_comp_latest():
    """Cheap passive peek - see /api/curve/latest for the same pattern."""
    regs = bus.get_latest_temp_comp()
    if regs is None:
        return jsonify({"error": "no temperature compensation data observed yet"}), 404
    return jsonify(_temp_comp_regs_to_json(regs))


@app.route("/api/temp-comp/read", methods=["POST"])
def api_temp_comp_read():
    """Actively requests temperature compensation directly from the board - see MachineBus
    "active reads"."""
    regs = bus.read_temp_comp_active()
    if regs is None:
        return jsonify({"error": FAN_TIMING_TEMP_COMP_NO_RESPONSE_MESSAGE}), 404
    return jsonify(_temp_comp_regs_to_json(regs))


@app.route("/api/temp-comp/send", methods=["POST"])
@_machine_command("send_pid")
def api_temp_comp_send():
    regs = _temp_comp_json_to_regs(request.get_json())
    result = bus.write_registers(ADDR_TEMP_COMP_UPPER, regs)
    return jsonify(asdict(result))


# ---------------- self-tuning (PID autotune - real, physical, perturbs the heaters) ----------------

@app.route("/api/selftune/<zone>", methods=["POST"])
def api_selftune(zone):
    if zone not in SELFTUNE_COILS:
        return jsonify({"error": f"unknown zone '{zone}', expected one of {list(SELFTUNE_COILS)}"}), 400
    value = bool(request.get_json().get("value", True))
    if not value:
        # Turning a self-tune OFF is the way back to safe - never gated.
        return jsonify(asdict(bus.write_coil(SELFTUNE_COILS[zone], False)))
    try:
        with _command("start", starts_heating=True):  # a self-tune heats for real: same rules as Start
            result = bus.write_coil(SELFTUNE_COILS[zone], True)
    except _CommandRefused as exc:
        return jsonify({"ok": False, "attempts": 0, "independently_verified": False, "detail": exc.reason}), 409
    return jsonify(asdict(result))


# ---------------- service restart without sudo (2026-11-12) ----------------
# Every deploy ends with a service restart, and `sudo systemctl restart` needs the Pi account's
# password - which turned out to be forgettable (and cannot be typed by anything automated). This
# app already runs as the ordinary `tim` user under systemd with Restart=on-failure, so it can
# restart itself by exiting with a NONZERO status: systemd treats that as a failure and starts it
# again after RestartSec. No privilege escalation is involved - a process is only ending itself.
#
# Deliberately conservative about WHEN, because a restart discards the in-memory safety state
# (active faults, the ramp tracker, the loaded-profile monitor) and the live-run logging:
#   - only from states where nothing is physically happening (idle / disconnected / unknown), never
#     during a run, pause, cooldown, preflight, self-tune, or an unacknowledged fault;
#   - never while a run is being logged, a start/stop is in progress, or a machine write is in flight;
#   - only under systemd (INVOCATION_ID is set by systemd for every service it launches) - started by
#     hand, nothing would bring the process back, so refuse rather than just kill the app.
_RESTARTABLE_STATES = frozenset({MachineState.IDLE, MachineState.DISCONNECTED, MachineState.UNKNOWN})
_RESTART_EXIT_CODE = 75  # EX_TEMPFAIL; any nonzero code makes Restart=on-failure bring the service back


def _restart_refusal() -> tuple[int, str] | None:
    """(http_status, reason) if a restart must not happen right now, else None."""
    if _inflight_ops > 0:
        return 409, "A machine command (start / send / self-tune) is in progress - wait for it to finish."
    if not os.environ.get("INVOCATION_ID"):
        return 501, ("This server is not running under systemd, so nothing would bring it back after "
                     "it exits. Restart it manually.")
    state = _current_machine_state(bus.get_live_reading())
    if state not in _RESTARTABLE_STATES:
        return 409, (f"Machine state is '{state.value}'. A restart is only allowed while the machine is "
                     f"idle, disconnected, or its state can't be read - stop the run / wait for the "
                     f"cooldown or self-tune to finish / acknowledge the fault first.")
    if _logging_active:
        return 409, "A run is currently being logged - stop it first so its log and summary are finalized."
    if _run_control_lock.locked() or _active_write_progress is not None:
        return 409, "A start/stop or machine write is in progress - wait for it to finish."
    return None


def _restart_process_soon() -> None:
    """Ends this process shortly after the HTTP response has been sent. Closes the serial port
    first so the next instance can open it cleanly; os._exit (not sys.exit) because this runs on a
    helper thread, where SystemExit would only end that thread."""
    def _do() -> None:
        global _restart_pending
        time.sleep(0.75)
        # Re-check just before closing the bus: something may have begun while the response was in flight
        # (a run started at the touchscreen, say). Cancel rather than restart under a live machine.
        with _command_gate_lock:
            if _inflight_ops > 0:
                _restart_pending = False
                logger.warning("service restart CANCELLED just before exit: a machine command is in progress")
                return
        try:
            bus.stop()
        except Exception:
            logger.exception("bus.stop() raised during a requested service restart - exiting anyway")
        os._exit(_RESTART_EXIT_CODE)
    threading.Thread(target=_do, name="service-restart", daemon=True).start()


@app.route("/api/admin/restart", methods=["POST"])
def api_admin_restart():
    if not (request.get_json(silent=True) or {}).get("confirm"):
        return jsonify({"ok": False, "detail": "Restart not confirmed (send {\"confirm\": true})."}), 400
    global _restart_pending
    with _command_gate_lock:
        refusal = _restart_refusal()
        if refusal is None:
            _restart_pending = True  # from here every other machine command is refused (see _command_refusal)
    if refusal is not None:
        status, reason = refusal
        return jsonify({"ok": False, "detail": reason}), status
    logger.warning("Service restart requested via /api/admin/restart - exiting with status %d so systemd restarts it",
                   _RESTART_EXIT_CODE)
    _restart_process_soon()
    return jsonify({"ok": True, "detail": "Restarting - the service will be back in about 10 seconds."})


def main() -> None:
    global bus, _interrupted_run_notice
    interrupted = run_recovery.check_for_interrupted_run()
    if interrupted is not None:
        _interrupted_run_notice = {
            "profile_name": interrupted.profile_name,
            "started_at": interrupted.started_at,
            "log_filename": interrupted.log_filename,
        }
    if len(sys.argv) > 1:
        # An explicit launch argument always wins - someone deliberately overriding the port
        # (testing a different adapter, etc.) shouldn't be silently second-guessed.
        com_port = sys.argv[1]
    else:
        # USB adapter persistent identity (2026-09-08, external feature-gap review, second pass) -
        # see connection_identity.py's module docstring for why the OS-assigned port path alone
        # (what the systemd service file would otherwise hardcode) isn't reliable across reboots/
        # replugs on a headless Pi. Falls back to the old hardcoded default when nothing's been
        # remembered yet (first run) or the remembered adapter isn't currently plugged in.
        com_port = resolve_remembered_port() or "COM4"
    bus = MachineBus(com_port)
    bus.set_ir_reading_correction(safety_limits.ir_reading_correction_c)
    try:
        bus.start()
    except Exception as exc:
        # Previously unguarded - a bad COM port crashed the whole process before app.run() ever
        # started, meaning the web UI itself was unreachable to fix it (a real chicken-and-egg
        # problem on a headless Pi with no console). Now the server comes up regardless, reachable
        # via /api/connection to retry with a different port. Added 2026-08-19.
        _set_connection_error(str(exc))
    else:
        _remember_current_port(com_port)
    _init_auth()
    threading.Thread(target=_poll_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, threaded=True)


if __name__ == "__main__":
    main()
