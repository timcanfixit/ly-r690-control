import calibration as cal


def test_offset_sign_convention_matches_roadmap_example():
    # Reference 100.0C, machine reads 106.4C -> machine reads HIGH -> offset is negative.
    point = cal.CalibrationPoint(zone="upper", reference_c=100.0, machine_c=106.4)
    assert abs(point.offset_c - (-6.4)) < 1e-9

    point2 = cal.CalibrationPoint(zone="upper", reference_c=200.0, machine_c=205.1)
    assert abs(point2.offset_c - (-5.1)) < 1e-9


def test_round_trip_add_and_get_history(tmp_path, monkeypatch):
    monkeypatch.setattr(cal, "CALIBRATION_PATH", tmp_path / "calibration_records.json")
    record = cal.CalibrationRecord(
        operator="Tim", reference_instrument="K-type thermocouple",
        points=[cal.CalibrationPoint(zone="upper", reference_c=220.0, machine_c=226.4)],
        note="test",
    )
    cal.add_calibration_record(record)
    history = cal.get_calibration_history()
    assert len(history) == 1
    assert history[0]["operator"] == "Tim"
    assert abs(history[0]["points"][0]["offset_c"] - (-6.4)) < 1e-9


def test_multiple_records_accumulate_not_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(cal, "CALIBRATION_PATH", tmp_path / "calibration_records.json")
    for i in range(3):
        cal.add_calibration_record(cal.CalibrationRecord(
            operator=f"op{i}", points=[cal.CalibrationPoint(zone="upper", reference_c=100.0, machine_c=100.0)],
        ))
    assert len(cal.get_calibration_history()) == 3


def test_missing_file_returns_empty_history(tmp_path, monkeypatch):
    monkeypatch.setattr(cal, "CALIBRATION_PATH", tmp_path / "does_not_exist.json")
    assert cal.get_calibration_history() == []
