"""Coverage for /api/thermal-model/status and the new (2026-09-16) /api/thermal-model/predict -
see thermal_model.py's own module docstring for what changed from the old scaffolding-only
version.
"""
import json

import thermal_model
import web_server


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


def _profile_json(upper_temp):
    return {"name": "Test", "sections": [{"upper_temp": upper_temp} for _ in range(8)]}


def test_status_reports_not_available_with_too_little_clean_data(tmp_path, monkeypatch):
    monkeypatch.setattr(thermal_model, "LOGS_DIR", tmp_path)
    client = web_server.app.test_client()

    resp = client.get("/api/thermal-model/status")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["available"] is False
    assert body["clean_runs_logged"] == 0


def test_status_reports_available_and_zone_models_once_trained(tmp_path, monkeypatch):
    monkeypatch.setattr(thermal_model, "LOGS_DIR", tmp_path)
    for i, target in enumerate([200, 220, 240, 260, 280]):
        _write_run(tmp_path, f"run_{i}", upper_target=target, actual_upper_peak=target - 5, time_to_peak_s=target / 2.0)
    client = web_server.app.test_client()

    resp = client.get("/api/thermal-model/status")

    body = resp.get_json()
    assert body["available"] is True
    assert body["zone_models"]["upper"]["sample_count"] == 5
    assert abs(body["zone_models"]["upper"]["slope"] - 1.0) < 1e-6


def test_predict_returns_not_available_when_no_model_trained(tmp_path, monkeypatch):
    monkeypatch.setattr(thermal_model, "LOGS_DIR", tmp_path)
    client = web_server.app.test_client()

    resp = client.post("/api/thermal-model/predict", json={"profile": _profile_json(250)})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["available"] is False
    assert "message" in body


def test_predict_returns_real_predictions_once_trained(tmp_path, monkeypatch):
    monkeypatch.setattr(thermal_model, "LOGS_DIR", tmp_path)
    for i, target in enumerate([200, 220, 240, 260, 280]):
        _write_run(tmp_path, f"run_{i}", upper_target=target, actual_upper_peak=target - 5, time_to_peak_s=target / 2.0)
    client = web_server.app.test_client()

    resp = client.post("/api/thermal-model/predict", json={"profile": _profile_json(300)})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["available"] is True
    pred = body["predictions"]["upper"]
    assert pred["commanded_peak_c"] == 300
    assert abs(pred["predicted_peak_c"] - 295.0) < 1e-6
    assert pred["sample_count"] == 5
