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
    a1_to_gridrange,
    gridrange_to_a1,
    parse_a1,
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
        # "A2:A" — column A, from row 2 down with no end row -> rows unbounded.
        services, _ = _make_services()
        gr = a1_to_gridrange(services, "SID", "Cliff!A2:A")
        assert gr == {"sheetId": 0, "startColumnIndex": 0, "endColumnIndex": 1}

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
        # startRowIndex present, endRowIndex omitted -> single-row span anchored at start.
        services, _ = _make_services()
        gr = {"sheetId": 0, "startColumnIndex": 0, "endColumnIndex": 1, "startRowIndex": 4}
        # rows present (start only) -> end defaults to start row.
        assert gridrange_to_a1(services, "SID", gr) == "Cliff!A5"


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
# Boundary: addressing imports no transport/CLI/pydantic symbols.
# --------------------------------------------------------------------------------------


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
