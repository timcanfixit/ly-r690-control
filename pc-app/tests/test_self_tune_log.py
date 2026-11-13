"""Tests for self_tune_log.py (2026-09-07) - confirmed live that a real self-tune session
previously left no trace anywhere once it ended. Same tmp_path/monkeypatch pattern as
test_calibration.py's round-trip tests."""
import self_tune_log as stl


def test_missing_file_returns_empty_history(tmp_path, monkeypatch):
    monkeypatch.setattr(stl, "SELF_TUNE_LOG_PATH", tmp_path / "does_not_exist.json")
    assert stl.get_self_tune_history() == []


def test_round_trip_record_and_get_history(tmp_path, monkeypatch):
    monkeypatch.setattr(stl, "SELF_TUNE_LOG_PATH", tmp_path / "self_tune_history.json")
    stl.record_self_tune_session("ir", started_at=1000.0, ended_at=1120.0)
    history = stl.get_self_tune_history()
    assert len(history) == 1
    assert history[0]["zone"] == "ir"
    assert history[0]["duration_s"] == 120.0


def test_multiple_sessions_accumulate(tmp_path, monkeypatch):
    monkeypatch.setattr(stl, "SELF_TUNE_LOG_PATH", tmp_path / "self_tune_history.json")
    stl.record_self_tune_session("upper", 0.0, 60.0)
    stl.record_self_tune_session("ir", 100.0, 200.0)
    stl.record_self_tune_session("ir", 300.0, 350.0)
    assert len(stl.get_self_tune_history()) == 3


def test_get_last_self_tune_returns_most_recent_by_end_time(tmp_path, monkeypatch):
    monkeypatch.setattr(stl, "SELF_TUNE_LOG_PATH", tmp_path / "self_tune_history.json")
    stl.record_self_tune_session("ir", 100.0, 200.0)
    stl.record_self_tune_session("ir", 300.0, 500.0)  # most recent by ended_at
    stl.record_self_tune_session("ir", 150.0, 250.0)  # started later than the first, but ends earlier than the second
    last = stl.get_last_self_tune("ir")
    assert last["ended_at"] == 500.0


def test_get_last_self_tune_returns_none_when_never_tuned(tmp_path, monkeypatch):
    monkeypatch.setattr(stl, "SELF_TUNE_LOG_PATH", tmp_path / "self_tune_history.json")
    assert stl.get_last_self_tune("ir") is None


def test_get_last_self_tune_ignores_other_zones(tmp_path, monkeypatch):
    monkeypatch.setattr(stl, "SELF_TUNE_LOG_PATH", tmp_path / "self_tune_history.json")
    stl.record_self_tune_session("upper", 0.0, 60.0)
    assert stl.get_last_self_tune("ir") is None


# ---- SelfTuneTracker - detects real start/end transitions from raw per-tick coil observations

def test_tracker_records_nothing_while_never_active():
    tracker = stl.SelfTuneTracker()
    tracker.observe(False, False, False)
    tracker.observe(False, False, False)
    assert tracker._active_since == {}


def test_tracker_detects_a_start_but_does_not_record_until_it_ends(tmp_path, monkeypatch):
    monkeypatch.setattr(stl, "SELF_TUNE_LOG_PATH", tmp_path / "self_tune_history.json")
    tracker = stl.SelfTuneTracker()
    tracker.observe(False, False, True)  # IR self-tune starts
    assert "ir" in tracker._active_since
    assert stl.get_self_tune_history() == []  # not recorded yet - still in progress


def test_tracker_records_a_full_start_to_end_session(tmp_path, monkeypatch):
    monkeypatch.setattr(stl, "SELF_TUNE_LOG_PATH", tmp_path / "self_tune_history.json")
    tracker = stl.SelfTuneTracker()
    tracker.observe(False, False, True)   # start
    tracker.observe(False, False, True)   # still active - no duplicate start
    tracker.observe(False, False, False)  # ends
    history = stl.get_self_tune_history()
    assert len(history) == 1
    assert history[0]["zone"] == "ir"
    assert "ir" not in tracker._active_since


def test_tracker_handles_multiple_zones_independently(tmp_path, monkeypatch):
    monkeypatch.setattr(stl, "SELF_TUNE_LOG_PATH", tmp_path / "self_tune_history.json")
    tracker = stl.SelfTuneTracker()
    tracker.observe(True, False, True)    # upper and IR both start
    tracker.observe(False, False, True)   # upper ends, IR still active
    tracker.observe(False, False, False)  # IR ends
    history = stl.get_self_tune_history()
    zones = sorted(h["zone"] for h in history)
    assert zones == ["ir", "upper"]


def test_tracker_never_records_a_session_still_in_progress(tmp_path, monkeypatch):
    """A session still active when the app closes/crashes must not be recorded with a guessed or
    truncated duration - only a fully-observed start-to-end session is ever written."""
    monkeypatch.setattr(stl, "SELF_TUNE_LOG_PATH", tmp_path / "self_tune_history.json")
    tracker = stl.SelfTuneTracker()
    tracker.observe(False, False, True)
    assert stl.get_self_tune_history() == []
