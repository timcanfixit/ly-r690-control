import config_backup as cb
import profile_store
from modbus_client import Profile, CurveSection
from safety_limits import SafetyLimits
from alerts import AlertConfig


def _profile(name, upper_temp=0):
    sections = [CurveSection() for _ in range(8)]
    sections[0].upper_temp = upper_temp
    return Profile(name=name, sections=sections)


def test_create_backup_includes_safety_limits_alert_config_and_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_store, "PROFILES_DIR", tmp_path)
    profile_store.save_profile(_profile("A", upper_temp=1))
    backup = cb.create_backup(SafetyLimits(), AlertConfig())
    assert backup["schema"] == cb.SCHEMA
    assert len(backup["profiles"]) == 1
    assert backup["profiles"][0]["name"] == "A"
    assert "safety_limits" in backup
    assert "alert_config" in backup


def test_restore_backup_rejects_unrecognized_schema():
    try:
        cb.restore_backup({"schema": "bogus"}, SafetyLimits(), AlertConfig())
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_restore_backup_restores_profiles_and_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_store, "PROFILES_DIR", tmp_path)
    backup = {
        "schema": cb.SCHEMA,
        "created_at": 0.0,
        "safety_limits": {"max_upper_temp_c": 111.0},
        "alert_config": {"wetting_temp_c": 222.0},
        "profiles": [{"name": "Restored", "sections": [vars(CurveSection()) for _ in range(8)], "metadata": {}}],
        "pid_history": [],
    }
    limits = SafetyLimits()
    alert_config = AlertConfig()
    summary = cb.restore_backup(backup, limits, alert_config)
    assert summary["profiles_restored"] == ["Restored"]
    assert summary["profiles_skipped"] == []
    assert limits.max_upper_temp_c == 111.0
    assert alert_config.wetting_temp_c == 222.0
    assert "Restored" in profile_store.list_profiles()


# ---- filename-collision resilience (2026-09-08 audit finding) - see profile_store.save_profile's
# own docstring for why a collision is now a real, raised error rather than silent corruption; a
# backup containing one colliding profile must not abort restoring every OTHER profile in it.

def test_restore_backup_skips_a_colliding_profile_without_aborting_the_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_store, "PROFILES_DIR", tmp_path)
    profile_store.save_profile(_profile("PS5 HDMI ENCODER / SMALL IC REFLOW"))
    backup = {
        "schema": cb.SCHEMA,
        "created_at": 0.0,
        "safety_limits": {},
        "alert_config": {},
        "profiles": [
            # Sanitizes to the same filename as the profile already saved above - must be skipped,
            # not raise and abort the whole restore.
            {"name": "PS5 HDMI ENCODER - SMALL IC REFLOW", "sections": [vars(CurveSection()) for _ in range(8)], "metadata": {}},
            {"name": "Unrelated Profile", "sections": [vars(CurveSection()) for _ in range(8)], "metadata": {}},
        ],
        "pid_history": [],
    }
    summary = cb.restore_backup(backup, SafetyLimits(), AlertConfig())
    assert summary["profiles_restored"] == ["Unrelated Profile"]
    assert len(summary["profiles_skipped"]) == 1
    assert summary["profiles_skipped"][0]["name"] == "PS5 HDMI ENCODER - SMALL IC REFLOW"
    assert "reason" in summary["profiles_skipped"][0]
    # The original, pre-existing profile must be completely untouched.
    assert "PS5 HDMI ENCODER / SMALL IC REFLOW" in profile_store.list_profiles()
    assert "Unrelated Profile" in profile_store.list_profiles()


def test_restore_backup_skips_case_only_duplicates_of_existing_profiles_and_reports_them(tmp_path, monkeypatch):
    """2026-11-12 regression: a restored backup that still carried the old ALL-CAPS copies of real
    profiles used to recreate every one of them as a separate near-duplicate file. They must be
    skipped (with the reason surfaced in profiles_skipped), while profiles that genuinely don't
    exist yet are still restored normally."""
    monkeypatch.setattr(profile_store, "PROFILES_DIR", tmp_path)
    sections = [CurveSection(upper_temp=100) for _ in range(8)]
    profile_store.save_profile(Profile(name="Xbox 360 XCGPU Reflow", sections=sections))

    def entry(name):
        return {"name": name, "sections": [vars(s) for s in sections], "metadata": {}, "version": 1}

    backup = {
        "schema": cb.SCHEMA,
        "profiles": [entry("XBOX 360 XCGPU REFLOW"), entry("Brand New Board")],
    }
    summary = cb.restore_backup(backup, SafetyLimits(), AlertConfig())

    assert summary["profiles_restored"] == ["Brand New Board"]
    assert [s["name"] for s in summary["profiles_skipped"]] == ["XBOX 360 XCGPU REFLOW"]
    assert "differs only by letter case" in summary["profiles_skipped"][0]["reason"]
    assert sorted(profile_store.list_profiles()) == ["Brand New Board", "Xbox 360 XCGPU Reflow"]
