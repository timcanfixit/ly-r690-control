from process_limits import ProcessLimits, parse_process_limits, classify_with_limits


# ---- parse_process_limits ----

def test_parse_returns_none_when_metadata_is_none():
    assert parse_process_limits(None) is None


def test_parse_returns_none_when_metadata_has_no_process_limits_key():
    assert parse_process_limits({"process_type": "BGA reflow"}) is None


def test_parse_returns_none_when_process_limits_is_empty_dict():
    assert parse_process_limits({"process_limits": {}}) is None


def test_parse_returns_none_when_every_field_is_none():
    assert parse_process_limits({"process_limits": {"min_time_above_wetting_s": None}}) is None


def test_parse_returns_limits_with_only_specified_fields_set():
    limits = parse_process_limits({"process_limits": {"min_time_above_wetting_s": 45.0}})
    assert limits.min_time_above_wetting_s == 45.0
    assert limits.max_time_above_wetting_s is None
    assert limits.max_peak_upper_c is None


def test_parse_ignores_unknown_keys():
    limits = parse_process_limits({"process_limits": {"min_time_above_wetting_s": 45.0, "bogus_field": 1}})
    assert limits.min_time_above_wetting_s == 45.0
    assert not hasattr(limits, "bogus_field")


# ---- ProcessLimits.is_empty ----

def test_is_empty_true_for_default_construction():
    assert ProcessLimits().is_empty()


def test_is_empty_false_when_any_field_set():
    assert not ProcessLimits(min_time_above_wetting_s=10.0).is_empty()


# ---- classify_with_limits ----

def test_pass_when_within_window_and_no_fault():
    limits = ProcessLimits(min_time_above_wetting_s=30.0, max_time_above_wetting_s=90.0)
    result, violations = classify_with_limits(0, 45.0, {}, limits)
    assert result == "PASS"
    assert violations == []


def test_fail_when_fault_occurred_even_if_window_met():
    limits = ProcessLimits(min_time_above_wetting_s=30.0)
    result, violations = classify_with_limits(1, 45.0, {}, limits)
    assert result == "FAIL"
    assert any("fault" in v for v in violations)


def test_fail_when_below_minimum_tal():
    limits = ProcessLimits(min_time_above_wetting_s=45.0)
    result, violations = classify_with_limits(0, 10.0, {}, limits)
    assert result == "FAIL"
    assert any("below required minimum" in v for v in violations)


def test_fail_when_above_maximum_tal():
    limits = ProcessLimits(max_time_above_wetting_s=60.0)
    result, violations = classify_with_limits(0, 90.0, {}, limits)
    assert result == "FAIL"
    assert any("exceeds allowed maximum" in v for v in violations)


def test_fail_when_peak_zone_exceeds_process_limit():
    limits = ProcessLimits(max_peak_upper_c=235.0)
    zone_stats = {"upper": {"max_c": 240.0}}
    result, violations = classify_with_limits(0, 0.0, zone_stats, limits)
    assert result == "FAIL"
    assert any("Upper peak" in v for v in violations)


def test_pass_when_peak_zone_within_process_limit():
    limits = ProcessLimits(max_peak_upper_c=235.0)
    zone_stats = {"upper": {"max_c": 230.0}}
    result, violations = classify_with_limits(0, 0.0, zone_stats, limits)
    assert result == "PASS"


def test_missing_zone_stats_does_not_crash_or_falsely_violate():
    limits = ProcessLimits(max_peak_upper_c=235.0)
    result, violations = classify_with_limits(0, 0.0, {}, limits)
    assert result == "PASS"


def test_never_returns_incomplete():
    """Unlike classify_process_result()'s conservative three-way guess, a configured window
    always resolves to PASS or FAIL - "not enough happened" becomes a real, named violation."""
    limits = ProcessLimits(min_time_above_wetting_s=45.0)
    result, _ = classify_with_limits(0, 0.0, {}, limits)
    assert result in ("PASS", "FAIL")
    assert result != "INCOMPLETE"
