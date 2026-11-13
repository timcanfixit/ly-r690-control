"""Tests for SafetyLimits persistence (2026-09-07) - confirmed live that its absence was a real,
standing gap: the app was restarted ~6 times in one session for unrelated deploys, and each
restart silently discarded every custom limit back to the code defaults (including a ramp-rate
limit deliberately raised from 120 to 380 during an earlier live false-positive investigation).
Same tmp_path/monkeypatch pattern as test_calibration.py's round-trip tests."""
import safety_limits as sl


def test_missing_file_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "SAFETY_LIMITS_PATH", tmp_path / "does_not_exist.json")
    limits = sl.load_safety_limits()
    assert limits == sl.SafetyLimits()


def test_round_trip_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "SAFETY_LIMITS_PATH", tmp_path / "safety_limits_config.json")
    limits = sl.SafetyLimits()
    limits.max_observed_ramp_c_per_min = 380.0
    limits.ir_reading_correction_c = 21.5
    sl.save_safety_limits(limits)

    loaded = sl.load_safety_limits()
    assert loaded.max_observed_ramp_c_per_min == 380.0
    assert loaded.ir_reading_correction_c == 21.5
    # Everything not explicitly changed should still match the code defaults.
    assert loaded.max_upper_temp_c == sl.SafetyLimits().max_upper_temp_c


def test_corrupt_file_falls_back_to_defaults_without_raising(tmp_path, monkeypatch):
    path = tmp_path / "safety_limits_config.json"
    path.write_text("{not valid json")
    monkeypatch.setattr(sl, "SAFETY_LIMITS_PATH", path)
    limits = sl.load_safety_limits()
    assert limits == sl.SafetyLimits()


def test_unknown_fields_in_file_are_ignored_not_fatal(tmp_path, monkeypatch):
    """A future downgrade (older code reading a newer-saved file) or hand-edited file with a typo
    must not crash the whole app on startup - just skip whatever it doesn't recognize."""
    path = tmp_path / "safety_limits_config.json"
    path.write_text('{"max_upper_temp_c": 350.0, "some_field_that_no_longer_exists": 1.0}')
    monkeypatch.setattr(sl, "SAFETY_LIMITS_PATH", path)
    limits = sl.load_safety_limits()
    assert limits.max_upper_temp_c == 350.0
    assert not hasattr(limits, "some_field_that_no_longer_exists")


def test_save_creates_a_file_that_load_can_read_back_exactly(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "SAFETY_LIMITS_PATH", tmp_path / "safety_limits_config.json")
    original = sl.SafetyLimits(
        max_upper_temp_c=300.0, max_lower_temp_c=310.0, max_ir_temp_c=290.0, max_probe_temp_c=280.0,
        max_observed_ramp_c_per_min=200.0, max_run_duration_s=1800.0, max_communication_outage_s=45.0,
        min_ramp_sample_interval_s=8.0, ir_reading_correction_c=17.0,
    )
    sl.save_safety_limits(original)
    assert sl.load_safety_limits() == original


# ---- per-field sanity validation (2026-09-07, external feature-gap review): a value that parses
# as valid JSON but is nonsensical (e.g. a zero/negative max-temperature, which would fault on
# literally the first reading and permanently block Start) must not be silently trusted just
# because total JSON corruption wasn't detected.

def test_zero_max_temperature_rejected_falls_back_to_default():
    """A file with max_upper_temp_c=0 would fault on every reading forever (bricking Start) if
    trusted as-is - this must fall back to the factory default instead."""
    import safety_limits as sl2
    lo, hi = sl2._SANE_BOUNDS["max_upper_temp_c"]
    assert not (lo <= 0.0 <= hi), "0.0 must actually be outside the sane range for this test to mean anything"


def test_negative_max_temperature_rejected(tmp_path, monkeypatch):
    path = tmp_path / "safety_limits_config.json"
    path.write_text('{"max_upper_temp_c": -50.0}')
    monkeypatch.setattr(sl, "SAFETY_LIMITS_PATH", path)
    limits = sl.load_safety_limits()
    assert limits.max_upper_temp_c == sl.SafetyLimits().max_upper_temp_c


def test_absurdly_large_ramp_limit_rejected(tmp_path, monkeypatch):
    path = tmp_path / "safety_limits_config.json"
    path.write_text('{"max_observed_ramp_c_per_min": 999999999.0}')
    monkeypatch.setattr(sl, "SAFETY_LIMITS_PATH", path)
    limits = sl.load_safety_limits()
    assert limits.max_observed_ramp_c_per_min == sl.SafetyLimits().max_observed_ramp_c_per_min


def test_non_numeric_value_rejected_not_fatal(tmp_path, monkeypatch):
    """A hand-edit gone wrong (e.g. a stray quote making a number into a string, or a typo'd
    boolean) must not crash startup."""
    path = tmp_path / "safety_limits_config.json"
    path.write_text('{"max_upper_temp_c": "not a number", "max_run_duration_s": true}')
    monkeypatch.setattr(sl, "SAFETY_LIMITS_PATH", path)
    limits = sl.load_safety_limits()
    assert limits.max_upper_temp_c == sl.SafetyLimits().max_upper_temp_c
    # bool is technically float()-convertible (True -> 1.0) but 1.0s is absurd for a run duration
    # and must still be caught by the range check, not silently accepted just because it parsed.
    assert limits.max_run_duration_s == sl.SafetyLimits().max_run_duration_s


def test_json_array_instead_of_object_rejected_not_fatal(tmp_path, monkeypatch):
    path = tmp_path / "safety_limits_config.json"
    path.write_text('[1, 2, 3]')
    monkeypatch.setattr(sl, "SAFETY_LIMITS_PATH", path)
    limits = sl.load_safety_limits()
    assert limits == sl.SafetyLimits()


def test_valid_value_within_bounds_is_still_accepted(tmp_path, monkeypatch):
    """The sanity check must not become so strict it rejects real, sensible configuration -
    380C/min was a real value an operator deliberately configured earlier this session."""
    path = tmp_path / "safety_limits_config.json"
    path.write_text('{"max_observed_ramp_c_per_min": 380.0}')
    monkeypatch.setattr(sl, "SAFETY_LIMITS_PATH", path)
    limits = sl.load_safety_limits()
    assert limits.max_observed_ramp_c_per_min == 380.0
