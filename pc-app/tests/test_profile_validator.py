from profile_validator import validate_profile
from safety_limits import SafetyLimits
from modbus_client import Profile, CurveSection
from profile_qualification import QualificationStatus


def _filled_section(**overrides):
    base = dict(upper_temp=200, upper_rate=2, upper_hold_time=30,
                lower_temp=150, lower_rate=2, lower_hold_time=30,
                ir_temp=100, ir_rate=2, ir_hold_time=30)
    base.update(overrides)
    return CurveSection(**base)


def test_valid_profile_has_no_errors():
    profile = Profile(sections=[_filled_section() for _ in range(8)])
    result = validate_profile(profile, SafetyLimits())
    assert result.errors == []
    assert result.blocks_send is False


def test_wrong_section_count_is_an_error():
    profile = Profile(sections=[_filled_section()])
    result = validate_profile(profile, SafetyLimits())
    assert result.blocks_send is True


def test_negative_value_is_an_error():
    profile = Profile(sections=[_filled_section(upper_temp=-5)] + [_filled_section() for _ in range(7)])
    result = validate_profile(profile, SafetyLimits())
    assert result.blocks_send is True


def test_exceeding_hard_limit_is_an_error():
    limits = SafetyLimits(max_upper_temp_c=180.0)
    profile = Profile(sections=[_filled_section(upper_temp=200)] + [_filled_section() for _ in range(7)])
    result = validate_profile(profile, limits)
    assert result.blocks_send is True


def test_all_zero_profile_is_an_error_not_just_a_warning():
    profile = Profile(sections=[CurveSection() for _ in range(8)])
    result = validate_profile(profile, SafetyLimits())
    assert result.blocks_send is True


def test_some_empty_sections_is_only_a_warning():
    profile = Profile(sections=[_filled_section() for _ in range(6)] + [CurveSection() for _ in range(2)])
    result = validate_profile(profile, SafetyLimits())
    assert result.blocks_send is False
    assert any("unfilled" in w for w in result.warnings)


def test_hold_time_reported_as_info_not_error():
    profile = Profile(sections=[_filled_section() for _ in range(8)])
    result = validate_profile(profile, SafetyLimits())
    assert any("hold time" in i for i in result.info)


# ---- recipe status metadata (2026-09-07, external feature-gap review) - a soft reminder only,
# never a structural error, and only fires when status is explicitly "Draft".

def test_draft_status_is_a_warning_not_an_error():
    profile = Profile(sections=[_filled_section() for _ in range(8)])
    result = validate_profile(profile, SafetyLimits(), metadata={"status": "Draft"})
    assert result.blocks_send is False
    assert any("Draft" in w for w in result.warnings)


def test_tested_status_produces_no_warning():
    profile = Profile(sections=[_filled_section() for _ in range(8)])
    result = validate_profile(profile, SafetyLimits(), metadata={"status": "Tested"})
    assert not any("Draft" in w for w in result.warnings)


def test_approved_status_produces_no_warning():
    profile = Profile(sections=[_filled_section() for _ in range(8)])
    result = validate_profile(profile, SafetyLimits(), metadata={"status": "Approved"})
    assert not any("Draft" in w for w in result.warnings)


def test_missing_metadata_produces_no_draft_warning():
    """Every profile that existed before this field was added has no status at all - must not be
    silently treated as "Draft" and nagged on the operator's entire existing library."""
    profile = Profile(sections=[_filled_section() for _ in range(8)])
    result = validate_profile(profile, SafetyLimits(), metadata=None)
    assert not any("Draft" in w for w in result.warnings)
    result2 = validate_profile(profile, SafetyLimits(), metadata={})
    assert not any("Draft" in w for w in result2.warnings)


# ---- profile qualification (2026-09-08, external feature-gap review, second pass) - see
# profile_qualification.py for the actual computation; these confirm validate_profile() surfaces
# it correctly and only when it should.

def _unqualified():
    return QualificationStatus(profile_name="P", total_runs=1, thermal_pass_count=1, repair_success_count=0)


def _qualified():
    return QualificationStatus(profile_name="P", total_runs=5, thermal_pass_count=3, repair_success_count=0)


def test_approved_but_unqualified_produces_a_warning():
    profile = Profile(sections=[_filled_section() for _ in range(8)])
    result = validate_profile(profile, SafetyLimits(), metadata={"status": "Approved"}, qualification=_unqualified())
    assert any("Approved" in w and "confirmed thermal PASS" in w for w in result.warnings)
    assert result.blocks_send is False  # never blocking - same as the Draft warning


def test_approved_and_qualified_produces_no_qualification_warning():
    profile = Profile(sections=[_filled_section() for _ in range(8)])
    result = validate_profile(profile, SafetyLimits(), metadata={"status": "Approved"}, qualification=_qualified())
    assert not any("confirmed thermal PASS" in w for w in result.warnings)


def test_approved_with_no_qualification_supplied_produces_no_warning():
    """Callers that don't pass qualification (e.g. no run-history lookup available) get no
    qualification check at all - not a false negative "unqualified" claim."""
    profile = Profile(sections=[_filled_section() for _ in range(8)])
    result = validate_profile(profile, SafetyLimits(), metadata={"status": "Approved"})
    assert not any("confirmed thermal PASS" in w for w in result.warnings)


def test_non_approved_status_ignores_qualification_entirely():
    profile = Profile(sections=[_filled_section() for _ in range(8)])
    result = validate_profile(profile, SafetyLimits(), metadata={"status": "Tested"}, qualification=_unqualified())
    assert not any("confirmed thermal PASS" in w for w in result.warnings)
