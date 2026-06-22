"""Unit tests for ``gsheets.core.addressing`` (DESIGN §5.2, §10).

A1 <-> GridRange conversion + sheet-name -> sheetId resolution. The Sheets service is fully
MOCKED (no network). The sheet-name resolution call
(``spreadsheets().get(fields="sheets.properties(sheetId,title,index)").execute()``) is wired
to a fixed sheet index; the A1<->GridRange direction is golden-mastered against
``tests/unit/golden/a1_to_gridrange.json``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gsheets.core.addressing import (
    _SHEET_INDEX_CACHE,
    a1_to_gridrange,
    gridrange_to_a1,
    parse_a1,
    sheet_index_cache,
)
from gsheets.core.errors import SheetsError
from gsheets.core.service import SheetsServices

# The fixed sheet index every test resolves against. Title -> (sheetId, index).
_SHEET_INDEX = [
    {"title": "Cliff", "sheetId": 0, "index": 0},
    {"title": "Plan", "sheetId": 17, "index": 1},
    {"title": "My Sheet", "sheetId": 42, "index": 2},
]


def _make_services(sheets_index=_SHEET_INDEX):
    """Build a SheetsServices whose sheets Resource returns ``sheets_index`` on get()."""
    resource = MagicMock(name="sheets_v4_service")
    payload = {
        "sheets": [
            {
                "properties": {
                    "title": s["title"],
                    "sheetId": s["sheetId"],
                    "index": s["index"],
                }
            }
            for s in sheets_index
        ]
    }
    resource.spreadsheets.return_value.get.return_value.execute.return_value = payload
    return SheetsServices(sheets=resource, drive=None), resource


# --------------------------------------------------------------------------------------
# parse_a1
# --------------------------------------------------------------------------------------


class TestParseA1:
    def test_sheet_qualified_bounded(self):
        assert parse_a1("Cliff!A2:D5") == {"sheet": "Cliff", "start": "A2", "end": "D5"}

    def test_single_cell(self):
        assert parse_a1("Cliff!B2") == {"sheet": "Cliff", "start": "B2", "end": "B2"}

    def test_unqualified_range(self):
        assert parse_a1("A1:D5") == {"sheet": None, "start": "A1", "end": "D5"}

    def test_unqualified_single_cell(self):
        assert parse_a1("B2") == {"sheet": None, "start": "B2", "end": "B2"}

    def test_bare_sheet_name_is_whole_sheet(self):
        assert parse_a1("Cliff") == {"sheet": "Cliff", "start": None, "end": None}

    def test_sheet_with_empty_range_is_whole_sheet(self):
        assert parse_a1("Cliff!") == {"sheet": "Cliff", "start": None, "end": None}

    def test_whole_column(self):
        assert parse_a1("Cliff!A:A") == {"sheet": "Cliff", "start": "A", "end": "A"}

    def test_whole_column_span(self):
        assert parse_a1("Cliff!A:D") == {"sheet": "Cliff", "start": "A", "end": "D"}

    def test_whole_row(self):
        assert parse_a1("Cliff!2:2") == {"sheet": "Cliff", "start": "2", "end": "2"}

    def test_whole_row_span(self):
        assert parse_a1("Cliff!2:5") == {"sheet": "Cliff", "start": "2", "end": "5"}

    def test_quoted_sheet_name(self):
        assert parse_a1("'My Sheet'!A1:B2") == {
            "sheet": "My Sheet",
            "start": "A1",
            "end": "B2",
        }

    def test_quoted_sheet_name_with_bang_inside(self):
        assert parse_a1("'Cliff!notes'!A1") == {
            "sheet": "Cliff!notes",
            "start": "A1",
            "end": "A1",
        }

    def test_quoted_sheet_name_with_escaped_quote(self):
        # A doubled '' inside the quotes is a literal single quote.
        assert parse_a1("'Bob''s data'!A1") == {
            "sheet": "Bob's data",
            "start": "A1",
            "end": "A1",
        }

    def test_quoted_bare_sheet_name(self):
        assert parse_a1("'My Sheet'") == {"sheet": "My Sheet", "start": None, "end": None}

    def test_whitespace_is_stripped(self):
        assert parse_a1("  Cliff!A1:B2  ") == {
            "sheet": "Cliff",
            "start": "A1",
            "end": "B2",
        }

    @pytest.mark.parametrize("bad", ["", "   ", "Cliff!A1:", "Cliff!:B2", "Cliff!A-1"])
    def test_malformed_raises_bad_range(self, bad):
        with pytest.raises(SheetsError) as ei:
            parse_a1(bad)
        assert ei.value.code == "bad_range"

    def test_non_string_raises(self):
        with pytest.raises(SheetsError) as ei:
            parse_a1(None)  # type: ignore[arg-type]
        assert ei.value.code == "bad_range"

    def test_unterminated_quote_raises(self):
        with pytest.raises(SheetsError) as ei:
            parse_a1("'unclosed!A1")
        assert ei.value.code == "bad_range"

    def test_quoted_name_not_followed_by_bang_raises(self):
        # A closed quote with trailing junk that is not "!..." is malformed (addressing.py:240).
        with pytest.raises(SheetsError) as ei:
            parse_a1("'My Sheet'extra")
        assert ei.value.code == "bad_range"
        assert "expected '!'" in str(ei.value)

    def test_empty_sheet_name_before_bang_raises(self):
        # "!A1" has an empty sheet prefix before the "!" (addressing.py:247).
        with pytest.raises(SheetsError) as ei:
            parse_a1("!A1")
        assert ei.value.code == "bad_range"
        assert "empty sheet name" in str(ei.value)

    def test_unqualified_triple_colon_token_is_treated_as_sheet_name(self):
        # "A:B:C" has >2 colon parts, so _looks_like_range rejects it (addressing.py:271); with
        # no "!" it is then taken as a bare (whole-sheet) sheet name, not a range.
        assert parse_a1("A:B:C") == {"sheet": "A:B:C", "start": None, "end": None}

    def test_unqualified_dangling_colon_token_is_treated_as_sheet_name(self):
        # "A1:" has an empty endpoint -> _looks_like_range rejects it (addressing.py:276); with
        # no "!" it degenerates to a bare (whole-sheet) sheet name.
        assert parse_a1("A1:") == {"sheet": "A1:", "start": None, "end": None}

    def test_zero_row_endpoint_rejected_by_split_cell(self):
        # parse_a1 accepts "A0" lexically (regex matches digits), but converting it raises
        # because the row index is < 1 (addressing.py:301, enforced in _split_cell).
        services, _ = _make_services()
        with pytest.raises(SheetsError) as ei:
            a1_to_gridrange(services, "SID", "Cliff!A0:A0")
        assert ei.value.code == "bad_range"
        assert "row index must be >= 1" in str(ei.value)


# --------------------------------------------------------------------------------------
# a1_to_gridrange + gridrange_to_a1 (golden master)
# --------------------------------------------------------------------------------------


class TestGridRangeGolden:
    def test_a1_to_gridrange_golden(self, load_golden):
        cases = load_golden("a1_to_gridrange")["cases"]
        services, _ = _make_services()
        for case in cases:
            got = a1_to_gridrange(services, "SID", case["a1"])
            assert got == case["gridrange"], f"a1_to_gridrange failed for {case['name']}"

    def test_gridrange_to_a1_golden(self, load_golden):
        cases = load_golden("a1_to_gridrange")["cases"]
        services, _ = _make_services()
        for case in cases:
            got = gridrange_to_a1(services, "SID", case["gridrange"])
            assert got == case["roundtrip"], f"gridrange_to_a1 failed for {case['name']}"

    def test_full_roundtrip_canonical(self, load_golden):
        # a1 -> gridrange -> a1 reproduces the canonical form; that canonical form is a
        # fixed point (gridrange -> a1 -> gridrange is identity on the GridRange).
        cases = load_golden("a1_to_gridrange")["cases"]
        services, _ = _make_services()
        for case in cases:
            gr = a1_to_gridrange(services, "SID", case["a1"])
            canonical = gridrange_to_a1(services, "SID", gr)
            assert canonical == case["roundtrip"], case["name"]
            gr2 = a1_to_gridrange(services, "SID", canonical)
            assert gr2 == gr, f"GridRange not a fixed point for {case['name']}"


# --------------------------------------------------------------------------------------
# a1_to_gridrange — index math + unbounded forms (explicit, not just golden)
# --------------------------------------------------------------------------------------


class TestA1ToGridRange:
    def test_zero_based_half_open(self):
        services, _ = _make_services()
        gr = a1_to_gridrange(services, "SID", "Cliff!A1:D5")
        # A1 1-based inclusive -> 0-based half-open.
        assert gr == {
            "sheetId": 0,
            "startRowIndex": 0,
            "endRowIndex": 5,
            "startColumnIndex": 0,
            "endColumnIndex": 4,
        }

    def test_whole_column_omits_row_indices(self):
        services, _ = _make_services()
        gr = a1_to_gridrange(services, "SID", "Cliff!A:A")
        assert "startRowIndex" not in gr
        assert "endRowIndex" not in gr
        assert gr["startColumnIndex"] == 0
        assert gr["endColumnIndex"] == 1

    def test_whole_row_omits_column_indices(self):
        services, _ = _make_services()
        gr = a1_to_gridrange(services, "SID", "Cliff!2:2")
        assert "startColumnIndex" not in gr
        assert "endColumnIndex" not in gr
        assert gr["startRowIndex"] == 1
        assert gr["endRowIndex"] == 2

    def test_whole_sheet_omits_all_indices(self):
        services, _ = _make_services()
        gr = a1_to_gridrange(services, "SID", "Cliff")
        assert gr == {"sheetId": 0}

    def test_partial_column_bound_row_unbound(self):
        # "A2:A" — column A, from row 2 DOWN: startRowIndex set (row 2), endRowIndex OPEN. Must NOT
        # widen to the whole column (which would silently include row 1). Matches Sheets semantics.
        services, _ = _make_services()
        gr = a1_to_gridrange(services, "SID", "Cliff!A2:A")
        assert gr == {
            "sheetId": 0,
            "startRowIndex": 1,
            "startColumnIndex": 0,
            "endColumnIndex": 1,
        }
        # "endRowIndex" stays OPEN (range runs to the bottom of the sheet).
        assert "endRowIndex" not in gr

    def test_partial_column_bound_top_unbound(self):
        # "A:A5" — column A from the TOP down to row 5: endRowIndex set, startRowIndex open.
        services, _ = _make_services()
        gr = a1_to_gridrange(services, "SID", "Cliff!A:A5")
        assert gr == {
            "sheetId": 0,
            "endRowIndex": 5,
            "startColumnIndex": 0,
            "endColumnIndex": 1,
        }
        assert "startRowIndex" not in gr

    def test_partial_row_bound_right_col_unbound(self):
        # "A2:C" — from A2 to the bottom of column C: rows open below row 2, cols A..C bounded.
        services, _ = _make_services()
        gr = a1_to_gridrange(services, "SID", "Cliff!A2:C")
        assert gr == {
            "sheetId": 0,
            "startRowIndex": 1,
            "startColumnIndex": 0,
            "endColumnIndex": 3,
        }

    def test_partial_ranges_round_trip(self):
        # The half-open forms must render back to themselves, not collapse to a single cell.
        services, _ = _make_services()
        for a1 in ("Cliff!A2:A", "Cliff!A:A5", "Cliff!A2:C", "Cliff!A2:2"):
            gr = a1_to_gridrange(services, "SID", a1)
            assert gridrange_to_a1(services, "SID", gr) == a1, a1

    def test_two_letter_column_index(self):
        services, _ = _make_services()
        gr = a1_to_gridrange(services, "SID", "Plan!AA1:AB2")
        assert gr["startColumnIndex"] == 26  # AA -> 26
        assert gr["endColumnIndex"] == 28  # AB inclusive -> exclusive 28
        assert gr["sheetId"] == 17

    def test_reversed_endpoints_are_normalised(self):
        services, _ = _make_services()
        gr = a1_to_gridrange(services, "SID", "Cliff!D5:A1")
        assert gr == {
            "sheetId": 0,
            "startRowIndex": 0,
            "endRowIndex": 5,
            "startColumnIndex": 0,
            "endColumnIndex": 4,
        }

    def test_unqualified_range_binds_first_sheet(self):
        services, _ = _make_services()
        gr = a1_to_gridrange(services, "SID", "A1:C3")
        assert gr["sheetId"] == 0

    def test_quoted_sheet_resolves(self):
        services, _ = _make_services()
        gr = a1_to_gridrange(services, "SID", "'My Sheet'!A1")
        assert gr["sheetId"] == 42

    def test_unknown_sheet_raises_sheet_not_found(self):
        services, _ = _make_services()
        with pytest.raises(SheetsError) as ei:
            a1_to_gridrange(services, "SID", "Nope!A1")
        assert ei.value.code == "sheet_not_found"

    def test_resolution_uses_cheap_mask(self):
        services, resource = _make_services()
        a1_to_gridrange(services, "SID", "Cliff!A1")
        # The per-call get must use the cheapest length-yielding mask, never grid data.
        _, kwargs = resource.spreadsheets.return_value.get.call_args
        assert kwargs["spreadsheetId"] == "SID"
        fields = kwargs["fields"]
        assert "sheets.properties" in fields
        assert "sheetId" in fields and "title" in fields
        assert "includeGridData" not in fields
        assert "rowData" not in fields


# --------------------------------------------------------------------------------------
# gridrange_to_a1 — inverse + sheetId resolution
# --------------------------------------------------------------------------------------


class TestGridRangeToA1:
    def test_bounded(self):
        services, _ = _make_services()
        gr = {
            "sheetId": 0,
            "startRowIndex": 1,
            "endRowIndex": 5,
            "startColumnIndex": 0,
            "endColumnIndex": 4,
        }
        assert gridrange_to_a1(services, "SID", gr) == "Cliff!A2:D5"

    def test_single_cell_collapses(self):
        services, _ = _make_services()
        gr = {
            "sheetId": 0,
            "startRowIndex": 6,
            "endRowIndex": 7,
            "startColumnIndex": 3,
            "endColumnIndex": 4,
        }
        assert gridrange_to_a1(services, "SID", gr) == "Cliff!D7"

    def test_whole_column(self):
        services, _ = _make_services()
        gr = {"sheetId": 0, "startColumnIndex": 0, "endColumnIndex": 1}
        assert gridrange_to_a1(services, "SID", gr) == "Cliff!A:A"

    def test_whole_column_span(self):
        services, _ = _make_services()
        gr = {"sheetId": 0, "startColumnIndex": 0, "endColumnIndex": 4}
        assert gridrange_to_a1(services, "SID", gr) == "Cliff!A:D"

    def test_whole_row(self):
        services, _ = _make_services()
        gr = {"sheetId": 0, "startRowIndex": 1, "endRowIndex": 2}
        assert gridrange_to_a1(services, "SID", gr) == "Cliff!2:2"

    def test_whole_row_span(self):
        services, _ = _make_services()
        gr = {"sheetId": 0, "startRowIndex": 1, "endRowIndex": 5}
        assert gridrange_to_a1(services, "SID", gr) == "Cliff!2:5"

    def test_whole_sheet(self):
        services, _ = _make_services()
        assert gridrange_to_a1(services, "SID", {"sheetId": 0}) == "Cliff"

    def test_quoted_sheet_name_requoted(self):
        services, _ = _make_services()
        gr = {
            "sheetId": 42,
            "startRowIndex": 0,
            "endRowIndex": 2,
            "startColumnIndex": 0,
            "endColumnIndex": 2,
        }
        assert gridrange_to_a1(services, "SID", gr) == "'My Sheet'!A1:B2"

    def test_resolves_sheetid_to_name(self):
        services, _ = _make_services()
        gr = {"sheetId": 17, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 1}
        assert gridrange_to_a1(services, "SID", gr) == "Plan!A1"

    def test_unknown_sheetid_raises(self):
        services, _ = _make_services()
        with pytest.raises(SheetsError) as ei:
            gridrange_to_a1(services, "SID", {"sheetId": 999})
        assert ei.value.code == "sheet_not_found"

    def test_missing_sheetid_raises_bad_range(self):
        services, _ = _make_services()
        with pytest.raises(SheetsError) as ei:
            gridrange_to_a1(services, "SID", {"startRowIndex": 0})
        assert ei.value.code == "bad_range"

    def test_non_dict_raises_bad_range(self):
        services, _ = _make_services()
        with pytest.raises(SheetsError) as ei:
            gridrange_to_a1(services, "SID", "not a dict")  # type: ignore[arg-type]
        assert ei.value.code == "bad_range"

    def test_open_ended_indices_default(self):
        # startRowIndex present, endRowIndex omitted -> OPEN-ended below (Google semantics): the
        # range runs from row 5 to the bottom of column A, rendered faithfully as "A5:A" (NOT
        # collapsed to the single cell "A5", which would misrepresent an unbounded range).
        services, _ = _make_services()
        gr = {"sheetId": 0, "startColumnIndex": 0, "endColumnIndex": 1, "startRowIndex": 4}
        assert gridrange_to_a1(services, "SID", gr) == "Cliff!A5:A"


# --------------------------------------------------------------------------------------
# Malformed sheet-index resolution (missing/None sheetId or title)
# --------------------------------------------------------------------------------------


class TestSheetResolutionEdgeCases:
    def test_unqualified_range_with_no_sheets_raises(self):
        # An unqualified range binds the first sheet; a spreadsheet with NO sheets cannot
        # resolve one (addressing.py:388).
        services, _ = _make_services(sheets_index=[])
        with pytest.raises(SheetsError) as ei:
            a1_to_gridrange(services, "SID", "A1:B2")
        assert ei.value.code == "sheet_not_found"
        assert "no sheets" in str(ei.value)

    def test_unqualified_range_first_sheet_missing_id_raises(self):
        # The first sheet exists but its properties omit sheetId (addressing.py:393).
        services, resource = _make_services()
        resource.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "Cliff", "index": 0}}]
        }
        with pytest.raises(SheetsError) as ei:
            a1_to_gridrange(services, "SID", "A1:B2")
        assert ei.value.code == "sheet_not_found"
        assert "first sheet" in str(ei.value)

    def test_named_sheet_without_id_raises(self):
        # A title match whose properties carry no sheetId (addressing.py:400).
        services, resource = _make_services()
        resource.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "Cliff", "index": 0}}]
        }
        with pytest.raises(SheetsError) as ei:
            a1_to_gridrange(services, "SID", "Cliff!A1")
        assert ei.value.code == "sheet_not_found"
        assert "has no sheetId" in str(ei.value)

    def test_gridrange_to_a1_sheet_without_title_raises(self):
        # gridrange_to_a1 resolves sheetId -> title; a matching sheet missing its title is an
        # error rather than rendering a prefix-less range (addressing.py:419).
        services, resource = _make_services()
        resource.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"sheetId": 7, "index": 0}}]
        }
        gr = {
            "sheetId": 7,
            "startRowIndex": 0,
            "endRowIndex": 1,
            "startColumnIndex": 0,
            "endColumnIndex": 1,
        }
        with pytest.raises(SheetsError) as ei:
            gridrange_to_a1(services, "SID", gr)
        assert ei.value.code == "sheet_not_found"
        assert "has no title" in str(ei.value)


# --------------------------------------------------------------------------------------
# Pure internal helpers — boundary branches not reachable via the validated public path
# --------------------------------------------------------------------------------------


class TestInternalHelperBranches:
    def test_split_cell_rejects_unparseable_token(self):
        # _CELL_RE (col-letters then digits) does not match a digit-then-letter token; the
        # public path pre-validates, so we drive the helper directly (addressing.py:294).
        from gsheets.core.addressing import _split_cell

        with pytest.raises(SheetsError) as ei:
            _split_cell("1A", "1A")
        assert ei.value.code == "bad_range"

    def test_split_cell_rejects_empty_endpoint(self):
        # An empty token carries neither a column nor a row (addressing.py:298).
        from gsheets.core.addressing import _split_cell

        with pytest.raises(SheetsError) as ei:
            _split_cell("", "orig")
        assert ei.value.code == "bad_range"
        assert "empty A1 endpoint" in str(ei.value)

    def test_split_cell_parses_column_only_and_row_only(self):
        # Column-only ("A") -> row None; row-only ("5") -> col None — the unbounded endpoints.
        from gsheets.core.addressing import _split_cell

        assert _split_cell("A", "A") == ("A", None)
        assert _split_cell("5", "5") == (None, 5)

    def test_index_to_col_rejects_negative(self):
        # A negative column index is a programming error, not a renderable column
        # (addressing.py:316).
        from gsheets.core.addressing import _index_to_col

        with pytest.raises(SheetsError) as ei:
            _index_to_col(-1)
        assert ei.value.code == "bad_range"

    def test_col_index_roundtrip_boundaries(self):
        # Pin the 0-based column math at the Z/AA and the two-letter boundary.
        from gsheets.core.addressing import _col_to_index, _index_to_col

        assert _col_to_index("A") == 0
        assert _col_to_index("Z") == 25
        assert _col_to_index("AA") == 26
        assert _col_to_index("AB") == 27
        assert _index_to_col(0) == "A"
        assert _index_to_col(25) == "Z"
        assert _index_to_col(26) == "AA"
        assert _index_to_col(27) == "AB"

    def test_looks_like_range_rejects_three_parts(self):
        # More than two colon-separated endpoints is never a valid A1 range
        # (addressing.py:271).
        from gsheets.core.addressing import _looks_like_range

        assert _looks_like_range("A:B:C") is False
        assert _looks_like_range("A1:B2") is True

    def test_looks_like_range_rejects_empty_endpoint(self):
        # An endpoint that is neither a column nor a row token fails the range check
        # (addressing.py:276).
        from gsheets.core.addressing import _looks_like_range

        assert _looks_like_range("A1:") is False
        assert _looks_like_range(":B2") is False


# --------------------------------------------------------------------------------------
# Google HttpError -> SheetsError classification on the resolution call
# --------------------------------------------------------------------------------------


class TestErrorClassification:
    def test_http_error_is_classified(self):
        from googleapiclient.errors import HttpError

        resp = type("Resp", (), {"status": 404, "reason": "Not Found"})()
        content = b'{"error": {"code": 404, "status": "NOT_FOUND", "message": "Requested entity was not found."}}'
        http_err = HttpError(resp=resp, content=content)

        resource = MagicMock(name="sheets_v4_service")
        resource.spreadsheets.return_value.get.return_value.execute.side_effect = http_err
        services = SheetsServices(sheets=resource, drive=None)

        with pytest.raises(SheetsError) as ei:
            a1_to_gridrange(services, "SID", "Cliff!A1")
        assert ei.value.code == "google_api_error"
        assert ei.value.status == 404

    def test_non_http_error_propagates(self):
        resource = MagicMock(name="sheets_v4_service")
        resource.spreadsheets.return_value.get.return_value.execute.side_effect = RuntimeError("boom")
        services = SheetsServices(sheets=resource, drive=None)
        with pytest.raises(RuntimeError):
            a1_to_gridrange(services, "SID", "Cliff!A1")


# --------------------------------------------------------------------------------------
# gridranges_intersect — used by ``describe`` to filter CF rules to a requested range (SPEC §3.3).
# --------------------------------------------------------------------------------------


from gsheets.core.addressing import gridranges_intersect  # noqa: E402


def _gr(sheet_id, r0=None, r1=None, c0=None, c1=None):
    """Build a GridRange dict, omitting unbounded (None) indices (the open-range convention)."""
    gr = {"sheetId": sheet_id}
    if r0 is not None:
        gr["startRowIndex"] = r0
    if r1 is not None:
        gr["endRowIndex"] = r1
    if c0 is not None:
        gr["startColumnIndex"] = c0
    if c1 is not None:
        gr["endColumnIndex"] = c1
    return gr


class TestGridRangesIntersect:
    def test_different_sheet_never_intersects(self):
        assert not gridranges_intersect(_gr(0, 0, 10, 0, 5), _gr(1, 0, 10, 0, 5))

    def test_overlapping_bounded_rectangles_intersect(self):
        # [rows 0:10, cols 0:5) vs [rows 5:20, cols 2:8): overlap at rows 5:10, cols 2:5.
        assert gridranges_intersect(_gr(0, 0, 10, 0, 5), _gr(0, 5, 20, 2, 8))

    def test_row_disjoint_does_not_intersect(self):
        # Same cols, but rows 0:10 vs 10:20 are half-open adjacent (no shared row).
        assert not gridranges_intersect(_gr(0, 0, 10, 0, 5), _gr(0, 10, 20, 0, 5))

    def test_col_disjoint_does_not_intersect(self):
        assert not gridranges_intersect(_gr(0, 0, 10, 0, 5), _gr(0, 0, 10, 5, 8))

    def test_unbounded_rows_cover_any_row_band(self):
        # A whole-column rule (no row indices) intersects any row band on the same sheet/cols.
        whole_col = _gr(0, c0=0, c1=1)  # column A, all rows
        assert gridranges_intersect(whole_col, _gr(0, 100, 200, 0, 1))

    def test_unbounded_cols_cover_any_col_band(self):
        whole_row = _gr(0, r0=4, r1=5)  # row 5, all cols
        assert gridranges_intersect(whole_row, _gr(0, 4, 5, 50, 60))

    def test_whole_sheet_intersects_everything_on_sheet(self):
        whole_sheet = _gr(0)
        assert gridranges_intersect(whole_sheet, _gr(0, 100, 200, 44, 45))

    def test_unbounded_cols_but_disjoint_rows_does_not_intersect(self):
        whole_row = _gr(0, r0=0, r1=1)  # row 1, all cols
        assert not gridranges_intersect(whole_row, _gr(0, 5, 10, 0, 1))


# --------------------------------------------------------------------------------------
# Boundary: addressing imports no transport/CLI/pydantic symbols.
# --------------------------------------------------------------------------------------


class TestSheetIndexCache:
    """The per-operation sheet-index cache (ISSUES.md #26, #27).

    ``_sheet_index`` — reached by every ``gridrange_to_a1`` / ``a1_to_gridrange`` — issues a network
    ``spreadsheets.get`` once per call UNLESS a ``sheet_index_cache()`` scope is active, in which
    case the sheet list is fetched ONCE and reused for the whole scope. The adapters open this scope
    around the entire core dispatch, so any operation resolving N ranges does 1 sheet-index get, not
    N (the #27 systemic fix).
    """

    @staticmethod
    def _get_count(resource):
        return resource.spreadsheets.return_value.get.call_count

    def test_outside_scope_every_conversion_refetches(self):
        # No scope => the contextvar default is None => no caching => one network get PER conversion.
        services, resource = _make_services()
        assert _SHEET_INDEX_CACHE.get() is None
        gridrange_to_a1(services, "SID", {"sheetId": 0})
        gridrange_to_a1(services, "SID", {"sheetId": 17})
        assert self._get_count(resource) == 2

    def test_scope_collapses_to_a_single_get(self):
        # The whole point of #27: N conversions inside one scope share ONE sheet-index lookup.
        services, resource = _make_services()
        with sheet_index_cache():
            gridrange_to_a1(services, "SID", {"sheetId": 0})
            gridrange_to_a1(services, "SID", {"sheetId": 17})
            a1_to_gridrange(services, "SID", "Plan!A1:B2")
        assert self._get_count(resource) == 1

    def test_scope_is_per_operation_not_shared_across_scopes(self):
        # Two SEPARATE scopes each fetch once, so a tab renamed BETWEEN operations is never served
        # from a stale cache (per-operation freshness): 2 conversions in 2 scopes => 2 gets.
        services, resource = _make_services()
        with sheet_index_cache():
            gridrange_to_a1(services, "SID", {"sheetId": 0})
        with sheet_index_cache():
            gridrange_to_a1(services, "SID", {"sheetId": 17})
        assert self._get_count(resource) == 2

    def test_reentrant_inner_scope_reuses_outer_cache(self):
        # A nested scope (the adapter opens one; a core fn such as read_conditional_formats nests its
        # own) must REUSE the outer cache — no second fetch — and must NOT tear it down on inner exit.
        services, resource = _make_services()
        with sheet_index_cache():
            outer_cache = _SHEET_INDEX_CACHE.get()
            assert outer_cache is not None
            gridrange_to_a1(services, "SID", {"sheetId": 0})
            with sheet_index_cache():
                # Same dict object — the inner scope did not shadow it with a fresh {}.
                assert _SHEET_INDEX_CACHE.get() is outer_cache
                gridrange_to_a1(services, "SID", {"sheetId": 17})
            # Outer cache survives the inner exit (inner did not reset the contextvar to None).
            assert _SHEET_INDEX_CACHE.get() is outer_cache
        assert self._get_count(resource) == 1

    def test_scope_resets_to_none_on_exit(self):
        services, _ = _make_services()
        assert _SHEET_INDEX_CACHE.get() is None
        with sheet_index_cache():
            assert _SHEET_INDEX_CACHE.get() is not None
        assert _SHEET_INDEX_CACHE.get() is None


def test_no_transport_imports():
    """addressing.py must import no transport/CLI/pydantic modules (DESIGN §1 boundary).

    Parses the module's actual ``import`` statements (not its prose) so docstring mentions
    of the forbidden names don't trip the guard.
    """
    import ast

    import gsheets.core.addressing as mod

    forbidden_roots = {"fastmcp", "mcp", "argparse", "pydantic"}
    tree = ast.parse(open(mod.__file__, encoding="utf-8").read())
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_roots.add(node.module.split(".")[0])
            # A `from gsheets.models import X` is also forbidden in core.
            if node.module and node.module.startswith("gsheets.models"):
                imported_roots.add("gsheets.models")
    assert not (forbidden_roots & imported_roots), sorted(forbidden_roots & imported_roots)
    assert "gsheets.models" not in imported_roots
