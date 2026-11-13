import data_logger as dl
from modbus_client import LiveReading


def _reading():
    return LiveReading(
        upper_temp=200.0, lower_temp=180.0, infrared_temp=100.0, probe_temp=90.0,
        running=True, running_mirror=True, vacuum=False, cooling=False, keep=False,
    )


def test_reentrant_start_run_closes_previous_file_handle(tmp_path, monkeypatch):
    """Regression test: calling start_run() twice without stop_run() in between (a
    duplicate/racing Start request) used to orphan the first file handle, open and never
    flushed/closed again for the life of the process."""
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    logger.start_run("A")
    first_file = logger._file
    assert not first_file.closed
    logger.start_run("B")
    assert first_file.closed
    logger.stop_run()


def test_same_second_starts_get_distinct_filenames(tmp_path, monkeypatch):
    """Regression test: the timestamp-only filename has 1-second resolution, so two runs started
    within the same second used to collide on the same filename and silently overwrite data."""
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    p1 = logger.start_run("A")
    p2 = logger.start_run("B")
    logger.stop_run()
    assert p1 != p2
    assert p1.exists() and p2.exists()


def test_log_and_list_runs_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    logger.start_run("TestProfile")
    logger.log(_reading())
    logger.log(_reading())
    logger.stop_run()

    runs = dl.list_runs()
    assert len(runs) == 1
    assert runs[0]["profile_name"] == "TestProfile"
    assert runs[0]["peak_upper"] == 200.0
    assert runs[0]["row_count"] == 2


def test_list_runs_empty_when_no_logs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path / "does_not_exist")
    assert dl.list_runs() == []


def test_log_before_start_run_is_a_safe_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    logger.log(_reading())  # must not raise


def test_list_runs_merges_finalization_sidecar_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    path = logger.start_run("WithSummary")
    logger.log(_reading())
    logger.stop_run()
    path.with_suffix(".summary.json").write_text(
        '{"fault_count": 2, "warning_count": 1, "time_above_wetting_s": 15.0, '
        '"cooling_rate_c_per_min": 12.5, "communication": {"read_attempts": 100}}'
    )
    runs = dl.list_runs()
    assert runs[0]["fault_count"] == 2
    assert runs[0]["warning_count"] == 1
    assert runs[0]["cooling_rate_c_per_min"] == 12.5
    assert runs[0]["communication"]["read_attempts"] == 100


def test_list_runs_reports_none_not_zero_when_sidecar_missing(tmp_path, monkeypatch):
    """A run logged before Run Finalization existed has no sidecar at all - must report as
    unavailable, not fabricate a 0 count."""
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    logger.start_run("NoSummary")
    logger.log(_reading())
    logger.stop_run()
    runs = dl.list_runs()
    assert runs[0]["fault_count"] is None
    assert runs[0]["warning_count"] is None


def test_list_runs_merges_zone_statistics_and_process_result(tmp_path, monkeypatch):
    """Added 2026-09-07 per external feature-gap review - list_runs() cherry-picks specific
    fields out of the sidecar rather than spreading the whole dict, so a new sidecar field needs
    an explicit merge line here too (this test exists to catch exactly that kind of omission)."""
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    path = logger.start_run("WithZoneStats")
    logger.log(_reading())
    logger.stop_run()
    path.with_suffix(".summary.json").write_text(
        '{"process_result": "PASS", "zone_statistics": {"upper": {"max_c": 250.0}}}'
    )
    runs = dl.list_runs()
    assert runs[0]["process_result"] == "PASS"
    assert runs[0]["zone_statistics"]["upper"]["max_c"] == 250.0


def test_list_runs_reports_none_for_process_result_when_sidecar_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    logger.start_run("NoSummary")
    logger.log(_reading())
    logger.stop_run()
    runs = dl.list_runs()
    assert runs[0]["process_result"] is None
    assert runs[0]["zone_statistics"] is None


def test_list_runs_reports_golden_false_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    logger.start_run("NotGolden")
    logger.log(_reading())
    logger.stop_run()
    assert dl.list_runs()[0]["golden"] is False


def test_mark_run_golden_by_filename_and_reflected_in_list_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    path = logger.start_run("GoldenCandidate")
    logger.log(_reading())
    logger.stop_run()

    dl.mark_run_golden(path.name, True)
    runs = dl.list_runs()
    assert runs[0]["golden"] is True

    dl.mark_run_golden(path.name, False)
    assert dl.list_runs()[0]["golden"] is False


def test_mark_run_golden_rejects_path_traversal_attempts(tmp_path, monkeypatch):
    """filename can come straight from an HTTP request body (the web app's golden-toggle route) -
    must not be usable to write outside LOGS_DIR."""
    import pytest
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    for bad_filename in ("../../etc/passwd", "run_../../evil.csv", "/etc/passwd", "not_a_run_file.csv", "run_20260907.txt"):
        with pytest.raises(ValueError):
            dl.mark_run_golden(bad_filename, True)


def test_delete_run_removes_csv_and_summary_sidecar(tmp_path, monkeypatch):
    """2026-09-16, real reported gap: no way to delete a run at all."""
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    path = logger.start_run("ToDelete")
    logger.log(_reading())
    logger.stop_run()
    dl.mark_run_notes(path.name, "some note")  # creates the .summary.json sidecar
    assert path.exists()
    assert path.with_suffix(".summary.json").exists()

    result = dl.delete_run(path.name)

    assert result["ok"] is True
    assert set(result["deleted"]) == {path.name, path.with_suffix(".summary.json").name}
    assert not path.exists()
    assert not path.with_suffix(".summary.json").exists()
    assert dl.list_runs() == []


def test_delete_run_without_a_summary_sidecar_still_works(tmp_path, monkeypatch):
    """A run that never got finalized (e.g. app crashed mid-run) has no .summary.json at all -
    deleting it must not fail just because the sidecar is missing."""
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    path = logger.start_run("NoSidecar")
    logger.log(_reading())
    logger.stop_run()
    assert not path.with_suffix(".summary.json").exists()

    result = dl.delete_run(path.name)

    assert result["ok"] is True
    assert result["deleted"] == [path.name]
    assert not path.exists()


def test_delete_run_reports_failure_for_nonexistent_run(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    result = dl.delete_run("run_20260101_000000.csv")
    assert result["ok"] is False


def test_delete_run_rejects_path_traversal_attempts(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    for bad_filename in ("../../etc/passwd", "run_../../evil.csv", "/etc/passwd", "not_a_run_file.csv", "run_20260907.txt"):
        with pytest.raises(ValueError):
            dl.delete_run(bad_filename)


def test_list_runs_reports_empty_notes_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    logger.start_run("NoNotes")
    logger.log(_reading())
    logger.stop_run()
    assert dl.list_runs()[0]["notes"] == ""


def test_mark_run_notes_by_filename_and_reflected_in_list_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    path = logger.start_run("NeedsANote")
    logger.log(_reading())
    logger.stop_run()

    dl.mark_run_notes(path.name, "Cold joint on U4, reflowed a second time at higher IR.")
    runs = dl.list_runs()
    assert runs[0]["notes"] == "Cold joint on U4, reflowed a second time at higher IR."

    dl.mark_run_notes(path.name, "")
    assert dl.list_runs()[0]["notes"] == ""


def test_mark_run_notes_rejects_path_traversal_attempts(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    for bad_filename in ("../../etc/passwd", "run_../../evil.csv", "/etc/passwd", "not_a_run_file.csv"):
        with pytest.raises(ValueError):
            dl.mark_run_notes(bad_filename, "hi")


def test_list_runs_reports_empty_repair_outcome_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    logger.start_run("NoOutcome")
    logger.log(_reading())
    logger.stop_run()
    run = dl.list_runs()[0]
    assert run["repair_outcome"] == ""
    assert run["repair_failure_reason"] == ""


def test_mark_repair_outcome_by_filename_and_reflected_in_list_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    path = logger.start_run("RepairTest")
    logger.log(_reading())
    logger.stop_run()

    dl.mark_repair_outcome(path.name, "Failed", "Still no video output.")
    run = dl.list_runs()[0]
    assert run["repair_outcome"] == "Failed"
    assert run["repair_failure_reason"] == "Still no video output."


def test_mark_repair_outcome_rejects_path_traversal_attempts(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    for bad_filename in ("../../etc/passwd", "run_../../evil.csv", "/etc/passwd", "not_a_run_file.csv"):
        with pytest.raises(ValueError):
            dl.mark_repair_outcome(bad_filename, "Success")


def test_get_run_summary_returns_the_full_sidecar(tmp_path, monkeypatch):
    """Unlike list_runs()'s cherry-picked subset - this returns everything in the sidecar,
    including events/config_snapshot which list_runs() never merges in at all."""
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    path = logger.start_run("DetailTest")
    logger.log(_reading())
    logger.stop_run()
    path.with_suffix(".summary.json").write_text(
        '{"events": [{"elapsed_s": 0.0, "description": "Run started"}], '
        '"config_snapshot": {"profile": {"name": "DetailTest"}}}'
    )
    summary = dl.get_run_summary(path.name)
    assert summary["events"][0]["description"] == "Run started"
    assert summary["config_snapshot"]["profile"]["name"] == "DetailTest"


def test_get_run_summary_returns_none_when_no_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    logger.start_run("NoSidecar")
    logger.log(_reading())
    logger.stop_run()
    runs = dl.list_runs()
    assert dl.get_run_summary(runs[0]["filename"]) is None


def test_get_run_summary_rejects_path_traversal_attempts(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    with pytest.raises(ValueError):
        dl.get_run_summary("../../etc/passwd")
