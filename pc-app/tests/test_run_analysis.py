import run_analysis as ra
from modbus_client import Profile, CurveSection


def _profile(name, upper_target=217):
    sections = [CurveSection(upper_temp=upper_target) for _ in range(8)]
    return Profile(name=name, sections=sections)


def test_compare_runs_filters_to_requested_filenames(monkeypatch):
    monkeypatch.setattr(ra, "list_runs", lambda: [
        {"filename": "a.csv", "peak_upper": 1}, {"filename": "b.csv", "peak_upper": 2},
    ])
    result = ra.compare_runs(["b.csv", "missing.csv"])
    assert result == [{"filename": "b.csv", "peak_upper": 2}]


def test_no_recommendation_below_minimum_run_count(monkeypatch):
    monkeypatch.setattr(ra, "list_runs", lambda: [
        {"profile_name": "P", "peak_upper": 220} for _ in range(ra._MIN_RUNS_FOR_RECOMMENDATION - 1)
    ])
    assert ra.recommend_for_profile(_profile("P")) is None


def test_recommends_reducing_target_on_consistent_overshoot(monkeypatch):
    monkeypatch.setattr(ra, "list_runs", lambda: [
        {"profile_name": "P", "peak_upper": 225} for _ in range(5)
    ])
    rec = ra.recommend_for_profile(_profile("P", upper_target=217))
    assert rec is not None
    assert rec.deviation_c > 0
    assert "reducing" in rec.suggestion


def test_recommends_raising_target_on_consistent_undershoot(monkeypatch):
    monkeypatch.setattr(ra, "list_runs", lambda: [
        {"profile_name": "P", "peak_upper": 205} for _ in range(5)
    ])
    rec = ra.recommend_for_profile(_profile("P", upper_target=217))
    assert rec.deviation_c < 0
    assert "raising" in rec.suggestion


# ---- repair outcome stats (2026-09-07, external feature-gap review, second pass) ----

def test_repair_outcome_stats_counts_by_category(monkeypatch):
    monkeypatch.setattr(ra, "list_runs", lambda: [
        {"profile_name": "P", "repair_outcome": "Success"},
        {"profile_name": "P", "repair_outcome": "Success"},
        {"profile_name": "P", "repair_outcome": "Failed"},
        {"profile_name": "P", "repair_outcome": ""},
    ])
    stats = ra.compute_repair_outcome_stats()
    assert stats["total_runs"] == 4
    assert stats["counts"]["Success"] == 2
    assert stats["counts"]["Failed"] == 1
    assert stats["counts"]["not_recorded"] == 1


def test_repair_outcome_stats_success_rate_excludes_not_recorded(monkeypatch):
    monkeypatch.setattr(ra, "list_runs", lambda: [
        {"profile_name": "P", "repair_outcome": "Success"},
        {"profile_name": "P", "repair_outcome": "Failed"},
        {"profile_name": "P", "repair_outcome": ""},
    ])
    stats = ra.compute_repair_outcome_stats()
    assert stats["success_rate"] == 0.5  # 1 of 2 RECORDED outcomes, not 1 of 3 total


def test_repair_outcome_stats_success_rate_none_when_nothing_recorded(monkeypatch):
    monkeypatch.setattr(ra, "list_runs", lambda: [{"profile_name": "P", "repair_outcome": ""}])
    stats = ra.compute_repair_outcome_stats()
    assert stats["success_rate"] is None


def test_repair_outcome_stats_scoped_to_one_profile(monkeypatch):
    monkeypatch.setattr(ra, "list_runs", lambda: [
        {"profile_name": "P1", "repair_outcome": "Success"},
        {"profile_name": "P2", "repair_outcome": "Failed"},
    ])
    stats = ra.compute_repair_outcome_stats(profile_name="P1")
    assert stats["total_runs"] == 1
    assert stats["counts"]["Success"] == 1
    assert stats["counts"]["Failed"] == 0


def test_repair_outcome_stats_handles_missing_field_as_not_recorded(monkeypatch):
    """Older runs from before this feature existed have no repair_outcome key at all - must be
    treated the same as an explicitly empty one, not crash or miscount."""
    monkeypatch.setattr(ra, "list_runs", lambda: [{"profile_name": "P"}])
    stats = ra.compute_repair_outcome_stats()
    assert stats["counts"]["not_recorded"] == 1


def test_no_suggestion_when_within_meaningful_deviation(monkeypatch):
    monkeypatch.setattr(ra, "list_runs", lambda: [
        {"profile_name": "P", "peak_upper": 218} for _ in range(5)
    ])
    rec = ra.recommend_for_profile(_profile("P", upper_target=217))
    assert "consistent with" in rec.suggestion


def test_runs_of_other_profiles_are_ignored(monkeypatch):
    monkeypatch.setattr(ra, "list_runs", lambda: [
        {"profile_name": "Other", "peak_upper": 300} for _ in range(5)
    ])
    assert ra.recommend_for_profile(_profile("P")) is None
