import threading

import config_change_history as cch


def test_round_trip_record_and_get(tmp_path, monkeypatch):
    monkeypatch.setattr(cch, "CONFIG_CHANGE_HISTORY_PATH", tmp_path / "config_change_history.json")
    cch.record_config_change(old_values={"a": 1}, new_values={"a": 2})
    history = cch.get_config_change_history()
    assert len(history) == 1
    assert history[0]["old_values"] == {"a": 1}
    assert history[0]["new_values"] == {"a": 2}


def test_note_is_recorded_and_defaults_to_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cch, "CONFIG_CHANGE_HISTORY_PATH", tmp_path / "config_change_history.json")
    cch.record_config_change(old_values=None, new_values={"a": 1})
    cch.record_config_change(old_values={"a": 1}, new_values={"a": 2}, note="Restored from X")
    history = cch.get_config_change_history()
    assert history[0]["note"] == ""
    assert history[1]["note"] == "Restored from X"


def test_first_change_has_no_old_values(tmp_path, monkeypatch):
    monkeypatch.setattr(cch, "CONFIG_CHANGE_HISTORY_PATH", tmp_path / "config_change_history.json")
    cch.record_config_change(old_values=None, new_values={"a": 1})
    assert cch.get_config_change_history()[0]["old_values"] is None


def test_concurrent_writes_do_not_lose_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(cch, "CONFIG_CHANGE_HISTORY_PATH", tmp_path / "config_change_history.json")
    n = 25
    threads = [threading.Thread(target=cch.record_config_change, args=({"a": 0}, {"a": i})) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(cch.get_config_change_history()) == n


def test_history_is_capped_to_max(tmp_path, monkeypatch):
    monkeypatch.setattr(cch, "CONFIG_CHANGE_HISTORY_PATH", tmp_path / "config_change_history.json")
    monkeypatch.setattr(cch, "_MAX_HISTORY", 5)
    for i in range(10):
        cch.record_config_change(old_values={"a": i - 1}, new_values={"a": i})
    history = cch.get_config_change_history()
    assert len(history) == 5
    assert history[-1]["new_values"] == {"a": 9}  # newest preserved, oldest dropped


def test_diff_returns_empty_list_when_old_is_none():
    assert cch.diff_config_values(None, {"a": 1}) == []


def test_diff_lists_only_changed_fields_sorted():
    diffs = cch.diff_config_values({"b": 1, "a": 1}, {"b": 1, "a": 2})
    assert diffs == ["a: 1 -> 2"]


def test_diff_multiple_fields_sorted_by_name():
    diffs = cch.diff_config_values({"z": 1, "a": 1}, {"z": 9, "a": 9})
    assert diffs == ["a: 1 -> 9", "z: 1 -> 9"]


def test_find_history_entry_returns_matching_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(cch, "CONFIG_CHANGE_HISTORY_PATH", tmp_path / "config_change_history.json")
    cch.record_config_change(old_values={"a": 1}, new_values={"a": 2})
    entries = cch.get_config_change_history()
    found = cch.find_history_entry(entries[0]["timestamp"])
    assert found == entries[0]


def test_find_history_entry_returns_none_when_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(cch, "CONFIG_CHANGE_HISTORY_PATH", tmp_path / "config_change_history.json")
    cch.record_config_change(old_values={"a": 1}, new_values={"a": 2})
    assert cch.find_history_entry(999999.0) is None
