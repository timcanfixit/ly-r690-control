"""Run Finalization - per-run summary statistics saved as a small JSON sidecar next to each run's
CSV (pc-app/logs/run_<stamp>.summary.json), for numbers that don't fit the CSV's fixed per-reading
columns: fault/warning counts observed DURING the run, and the delta in machine_bus communication
counters between run start and run stop.

Deliberately does NOT compute deviation-from-target or overshoot here: that would need the exact
profile version that was ACTUALLY sent for this specific historical run, and run-to-profile-
version linkage doesn't exist yet (see profile_store.py's versioning) - computing it against
whatever a profile of the same name currently contains could silently misattribute a deviation to
the wrong version if the profile was edited since this run happened. Left as a documented gap
rather than guessed at - a natural follow-up once that linkage exists.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from process_limits import ProcessLimits, classify_with_limits


class RunFinalizationTracker:
    """Create one at run start, call record_fault()/record_warning() as they occur during the
    run (any severity counts - the sidecar distinguishes counts, not which specific faults), then
    pass it to finalize_run() once at run stop.

    `events`/record_event() (2026-09-07, external feature-gap review): a plain timestamped list,
    saved with the run, answering "what actually happened and when" for a specific historical run
    without having to cross-reference application logs (which rotate/aren't kept per-run at all).
    Currently wired up for fault/warning raises and the run start/stop bookends - NOT yet for
    every manual operator action (vacuum/cooling/keep toggles, PID/profile sends mid-run) since
    those live at many separate call sites across both frontends; record_event() is the same
    mechanism a future pass would use to add those, this just doesn't claim to cover them yet.

    `config_snapshot` (also 2026-09-07): whatever was actually in effect when the run STARTED
    (safety limits, alert thresholds, the profile actually sent) - captured once at construction,
    not re-read at finalize time, since settings can change mid-run and what matters for
    understanding a specific historical run is what governed it when it began."""

    def __init__(self, comm_snapshot_start: Optional[dict] = None, config_snapshot: Optional[dict] = None):
        self.fault_count = 0
        self.warning_count = 0
        self.comm_snapshot_start = comm_snapshot_start or {}
        self.config_snapshot = config_snapshot or {}
        self._start_monotonic = time.monotonic()
        self.events: list[dict] = []
        self.record_event("Run started")

    def record_fault(self) -> None:
        self.fault_count += 1

    def record_warning(self) -> None:
        self.warning_count += 1

    def record_event(self, description: str) -> None:
        self.events.append({
            "elapsed_s": round(time.monotonic() - self._start_monotonic, 2),
            "description": description,
        })


_COMM_KEYS = (
    "read_attempts", "read_successes", "read_failures",
    "write_attempts", "write_successes", "write_failures", "retry_count",
)


def compute_comm_delta(start_snapshot: dict, end_snapshot: dict) -> dict:
    return {k: end_snapshot.get(k, 0) - start_snapshot.get(k, 0) for k in _COMM_KEYS}


def compute_time_above_wetting_s(rows: list[dict], wetting_temp_c: float) -> float:
    """`rows` are csv.DictReader rows (string values) - the same shape data_logger.list_runs()
    already reads. Sums real elapsed-time deltas between consecutive rows where THIS row's Upper
    temperature was at or above wetting - the same representative-zone choice alerts.py's own
    wetting check already uses."""
    total = 0.0
    prev_elapsed = None
    for row in rows:
        try:
            elapsed = float(row.get("elapsed_s") or 0)
            upper = float(row.get("upper_temp") or 0)
        except (TypeError, ValueError):
            continue
        if prev_elapsed is not None and upper >= wetting_temp_c:
            total += max(0.0, elapsed - prev_elapsed)
        prev_elapsed = elapsed
    return total


_ZONE_CSV_COLUMNS = {
    "upper": "upper_temp", "lower": "lower_temp", "infrared": "infrared_temp", "probe": "probe_temp",
}


def compute_zone_statistics(rows: list[dict]) -> dict:
    """Per-zone min/max/average/time-to-peak from a run's own already-logged rows - added
    2026-09-07 per external feature-gap review. Deliberately simple descriptive statistics, not a
    claim about process quality (see classify_process_result() for the one judgment call this
    module does make, and its own docstring for why it stays conservative). A zone with no
    parseable readings at all (shouldn't normally happen, but a truncated/corrupt CSV tail is a
    real possibility) is simply omitted rather than reported as a fabricated 0.0."""
    stats: dict[str, dict] = {}
    for zone, column in _ZONE_CSV_COLUMNS.items():
        values: list[tuple[float, float]] = []  # (elapsed_s, temp)
        for row in rows:
            try:
                elapsed = float(row.get("elapsed_s") or 0)
                temp = float(row.get(column) or 0)
            except (TypeError, ValueError):
                continue
            values.append((elapsed, temp))
        if not values:
            continue
        temps = [t for _, t in values]
        peak_elapsed, peak_temp = max(values, key=lambda v: v[1])
        stats[zone] = {
            "min_c": min(temps),
            "max_c": peak_temp,
            "avg_c": sum(temps) / len(temps),
            "time_to_peak_s": peak_elapsed,
        }
    return stats


def classify_process_result(fault_count: int, time_above_wetting_s: float) -> str:
    """Deliberately conservative three-way classification, NOT a real metallurgical pass/fail
    against a target temperature window - that would need per-recipe process limits (target ±
    tolerance, required TAL range, etc.) which this project doesn't have a data model for yet (see
    this module's own top-level docstring for the same "don't guess at run-to-profile-version
    linkage" caution). What this CAN say honestly from data already logged:

    - "FAIL": a fault was raised during the run - a real, already-confirmed problem.
    - "PASS": no fault, AND the run actually reached wetting temperature at some point - real
      reflow activity was observed, not just "nothing went wrong".
    - "INCOMPLETE": no fault, but wetting was never reached - ambiguous on purpose (a short manual
      test, a self-tune, or a genuinely aborted/too-cold run all look the same from this data
      alone) - reporting this as FAIL would falsely flag legitimate non-reflow runs; reporting it
      as PASS would claim a successful reflow that never happened. See docs for the recipe/
      process-limits system that would let this become a real target-window PASS/FAIL."""
    if fault_count > 0:
        return "FAIL"
    if time_above_wetting_s > 0:
        return "PASS"
    return "INCOMPLETE"


def compute_cooling_rate_c_per_min(rows: list[dict]) -> Optional[float]:
    """Rate of Upper temperature decrease across whatever portion of THIS run's own logged data
    had the cooling coil active - None if cooling was never observed active during the run. The
    log ends the instant Stop is pressed, so a run stopped before any post-run cooldown simply has
    nothing to measure here; that's an honest "not observed", not a zero."""
    cooling_rows = [r for r in rows if (r.get("cooling") or "").strip().lower() in ("true", "1")]
    if len(cooling_rows) < 2:
        return None
    try:
        first, last = cooling_rows[0], cooling_rows[-1]
        dt_min = (float(last["elapsed_s"]) - float(first["elapsed_s"])) / 60.0
        if dt_min <= 0:
            return None
        d_temp = float(first["upper_temp"]) - float(last["upper_temp"])
        return d_temp / dt_min
    except (KeyError, ValueError):
        return None


def finalize_run(csv_path: Path, tracker: RunFinalizationTracker, comm_snapshot_end: dict,
                  rows: list[dict], wetting_temp_c: float,
                  process_limits: Optional[ProcessLimits] = None) -> dict:
    """Computes and saves the sidecar summary for a just-finished run. `rows` is the run's own
    already-logged CSV rows (csv.DictReader output) - nothing here re-reads live machine state.

    `process_limits` (2026-09-07, external feature-gap review): when the profile that was
    actually run has a configured quality window (see process_limits.py), "process_result"
    becomes a real PASS/FAIL against that window instead of the conservative three-way guess -
    "process_result_basis" and "process_limit_violations" record which method was used and why,
    so a historical run's judgment is always traceable rather than a bare label."""
    tracker.record_event("Run stopped")
    time_above_wetting_s = compute_time_above_wetting_s(rows, wetting_temp_c)
    zone_statistics = compute_zone_statistics(rows)
    if process_limits is not None and not process_limits.is_empty():
        process_result, violations = classify_with_limits(
            tracker.fault_count, time_above_wetting_s, zone_statistics, process_limits,
        )
        process_result_basis = "process_limits"
    else:
        process_result = classify_process_result(tracker.fault_count, time_above_wetting_s)
        violations = []
        process_result_basis = "conservative"
    summary = {
        "fault_count": tracker.fault_count,
        "warning_count": tracker.warning_count,
        "communication": compute_comm_delta(tracker.comm_snapshot_start, comm_snapshot_end),
        "time_above_wetting_s": time_above_wetting_s,
        "cooling_rate_c_per_min": compute_cooling_rate_c_per_min(rows),
        "zone_statistics": zone_statistics,
        "process_result": process_result,
        "process_result_basis": process_result_basis,
        "process_limit_violations": violations,
        "events": tracker.events,
        "config_snapshot": tracker.config_snapshot,
    }
    csv_path.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def load_summary(csv_path: Path) -> Optional[dict]:
    summary_path = csv_path.with_suffix(".summary.json")
    if not summary_path.exists():
        return None
    try:
        return json.loads(summary_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


_REPAIR_OUTCOMES = ("Success", "Partial", "Failed", "Unknown")


def set_repair_outcome(csv_path: Path, outcome: str, failure_reason: str = "") -> dict:
    """Attaches a repair outcome to a specific run - 2026-09-07, external feature-gap review,
    second pass. Deliberately the SMALLEST possible version of this idea: one status field plus
    one free-text reason, no job/customer/ticket tracking, no separate database - this is a
    reflow-quality signal recorded against the run that produced it, not a repair-shop CRM (which
    this project has repeatedly decided against building - see the "shelve unless scale changes"
    tier of the external feature-gap review). `outcome` should be one of _REPAIR_OUTCOMES but
    isn't enforced here (the UI constrains it; this layer just stores whatever it's given, the
    same trust level as set_run_notes() for free text)."""
    summary = load_summary(csv_path) or {}
    summary["repair_outcome"] = outcome
    summary["repair_failure_reason"] = failure_reason
    csv_path.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def set_run_notes(csv_path: Path, notes: str) -> dict:
    """Attaches (or clears, with notes="") a free-text operator note to a specific historical run -
    2026-09-07, external feature-gap review, second pass. The kind of thing worth writing down
    ("cold joint on U4, reflowed a second time at higher IR") has nowhere else to live - the CSV is
    fixed-column telemetry, and the config snapshot/event timeline are both auto-generated, not
    operator-editable. Same "works even with no sidecar yet" behavior as set_run_golden() for the
    same reason - a note shouldn't require finalize_run to have already run."""
    summary = load_summary(csv_path) or {}
    summary["notes"] = notes
    csv_path.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def set_run_golden(csv_path: Path, golden: bool) -> dict:
    """Marks (or unmarks) a run as a reference "golden run" to compare later runs against -
    2026-09-07, external feature-gap review. Deliberately reuses compare_runs() (run_analysis.py)
    rather than building a separate comparison mechanism - a golden run is just an ordinary run
    the operator has flagged as a known-good reference; comparing "this run vs the golden one" is
    exactly the same operation as comparing any other two runs. Any number of runs may be marked
    golden at once (e.g. one per profile) - this deliberately doesn't enforce "only one golden run
    ever", that's a UI/operator convention, not something this layer should silently assume.
    Works even for a run with no finalization sidecar yet (older runs, or one that was never
    finalized for some other reason) - creates a minimal one rather than requiring finalize_run to
    have already run."""
    summary = load_summary(csv_path) or {}
    summary["golden"] = golden
    csv_path.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2))
    return summary
