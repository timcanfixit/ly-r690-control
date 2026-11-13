import register_viewer as rv


# ---- validate_register_read_request ----

def test_valid_holding_register_request_passes():
    assert rv.validate_register_read_request(3, 200, 33) is None


def test_valid_coil_request_passes():
    assert rv.validate_register_read_request(1, 150, 29) is None


def test_unsupported_func_code_rejected():
    err = rv.validate_register_read_request(4, 0, 1)
    assert err is not None
    assert "Unsupported function code" in err


def test_write_func_codes_rejected():
    """This module is read-only by construction - write function codes (5, 6, 15, 16) must never
    be accepted here."""
    for code in (5, 6, 15, 16):
        err = rv.validate_register_read_request(code, 0, 1)
        assert err is not None


def test_negative_start_address_rejected():
    err = rv.validate_register_read_request(3, -1, 1)
    assert err is not None
    assert "Start address" in err


def test_zero_count_rejected():
    err = rv.validate_register_read_request(3, 0, 0)
    assert err is not None
    assert "Count" in err


def test_negative_count_rejected():
    err = rv.validate_register_read_request(3, 0, -5)
    assert err is not None


def test_holding_register_count_over_max_rejected():
    err = rv.validate_register_read_request(3, 0, rv._MAX_HOLDING_REGISTER_COUNT + 1)
    assert err is not None
    assert "exceeds the maximum" in err


def test_holding_register_count_at_max_accepted():
    assert rv.validate_register_read_request(3, 0, rv._MAX_HOLDING_REGISTER_COUNT) is None


def test_coil_count_over_max_rejected():
    err = rv.validate_register_read_request(1, 0, rv._MAX_COIL_COUNT + 1)
    assert err is not None


# ---- format_register_rows ----

def test_format_holding_register_rows_includes_hex():
    rows = rv.format_register_rows(3, 200, [10, 255, 4096])
    assert rows[0] == {"address": 200, "value": 10, "hex": "0x000A"}
    assert rows[1] == {"address": 201, "value": 255, "hex": "0x00FF"}
    assert rows[2] == {"address": 202, "value": 4096, "hex": "0x1000"}


def test_format_coil_rows_has_no_hex():
    rows = rv.format_register_rows(1, 150, [True, False])
    assert rows[0] == {"address": 150, "value": True}
    assert "hex" not in rows[0]


def test_format_negative_register_value_masks_to_16_bit_hex():
    """Some registers are signed (e.g. temperature compensation) - a negative value must still
    format as a plausible unsigned hex representation, not crash or produce a negative hex."""
    rows = rv.format_register_rows(3, 0, [-1])
    assert rows[0]["hex"] == "0xFFFF"


def test_format_empty_values_produces_empty_rows():
    assert rv.format_register_rows(3, 0, []) == []
