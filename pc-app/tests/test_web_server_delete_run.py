"""Coverage for DELETE /api/runs/<filename> - 2026-09-16, real reported gap: no way to delete a
run from Run History at all.
"""
import data_logger as dl
import web_server
from modbus_client import LiveReading


def _reading():
    return LiveReading(
        upper_temp=200.0, lower_temp=180.0, infrared_temp=100.0, probe_temp=90.0,
        running=True, running_mirror=True, vacuum=False, cooling=False, keep=False,
    )


def test_delete_run_removes_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    logger = dl.RunLogger()
    path = logger.start_run("ToDelete")
    logger.log(_reading())
    logger.stop_run()
    client = web_server.app.test_client()

    resp = client.delete(f"/api/runs/{path.name}")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert not path.exists()


def test_delete_run_404s_for_a_run_that_does_not_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    client = web_server.app.test_client()

    resp = client.delete("/api/runs/run_20260101_000000.csv")

    assert resp.status_code == 404
    assert resp.get_json()["ok"] is False


def test_delete_run_rejects_path_traversal_with_400_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path)
    client = web_server.app.test_client()

    resp = client.delete("/api/runs/../../etc/passwd")

    # Flask's own URL routing normalizes "../" before it ever reaches the view function on most
    # setups, but the guard inside delete_run() itself must independently reject it regardless -
    # this asserts the response is never a fabricated 200 success.
    assert resp.status_code in (400, 404)
    assert resp.get_json()["ok"] is False
