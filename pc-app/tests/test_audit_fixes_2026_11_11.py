"""Regression tests for the confirmed findings from the 2026-11-11 external code audit
(C01 duplicate Start, C06 webhook secret, C07 non-finite safety limits, R03 rename, R06 corrupt
run CSV, R08 delete of the active run)."""
import json
import pytest

import data_logger as dl
import notifications
import profile_store as ps
import web_server
from modbus_client import CurveSection, Profile
from safety_limits import parse_safety_limit_updates


# ---- C07 ----
@pytest.mark.parametrize("bad", ["nan", float("nan"), float("inf"), "abc", None, True, 5.0, 9999.0])
def test_parse_rejects_non_finite_or_out_of_range(bad):
    with pytest.raises(ValueError):
        parse_safety_limit_updates({"max_upper_temp_c": bad})


def test_safety_limits_route_rejects_whole_payload_and_applies_nothing(monkeypatch):
    monkeypatch.setattr(web_server, "save_safety_limits", lambda *_: None)
    before = web_server.safety_limits.max_lower_temp_c
    c = web_server.app.test_client()
    r = c.post("/api/safety-limits", json={"max_lower_temp_c": 250, "max_upper_temp_c": "nan"})
    assert r.status_code == 400
    assert web_server.safety_limits.max_lower_temp_c == before, "earlier valid field must not be half-applied"
    assert web_server.safety_limits.max_upper_temp_c == web_server.safety_limits.max_upper_temp_c  # i.e. not NaN


# ---- C06 ----
def test_webhook_get_never_returns_the_secret_and_echoed_mask_keeps_stored_url(monkeypatch):
    secret = "https://discord.com/api/webhooks/123456/TOKENSECRETVALUE"
    monkeypatch.setattr(web_server, "webhook_config", notifications.WebhookConfig(url=secret, enabled=True))
    monkeypatch.setattr(notifications, "save_webhook_config", lambda *_: None)
    c = web_server.app.test_client()
    shown = c.get("/api/webhook").get_json()["url"]
    assert "TOKENSECRET" not in shown and shown.startswith("https://discord.com/")
    c.post("/api/webhook", json={"url": shown, "enabled": True})
    assert web_server.webhook_config.url == secret


# ---- R03 ----
def test_rename_preserves_metadata_history_and_version(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    p = Profile(name="Alpha / One", sections=[CurveSection(upper_temp=100) for _ in range(8)])
    ps.save_profile(p, metadata={"board": "X"})
    p.sections[0].upper_temp = 150
    ps.save_profile(p)
    ps.rename_profile("Alpha / One", "Beta")
    assert ps.get_profile_metadata("Beta") == {"board": "X"}
    assert len(ps.get_profile_history("Beta")) == 1
    assert ps.get_profile_version("Beta") == 2
    assert ps.load_profile("Beta").sections[0].upper_temp == 150
    assert not (tmp_path / "Alpha - One.json").exists()


def test_rename_refuses_to_overwrite_a_different_existing_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    ps.save_profile(Profile(name="A", sections=[CurveSection() for _ in range(8)]))
    ps.save_profile(Profile(name="B", sections=[CurveSection() for _ in range(8)]))
    with pytest.raises(ValueError):
        ps.rename_profile("A", "B")
    assert ps.load_profile("B").name == "B"


# ---- R06 ----
def test_one_corrupt_run_csv_does_not_break_the_history_list(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    hdr = "timestamp,elapsed_s,upper_temp,lower_temp,infrared_temp,probe_temp,running,vacuum,cooling,profile_name\n"
    (tmp_path / "run_20260101_000000.csv").write_text(hdr + "1,1,abc,1,1,1,True,False,False,x\n")
    (tmp_path / "run_20260102_000000.csv").write_text(hdr + "1,1,50,1,1,1,True,False,False,y\n")
    runs = dl.list_runs()
    assert [r["profile_name"] for r in runs] == ["y", "x"]


# ---- C01 / R08 ----
def test_second_start_is_rejected_while_a_run_is_open(monkeypatch):
    monkeypatch.setattr(web_server, "_logging_active", True)
    monkeypatch.setattr(web_server, "_active_profile_name", "First")
    from unittest.mock import Mock
    import time as _t
    stub = Mock()
    stub.is_connected = True
    stub.get_temps_timestamp.return_value = _t.monotonic()
    stub.coil_snapshot_age.return_value = 0.0
    stub.config_write_in_progress.return_value = False
    stub.config_incomplete.return_value = []
    monkeypatch.setattr(web_server, "bus", stub, raising=False)
    c = web_server.app.test_client()
    r = c.post("/api/start", json={"profile": {"name": "Second", "sections": [{"upper_temp": 100} for _ in range(8)]}})
    assert r.status_code == 409 and r.get_json()["ok"] is False


def test_cannot_delete_the_run_currently_being_logged(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    f = tmp_path / "run_20260101_000000.csv"
    f.write_text("x")
    monkeypatch.setattr(web_server, "_current_log_path", f)
    monkeypatch.setattr(web_server, "_logging_active", True)
    r = web_server.app.test_client().delete(f"/api/runs/{f.name}")
    assert r.status_code == 409 and f.exists()
