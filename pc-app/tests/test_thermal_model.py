"""Tests for the real (2026-09-16) thermal model - see thermal_model.py's module docstring for
what changed from the old scaffolding-only version: a real per-zone linear fit trained only on
PASS runs (a direct fix for a real reported concern - the old readiness counter, and training data
before this fix, counted every run indiscriminately, including faulted/incomplete ones), plus a
much lower MIN_CLEAN_RUNS_FOR_MODEL (5, not the old arbitrary 20) since every prediction now also
reports its own R-squared/sample_count rather than hiding behind a big threshold.
"""
import json

import thermal_model
from modbus_client import Profile, CurveSection


def _sections(upper_target):
    return [
        {
            "upper_temp": upper_target, "upper_rate": 2, "upper_hold_time": 30,
            "lower_temp": 0, "lower_rate": 0, "lower_hold_time": 0,
            "ir_temp": 0, "ir_rate": 0, "ir_hold_time": 0,
        }
        for _ in range(8)
    ]


def _write_run(tmp_path, name, upper_target, actual_upper_peak, time_to_peak_s, process_result="PASS"):
    csv_path = tmp_path / f"{name}.csv"
    csv_path.write_text("timestamp,elapsed_s,upper_temp,lower_temp,infrared_temp,probe_temp,running,vacuum,cooling,profile_name\n")
    summary = {
        "process_result": process_result,
        "config_snapshot": {"profile": {"name": "Test", "sections": _sections(upper_target)}},
        "zone_statistics": {
            "upper": {"min_c": 25.0, "max_c": actual_upper_peak, "avg_c": actual_upper_peak / 2.0, "time_to_peak_s": time_to_peak_s},
        },
    }
    csv_path.with_suffix(".summary.json").write_text(json.dumps(summary))
    return csv_path


def _profile(upper_target):
    return Profile(name="Test", sections=[
        CurveSection(upper_temp=upper_target, upper_rate=2, upper_hold_time=30) for _ in range(8)
    ])


def test_not_enough_clean_runs_reports_available_false(tmp_path, monkeypatch):
    monkeypatch.setattr(thermal_model, "LOGS_DIR", tmp_path)
    for i in range(3):
        _write_run(tmp_path, f"run_{i}", upper_target=200 + i * 10, actual_upper_peak=195 + i * 10, time_to_peak_s=60)

    status = thermal_model.get_model_status()

    assert status.available is False
    assert status.clean_runs_logged == 3
    assert status.total_runs_logged == 3
    assert status.runs_needed == thermal_model.MIN_CLEAN_RUNS_FOR_MODEL - 3
    assert "3/5" in status.message or f"3/{thermal_model.MIN_CLEAN_RUNS_FOR_MODEL}" in status.message


def test_failed_and_incomplete_runs_do_not_count_toward_readiness_or_training(tmp_path, monkeypatch):
    """The real reported concern (2026-09-16): failures must not skew the count OR the fit."""
    monkeypatch.setattr(thermal_model, "LOGS_DIR", tmp_path)
    # 5 clean PASS runs with a perfect linear relationship: actual = target - 5.
    for i, target in enumerate([200, 220, 240, 260, 280]):
        _write_run(tmp_path, f"run_pass_{i}", upper_target=target, actual_upper_peak=target - 5, time_to_peak_s=target / 2.0)
    # 4 FAIL/INCOMPLETE runs with wildly different (anomalous) outcomes that would wreck the fit
    # if counted - e.g. an emergency stop that never got anywhere near its target.
    _write_run(tmp_path, "run_fail_1", upper_target=300, actual_upper_peak=60, time_to_peak_s=5, process_result="FAIL")
    _write_run(tmp_path, "run_fail_2", upper_target=300, actual_upper_peak=40, time_to_peak_s=3, process_result="FAIL")
    _write_run(tmp_path, "run_incomplete_1", upper_target=300, actual_upper_peak=30, time_to_peak_s=2, process_result="INCOMPLETE")
    _write_run(tmp_path, "run_incomplete_2", upper_target=300, actual_upper_peak=20, time_to_peak_s=1, process_result="INCOMPLETE")

    status = thermal_model.get_model_status()

    assert status.total_runs_logged == 9, "total must still reflect every logged run"
    assert status.clean_runs_logged == 5, "only PASS runs count toward readiness"
    assert status.available is True
    upper_model = status.zone_models["upper"]
    assert upper_model.sample_count == 5, "the 4 failed/incomplete runs must be excluded from training"
    assert abs(upper_model.slope - 1.0) < 1e-9
    assert abs(upper_model.intercept - (-5.0)) < 1e-9
    assert upper_model.r_squared > 0.999, "a perfect fit on the 5 clean runs - would be wrecked if failures were included"


def test_enough_clean_runs_with_variation_fits_a_real_model(tmp_path, monkeypatch):
    monkeypatch.setattr(thermal_model, "LOGS_DIR", tmp_path)
    for i, target in enumerate([200, 220, 240, 260, 280]):
        _write_run(tmp_path, f"run_{i}", upper_target=target, actual_upper_peak=target - 5, time_to_peak_s=target / 2.0)

    status = thermal_model.get_model_status()

    assert status.available is True
    assert "upper" in status.zone_models
    assert "lower" not in status.zone_models, "no lower target was ever set - nothing to fit"
    assert "probe" not in status.zone_models, "probe has no profile-target concept at all"
    assert status.zone_models["upper"].r_squared > 0.999


def test_no_target_variation_reports_a_flat_model_not_a_fabricated_slope(tmp_path, monkeypatch):
    """Every historical run commanded the SAME target - there's no real slope to fit from a single
    X value. Must degrade honestly to a flat (slope=0) model instead of a divide-by-zero or a
    fabricated line through one point."""
    monkeypatch.setattr(thermal_model, "LOGS_DIR", tmp_path)
    for i, actual in enumerate([245.0, 246.0, 247.0, 248.0, 249.0]):
        _write_run(tmp_path, f"run_{i}", upper_target=250, actual_upper_peak=actual, time_to_peak_s=100)

    status = thermal_model.get_model_status()

    assert status.available is True
    model = status.zone_models["upper"]
    assert model.slope == 0.0
    assert model.r_squared is None
    assert abs(model.intercept - 247.0) < 1e-9  # mean of the 5 actual peaks
    assert "flat" in status.message


def test_predict_returns_none_when_model_not_available(tmp_path, monkeypatch):
    monkeypatch.setattr(thermal_model, "LOGS_DIR", tmp_path)
    _write_run(tmp_path, "run_0", upper_target=200, actual_upper_peak=195, time_to_peak_s=60)

    assert thermal_model.predict_profile_response(_profile(250)) is None


def test_predict_applies_the_fitted_line_to_a_new_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(thermal_model, "LOGS_DIR", tmp_path)
    for i, target in enumerate([200, 220, 240, 260, 280]):
        _write_run(tmp_path, f"run_{i}", upper_target=target, actual_upper_peak=target - 5, time_to_peak_s=target / 2.0)

    predictions = thermal_model.predict_profile_response(_profile(300))

    assert predictions is not None
    pred = predictions["upper"]
    assert pred.commanded_peak_c == 300
    assert abs(pred.predicted_peak_c - 295.0) < 1e-6  # slope=1, intercept=-5 -> 300-5=295
    assert pred.sample_count == 5
    assert pred.r_squared > 0.999
    assert pred.typical_error_c < 1e-6  # a perfect in-sample fit


def test_predict_omits_a_zone_with_no_real_target_in_this_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(thermal_model, "LOGS_DIR", tmp_path)
    for i, target in enumerate([200, 220, 240, 260, 280]):
        _write_run(tmp_path, f"run_{i}", upper_target=target, actual_upper_peak=target - 5, time_to_peak_s=target / 2.0)

    predictions = thermal_model.predict_profile_response(_profile(0))  # no real Upper target set

    assert predictions is None, "the only trained zone (upper) has no real target in this profile"
