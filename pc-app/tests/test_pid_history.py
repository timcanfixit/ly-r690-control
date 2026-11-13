import threading

import pid_history as ph


def test_round_trip_record_and_get(tmp_path, monkeypatch):
    monkeypatch.setattr(ph, "PID_HISTORY_PATH", tmp_path / "pid_history.json")
    ph.record_pid_change(old_values=[1, 2, 3], new_values=[4, 5, 6], note="test")
    history = ph.get_pid_history()
    assert len(history) == 1
    assert history[0]["old_values"] == [1, 2, 3]
    assert history[0]["new_values"] == [4, 5, 6]
    assert history[0]["note"] == "test"


def test_first_send_has_no_old_values(tmp_path, monkeypatch):
    monkeypatch.setattr(ph, "PID_HISTORY_PATH", tmp_path / "pid_history.json")
    ph.record_pid_change(old_values=None, new_values=[1, 2, 3])
    assert ph.get_pid_history()[0]["old_values"] is None


def test_concurrent_writes_do_not_lose_entries(tmp_path, monkeypatch):
    """Regression test for a read-modify-write race: Flask's threaded dev server could otherwise
    let two PID sends landing at the same instant each read the same pre-change file and each
    write back a version missing the other's entry."""
    monkeypatch.setattr(ph, "PID_HISTORY_PATH", tmp_path / "pid_history.json")
    n = 25
    threads = [
        threading.Thread(target=ph.record_pid_change, args=([0], [i]))
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ph.get_pid_history()) == n


# ---- diff_pid_values (2026-09-08, external feature-gap review, second pass) ----

def test_diff_returns_empty_list_when_old_is_none():
    assert ph.diff_pid_values(None, [1, 2, 3]) == []


def test_diff_lists_only_changed_registers():
    diffs = ph.diff_pid_values([1, 2, 3], [1, 99, 3])
    assert diffs == ["register 1: 2 -> 99"]


def test_diff_empty_when_nothing_changed():
    assert ph.diff_pid_values([1, 2, 3], [1, 2, 3]) == []


def test_diff_lists_multiple_changes_in_order():
    diffs = ph.diff_pid_values([1, 2, 3], [9, 2, 8])
    assert diffs == ["register 0: 1 -> 9", "register 2: 3 -> 8"]


# ---- find_history_entry (2026-09-08, external feature-gap review, second pass) ----

def test_find_history_entry_returns_matching_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(ph, "PID_HISTORY_PATH", tmp_path / "pid_history.json")
    ph.record_pid_change(old_values=[1], new_values=[2])
    entries = ph.get_pid_history()
    found = ph.find_history_entry(entries[0]["timestamp"])
    assert found == entries[0]


def test_find_history_entry_returns_none_when_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(ph, "PID_HISTORY_PATH", tmp_path / "pid_history.json")
    ph.record_pid_change(old_values=[1], new_values=[2])
    assert ph.find_history_entry(999999.0) is None
