"""Profile qualification - 2026-09-08, external feature-gap review, second pass.

Answers "has this specific profile actually been proven, not just marked Approved by hand" -
computed entirely from run history already logged (thermal process_result, and, when an operator
has bothered to record one, repair_outcome), the same "never fabricate, only report what's
measured" rule everything else built this session follows.

Deliberately informational, matching this project's existing "status is a soft reminder, never
enforced" decision (see profile_validator.py's own docstring for the Draft-status warning this
sits alongside) - this project has repeatedly decided against auto-blocking or auto-changing
anything based on a profile's metadata (see the metadata dialog's own tooltip: "nothing here is
enforced or auto-changed"). QUALIFICATION_THRESHOLD_RUNS is a suggestion surfaced as a
non-blocking warning when a profile is marked "Approved" without having earned it yet under either
measure, never a gate that prevents sending or starting it.
"""
from __future__ import annotations

from dataclasses import dataclass

from data_logger import list_runs

QUALIFICATION_THRESHOLD_RUNS = 3


@dataclass
class QualificationStatus:
    profile_name: str
    total_runs: int
    thermal_pass_count: int
    repair_success_count: int

    @property
    def qualified(self) -> bool:
        """True once EITHER signal alone reaches the threshold - a logged repair Success is at
        least as strong evidence as an automatic thermal PASS, and requiring both would penalize
        an operator who hasn't gotten around to logging repair outcomes even though the thermal
        profile itself is already well-proven."""
        return (self.thermal_pass_count >= QUALIFICATION_THRESHOLD_RUNS
                or self.repair_success_count >= QUALIFICATION_THRESHOLD_RUNS)


def compute_qualification_status(profile_name: str) -> QualificationStatus:
    runs = [r for r in list_runs() if r["profile_name"] == profile_name]
    thermal_pass_count = sum(1 for r in runs if r.get("process_result") == "PASS")
    repair_success_count = sum(1 for r in runs if r.get("repair_outcome") == "Success")
    return QualificationStatus(
        profile_name=profile_name,
        total_runs=len(runs),
        thermal_pass_count=thermal_pass_count,
        repair_success_count=repair_success_count,
    )
