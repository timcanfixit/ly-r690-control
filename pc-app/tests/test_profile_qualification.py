import profile_qualification as pq


def test_qualified_when_thermal_pass_count_meets_threshold(monkeypatch):
    runs = [{"profile_name": "P", "process_result": "PASS", "repair_outcome": ""} for _ in range(pq.QUALIFICATION_THRESHOLD_RUNS)]
    monkeypatch.setattr(pq, "list_runs", lambda: runs)
    status = pq.compute_qualification_status("P")
    assert status.thermal_pass_count == pq.QUALIFICATION_THRESHOLD_RUNS
    assert status.qualified


def test_qualified_when_repair_success_count_meets_threshold_even_with_no_thermal_pass(monkeypatch):
    runs = [{"profile_name": "P", "process_result": "FAIL", "repair_outcome": "Success"} for _ in range(pq.QUALIFICATION_THRESHOLD_RUNS)]
    monkeypatch.setattr(pq, "list_runs", lambda: runs)
    status = pq.compute_qualification_status("P")
    assert status.thermal_pass_count == 0
    assert status.repair_success_count == pq.QUALIFICATION_THRESHOLD_RUNS
    assert status.qualified


def test_not_qualified_below_threshold_on_both_measures(monkeypatch):
    runs = [{"profile_name": "P", "process_result": "PASS", "repair_outcome": ""}]  # only 1, need 3
    monkeypatch.setattr(pq, "list_runs", lambda: runs)
    status = pq.compute_qualification_status("P")
    assert not status.qualified


def test_runs_of_other_profiles_are_ignored(monkeypatch):
    runs = [{"profile_name": "Other", "process_result": "PASS", "repair_outcome": "Success"} for _ in range(5)]
    monkeypatch.setattr(pq, "list_runs", lambda: runs)
    status = pq.compute_qualification_status("P")
    assert status.total_runs == 0
    assert not status.qualified


def test_missing_fields_do_not_crash_and_count_as_not_passing(monkeypatch):
    """Older runs from before process_result/repair_outcome existed have neither key at all."""
    monkeypatch.setattr(pq, "list_runs", lambda: [{"profile_name": "P"}])
    status = pq.compute_qualification_status("P")
    assert status.total_runs == 1
    assert status.thermal_pass_count == 0
    assert status.repair_success_count == 0
