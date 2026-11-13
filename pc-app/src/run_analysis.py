"""Cross-run analysis built entirely from data_logger.list_runs()'s existing summaries - no new
instrumentation needed, just using history that's already being logged. Two things:

1. compare_runs(): pick any set of runs and see their stats side by side - a plain table (matching
   the roadmap's own example format), not an overlaid chart. An overlay would need the full
   per-second CSV data for each run loaded and re-plotted together - a real feature, just a bigger
   one than this pass covers; flagging it as a reasonable future addition rather than building it
   silently smaller than intended.

2. recommend_for_profile(): groups past runs by profile name and flags a CONSISTENT deviation
   between the profile's own target peak and what actually happened across multiple real runs.
   Requires at least 3 runs of the same profile before saying anything - one or two runs could
   just be noise, not a pattern. This only ever produces a suggestion for a human to consider; it
   never modifies a profile itself (see the roadmap's own rule: "Do not automatically modify
   profiles").
"""
from __future__ import annotations

from dataclasses import dataclass

from data_logger import list_runs
from modbus_client import Profile

_MIN_RUNS_FOR_RECOMMENDATION = 3
_MEANINGFUL_DEVIATION_C = 5.0


def compare_runs(filenames: list[str]) -> list[dict]:
    all_runs = {r["filename"]: r for r in list_runs()}
    return [all_runs[f] for f in filenames if f in all_runs]


def compute_repair_outcome_stats(profile_name: str | None = None) -> dict:
    """Tallies data_logger.list_runs()'s "repair_outcome" field - 2026-09-07, external
    feature-gap review, second pass. Pass a profile_name to scope to runs of just that profile
    (e.g. "does this specific recipe actually fix boards"), or None for every run with an outcome
    recorded at all. Runs with no outcome set yet (repair_outcome == "") are counted separately as
    "not_recorded" rather than silently excluded - a shop that's used this for two weeks should be
    able to see how much of its own history still has no outcome logged, not just the runs that
    do."""
    runs = list_runs()
    if profile_name is not None:
        runs = [r for r in runs if r["profile_name"] == profile_name]

    counts = {"Success": 0, "Partial": 0, "Failed": 0, "Unknown": 0, "not_recorded": 0}
    for r in runs:
        outcome = r.get("repair_outcome") or ""
        if outcome in counts:
            counts[outcome] += 1
        else:
            counts["not_recorded"] += 1

    recorded = len(runs) - counts["not_recorded"]
    success_rate = (counts["Success"] / recorded) if recorded > 0 else None
    return {
        "profile_name": profile_name,
        "total_runs": len(runs),
        "counts": counts,
        "success_rate": success_rate,
    }


@dataclass
class ProfileRecommendation:
    profile_name: str
    run_count: int
    avg_peak_upper: float
    target_peak_upper: float
    deviation_c: float
    suggestion: str


def recommend_for_profile(profile: Profile) -> ProfileRecommendation | None:
    matching = [r for r in list_runs() if r["profile_name"] == profile.name]
    if len(matching) < _MIN_RUNS_FOR_RECOMMENDATION:
        return None

    avg_peak_upper = sum(r["peak_upper"] for r in matching) / len(matching)
    target_peak_upper = max((s.upper_temp for s in profile.sections), default=0)
    deviation = avg_peak_upper - target_peak_upper

    if abs(deviation) < _MEANINGFUL_DEVIATION_C:
        suggestion = "Peak Upper temperature is consistent with this profile's target - no change suggested."
    elif deviation > 0:
        suggestion = (
            f"This profile has consistently overshot its Upper target by about {deviation:.1f}C "
            f"across {len(matching)} runs - consider reducing the peak Upper target."
        )
    else:
        suggestion = (
            f"This profile has consistently undershot its Upper target by about {abs(deviation):.1f}C "
            f"across {len(matching)} runs - consider raising the peak Upper target, or checking for "
            f"a slow/underpowered zone."
        )

    return ProfileRecommendation(
        profile_name=profile.name,
        run_count=len(matching),
        avg_peak_upper=avg_peak_upper,
        target_peak_upper=target_peak_upper,
        deviation_c=deviation,
        suggestion=suggestion,
    )
