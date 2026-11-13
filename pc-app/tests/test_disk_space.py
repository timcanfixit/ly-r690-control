"""Tests for data_logger.check_disk_space() (2026-09-07, external feature-gap review) - every
run's telemetry lands under LOGS_DIR forever, and nothing previously watched whether that disk
was filling up."""
import data_logger as dl


def test_real_disk_check_returns_ok_shape(tmp_path, monkeypatch):
    """Not mocking shutil.disk_usage here - this exercises the real syscall against a real
    (tiny, harmless) temp directory, confirming the function doesn't raise and returns the
    expected shape on a normal, healthy filesystem."""
    monkeypatch.setattr(dl, "LOGS_DIR", tmp_path / "logs")
    result = dl.check_disk_space()
    assert result["ok"] is True
    assert result["level"] in ("ok", "warning", "critical")
    assert 0.0 <= result["free_pct"] <= 100.0
    assert result["free_bytes"] >= 0
    assert result["total_bytes"] > 0


def test_creates_logs_dir_if_missing(tmp_path, monkeypatch):
    logs_dir = tmp_path / "does" / "not" / "exist" / "yet"
    monkeypatch.setattr(dl, "LOGS_DIR", logs_dir)
    assert not logs_dir.exists()
    dl.check_disk_space()
    assert logs_dir.exists()


def test_level_thresholds_are_percentage_based(monkeypatch):
    """Level classification itself, independent of any real filesystem - stub shutil.disk_usage
    directly to hit each threshold band precisely."""
    from collections import namedtuple
    Usage = namedtuple("Usage", ["total", "used", "free"])

    def make_usage(free_pct):
        total = 1000
        free = int(total * free_pct / 100.0)
        return Usage(total=total, used=total - free, free=free)

    monkeypatch.setattr(dl, "LOGS_DIR", type("P", (), {"mkdir": lambda self, **k: None})())

    monkeypatch.setattr(dl.shutil, "disk_usage", lambda path: make_usage(50.0))
    assert dl.check_disk_space()["level"] == "ok"

    monkeypatch.setattr(dl.shutil, "disk_usage", lambda path: make_usage(15.0))
    assert dl.check_disk_space()["level"] == "warning"

    monkeypatch.setattr(dl.shutil, "disk_usage", lambda path: make_usage(5.0))
    assert dl.check_disk_space()["level"] == "critical"


def test_filesystem_error_returns_unknown_level_not_fatal(monkeypatch):
    class BrokenPath:
        def mkdir(self, **kwargs):
            raise OSError("mount point disappeared")

    monkeypatch.setattr(dl, "LOGS_DIR", BrokenPath())
    result = dl.check_disk_space()
    assert result["ok"] is False
    assert result["level"] == "unknown"
