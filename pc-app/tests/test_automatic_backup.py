import time

import automatic_backup as ab
from safety_limits import SafetyLimits
from alerts import AlertConfig


def test_list_automatic_backups_empty_when_no_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "BACKUPS_DIR", tmp_path / "nonexistent")
    assert ab.list_automatic_backups() == []


def test_is_backup_due_true_when_none_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "BACKUPS_DIR", tmp_path)
    assert ab.is_backup_due() is True


def test_create_automatic_backup_writes_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "BACKUPS_DIR", tmp_path)
    path = ab.create_automatic_backup(SafetyLimits(), AlertConfig())
    assert path.exists()
    assert path.name.startswith("backup_")
    backups = ab.list_automatic_backups()
    assert len(backups) == 1
    assert backups[0]["filename"] == path.name


def test_is_backup_due_false_immediately_after_creating_one(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "BACKUPS_DIR", tmp_path)
    ab.create_automatic_backup(SafetyLimits(), AlertConfig())
    assert ab.is_backup_due() is False


def test_is_backup_due_true_once_interval_has_elapsed(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "BACKUPS_DIR", tmp_path)
    monkeypatch.setattr(ab, "_AUTO_BACKUP_INTERVAL_S", 0.05)
    ab.create_automatic_backup(SafetyLimits(), AlertConfig())
    assert ab.is_backup_due() is False
    time.sleep(0.1)
    assert ab.is_backup_due() is True


def test_run_automatic_backup_if_due_creates_one_when_due(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "BACKUPS_DIR", tmp_path)
    path = ab.run_automatic_backup_if_due(SafetyLimits(), AlertConfig())
    assert path is not None
    assert path.exists()


def test_run_automatic_backup_if_due_skips_when_not_due(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "BACKUPS_DIR", tmp_path)
    ab.create_automatic_backup(SafetyLimits(), AlertConfig())
    result = ab.run_automatic_backup_if_due(SafetyLimits(), AlertConfig())
    assert result is None
    assert len(ab.list_automatic_backups()) == 1  # no duplicate created


def test_prune_keeps_only_the_most_recent_max_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "BACKUPS_DIR", tmp_path)
    monkeypatch.setattr(ab, "_MAX_AUTO_BACKUPS", 3)
    for i in range(5):
        # Distinct filenames/timestamps - write directly to avoid the real-time sleep a genuine
        # stamp-per-second scheme would need for 5 backups in a fast test.
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / f"backup_2026090{i}_000000.json").write_text(
            f'{{"created_at": {1000 + i}, "profiles": []}}'
        )
    ab._prune_old_backups()
    remaining = ab.list_automatic_backups()
    assert len(remaining) == 3
    # The 3 most recent (highest created_at) survive
    assert {b["created_at"] for b in remaining} == {1002, 1003, 1004}


def test_list_automatic_backups_skips_corrupt_files(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "BACKUPS_DIR", tmp_path)
    (tmp_path / "backup_bad.json").write_text("not json")
    ab.create_automatic_backup(SafetyLimits(), AlertConfig())
    backups = ab.list_automatic_backups()
    assert len(backups) == 1  # the corrupt one is silently skipped, not crashed on
