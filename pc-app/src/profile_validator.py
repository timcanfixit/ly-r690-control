"""Dedicated profile validator - a fuller check than preflight.py's inline profile check (which
only compares against safety_limits.py's hard temperature ceilings right before Start). This one
also runs before "Send to machine" and checks structural/completeness issues preflight doesn't:
missing/wrong section count, negative values, and sections that are entirely zero (almost always
an unfilled/placeholder section, not an intentional "off" step - a real reflow profile always has
non-zero rate/hold values even in a step where a zone's target temp happens to be 0).

Deliberately excludes ramp-rate and total-duration validation - see safety_limits.py's module
docstring for why (the profile's own "rate" field has never-confirmed real-world units, and
duration can't be estimated without them). Only a rough hold-time-only duration lower bound is
reported as an informational note, explicitly caveated as excluding ramp time.

Errors block sending (see main_window.py's _on_send_to_machine / web_server.py's
api_curve_send). Warnings are shown but do not block - the operator can still choose to send.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from modbus_client import Profile, CURVE_SECTIONS
from safety_limits import SafetyLimits
from profile_qualification import QualificationStatus, QUALIFICATION_THRESHOLD_RUNS

_ZONE_LIMIT_ATTR = {"upper_temp": "max_upper_temp_c", "lower_temp": "max_lower_temp_c", "ir_temp": "max_ir_temp_c"}
_SECTION_FIELDS = (
    "upper_temp", "upper_rate", "upper_hold_time",
    "lower_temp", "lower_rate", "lower_hold_time",
    "ir_temp", "ir_rate", "ir_hold_time",
)


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    @property
    def blocks_send(self) -> bool:
        return bool(self.errors)


def validate_profile(profile: Profile, limits: SafetyLimits, metadata: Optional[dict] = None,
                      qualification: Optional[QualificationStatus] = None) -> ValidationResult:
    """`metadata` (2026-09-07, external feature-gap review - see profile_store.save_profile()'s
    metadata field, now extended with a "status" of Draft/Tested/Approved) is optional and only
    used for the one soft, non-structural check below: a real reminder ("has this actually been
    tried?"), not a safety concern - deliberately a warning, never an error, and deliberately only
    fires when status is explicitly "Draft", never when metadata/status is simply absent. Every
    profile that existed before this field was added has no status at all - treating "unknown" the
    same as "Draft" would nag on the operator's entire existing, already-proven library the first
    time they hit Send after this feature shipped, which is worse than saying nothing.

    `qualification` (2026-09-08, external feature-gap review, second pass - see
    profile_qualification.py) is the same idea one step further: a profile marked "Approved" that
    hasn't actually earned it yet (few/no confirmed successful runs in its own history) gets a
    similarly non-blocking warning. Optional and only checked when both metadata says "Approved"
    AND a qualification status was actually supplied - callers that don't have run history handy
    (or don't want the extra lookup) simply omit it and get no qualification check at all, same as
    omitting metadata skips the Draft check."""
    result = ValidationResult()

    if profile is None:
        result.errors.append("No profile provided.")
        return result
    if metadata and metadata.get("status") == "Draft":
        result.warnings.append(
            "This profile's status is 'Draft' (Metadata) - has it actually been run/validated "
            "before? Update its status once you're confident in it, so this stops asking."
        )
    if metadata and metadata.get("status") == "Approved" and qualification is not None and not qualification.qualified:
        result.warnings.append(
            f"This profile's status is 'Approved' (Metadata) but only has "
            f"{qualification.thermal_pass_count} confirmed thermal PASS run(s) and "
            f"{qualification.repair_success_count} logged repair Success outcome(s) so far "
            f"(recommend >= {QUALIFICATION_THRESHOLD_RUNS} of either) - consider more testing "
            f"before relying on it as Approved."
        )
    if len(profile.sections) != CURVE_SECTIONS:
        result.errors.append(f"Expected {CURVE_SECTIONS} sections, got {len(profile.sections)}.")
        return result

    empty_sections = 0
    for idx, section in enumerate(profile.sections, start=1):
        values = {f: getattr(section, f) for f in _SECTION_FIELDS}
        if all(v == 0 for v in values.values()):
            empty_sections += 1
            continue
        for name, value in values.items():
            if value < 0:
                result.errors.append(f"Section {idx}: {name.replace('_', ' ')} is negative ({value}).")
        for zone_attr, limit_attr in _ZONE_LIMIT_ATTR.items():
            value = values[zone_attr]
            limit = getattr(limits, limit_attr)
            if value > limit:
                result.errors.append(
                    f"Section {idx}: {zone_attr.replace('_', ' ')} ({value}C) exceeds the "
                    f"configured hard maximum ({limit:.0f}C)."
                )

    if empty_sections == CURVE_SECTIONS:
        result.errors.append("Every section is entirely zero - this profile has not been filled in.")
    elif empty_sections:
        result.warnings.append(f"{empty_sections} of {CURVE_SECTIONS} section(s) are entirely zero - likely unfilled.")

    hold_time_lower_bound = sum(
        max(s.upper_hold_time, s.lower_hold_time, s.ir_hold_time) for s in profile.sections
    )
    result.info.append(
        f"Total hold time across all sections: {hold_time_lower_bound}s. This does NOT include "
        f"ramp time between sections - total run duration will be longer (see module docstring "
        f"for why ramp time isn't estimated here)."
    )

    return result
