"""Raw register/coil viewer - 2026-09-08, external feature-gap review, second pass (the "permanent
register viewer" item).

A standing, read-only tool for browsing arbitrary Modbus registers/coils directly, for ad hoc
troubleshooting when something's behaving oddly and the operator wants to look at the wire-level
data itself rather than wait for a dedicated feature to be built for it. Complements (doesn't
replace) the specific typed readers elsewhere in this app (read_temps_active, read_pid_active,
etc.) - those interpret known registers into meaningful values; this is for the ones that aren't
yet named/understood, or for confirming a specific address's live value while investigating a new
one.

Deliberately READ-ONLY by construction - no write path exists here at all, in this module or in
MachineBus.read_raw_active(). A register viewer that could also write would effectively be an
unrestricted low-level machine control panel, a much bigger safety surface than "let me look at
some numbers."
"""
from __future__ import annotations

from typing import Optional

# Modbus RTU practical limits - FC3 (holding registers) tops out at 125 registers per request per
# the spec; FC1 (coils) at 2000 bits, kept here at the same 125 ceiling as holding registers rather
# than the spec max, since nothing in this app has ever needed more than a few dozen at once (the
# largest existing typed read is PID_COUNT=33) - generous headroom against fat-fingering a huge
# count, not a value tuned to what this specific board actually has.
_MAX_HOLDING_REGISTER_COUNT = 125
_MAX_COIL_COUNT = 125
SUPPORTED_FUNC_CODES = {1: "Coils (FC1)", 3: "Holding Registers (FC3)"}


def validate_register_read_request(func_code: int, start_address: int, count: int) -> Optional[str]:
    """Returns an error message if the request is invalid, or None if it's OK to send. Pure
    validation - no bus access - so a caller (either app's UI) can reject an obviously bad request
    before ever touching the machine."""
    if func_code not in SUPPORTED_FUNC_CODES:
        return f"Unsupported function code {func_code} - only {', '.join(f'{k} ({v})' for k, v in SUPPORTED_FUNC_CODES.items())} are supported."
    if start_address < 0:
        return "Start address must be 0 or greater."
    if count < 1:
        return "Count must be at least 1."
    max_count = _MAX_COIL_COUNT if func_code == 1 else _MAX_HOLDING_REGISTER_COUNT
    if count > max_count:
        return f"Count {count} exceeds the maximum of {max_count} for this function code."
    return None


def format_register_rows(func_code: int, start_address: int, values: list) -> list[dict]:
    """Turns a flat values list into [{"address": int, "value": ...}, ...] rows for display -
    holding-register values also get a hex representation (useful when eyeballing raw wire data),
    coil values don't (a bool has no more useful a hex form)."""
    rows = []
    for i, value in enumerate(values):
        row = {"address": start_address + i, "value": value}
        if func_code == 3:
            row["hex"] = f"0x{value & 0xFFFF:04X}"
        rows.append(row)
    return rows
