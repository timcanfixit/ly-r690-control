import threading

import profile_store as ps
from modbus_client import Profile, CurveSection


def _profile(name, upper_temp=0):
    sections = [CurveSection() for _ in range(8)]
    sections[0].upper_temp = upper_temp
    return Profile(name=name, sections=sections)


def test_save_and_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    ps.save_profile(_profile("Test", upper_temp=200))
    loaded = ps.load_profile("Test")
    assert loaded.name == "Test"
    assert loaded.sections[0].upper_temp == 200


def test_versioning_increments_and_archives_history(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    ps.save_profile(_profile("Versioned", upper_temp=1))
    assert ps.get_profile_version("Versioned") == 1
    ps.save_profile(_profile("Versioned", upper_temp=2))
    assert ps.get_profile_version("Versioned") == 2
    history = ps.get_profile_history("Versioned")
    assert len(history) == 1
    assert history[0]["version"] == 1
    assert history[0]["sections"][0]["upper_temp"] == 1


def test_metadata_preserved_across_a_metadata_none_save(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    ps.save_profile(_profile("WithMeta"), metadata={"process_type": "Reflow"})
    ps.save_profile(_profile("WithMeta", upper_temp=5))  # metadata=None -> leave unchanged
    assert ps.get_profile_metadata("WithMeta") == {"process_type": "Reflow"}


def test_restore_creates_a_new_version_with_old_content(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    ps.save_profile(_profile("Restorable", upper_temp=1))   # v1
    ps.save_profile(_profile("Restorable", upper_temp=2))   # v2
    ps.save_profile(_profile("Restorable", upper_temp=3))   # v3
    restored = ps.restore_profile_version("Restorable", 1)
    assert restored.sections[0].upper_temp == 1
    assert ps.get_profile_version("Restorable") == 4
    assert ps.load_profile("Restorable").sections[0].upper_temp == 1


def test_restore_raises_for_unknown_version(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    ps.save_profile(_profile("Solo"))
    try:
        ps.restore_profile_version("Solo", 99)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_unsafe_filename_characters_are_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    ps.save_profile(_profile("PS5 HDMI ENCODER / SMALL IC REFLOW"))
    assert "PS5 HDMI ENCODER / SMALL IC REFLOW" in ps.list_profiles()


# ---- filename-collision detection (2026-09-08 audit finding) - _safe_filename() only
# guarantees a filesystem-safe name, not a UNIQUE one; two different real names can sanitize to
# the identical filename (e.g. a "/" and a "-" both get replaced with "-"). Before this fix,
# saving the second one silently corrupted the first: its sections got archived under the wrong
# name, its metadata could leak onto the new profile, and it vanished from list_profiles().

def test_save_raises_on_genuine_collision_with_a_different_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    ps.save_profile(_profile("PS5 HDMI ENCODER / SMALL IC REFLOW"))  # sanitizes to "...  - ... "
    try:
        ps.save_profile(_profile("PS5 HDMI ENCODER - SMALL IC REFLOW"))  # same sanitized filename
        assert False, "expected ValueError for the filename collision"
    except ValueError as exc:
        assert "PS5 HDMI ENCODER / SMALL IC REFLOW" in str(exc)


def test_save_collision_does_not_corrupt_the_original_profile(tmp_path, monkeypatch):
    """The original must survive completely untouched after the rejected save - both its content
    and its version number."""
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    ps.save_profile(_profile("PS5 HDMI ENCODER / SMALL IC REFLOW", upper_temp=42))
    try:
        ps.save_profile(_profile("PS5 HDMI ENCODER - SMALL IC REFLOW", upper_temp=999))
    except ValueError:
        pass
    original = ps.load_profile("PS5 HDMI ENCODER / SMALL IC REFLOW")
    assert original.sections[0].upper_temp == 42
    assert ps.get_profile_version("PS5 HDMI ENCODER / SMALL IC REFLOW") == 1
    assert "PS5 HDMI ENCODER / SMALL IC REFLOW" in ps.list_profiles()


def test_saving_the_same_name_repeatedly_is_not_a_collision(tmp_path, monkeypatch):
    """Sanity check - normal re-saves of the SAME profile (the overwhelmingly common case) must
    never be affected by the collision check."""
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    ps.save_profile(_profile("Normal", upper_temp=1))
    ps.save_profile(_profile("Normal", upper_temp=2))  # must not raise
    assert ps.get_profile_version("Normal") == 2


def test_rename_to_a_name_sharing_the_same_sanitized_path_is_allowed(tmp_path, monkeypatch):
    """A rename where the OLD and NEW names sanitize to the identical filename (e.g. "Test:1" ->
    "Test?1", both becoming "Test-1.json") is a legitimate in-place rename of ONE profile, not a
    collision with some unrelated second profile - must not raise."""
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    ps.save_profile(_profile("Test:1"))
    new_path = ps.rename_profile("Test:1", "Test?1")
    assert new_path.exists()
    assert ps.load_profile("Test?1").name == "Test?1"
    assert "Test:1" not in ps.list_profiles()


def test_rename_still_raises_on_a_genuine_collision_with_a_third_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    ps.save_profile(_profile("Source"))
    ps.save_profile(_profile("PS5 HDMI ENCODER / SMALL IC REFLOW"))
    try:
        ps.rename_profile("Source", "PS5 HDMI ENCODER - SMALL IC REFLOW")
        assert False, "expected ValueError for the filename collision"
    except ValueError:
        pass
    # Neither the source nor the pre-existing collider should have been touched.
    assert "Source" in ps.list_profiles()
    assert "PS5 HDMI ENCODER / SMALL IC REFLOW" in ps.list_profiles()


def test_concurrent_saves_of_the_same_profile_lose_no_versions(tmp_path, monkeypatch):
    """Regression test for a read-archive-write race on the same profile file."""
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    n = 30
    threads = [threading.Thread(target=ps.save_profile, args=(_profile("Concurrent", upper_temp=i),)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ps.get_profile_version("Concurrent") == n
    assert len(ps.get_profile_history("Concurrent")) == n - 1


# ---- case-variant duplicate guard (2026-11-12) ----
# 13 stale ALL-CAPS copies of real profiles came back in one backup restore, hours after being
# cleaned up, because the Pi's case-sensitive filesystem happily gave "XBOX 360 XCGPU REFLOW" and
# "Xbox 360 XCGPU Reflow" two separate files.

def test_save_refuses_a_name_that_differs_only_by_case_from_an_existing_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    ps.save_profile(_profile("Xbox 360 XCGPU Reflow", upper_temp=125))
    try:
        ps.save_profile(_profile("XBOX 360 XCGPU REFLOW", upper_temp=999))
        assert False, "expected ValueError for the case-only duplicate"
    except ValueError as exc:
        assert "differs only by letter case" in str(exc)
        assert "Xbox 360 XCGPU Reflow" in str(exc)
    # The real profile is untouched and no second file appeared.
    assert ps.list_profiles() == ["Xbox 360 XCGPU Reflow"]
    assert ps.load_profile("Xbox 360 XCGPU Reflow").sections[0].upper_temp == 125


def test_resaving_the_exact_same_name_is_still_a_normal_versioned_save(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    ps.save_profile(_profile("PS5 APU Reflow", upper_temp=1))
    ps.save_profile(_profile("PS5 APU Reflow", upper_temp=2))
    assert ps.get_profile_version("PS5 APU Reflow") == 2


def test_unrelated_names_are_not_mistaken_for_case_variants(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    ps.save_profile(_profile("PS4 APU Reflow"))
    ps.save_profile(_profile("PS4 APU Reball"))
    ps.save_profile(_profile("PS5 APU Reflow"))
    assert len(ps.list_profiles()) == 3


def test_rename_refuses_a_case_only_collision_with_a_different_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    ps.save_profile(_profile("Source"))
    ps.save_profile(_profile("Target Board"))
    try:
        ps.rename_profile("Source", "TARGET BOARD")
        assert False, "expected ValueError for the case-only duplicate"
    except ValueError as exc:
        assert "differs only by letter case" in str(exc)
    assert sorted(ps.list_profiles()) == ["Source", "Target Board"]


def test_rename_may_change_only_the_case_of_its_own_name(tmp_path, monkeypatch):
    """Renaming a profile to a different capitalization of ITS OWN name is a legitimate edit, not
    a collision with some other profile."""
    monkeypatch.setattr(ps, "PROFILES_DIR", tmp_path)
    ps.save_profile(_profile("ps5 apu reflow", upper_temp=7))
    ps.rename_profile("ps5 apu reflow", "PS5 APU Reflow")
    assert ps.list_profiles() == ["PS5 APU Reflow"]
    assert ps.load_profile("PS5 APU Reflow").sections[0].upper_temp == 7
