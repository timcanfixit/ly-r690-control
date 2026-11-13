"""Coverage for /api/system-health's 2026-09-15 enrichment (System Health tab): bundles disk usage
and the Modbus comm-diagnostics counters alongside the existing host CPU/memory/uptime stats, so
the tab can show Pi health and bus-link health together from one endpoint instead of three.
"""
import web_server


class StubBus:
    def get_diagnostics(self):
        return {
            "avg_response_time_s": 0.31, "last_response_time_s": 0.31, "read_attempts": 10,
            "read_failures": 0, "read_successes": 10, "response_time_sample_count": 10,
            "retry_count": 2, "write_attempts": 1, "write_failures": 0, "write_successes": 1,
        }


def test_system_health_endpoint_includes_disk_and_comm(monkeypatch):
    monkeypatch.setattr(web_server, "bus", StubBus(), raising=False)
    monkeypatch.setattr(web_server, "check_disk_space", lambda: {"ok": True, "free_pct": 42.0, "free_bytes": 1, "total_bytes": 2, "level": "ok"})
    monkeypatch.setattr(web_server, "get_system_health", lambda: {"available": True, "level": "ok", "load_avg": (0.1, 0.1, 0.1), "cpu_count": 4, "mem_used_pct": 20.0, "mem_total_mb": 1000.0, "mem_available_mb": 800.0, "uptime_s": 100.0, "load_level": "ok", "mem_level": "ok"})
    client = web_server.app.test_client()

    resp = client.get("/api/system-health")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["available"] is True
    assert body["disk"]["free_pct"] == 42.0
    assert body["comm"]["read_successes"] == 10
    assert body["comm"]["write_attempts"] == 1
