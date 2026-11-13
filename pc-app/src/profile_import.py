"""
Bulk profile import from CSV/XLSX files - built so an existing library of profiles (e.g.
transcribed from the touchscreen by hand, or an existing spreadsheet the user already keeps)
can be loaded in one shot instead of typing each one into the Profile Editor table individually.

Two layouts are auto-detected, tried in this order:

1. "Long" format - one row per (profile, attribute), a profile_name column groups rows into
   profiles:

       profile_name, attribute,        section_1, section_2, ..., section_8
       PS5 LF v1,    upper_temp,       150,       165,       ..., 282
       PS5 LF v1,    upper_rate,       2,         2,         ..., 2
       ... (9 attribute rows per profile) ...
       Another profile, upper_temp,    ...

   Multiple profiles are just multiple groups of rows sharing the same profile_name - rows for
   different profiles don't need to be contiguous.

2. "Form block" format - matches real-world reference sheets people already keep (a title row,
   optional description text, then a table matching the machine's own screen layout):

       XBOX 360 XCGPU REFLOW
       Reflow (in-place, no reball)  |  XCGPU  |  Lead-free (factory SAC)  |  ...
       Function,     1 section, 2 section, ..., 8 section
       Upper Temper, 150,       165,       ..., 282
       Upper Rate,   2,         2,         ..., 2
       Holding time, 55,        55,        ..., 56
       Lower Temper, ...
       ...  (9 rows total, same fixed order as the app's own table)
       (any other rows - reference notes, stage tables, etc. - are ignored)

       Next Profile Name
       Function, ...

   Detected by scanning for a section-header row: either the first cell is "Function"
   (case-insensitive), OR - more robustly, and not dependent on that exact label at all - the
   next 8 cells are exactly the integers 1, 2, 3, ..., 8 (confirmed live 2026-09-06: a user's own
   reference sheet had every "Function" label renamed to "TYPE THIS", which silently broke every
   sheet's import under a literal-string-only check even though the numeric section-number row
   was completely intact). The profile name is the nearest non-blank cell in column A above the
   header row, and the following 9 rows are read positionally in the fixed
   Upper/Lower/IR x Temper/Rate/Holding-time order - the header row's own column-A label isn't
   otherwise parsed, so "Function"/"TYPE THIS"/anything else all work the same.

Temperature attributes (upper_temp/lower_temp/ir_temp) are entered as real degrees C (e.g. 165),
matching what the Profile Editor table shows AND what the machine's own curve registers actually
store - confirmed ground truth in docs/06-modbus-register-map.md (a live touchscreen edit of
130->180 landed at address 100 as literally 180, not 1800). Earlier versions of this parser
applied a x10 scale here on the mistaken assumption that curve registers used the same fixed-point
scale as PID/temp-compensation/live readings - they don't. That bug shipped 1200 instead of 120
for every temperature field in every profile ever imported through this app, silently accepted by
the board's write-ACK (which only confirms the write happened, not that the value passed the
board's own sanity check) and then silently reset to 0 by the board - confirmed live 2026-08-19.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from modbus_client import CurveSection, CURVE_ATTRIBUTES, CURVE_SECTIONS, Profile

# Accepts reasonable human variants of each attribute name, normalized (lowercased, spaces and
# hyphens collapsed to underscores) before lookup - so "Upper Temp", "upper-temp", "UPPER_TEMP"
# all resolve the same way. Used by the long-format parser only - the form-block parser reads
# positionally instead, since "Holding time" alone is ambiguous (it repeats 3x per profile).
ATTR_ALIASES = {
    "upper_temp": "upper_temp", "upper_temperature": "upper_temp", "upper_temper": "upper_temp",
    "upper_rate": "upper_rate",
    "upper_hold_time": "upper_hold_time", "upper_holding_time": "upper_hold_time", "upper_hold": "upper_hold_time",
    "lower_temp": "lower_temp", "lower_temperature": "lower_temp", "lower_temper": "lower_temp",
    "lower_rate": "lower_rate",
    "lower_hold_time": "lower_hold_time", "lower_holding_time": "lower_hold_time", "lower_hold": "lower_hold_time",
    "ir_temp": "ir_temp", "ir_temperature": "ir_temp", "ir_temper": "ir_temp", "infrared_temp": "ir_temp",
    "ir_rate": "ir_rate", "infrared_rate": "ir_rate",
    "ir_hold_time": "ir_hold_time", "ir_holding_time": "ir_hold_time", "infrared_hold_time": "ir_hold_time",
}


@dataclass
class ImportResult:
    profiles: list[Profile]
    warnings: list[str]


def _normalize(text) -> str:
    return "_".join(str(text).strip().lower().replace("-", " ").split())


def _find_column(header: list, candidates: set[str]) -> int:
    for i, cell in enumerate(header):
        if _normalize(cell) in candidates:
            return i
    return -1


def _parse_section_values(row: list, cols: list[int], attr: str, row_label: str, warnings: list[str]) -> list[int]:
    """Reads up to CURVE_SECTIONS numeric cells at `cols` from `row`. Curve registers are raw
    integers (see module docstring) - no scaling applied to any attribute, temperature or
    otherwise. Missing/short rows pad with 0."""
    values = []
    for col in cols[:CURVE_SECTIONS]:
        raw_cell = row[col] if col < len(row) else 0
        try:
            value = float(raw_cell) if raw_cell not in (None, "") else 0.0
        except (ValueError, TypeError):
            warnings.append(f"{row_label}: non-numeric value '{raw_cell}', treated as 0.")
            value = 0.0
        values.append(int(round(value)))
    while len(values) < CURVE_SECTIONS:
        values.append(0)
    return values


def _build_profile(name: str, attrs: dict[str, list[int]], warnings: list[str]) -> Profile:
    missing = [a for a in CURVE_ATTRIBUTES if a not in attrs]
    if missing:
        warnings.append(f"Profile '{name}': missing {', '.join(missing)}, defaulted to 0.")
    profile = Profile(name=name)
    for attr_name in CURVE_ATTRIBUTES:
        values = attrs.get(attr_name, [0] * CURVE_SECTIONS)
        for section_idx, v in enumerate(values):
            setattr(profile.sections[section_idx], attr_name, v)
    return profile


def _parse_long_format(rows: list[list]) -> ImportResult | None:
    """Returns None (not an error) if this file doesn't look like the long format at all, so
    the caller can fall back to the form-block parser instead of reporting a hard failure."""
    header = [str(c) for c in rows[0]]
    name_col = _find_column(header, {"profile_name", "profile", "name"})
    attr_col = _find_column(header, {"attribute", "attr", "parameter", "row"})
    if name_col == -1 or attr_col == -1:
        return None
    section_cols = [i for i in range(len(header)) if i not in (name_col, attr_col)]

    warnings: list[str] = []
    grouped: dict[str, dict[str, list[int]]] = {}

    for r_idx, row in enumerate(rows[1:], start=2):
        if not row or all(c in (None, "") for c in row):
            continue
        if len(row) <= max(name_col, attr_col):
            warnings.append(f"Row {r_idx}: too short, skipped.")
            continue
        profile_name = str(row[name_col]).strip()
        attr_raw = _normalize(row[attr_col])
        if not profile_name:
            warnings.append(f"Row {r_idx}: missing profile name, skipped.")
            continue
        attr = ATTR_ALIASES.get(attr_raw)
        if attr is None:
            warnings.append(f"Row {r_idx}: unrecognized attribute '{row[attr_col]}', skipped.")
            continue
        values = _parse_section_values(row, section_cols, attr, f"Row {r_idx}", warnings)
        grouped.setdefault(profile_name, {})[attr] = values

    profiles = [_build_profile(name, attrs, warnings) for name, attrs in grouped.items()]
    if not profiles:
        warnings.append("No valid profile rows found under the long format's profile_name/attribute columns.")
    return ImportResult(profiles=profiles, warnings=warnings)


def _is_section_header_row(row: list) -> bool:
    """A form-block's header row is identified two ways: the traditionally-documented literal
    label 'Function' in column A, OR columns 1-8 being exactly the integers 1..8 (the section
    numbers) regardless of whatever text is in column A. The second check is what actually
    matters - confirmed live 2026-09-06 when a user reference sheet had every 'Function' label
    renamed to 'TYPE THIS' (a self-authored reminder of where to type values), which silently
    broke every single sheet's import under the literal-string-only check even though the
    section-number row itself (1, 2, 3, ..., 8) was completely intact and unambiguous. Detecting
    on that numeric pattern instead survives any future relabeling of column A's own text."""
    if not row:
        return False
    if _normalize(row[0]) == "function":
        return True
    cells = row[1:9]
    return len(cells) == 8 and all(cells[k] == k + 1 for k in range(8))


def _parse_form_blocks(rows: list[list], default_name: str | None = None) -> ImportResult:
    """Matches reference-sheet-style files: scans for a section-header row (see
    _is_section_header_row), takes the nearest non-blank cell above it as the profile name, and
    reads the next 9 rows positionally in the fixed CURVE_ATTRIBUTES order.

    `default_name`, if given, is used when the column-A scan finds nothing - this is the xlsx
    sheet's own tab name, passed down from parse_xlsx(). Needed because the "nearest non-blank
    cell above the header, stopping at the first blank" scan assumes the title sits immediately
    above the header with no gap - real reference sheets often have a title, then a blank spacer
    row, then multi-line description/instruction text, then another blank spacer, then the
    header (confirmed live 2026-09-06: every sheet in a real user file has exactly this shape),
    which makes the scan stop at the spacer immediately above the header and never reach the
    actual title several rows further up. Since an xlsx built one-profile-per-sheet already has
    the correct name sitting right there as the sheet's own tab name, falling back to it beats
    the generic "Imported profile (row N)" placeholder whenever the in-sheet scan comes up empty."""
    warnings: list[str] = []
    profiles = []
    n = len(rows)

    for i, row in enumerate(rows):
        if not _is_section_header_row(row):
            continue

        name = None
        j = i - 1
        while j >= 0:
            first_cell = rows[j][0] if rows[j] else None
            if first_cell in (None, ""):
                break
            name = str(first_cell).strip()
            j -= 1
        if not name:
            name = default_name or f"Imported profile (row {i + 1})"

        attrs_found: dict[str, list[int]] = {}
        for offset, attr in enumerate(CURVE_ATTRIBUTES):
            data_row_idx = i + 1 + offset
            if data_row_idx >= n:
                break
            data_row = rows[data_row_idx] or []
            attrs_found[attr] = _parse_section_values(
                data_row, list(range(1, 1 + CURVE_SECTIONS)), attr, f"Row {data_row_idx + 1} ('{name}')", warnings,
            )

        profiles.append(_build_profile(name, attrs_found, warnings))

    if not profiles:
        warnings.append(
            "No section-header rows found. Expected a row starting with 'Function' (or any label "
            "at all, as long as the next 8 cells are exactly 1, 2, 3, ..., 8) followed by 9 rows "
            "(Upper/Lower/IR x Temper/Rate/Holding time, in that order) - see profile_import.py "
            "for the exact layout."
        )
    return ImportResult(profiles=profiles, warnings=warnings)


def _rows_to_result(rows: list[list], default_name: str | None = None) -> ImportResult:
    if not rows:
        return ImportResult(profiles=[], warnings=["File is empty."])
    long_result = _parse_long_format(rows)
    if long_result is not None:
        return long_result
    return _parse_form_blocks(rows, default_name=default_name)


def parse_csv(data: bytes) -> ImportResult:
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    return _rows_to_result(rows)


def parse_xlsx(data: bytes) -> ImportResult:
    """Scans every sheet in the workbook, not just the active one - a reference file may keep
    profile data on one sheet and unrelated notes on another; sheets with neither recognized
    layout just contribute nothing (no error), so this is safe to run broadly."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)

    all_profiles: list[Profile] = []
    all_warnings: list[str] = []
    any_matched = False
    for sheet_name in wb.sheetnames:
        rows = [list(row) for row in wb[sheet_name].iter_rows(values_only=True)]
        if not rows:
            continue
        result = _rows_to_result(rows, default_name=sheet_name)
        if result.profiles:
            any_matched = True
            all_profiles.extend(result.profiles)
            all_warnings.extend(f"[{sheet_name}] {w}" for w in result.warnings)

    if not any_matched:
        all_warnings.append(f"No recognized profile data found in any sheet ({', '.join(wb.sheetnames)}).")
    return ImportResult(profiles=all_profiles, warnings=all_warnings)


# ---------------- export (reverse of the above - backup/share an existing local library) ----------------

def _export_header() -> list[str]:
    return ["profile_name", "attribute"] + [f"section_{i + 1}" for i in range(CURVE_SECTIONS)]


def export_csv(profiles: list[Profile]) -> bytes:
    """Writes profiles in the "long" format (see module docstring) - the simplest to generate and
    already fully understood by parse_csv, so exporting then re-importing round-trips exactly.
    Lets an existing local profile library (pc-app/profiles/*.json) be backed up or shared as a
    plain file instead of only living in this app's own storage."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(_export_header())
    for profile in profiles:
        for attr in CURVE_ATTRIBUTES:
            writer.writerow([profile.name, attr] + [getattr(s, attr) for s in profile.sections])
    return output.getvalue().encode("utf-8")


def export_xlsx(profiles: list[Profile]) -> bytes:
    """Same long format and same round-trip guarantee as export_csv, written as a single-sheet
    .xlsx instead."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Profiles"
    ws.append(_export_header())
    for profile in profiles:
        for attr in CURVE_ATTRIBUTES:
            ws.append([profile.name, attr] + [getattr(s, attr) for s in profile.sections])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
