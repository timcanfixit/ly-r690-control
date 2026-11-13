"""Coverage for /api/stop's enriched run_completed notification (2026-09-14, explicit request: "i
dont get a complete notification ... a summary of the temps using ai"). Before this, a manual Stop
only ever sent the bare "Run stopped: <name>" string - now it includes the run's real computed
peak temps/duration/result/fault count (see run_summary_ai.py), with an optional AI-phrased
sentence appended on top when Ollama is configured.
"""
import time

import run_recovery
import web_server
from machine_bus import WriteResult
from modbus_client import LiveReading
from run_finalization import RunFinalizationTracker
from profile_search import OllamaConfig


class StubBus:
    def __init__(self):
        self._running = True

    def get_live_reading(self):
        return LiveReading(
            upper_temp=277.6, lower_temp=200.0, infrared_temp=150.0, probe_temp=180.0,
            running=self._running, running_mirror=self._running, vacuum=False, cooling=False, keep=False,
        )

    def get_diagnostics(self):
        return {}

    def write_coil(self, address, value, on_progress=None):
        self._running = value
        return WriteResult(ok=True, attempts=1, detail="stub", independently_verified=True)


def _setup(monkeypatch, tmp_path):
    marker = tmp_path / "active_run.json"
    monkeypatch.setattr(run_recovery, "MARKER_PATH", marker)
    csv_path = tmp_path / "run_test.csv"
    csv_path.write_text(
        "timestamp,elapsed_s,upper_temp,lower_temp,infrared_temp,probe_temp,running,vacuum,cooling,profile_name\n"
        "0.0,0.0,200,200,200,200,True,False,False,Test\n"
        "1.0,0.5,277.6,200,200,200,True,False,False,Test\n"
    )
    run_recovery.mark_run_started("Test", csv_path.name)

    monkeypatch.setattr(web_server, "bus", StubBus(), raising=False)
    monkeypatch.setattr(web_server, "_run_tracker", RunFinalizationTracker())
    monkeypatch.setattr(web_server, "_current_log_path", csv_path)
    monkeypatch.setattr(web_server, "_logging_active", True)
    monkeypatch.setattr(web_server, "_run_process_limits", None)
    monkeypatch.setattr(web_server, "_run_start_time", time.monotonic() - 60.0)
    monkeypatch.setattr(web_server, "_active_profile_name", "Test")
    monkeypatch.setattr(web_server, "ollama_config", OllamaConfig(enabled=False))

    notified = []
    monkeypatch.setattr(web_server.notifications, "notify_async", lambda config, event, message: notified.append((event, message)))
    return notified


def test_api_stop_notification_includes_real_stats_not_just_bare_string(tmp_path, monkeypatch):
    notified = _setup(monkeypatch, tmp_path)
    client = web_server.app.test_client()

    resp = client.post("/api/stop")

    assert resp.status_code == 200
    assert len(notified) == 1
    event, message = notified[0]
    assert event == "run_completed"
    assert message != "Run stopped: Test", "must be the enriched message, not the old bare string"
    assert "Run stopped: Test" in message
    assert "Upper 277.6C" in message
    assert "Duration: 60s" in message


def test_api_stop_falls_back_to_bare_string_when_nothing_to_finalize(tmp_path, monkeypatch):
    """No active run tracked (e.g. Stop pressed while already idle) - _finalize_current_run()
    returns None, so the notification must fall back to the old plain message rather than crash
    or claim fake stats."""
    notified = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(web_server, "_run_tracker", None)
    monkeypatch.setattr(web_server, "_current_log_path", None)
    client = web_server.app.test_client()

    client.post("/api/stop")

    assert len(notified) == 1
    assert notified[0][1] == "Run stopped: Test"
