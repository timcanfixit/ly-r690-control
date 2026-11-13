from pathlib import Path

from run_finalization import (
    RunFinalizationTracker, compute_comm_delta, compute_time_above_wetting_s,
    compute_cooling_rate_c_per_min, compute_zone_statistics, classify_process_result,
    finalize_run, load_summary, set_run_golden, set_run_notes, set_repair_outcome,
)
from process_limits import ProcessLimits


def test_tracker_counts_faults_and_warnings_separately():
    tracker = RunFinalizationTracker()
    tracker.record_fault()
    tracker.record_fault()
    tracker.record_warning()
    assert tracker.fault_count == 2
    assert tracker.warning_count == 1


# ---- event timeline + config snapshot (2026-09-07, external feature-gap review)

def test_tracker_auto_records_a_start_event():
    """Every tracker begins its own timeline with a "Run started" entry at construction, so a
    caller never has to remember to record it separately."""
    tracker = RunFinalizationTracker()
    assert len(tracker.events) == 1
    assert tracker.events[0]["description"] == "Run started"
    assert tracker.events[0]["elapsed_s"] == 0.0


def test_record_event_appends_with_elapsed_time():
    tracker = RunFinalizationTracker()
    tracker.record_event("Vacuum enabled")
    assert len(tracker.events) == 2
    assert tracker.events[1]["description"] == "Vacuum enabled"
    assert tracker.events[1]["elapsed_s"] >= 0.0


def test_config_snapshot_defaults_to_empty_dict_not_none():
    tracker = RunFinalizationTracker()
    assert tracker.config_snapshot == {}


def test_config_snapshot_stores_whatever_was_passed():
    snapshot = {"safety_limits": {"max_upper_temp_c": 320.0}, "profile": {"name": "PS5 APU"}}
    tracker = RunFinalizationTracker(config_snapshot=snapshot)
    assert tracker.config_snapshot == snapshot


def test_finalize_run_includes_events_and_config_snapshot_and_appends_stop_event(tmp_path):
    csv_path = tmp_path / "run_20260907_150000.csv"
    csv_path.write_text("dummy")
    tracker = RunFinalizationTracker(config_snapshot={"profile": {"name": "Test Profile"}})
    tracker.record_event("Vacuum enabled")
    rows = [{"elapsed_s": "0", "upper_temp": "100", "cooling": "False"}]
    summary = finalize_run(csv_path, tracker, {}, rows, wetting_temp_c=217.0)
    descriptions = [e["description"] for e in summary["events"]]
    assert descriptions == ["Run started", "Vacuum enabled", "Run stopped"]
    assert summary["config_snapshot"]["profile"]["name"] == "Test Profile"


def test_comm_delta_computes_difference_not_absolute():
    start = {"read_attempts": 10, "read_successes": 9, "read_failures": 1,
             "write_attempts": 2, "write_successes": 2, "write_failures": 0, "retry_count": 3}
    end = {"read_attempts": 50, "read_successes": 45, "read_failures": 5,
           "write_attempts": 4, "write_successes": 3, "write_failures": 1, "retry_count": 20}
    delta = compute_comm_delta(start, end)
    assert delta["read_attempts"] == 40
    assert delta["write_failures"] == 1
    assert delta["retry_count"] == 17


def test_time_above_wetting_sums_only_qualifying_intervals():
    rows = [
        {"elapsed_s": "0", "upper_temp": "100"},
        {"elapsed_s": "10", "upper_temp": "210"},   # below wetting (217) - gap to next not counted
        {"elapsed_s": "20", "upper_temp": "220"},   # at/above wetting - counts the 20->30 gap
        {"elapsed_s": "30", "upper_temp": "225"},   # still above - counts the 30->40 gap
        {"elapsed_s": "40", "upper_temp": "150"},   # dropped below
    ]
    total = compute_time_above_wetting_s(rows, wetting_temp_c=217.0)
    assert total == 20.0  # (30-20) + (40-30)


def test_time_above_wetting_zero_when_never_reached():
    rows = [{"elapsed_s": str(i * 10), "upper_temp": "100"} for i in range(5)]
    assert compute_time_above_wetting_s(rows, wetting_temp_c=217.0) == 0.0


def test_cooling_rate_none_when_cooling_never_observed():
    rows = [{"elapsed_s": "0", "upper_temp": "200", "cooling": "False"}]
    assert compute_cooling_rate_c_per_min(rows) is None


def test_cooling_rate_computed_from_observed_cooling_window():
    rows = [
        {"elapsed_s": "0", "upper_temp": "250", "cooling": "True"},
        {"elapsed_s": "60", "upper_temp": "220", "cooling": "True"},
    ]
    rate = compute_cooling_rate_c_per_min(rows)
    assert abs(rate - 30.0) < 1e-9  # 30C drop over 1 minute


def test_finalize_run_writes_and_loads_sidecar(tmp_path):
    csv_path = tmp_path / "run_20260822_120000.csv"
    csv_path.write_text("dummy")  # finalize_run doesn't need real CSV content, just the path
    tracker = RunFinalizationTracker(comm_snapshot_start={"read_attempts": 0})
    tracker.record_fault()
    rows = [
        {"elapsed_s": "0", "upper_temp": "220", "cooling": "False"},
        {"elapsed_s": "10", "upper_temp": "220", "cooling": "False"},
    ]
    summary = finalize_run(csv_path, tracker, {"read_attempts": 5}, rows, wetting_temp_c=217.0)
    assert summary["fault_count"] == 1
    assert summary["communication"]["read_attempts"] == 5

    loaded = load_summary(csv_path)
    assert loaded == summary


def test_load_summary_returns_none_when_missing(tmp_path):
    assert load_summary(tmp_path / "run_nonexistent.csv") is None


# ---- per-zone statistics + process-result classification (2026-09-07, external feature-gap
# review) - see compute_zone_statistics()/classify_process_result()'s own docstrings for exactly
# what these do and, just as importantly, don't claim to (no real target-window pass/fail without
# a process-limits data model this project doesn't have yet).

def test_zone_statistics_computes_min_max_avg_and_time_to_peak():
    rows = [
        {"elapsed_s": "0", "upper_temp": "100", "lower_temp": "100", "infrared_temp": "50", "probe_temp": "50"},
        {"elapsed_s": "10", "upper_temp": "200", "lower_temp": "150", "infrared_temp": "80", "probe_temp": "70"},
        {"elapsed_s": "20", "upper_temp": "250", "lower_temp": "180", "infrared_temp": "90", "probe_temp": "85"},
        {"elapsed_s": "30", "upper_temp": "220", "lower_temp": "160", "infrared_temp": "85", "probe_temp": "80"},
    ]
    stats = compute_zone_statistics(rows)
    assert stats["upper"]["min_c"] == 100.0
    assert stats["upper"]["max_c"] == 250.0
    assert stats["upper"]["avg_c"] == (100 + 200 + 250 + 220) / 4
    assert stats["upper"]["time_to_peak_s"] == 20.0  # peak (250) happened at elapsed_s=20
    assert stats["probe"]["max_c"] == 85.0


def test_zone_statistics_skips_zones_with_no_parseable_data():
    rows = [{"elapsed_s": "0", "upper_temp": "not a number"}]
    stats = compute_zone_statistics(rows)
    assert "upper" not in stats


def test_zone_statistics_empty_rows_produces_empty_stats():
    assert compute_zone_statistics([]) == {}


def test_process_result_fail_when_any_fault_occurred():
    """A fault always means FAIL, regardless of whether wetting was reached - a real, confirmed
    problem outranks the ambiguous "did it reflow" signal."""
    assert classify_process_result(fault_count=1, time_above_wetting_s=45.0) == "FAIL"
    assert classify_process_result(fault_count=1, time_above_wetting_s=0.0) == "FAIL"


def test_process_result_pass_when_no_fault_and_wetting_reached():
    assert classify_process_result(fault_count=0, time_above_wetting_s=45.0) == "PASS"


def test_process_result_incomplete_when_no_fault_but_wetting_never_reached():
    """Deliberately NOT called FAIL - a short manual test or an intentionally low-temp run
    looks identical to an aborted reflow from this data alone, and calling it FAIL would be a
    real, confident-sounding claim this data doesn't actually support."""
    assert classify_process_result(fault_count=0, time_above_wetting_s=0.0) == "INCOMPLETE"


def test_finalize_run_includes_zone_statistics_and_process_result(tmp_path):
    csv_path = tmp_path / "run_20260907_120000.csv"
    csv_path.write_text("dummy")
    tracker = RunFinalizationTracker()
    rows = [
        {"elapsed_s": "0", "upper_temp": "100", "lower_temp": "100", "infrared_temp": "100", "probe_temp": "100", "cooling": "False"},
        {"elapsed_s": "10", "upper_temp": "230", "lower_temp": "230", "infrared_temp": "230", "probe_temp": "230", "cooling": "False"},
    ]
    summary = finalize_run(csv_path, tracker, {}, rows, wetting_temp_c=217.0)
    assert summary["zone_statistics"]["upper"]["max_c"] == 230.0
    assert summary["process_result"] == "PASS"  # no fault, and it crossed wetting (217) at elapsed_s=10


# ---- golden run marking (2026-09-07, external feature-gap review) - deliberately reuses
# run_analysis.compare_runs() for the actual comparison; this is only the "mark it" mechanism.

def test_set_run_golden_creates_a_summary_when_none_exists(tmp_path):
    """Must work even for a run with no finalization sidecar at all - not require finalize_run
    to have already run."""
    csv_path = tmp_path / "run_20260907_160000.csv"
    csv_path.write_text("dummy")
    summary = set_run_golden(csv_path, True)
    assert summary["golden"] is True
    assert load_summary(csv_path)["golden"] is True


def test_set_run_golden_preserves_existing_summary_fields(tmp_path):
    csv_path = tmp_path / "run_20260907_161000.csv"
    csv_path.write_text("dummy")
    tracker = RunFinalizationTracker()
    tracker.record_fault()
    finalize_run(csv_path, tracker, {}, [{"elapsed_s": "0", "upper_temp": "100"}], wetting_temp_c=217.0)

    set_run_golden(csv_path, True)
    summary = load_summary(csv_path)
    assert summary["golden"] is True
    assert summary["fault_count"] == 1  # untouched by the golden marking


def test_set_run_golden_can_be_unmarked(tmp_path):
    csv_path = tmp_path / "run_20260907_162000.csv"
    csv_path.write_text("dummy")
    set_run_golden(csv_path, True)
    set_run_golden(csv_path, False)
    assert load_summary(csv_path)["golden"] is False


# ---- process_limits wiring (2026-09-07, external feature-gap review, second pass) - see
# process_limits.py for the actual classification logic; these confirm finalize_run() picks the
# right method and records which one it used.

def test_finalize_run_uses_conservative_classification_when_no_process_limits(tmp_path):
    csv_path = tmp_path / "run_20260907_170000.csv"
    csv_path.write_text("dummy")
    tracker = RunFinalizationTracker()
    rows = [{"elapsed_s": "0", "upper_temp": "100", "cooling": "False"}]
    summary = finalize_run(csv_path, tracker, {}, rows, wetting_temp_c=217.0)
    assert summary["process_result"] == "INCOMPLETE"
    assert summary["process_result_basis"] == "conservative"
    assert summary["process_limit_violations"] == []


def test_finalize_run_uses_process_limits_when_provided_and_non_empty(tmp_path):
    csv_path = tmp_path / "run_20260907_171000.csv"
    csv_path.write_text("dummy")
    tracker = RunFinalizationTracker()
    rows = [
        {"elapsed_s": "0", "upper_temp": "220", "cooling": "False"},
        {"elapsed_s": "5", "upper_temp": "220", "cooling": "False"},
    ]
    limits = ProcessLimits(min_time_above_wetting_s=45.0)
    summary = finalize_run(csv_path, tracker, {}, rows, wetting_temp_c=217.0, process_limits=limits)
    assert summary["process_result"] == "FAIL"  # only 5s above wetting, needs 45s
    assert summary["process_result_basis"] == "process_limits"
    assert any("below required minimum" in v for v in summary["process_limit_violations"])


def test_set_run_notes_creates_a_summary_when_none_exists(tmp_path):
    csv_path = tmp_path / "run_20260908_100000.csv"
    csv_path.write_text("dummy")
    summary = set_run_notes(csv_path, "Cold joint on U4.")
    assert summary["notes"] == "Cold joint on U4."
    assert load_summary(csv_path)["notes"] == "Cold joint on U4."


def test_set_run_notes_preserves_existing_summary_fields(tmp_path):
    csv_path = tmp_path / "run_20260908_101000.csv"
    csv_path.write_text("dummy")
    tracker = RunFinalizationTracker()
    tracker.record_fault()
    finalize_run(csv_path, tracker, {}, [{"elapsed_s": "0", "upper_temp": "100"}], wetting_temp_c=217.0)

    set_run_notes(csv_path, "Retest after rework.")
    summary = load_summary(csv_path)
    assert summary["notes"] == "Retest after rework."
    assert summary["fault_count"] == 1


def test_set_run_notes_can_be_cleared(tmp_path):
    csv_path = tmp_path / "run_20260908_102000.csv"
    csv_path.write_text("dummy")
    set_run_notes(csv_path, "Some note.")
    set_run_notes(csv_path, "")
    assert load_summary(csv_path)["notes"] == ""


def test_set_repair_outcome_creates_a_summary_when_none_exists(tmp_path):
    csv_path = tmp_path / "run_20260908_110000.csv"
    csv_path.write_text("dummy")
    summary = set_repair_outcome(csv_path, "Success")
    assert summary["repair_outcome"] == "Success"
    assert summary["repair_failure_reason"] == ""
    assert load_summary(csv_path)["repair_outcome"] == "Success"


def test_set_repair_outcome_stores_failure_reason(tmp_path):
    csv_path = tmp_path / "run_20260908_111000.csv"
    csv_path.write_text("dummy")
    set_repair_outcome(csv_path, "Failed", "Board still didn't boot after reflow.")
    summary = load_summary(csv_path)
    assert summary["repair_outcome"] == "Failed"
    assert summary["repair_failure_reason"] == "Board still didn't boot after reflow."


def test_set_repair_outcome_preserves_existing_summary_fields(tmp_path):
    csv_path = tmp_path / "run_20260908_112000.csv"
    csv_path.write_text("dummy")
    tracker = RunFinalizationTracker()
    tracker.record_fault()
    finalize_run(csv_path, tracker, {}, [{"elapsed_s": "0", "upper_temp": "100"}], wetting_temp_c=217.0)

    set_repair_outcome(csv_path, "Partial")
    summary = load_summary(csv_path)
    assert summary["repair_outcome"] == "Partial"
    assert summary["fault_count"] == 1


def test_finalize_run_falls_back_to_conservative_when_process_limits_is_empty(tmp_path):
    """An empty ProcessLimits() (all fields None) means "no window configured" just like passing
    None - must not be treated as "a window exists and nothing failed"."""
    csv_path = tmp_path / "run_20260907_172000.csv"
    csv_path.write_text("dummy")
    tracker = RunFinalizationTracker()
    rows = [{"elapsed_s": "0", "upper_temp": "100", "cooling": "False"}]
    summary = finalize_run(csv_path, tracker, {}, rows, wetting_temp_c=217.0, process_limits=ProcessLimits())
    assert summary["process_result_basis"] == "conservative"
