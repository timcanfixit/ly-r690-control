"""Regression tests for profile_import.py's form-block parser - covers two real bugs found live
2026-09-06 importing a user's actual reference workbook, both caused by the parser being too
brittle about a real-world sheet's exact shape rather than the two structural signals that
actually matter (a numeric 1..8 section-number row, and the sheet's own tab name)."""
import io

import openpyxl

import profile_import as pi


def _xlsx_bytes(sheets: dict) -> bytes:
    """Builds a minimal in-memory xlsx with one sheet per (name, rows) entry."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for sheet_name, rows in sheets.items():
        ws = wb.create_sheet(sheet_name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _profile_rows(header_label="Function"):
    """A single form-block, in the traditionally-documented layout (header immediately above the
    9 attribute rows, title immediately above the header, no gaps)."""
    return [
        ["Some Board Reflow"],
        [header_label, 1, 2, 3, 4, 5, 6, 7, 8],
        ["Upper Temper", 130, 160, 185, 205, 225, 245, 258, 262],
        ["Upper Rate", 2, 2, 2, 2, 2, 3, 3, 2],
        ["Holding time", 50, 55, 75, 75, 45, 45, 45, 40],
        ["Lower Temper", 135, 168, 195, 220, 242, 260, 270, 272],
        ["Lower Rate", 2, 2, 2, 2, 3, 3, 3, 2],
        ["Holding time", 50, 55, 75, 75, 45, 45, 45, 40],
        ["IR Temper", 100, 135, 160, 178, 190, 200, 206, 208],
        ["IR Rate", 3, 3, 3, 3, 3, 3, 3, 3],
        ["Holding time", 50, 55, 75, 75, 45, 45, 45, 40],
    ]


def test_traditional_function_label_still_works():
    """Backward compatibility: the originally-documented literal 'Function' label must keep
    working exactly as before."""
    data = _xlsx_bytes({"Sheet1": _profile_rows("Function")})
    result = pi.parse_xlsx(data)
    assert len(result.profiles) == 1
    assert result.profiles[0].name == "Some Board Reflow"
    assert result.profiles[0].sections[0].upper_temp == 130


def test_relabeled_header_still_detected_via_numeric_pattern():
    """Regression test (2026-09-06): a real user reference workbook had every 'Function' label
    renamed to 'TYPE THIS' (a self-authored reminder), which silently broke every sheet's import
    under a literal-string-only check even though the numeric section-number row (1, 2, ..., 8)
    was completely intact. The header must be detected by that numeric pattern regardless of
    whatever text sits in column A."""
    data = _xlsx_bytes({"Sheet1": _profile_rows("TYPE THIS")})
    result = pi.parse_xlsx(data)
    assert len(result.profiles) == 1
    assert result.profiles[0].sections[0].upper_temp == 130
    assert result.warnings == []


def test_any_arbitrary_label_works_as_long_as_numbers_are_1_through_8():
    data = _xlsx_bytes({"Sheet1": _profile_rows("Whatever you want")})
    result = pi.parse_xlsx(data)
    assert len(result.profiles) == 1


def test_row_of_non_sequential_numbers_is_not_mistaken_for_a_header():
    """A row that merely contains 8 numbers, but not exactly 1..8 in order, must not be treated
    as a section-number header - avoids false-positive detection on unrelated numeric data."""
    rows = [
        ["Some Board"],
        ["Not a header", 10, 20, 30, 40, 50, 60, 70, 80],
    ]
    data = _xlsx_bytes({"Sheet1": rows})
    result = pi.parse_xlsx(data)
    assert result.profiles == []


def test_sheet_name_used_as_fallback_when_title_scan_finds_a_gap():
    """Regression test (2026-09-06): a real reference sheet has a title row, then a blank spacer,
    then multi-line instructional text, then ANOTHER blank spacer, then the header row - the
    "nearest non-blank cell above the header" scan stops at the first blank (the spacer
    immediately above the header) and never reaches the actual title several rows further up,
    producing a useless "Imported profile (row N)" name. Since an xlsx built one-profile-per-sheet
    already has the correct name as the sheet's own tab name, that must be used instead."""
    rows = [
        ["Xbox 360 XCGPU Reflow"],
        ["Lead-free (factory SAC)"],
        [None],
        ["TARGET: bond a K-type TC flat to the board..."],
        ["Do this: 1) type the blue -> 2) BOND the TC..."],
        [None],  # blank spacer immediately above the header - breaks the naive upward scan
        ["TYPE THIS", 1, 2, 3, 4, 5, 6, 7, 8],
        ["Upper Temper", 130, 160, 185, 205, 225, 245, 258, 262],
        ["Upper Rate", 2, 2, 2, 2, 2, 3, 3, 2],
        ["Holding time", 50, 55, 75, 75, 45, 45, 45, 40],
        ["Lower Temper", 135, 168, 195, 220, 242, 260, 270, 272],
        ["Lower Rate", 2, 2, 2, 2, 3, 3, 3, 2],
        ["Holding time", 50, 55, 75, 75, 45, 45, 45, 40],
        ["IR Temper", 100, 135, 160, 178, 190, 200, 206, 208],
        ["IR Rate", 3, 3, 3, 3, 3, 3, 3, 3],
        ["Holding time", 50, 55, 75, 75, 45, 45, 45, 40],
    ]
    data = _xlsx_bytes({"Xbox 360 XCGPU Reflow": rows})
    result = pi.parse_xlsx(data)
    assert len(result.profiles) == 1
    assert result.profiles[0].name == "Xbox 360 XCGPU Reflow"
    assert result.profiles[0].sections[0].upper_temp == 130


def test_all_fifteen_sheet_style_real_world_shape_imports_cleanly():
    """End-to-end shape check mirroring the real file that triggered this fix: multiple sheets,
    each with a title/description/blank-spacer/instructions/blank-spacer/TYPE-THIS-header shape,
    every sheet should produce exactly one correctly-named, fully-populated profile."""
    def sheet_rows(title, base_temp):
        return [
            [title],
            ["Lead-free (factory SAC)"],
            [None],
            ["TARGET: ..."],
            ["Do this: ..."],
            [None],
            ["TYPE THIS", 1, 2, 3, 4, 5, 6, 7, 8],
            ["Upper Temper"] + [base_temp + i * 10 for i in range(8)],
            ["Upper Rate"] + [2] * 8,
            ["Holding time"] + [50] * 8,
            ["Lower Temper"] + [base_temp + i * 10 for i in range(8)],
            ["Lower Rate"] + [2] * 8,
            ["Holding time"] + [50] * 8,
            ["IR Temper"] + [base_temp - 20 + i * 10 for i in range(8)],
            ["IR Rate"] + [3] * 8,
            ["Holding time"] + [50] * 8,
        ]

    names = [f"Board {i}" for i in range(15)]
    data = _xlsx_bytes({name: sheet_rows(name, 100 + i) for i, name in enumerate(names)})
    result = pi.parse_xlsx(data)
    assert len(result.profiles) == 15
    assert {p.name for p in result.profiles} == set(names)
    assert result.warnings == []
    for p in result.profiles:
        assert all(getattr(s, "upper_temp") > 0 for s in p.sections)
