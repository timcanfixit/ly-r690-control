import machine_config_snapshot as mcs


def test_save_and_load_known_good_config(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs, "KNOWN_GOOD_CONFIG_PATH", tmp_path / "known_good_config.json")
    saved = mcs.save_known_good_config(pid=[1, 2, 3], fan_timing=[10, 20, 30, 40], temp_comp=[0, 0, 0, 0])
    loaded = mcs.load_known_good_config()
    assert loaded.pid == [1, 2, 3]
    assert loaded.fan_timing == [10, 20, 30, 40]
    assert loaded.temp_comp == [0, 0, 0, 0]
    assert loaded.captured_at == saved.captured_at


def test_load_returns_none_when_no_file_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs, "KNOWN_GOOD_CONFIG_PATH", tmp_path / "missing.json")
    assert mcs.load_known_good_config() is None


def test_load_returns_none_on_corrupt_json(tmp_path, monkeypatch):
    path = tmp_path / "known_good_config.json"
    path.write_text("not json")
    monkeypatch.setattr(mcs, "KNOWN_GOOD_CONFIG_PATH", path)
    assert mcs.load_known_good_config() is None


def test_compute_drift_no_drift_when_identical():
    known = mcs.ConfigSnapshot(pid=[1, 2, 3], fan_timing=[10, 20, 30, 40], temp_comp=[0, 0, 0, 0])
    result = mcs.compute_drift(known, pid=[1, 2, 3], fan_timing=[10, 20, 30, 40], temp_comp=[0, 0, 0, 0])
    assert result["has_drift"] is False
    assert result["pid_diffs"] == []
    assert result["fan_timing_diffs"] == []
    assert result["temp_comp_diffs"] == []


def test_compute_drift_detects_pid_change():
    known = mcs.ConfigSnapshot(pid=[1, 2, 3], fan_timing=[10, 20, 30, 40], temp_comp=[0, 0, 0, 0])
    result = mcs.compute_drift(known, pid=[1, 99, 3], fan_timing=[10, 20, 30, 40], temp_comp=[0, 0, 0, 0])
    assert result["has_drift"] is True
    assert result["pid_diffs"] == ["register 1: 2 -> 99"]
    assert result["fan_timing_diffs"] == []


def test_compute_drift_detects_fan_timing_change_with_label():
    known = mcs.ConfigSnapshot(pid=[1, 2, 3], fan_timing=[10, 20, 30, 40], temp_comp=[0, 0, 0, 0])
    result = mcs.compute_drift(known, pid=[1, 2, 3], fan_timing=[10, 25, 30, 40], temp_comp=[0, 0, 0, 0])
    assert result["has_drift"] is True
    assert result["fan_timing_diffs"] == ["Fan stop temp: 20 -> 25"]


def test_compute_drift_detects_temp_comp_change_with_label():
    known = mcs.ConfigSnapshot(pid=[1, 2, 3], fan_timing=[10, 20, 30, 40], temp_comp=[0, 0, 0, 0])
    result = mcs.compute_drift(known, pid=[1, 2, 3], fan_timing=[10, 20, 30, 40], temp_comp=[5, 0, 0, 0])
    assert result["has_drift"] is True
    assert result["temp_comp_diffs"] == ["Upper: 0 -> 5"]


def test_compute_drift_handles_length_mismatch_without_crashing():
    known = mcs.ConfigSnapshot(pid=[1, 2, 3], fan_timing=[10, 20, 30, 40], temp_comp=[0, 0, 0, 0])
    result = mcs.compute_drift(known, pid=[1, 2], fan_timing=[10, 20, 30, 40], temp_comp=[0, 0, 0, 0])
    assert result["has_drift"] is True
    assert "Register count changed" in result["pid_diffs"][0]


def test_compute_drift_detects_multiple_simultaneous_changes():
    known = mcs.ConfigSnapshot(pid=[1, 2, 3], fan_timing=[10, 20, 30, 40], temp_comp=[0, 0, 0, 0])
    result = mcs.compute_drift(known, pid=[1, 2, 3], fan_timing=[10, 20, 30, 99], temp_comp=[0, 0, 7, 0])
    assert len(result["fan_timing_diffs"]) == 1
    assert len(result["temp_comp_diffs"]) == 1
    assert result["temp_comp_diffs"] == ["Infrared: 0 -> 7"]
