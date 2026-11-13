import time
from dataclasses import dataclass

import system_diagnostics as sd


@dataclass
class FakeReading:
    upper_temp: float = 200.0
    lower_temp: float = 150.0
    infrared_temp: float = 100.0
    probe_temp: float = 90.0


def _base_kwargs(**overrides):
    kwargs = dict(
        connected=True,
        live_reading=FakeReading(),
        disk_status={"level": "ok", "free_pct": 50.0},
        system_health_status={"available": False, "error": "Not available on Windows."},
        maintenance_status={"calibration": {}, "self_tune": {}},
        automatic_backups=[{"created_at": time.time(), "profile_count": 5}],
        remembered_adapter=None,
        config_drift=None,
        pid_regs=[1] * 33,
        fan_timing_regs=[1, 2, 3, 4],
    )
    kwargs.update(overrides)
    return kwargs


def _find(checks, name):
    return next(c for c in checks if c.name == name)


def test_all_ok_produces_overall_ok():
    checks = sd.run_diagnostics(**_base_kwargs())
    assert sd.overall_status(checks) == "ok"


def test_not_connected_is_critical():
    checks = sd.run_diagnostics(**_base_kwargs(connected=False, live_reading=None))
    assert _find(checks, "Communication").status == "critical"
    assert sd.overall_status(checks) == "critical"


def test_connected_but_no_reading_is_warning():
    checks = sd.run_diagnostics(**_base_kwargs(live_reading=None))
    assert _find(checks, "Communication").status == "warning"


def test_implausible_sensor_reading_is_critical():
    checks = sd.run_diagnostics(**_base_kwargs(live_reading=FakeReading(upper_temp=9999.0)))
    check = _find(checks, "Sensors")
    assert check.status == "critical"
    assert "Upper" in check.detail


def test_no_reading_sensors_is_info_not_critical():
    checks = sd.run_diagnostics(**_base_kwargs(live_reading=None))
    assert _find(checks, "Sensors").status == "info"


def test_missing_pid_regs_is_critical():
    checks = sd.run_diagnostics(**_base_kwargs(pid_regs=None))
    check = _find(checks, "Register Readability")
    assert check.status == "critical"
    assert "PID" in check.detail


def test_missing_fan_timing_regs_is_critical():
    checks = sd.run_diagnostics(**_base_kwargs(fan_timing_regs=None))
    assert _find(checks, "Register Readability").status == "critical"


def test_disk_space_critical_propagates():
    checks = sd.run_diagnostics(**_base_kwargs(disk_status={"level": "critical", "free_pct": 2.0}))
    assert _find(checks, "Disk Space").status == "critical"
    assert sd.overall_status(checks) == "critical"


def test_disk_space_unknown_is_info():
    checks = sd.run_diagnostics(**_base_kwargs(disk_status={"level": "unknown", "error": "boom"}))
    assert _find(checks, "Disk Space").status == "info"


def test_system_health_unavailable_is_info():
    checks = sd.run_diagnostics(**_base_kwargs())
    assert _find(checks, "Host System Health").status == "info"


def test_system_health_warning_propagates():
    checks = sd.run_diagnostics(**_base_kwargs(system_health_status={
        "available": True, "level": "warning", "load_avg": (5.0, 1.0, 1.0), "cpu_count": 4, "mem_used_pct": 80.0,
    }))
    assert _find(checks, "Host System Health").status == "warning"


def test_config_drift_none_is_info():
    checks = sd.run_diagnostics(**_base_kwargs(config_drift=None))
    assert _find(checks, "Machine Configuration").status == "info"


def test_config_drift_detected_is_warning():
    checks = sd.run_diagnostics(**_base_kwargs(config_drift={
        "has_drift": True, "pid_diffs": ["register 1: 2 -> 3"], "fan_timing_diffs": [], "temp_comp_diffs": [],
    }))
    check = _find(checks, "Machine Configuration")
    assert check.status == "warning"
    assert "register 1" in check.detail


def test_config_drift_none_detected_is_ok():
    checks = sd.run_diagnostics(**_base_kwargs(config_drift={
        "has_drift": False, "pid_diffs": [], "fan_timing_diffs": [], "temp_comp_diffs": [],
    }))
    assert _find(checks, "Machine Configuration").status == "ok"


def test_calibration_overdue_is_warning():
    checks = sd.run_diagnostics(**_base_kwargs(maintenance_status={
        "calibration": {"upper": {"status": "overdue"}, "lower": {"status": "ok"}}, "self_tune": {},
    }))
    check = _find(checks, "Calibration")
    assert check.status == "warning"
    assert "Upper" in check.detail


def test_self_tune_overdue_is_warning():
    checks = sd.run_diagnostics(**_base_kwargs(maintenance_status={
        "calibration": {}, "self_tune": {"ir": {"status": "overdue"}},
    }))
    assert _find(checks, "Self-Tune Review").status == "warning"


def test_no_automatic_backups_is_warning():
    checks = sd.run_diagnostics(**_base_kwargs(automatic_backups=[]))
    assert _find(checks, "Automatic Backups").status == "warning"


def test_stale_automatic_backup_is_warning():
    old_time = time.time() - (sd._AUTO_BACKUP_INTERVAL_S * 3)
    checks = sd.run_diagnostics(**_base_kwargs(automatic_backups=[{"created_at": old_time, "profile_count": 1}]))
    assert _find(checks, "Automatic Backups").status == "warning"


def test_recent_automatic_backup_is_ok():
    checks = sd.run_diagnostics(**_base_kwargs())
    assert _find(checks, "Automatic Backups").status == "ok"


def test_no_remembered_adapter_is_info():
    checks = sd.run_diagnostics(**_base_kwargs(remembered_adapter=None))
    assert _find(checks, "USB Adapter Identity").status == "info"


def test_remembered_adapter_resolving_is_ok():
    checks = sd.run_diagnostics(**_base_kwargs(remembered_adapter={"currently_resolves_to": "COM4"}))
    assert _find(checks, "USB Adapter Identity").status == "ok"


def test_remembered_adapter_not_resolving_is_warning():
    checks = sd.run_diagnostics(**_base_kwargs(remembered_adapter={"currently_resolves_to": None}))
    assert _find(checks, "USB Adapter Identity").status == "warning"


def test_overall_status_is_worst_of_all_checks():
    checks = [
        sd.DiagnosticCheck("A", "ok", ""),
        sd.DiagnosticCheck("B", "warning", ""),
        sd.DiagnosticCheck("C", "info", ""),
    ]
    assert sd.overall_status(checks) == "warning"


def test_overall_status_ok_when_no_checks():
    assert sd.overall_status([]) == "ok"
