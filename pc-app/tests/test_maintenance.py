"""Tests for maintenance.py (2026-09-07, external feature-gap review) - everything here is built
from data this app already logs (runs, calibration records, self-tune sessions), never a
fabricated hardware-wear estimate. Same tmp_path/monkeypatch pattern as test_calibration.py."""
import maintenance as mnt
import data_logger as dl
import calibration as cal
import self_tune_log as stl


def test_load_missing_config_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(mnt, "MAINTENANCE_CONFIG_PATH", tmp_path / "does_not_exist.json")
    config = mnt.load_maintenance_config()
    assert config == mnt.MaintenanceConfig()


def test_round_trip_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(mnt, "MAINTENANCE_CONFIG_PATH", tmp_path / "maintenance_config.json")
    config = mnt.MaintenanceConfig(calibration_interval_days=30.0, self_tune_review_interval_days=60.0)
    mnt.save_maintenance_config(config)
    loaded = mnt.load_maintenance_config()
    assert loaded == config


def test_zero_interval_rejected_falls_back_to_default(tmp_path, monkeypatch):
    """A 0 or negative interval would report everything as eternally overdue - must not be
    trusted as-is, same sanity-bounds philosophy as safety_limits.py."""
    path = tmp_path / "maintenance_config.json"
    path.write_text('{"calibration_interval_days": 0.0}')
    monkeypatch.setattr(mnt, "MAINTENANCE_CONFIG_PATH", path)
    config = mnt.load_maintenance_config()
    assert config.calibration_interval_days == mnt.MaintenanceConfig().calibration_interval_days


def test_corrupt_file_falls_back_to_defaults_without_raising(tmp_path, monkeypatch):
    path = tmp_path / "maintenance_config.json"
    path.write_text("{not valid json")
    monkeypatch.setattr(mnt, "MAINTENANCE_CONFIG_PATH", path)
    assert mnt.load_maintenance_config() == mnt.MaintenanceConfig()


# ---- _due_status - the four-state judgment shared by every maintenance item

def test_due_status_never_when_no_timestamp():
    assert mnt._due_status(None, interval_days=90.0) == "never"


def test_due_status_ok_when_well_within_interval():
    assert mnt._due_status(days_since=10.0, interval_days=90.0) == "ok"


def test_due_status_due_soon_within_final_20_percent():
    assert mnt._due_status(days_since=75.0, interval_days=90.0) == "due_soon"  # 83% through


def test_due_status_overdue_past_interval():
    assert mnt._due_status(days_since=100.0, interval_days=90.0) == "overdue"


def test_due_status_overdue_at_exactly_the_interval():
    assert mnt._due_status(days_since=90.0, interval_days=90.0) == "overdue"


# ---- compute_maintenance_status - full integration against real data sources

def _stub_empty_runs(monkeypatch):
    monkeypatch.setattr(mnt, "list_runs", lambda: [])


def test_status_with_no_data_at_all_reports_never_and_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(mnt, "MAINTENANCE_CONFIG_PATH", tmp_path / "maintenance_config.json")
    monkeypatch.setattr(cal, "CALIBRATION_PATH", tmp_path / "calibration_records.json")
    monkeypatch.setattr(stl, "SELF_TUNE_LOG_PATH", tmp_path / "self_tune_history.json")
    _stub_empty_runs(monkeypatch)

    status = mnt.compute_maintenance_status()
    assert status["runs"]["total_runs"] == 0
    assert status["runs"]["total_run_hours"] == 0.0
    for zone in mnt.CALIBRATION_ZONES:
        assert status["calibration"][zone]["status"] == "never"
    for zone in stl.ZONES:
        assert status["self_tune"][zone]["status"] == "never"
        assert status["self_tune"][zone]["session_count"] == 0


def test_status_aggregates_run_stats_correctly(monkeypatch):
    fake_runs = [
        {"duration_s": 3600.0, "fault_count": 1, "warning_count": 2},
        {"duration_s": 1800.0, "fault_count": 0, "warning_count": 1},
        {"duration_s": 900.0, "fault_count": None, "warning_count": None},  # predates Run Finalization
    ]
    monkeypatch.setattr(mnt, "list_runs", lambda: fake_runs)
    status = mnt.compute_maintenance_status()
    assert status["runs"]["total_runs"] == 3
    assert status["runs"]["total_run_hours"] == 1.75  # (3600+1800+900)/3600
    assert status["runs"]["total_faults"] == 1
    assert status["runs"]["total_warnings"] == 3
    assert status["runs"]["runs_with_finalization_data"] == 2


def test_calibration_zone_reports_days_since_and_correct_status(tmp_path, monkeypatch):
    """CalibrationRecord's timestamp default is bound to time.time at class-definition time
    (dataclass field(default_factory=...)), so it can't be controlled via monkeypatching the
    module's `time` attribute after the fact - pass timestamp= explicitly instead."""
    monkeypatch.setattr(cal, "CALIBRATION_PATH", tmp_path / "calibration_records.json")
    _stub_empty_runs(monkeypatch)
    now = 1_000_000.0
    ten_days_ago = now - 10 * mnt.SECONDS_PER_DAY
    cal.add_calibration_record(cal.CalibrationRecord(
        timestamp=ten_days_ago,
        points=[cal.CalibrationPoint(zone="ir", reference_c=220.0, machine_c=215.0)],
    ))
    config = mnt.MaintenanceConfig(calibration_interval_days=90.0)
    status = mnt.compute_maintenance_status(config=config, now=now)
    assert abs(status["calibration"]["ir"]["days_since"] - 10.0) < 0.01
    assert status["calibration"]["ir"]["status"] == "ok"
    assert status["calibration"]["upper"]["status"] == "never"  # never calibrated


def test_calibration_zone_uses_most_recent_record_across_multiple(tmp_path, monkeypatch):
    monkeypatch.setattr(cal, "CALIBRATION_PATH", tmp_path / "calibration_records.json")
    _stub_empty_runs(monkeypatch)
    now = 1_000_000.0

    old_ts = now - 200 * mnt.SECONDS_PER_DAY
    cal.add_calibration_record(cal.CalibrationRecord(
        timestamp=old_ts, points=[cal.CalibrationPoint(zone="ir", reference_c=220.0, machine_c=210.0)],
    ))
    recent_ts = now - 5 * mnt.SECONDS_PER_DAY
    cal.add_calibration_record(cal.CalibrationRecord(
        timestamp=recent_ts, points=[cal.CalibrationPoint(zone="ir", reference_c=222.0, machine_c=218.0)],
    ))

    status = mnt.compute_maintenance_status(now=now)
    assert abs(status["calibration"]["ir"]["days_since"] - 5.0) < 0.01


def test_self_tune_zone_reports_last_session_and_status(tmp_path, monkeypatch):
    monkeypatch.setattr(stl, "SELF_TUNE_LOG_PATH", tmp_path / "self_tune_history.json")
    _stub_empty_runs(monkeypatch)
    now = 1_000_000.0
    stl.record_self_tune_session("ir", now - 200 * mnt.SECONDS_PER_DAY, now - 200 * mnt.SECONDS_PER_DAY + 60)

    config = mnt.MaintenanceConfig(self_tune_review_interval_days=180.0)
    status = mnt.compute_maintenance_status(config=config, now=now)
    assert status["self_tune"]["ir"]["status"] == "overdue"
    assert status["self_tune"]["ir"]["session_count"] == 1
    assert status["self_tune"]["upper"]["status"] == "never"
