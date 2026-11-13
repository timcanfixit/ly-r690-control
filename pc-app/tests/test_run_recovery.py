import run_recovery as rr


def test_no_marker_means_no_interrupted_run(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "MARKER_PATH", tmp_path / "active_run.json")
    assert rr.check_for_interrupted_run() is None


def test_marker_left_behind_is_detected_as_interrupted(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "MARKER_PATH", tmp_path / "active_run.json")
    rr.mark_run_started("MyProfile", "run_20260822_120000.csv")
    found = rr.check_for_interrupted_run()
    assert found is not None
    assert found.profile_name == "MyProfile"
    assert found.log_filename == "run_20260822_120000.csv"


def test_clean_stop_clears_the_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "MARKER_PATH", tmp_path / "active_run.json")
    rr.mark_run_started("MyProfile", "run_x.csv")
    rr.mark_run_ended()
    assert rr.check_for_interrupted_run() is None


def test_mark_ended_is_safe_to_call_when_no_marker_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "MARKER_PATH", tmp_path / "active_run.json")
    rr.mark_run_ended()  # must not raise


def test_corrupt_marker_file_is_treated_as_no_interrupted_run(tmp_path, monkeypatch):
    marker = tmp_path / "active_run.json"
    marker.write_text("not valid json{{{")
    monkeypatch.setattr(rr, "MARKER_PATH", marker)
    assert rr.check_for_interrupted_run() is None


def test_mark_run_as_interrupted_flags_the_sidecar(tmp_path, monkeypatch):
    import data_logger as dl

    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    csv_path = tmp_path / "run_x.csv"
    csv_path.write_text("dummy")
    rr.mark_run_as_interrupted("run_x.csv")
    import json
    summary = json.loads(csv_path.with_suffix(".summary.json").read_text())
    assert summary["interrupted"] is True


def test_acknowledge_interrupted_run_flags_and_clears_marker(tmp_path, monkeypatch):
    import data_logger as dl

    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(rr, "MARKER_PATH", tmp_path / "active_run.json")
    csv_path = tmp_path / "run_y.csv"
    csv_path.write_text("dummy")
    rr.mark_run_started("P", "run_y.csv")

    rr.acknowledge_interrupted_run("run_y.csv")

    assert rr.check_for_interrupted_run() is None
    import json
    summary = json.loads(csv_path.with_suffix(".summary.json").read_text())
    assert summary["interrupted"] is True
