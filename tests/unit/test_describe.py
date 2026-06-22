"""Unit tests for ``gsheets.core.describe`` (SPEC §3 — unified one-call region read).

``describe`` issues ONE ``spreadsheets.get(ranges=[...], includeGridData=True, fields=<union mask>)``
and, per requested range, returns ``{range, cells, merges, conditionalFormats, tables, bandedRanges,
protectedRanges, validationSummary}`` by reusing the EXISTING serializers (``reads._serialize_cells``,
``reads._serialize_cf_rules`` + the addressing intersect filter, ``structure._serialize_{tables,
banding,protected}``) on slices of that single response.

These tests run against a MOCKED service: one ``_GetRecorder`` serves both the describe data get
(``includeGridData=True``) and the sheet-index get the addressing layer issues. They pin:

* the ONE get's outbound mask (tight union; never the heavy bare ``includeGridData`` blob);
* multi-range AND multi-sheet block-to-range mapping;
* CF rules filtered to those INTERSECTING each requested range (range-scoped CF for free);
* ``max_cells`` -> ``result_too_large`` (like ``read_values``);
* NO cache (each call re-issues the get).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gsheets.core.errors import SheetsError
from gsheets.core.reads import describe
from gsheets.core.service import SheetsServices

GOLDEN_DIR = Path(__file__).parent / "golden"


def load_golden(name: str) -> dict:
    """Load a committed golden ``describe`` fixture."""
    filename = name if name.endswith(".json") else f"{name}.json"
    return json.loads((GOLDEN_DIR / filename).read_text())


# --------------------------------------------------------------------------- helpers


class _GetRecorder:
    """Records each ``get(**kwargs)`` and returns a queued response on ``.execute()``.

    Dispatches by the requested ``fields`` mask: the sheet-index lookup
    (``sheets.properties(sheetId,title,index)``) returns the fixed index; everything else pops the
    data-response queue. The data queue is RE-USED per call so a second describe call (no-cache
    assertion) needs its own queued response.
    """

    _SHEET_INDEX_FIELDS = "sheets.properties(sheetId,title,index)"

    def __init__(self, data_responses, sheet_index_response):
        self._data = list(data_responses)
        self._sheet_index = sheet_index_response
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("fields") == self._SHEET_INDEX_FIELDS:
            resp = self._sheet_index
        else:
            resp = self._data.pop(0) if self._data else {}
        req = MagicMock(name="request")
        req.execute.return_value = resp
        return req

    @property
    def data_calls(self) -> list[dict]:
        return [c for c in self.calls if c.get("fields") != self._SHEET_INDEX_FIELDS]


def _make_service(*, data_responses=None, sheet_index=None, account_email=None):
    sheets = MagicMock(name="sheets_v4")
    rec = _GetRecorder(data_responses or [], sheet_index or {"sheets": []})
    sheets.spreadsheets.return_value.get = rec
    services = SheetsServices(sheets=sheets, drive=None, account_email=account_email)
    return services, rec


def _make_http_error(status=403):
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = status
    resp.reason = "Forbidden"
    content = (
        b'{"error": {"code": %d, "status": "PERMISSION_DENIED", "message": "nope"}}' % status
    )
    return HttpError(resp=resp, content=content)


SHEET_ID = "<TEST_SHEET_ID>"

# A two-tab index (Cliff=0, Plan=7) for multi-sheet mapping.
_INDEX = {
    "sheets": [
        {"properties": {"sheetId": 0, "title": "Cliff", "index": 0}},
        {"properties": {"sheetId": 7, "title": "Plan", "index": 1}},
    ]
}


def _cell(value=None, formula=None):
    cell: dict = {}
    if formula is not None:
        cell["userEnteredValue"] = {"formulaValue": formula}
    if value is not None:
        cell["formattedValue"] = str(value)
    return cell


def _block(start_row, start_col, rowdata):
    return {"startRow": start_row, "startColumn": start_col, "rowData": rowdata}


# A boolean CF rule over Cliff!A2:A100 (cols A, rows 1..99 0-based half-open).
_CF_A = {
    "ranges": [
        {"sheetId": 0, "startRowIndex": 1, "endRowIndex": 100,
         "startColumnIndex": 0, "endColumnIndex": 1}
    ],
    "booleanRule": {
        "condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]},
        "format": {"backgroundColorStyle": {"rgbColor": {"red": 0.0, "green": 1.0, "blue": 0.0}}},
    },
}
# A boolean CF rule over Cliff!Z2:Z100 (col Z) — should NOT intersect a request in column A.
_CF_Z = {
    "ranges": [
        {"sheetId": 0, "startRowIndex": 1, "endRowIndex": 100,
         "startColumnIndex": 25, "endColumnIndex": 26}
    ],
    "booleanRule": {
        "condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": "x"}]},
        "format": {"backgroundColorStyle": {"rgbColor": {"red": 1.0, "green": 0.0, "blue": 0.0}}},
    },
}


# =========================================================================== mask


class TestDescribeMask:
    def _run(self):
        payload = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff"},
                    "data": [_block(0, 0, [{"values": [_cell(value="x")]}])],
                }
            ]
        }
        services, rec = _make_service(data_responses=[payload], sheet_index=_INDEX)
        describe(services, SHEET_ID, ["Cliff!A1:B2"])
        return rec.data_calls[0]

    def test_one_get_with_include_grid_data_and_ranges(self):
        sent = self._run()
        assert sent["spreadsheetId"] == SHEET_ID
        assert sent["ranges"] == ["Cliff!A1:B2"]
        assert sent["includeGridData"] is True

    def test_mask_unions_inspect_structure_cf(self):
        fields = self._run()["fields"]
        # per-cell (inspect) facets
        assert "effectiveValue" in fields
        assert "userEnteredValue" in fields
        assert "userEnteredFormat" in fields
        assert "effectiveFormat" in fields
        assert "dataValidation" in fields
        # structural facets
        assert "merges" in fields
        assert "conditionalFormats" in fields
        assert "tables" in fields
        assert "bandedRanges" in fields
        assert "protectedRanges" in fields
        # never the heavy bare includeGridData blob in the mask itself
        assert "includeGridData" not in fields


# =========================================================================== single range


class TestDescribeSingleRange:
    def test_cells_merges_and_intersecting_cf(self):
        payload = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff"},
                    "data": [
                        _block(
                            0, 0,
                            [{"values": [_cell(formula="=SUM(B:B)", value="10"), _cell(value="hi")]}],
                        )
                    ],
                    "merges": [
                        {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1,
                         "startColumnIndex": 0, "endColumnIndex": 2}
                    ],
                    "conditionalFormats": [_CF_A, _CF_Z],
                    "tables": [],
                    "bandedRanges": [],
                    "protectedRanges": [],
                }
            ]
        }
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        out = describe(services, SHEET_ID, ["Cliff!A1:B2"])

        assert out["ok"] is True
        assert out["spreadsheetId"] == SHEET_ID
        assert len(out["regions"]) == 1
        region = out["regions"][0]
        assert region["range"] == "Cliff!A1:B2"
        # cells reuse inspect's serializer (padded to the returned grid block's extent, exactly
        # like inspect — one rowData row here -> two cells).
        assert region["cells"] == [
            {"a1": "A1", "value": "10", "formula": "=SUM(B:B)"},
            {"a1": "B1", "value": "hi"},
        ]
        # merges resolved to A1
        assert region["merges"] == ["Cliff!A1:B1"]
        # ONLY the CF rule whose range (col A) intersects the request is kept; col Z dropped.
        assert len(region["conditionalFormats"]) == 1
        cf = region["conditionalFormats"][0]
        assert cf["index"] == 0  # original array index preserved
        assert cf["ranges"] == ["Cliff!A2:A100"]
        assert cf["condition"]["type"] == "NUMBER_GREATER"
        assert region["tables"] == []
        assert region["bandedRanges"] == []
        assert region["protectedRanges"] == []

    def test_cf_index_preserved_when_earlier_rule_filtered_out(self):
        # _CF_Z (index 0) is filtered out for a col-A request, but the surviving _CF_A keeps
        # its ORIGINAL index 1 (priority is array position — must not renumber).
        payload = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff"},
                    "data": [_block(0, 0, [{"values": [_cell(value="x")]}])],
                    "conditionalFormats": [_CF_Z, _CF_A],
                }
            ]
        }
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        out = describe(services, SHEET_ID, ["Cliff!A1:A50"])
        rules = out["regions"][0]["conditionalFormats"]
        assert len(rules) == 1
        assert rules[0]["index"] == 1
        assert rules[0]["ranges"] == ["Cliff!A2:A100"]


# =========================================================================== validation summary


class TestDescribeValidationSummary:
    def test_validation_summary_counts_and_distinct_lines(self, monkeypatch):
        from gsheets.core import reads as reads_mod

        monkeypatch.setattr(
            reads_mod, "validation_to_rule",
            lambda g: {"type": "ONE_OF_LIST", "values": ["Yes", "No"]},
        )
        v = {"condition": {"type": "ONE_OF_LIST"}}
        rowdata = [
            {"values": [_cell(value="Yes"), _cell(value="No")]},
        ]
        # attach validation to both cells
        rowdata[0]["values"][0]["dataValidation"] = v
        rowdata[0]["values"][1]["dataValidation"] = v
        payload = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff"},
                    "data": [_block(0, 0, rowdata)],
                }
            ]
        }
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        out = describe(services, SHEET_ID, ["Cliff!A1:B1"])
        summary = out["regions"][0]["validationSummary"]
        assert summary["cells"] == 2
        assert summary["rules"] == ["ONE_OF_LIST(Yes,No)"]

    def test_validation_summary_empty_when_none(self):
        payload = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff"},
                    "data": [_block(0, 0, [{"values": [_cell(value="x")]}])],
                }
            ]
        }
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        out = describe(services, SHEET_ID, ["Cliff!A1"])
        assert out["regions"][0]["validationSummary"] == {"cells": 0, "rules": []}


# =========================================================================== multi-range / sheet


class TestDescribeMultiRange:
    def test_multi_range_same_sheet_mapped_by_block_offset(self):
        # Two ranges on Cliff: A1:A1 (block @0,0) and C3:C3 (block @2,2). Map by start offset.
        payload = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff"},
                    "data": [
                        _block(0, 0, [{"values": [_cell(value="top")]}]),
                        _block(2, 2, [{"values": [_cell(value="mid")]}]),
                    ],
                }
            ]
        }
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        out = describe(services, SHEET_ID, ["Cliff!A1", "Cliff!C3"])
        assert [r["range"] for r in out["regions"]] == ["Cliff!A1", "Cliff!C3"]
        assert out["regions"][0]["cells"] == [{"a1": "A1", "value": "top"}]
        assert out["regions"][1]["cells"] == [{"a1": "C3", "value": "mid"}]

    def test_multi_sheet_mapped_across_sheets(self):
        payload = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff"},
                    "data": [_block(0, 0, [{"values": [_cell(value="c")]}])],
                },
                {
                    "properties": {"sheetId": 7, "title": "Plan"},
                    "data": [_block(0, 0, [{"values": [_cell(value="p")]}])],
                },
            ]
        }
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        out = describe(services, SHEET_ID, ["Cliff!A1", "Plan!A1"])
        assert [r["range"] for r in out["regions"]] == ["Cliff!A1", "Plan!A1"]
        assert out["regions"][0]["cells"] == [{"a1": "A1", "value": "c"}]
        assert out["regions"][1]["cells"] == [{"a1": "A1", "value": "p"}]
        # per-sheet structural features attach to the right sheet's region (empty here).
        assert out["regions"][1]["sheet"] == "Plan"


# =========================================================================== guards


class TestDescribeGuards:
    def test_empty_ranges_raises(self):
        services, _ = _make_service(sheet_index=_INDEX)
        with pytest.raises(SheetsError) as ei:
            describe(services, SHEET_ID, [])
        assert ei.value.code == "empty_ranges"

    def test_bad_range_raises_before_api(self):
        services, rec = _make_service(sheet_index=_INDEX)
        with pytest.raises(SheetsError) as ei:
            describe(services, SHEET_ID, [""])
        assert ei.value.code == "bad_range"
        assert rec.data_calls == []

    def test_max_cells_exceeded_raises_result_too_large(self):
        rowdata = [{"values": [_cell(value="a"), _cell(value="b"), _cell(value="c")]}]
        payload = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff"},
                    "data": [_block(0, 0, rowdata)],
                }
            ]
        }
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        with pytest.raises(SheetsError) as ei:
            describe(services, SHEET_ID, ["Cliff!A1:C1"], max_cells=2)
        assert ei.value.code == "result_too_large"

    def test_max_cells_zero_or_negative_raises(self):
        services, _ = _make_service(sheet_index=_INDEX)
        with pytest.raises(SheetsError) as ei:
            describe(services, SHEET_ID, ["Cliff!A1"], max_cells=0)
        assert ei.value.code == "bad_max_cells"

    def test_http_error_classified(self):
        services, _ = _make_service(sheet_index=_INDEX)

        def _boom(**kwargs):
            if kwargs.get("fields") == "sheets.properties(sheetId,title,index)":
                req = MagicMock()
                req.execute.return_value = _INDEX
                return req
            req = MagicMock()
            req.execute.side_effect = _make_http_error(404)
            return req

        services.sheets.spreadsheets.return_value.get = _boom
        with pytest.raises(SheetsError) as ei:
            describe(services, SHEET_ID, ["Cliff!A1"])
        assert ei.value.code == "google_api_error"
        assert ei.value.status == 404

    def test_no_cache_reissues_get_each_call(self):
        payload = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff"},
                    "data": [_block(0, 0, [{"values": [_cell(value="x")]}])],
                }
            ]
        }
        # Two identical payloads queued: a cache would only consume one.
        services, rec = _make_service(
            data_responses=[payload, payload], sheet_index=_INDEX
        )
        describe(services, SHEET_ID, ["Cliff!A1"])
        describe(services, SHEET_ID, ["Cliff!A1"])
        assert len(rec.data_calls) == 2


# =========================================================================== golden master


def test_describe_region_golden():
    """Golden-master (SPEC §3.5): a realistic includeGridData response -> the EXACT region view.

    Drives the committed ``describe_region`` fixture (a multi-range, multi-sheet
    ``spreadsheets.get(includeGridData=True)`` response) through the mocked service and asserts the
    full ``describe`` output byte-for-byte. Exercises every facet at once: per-cell flatten (formula
    + effectiveFormat), merges -> A1, range-scoped CF (the col-Z rule is filtered OUT of the col-A:C
    region), a protected range, and multi-sheet block mapping. Changing any serializer's shape must
    update this fixture in the same change.
    """
    golden = load_golden("describe_region")
    services, _ = _make_service(
        data_responses=[golden["response"]], sheet_index=golden["sheet_index"]
    )
    out = describe(services, golden["spreadsheetId"], golden["ranges"])
    assert out == golden["expected"]


# =========================================================================== data_filters (SPEC §6 P2)


class _ByDataFilterRecorder:
    """Records each ``getByDataFilter(**kwargs)`` and returns a queued response on ``.execute()``."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        resp = self._responses.pop(0) if self._responses else {}
        req = MagicMock(name="request")
        req.execute.return_value = resp
        return req


def _service_with_data_filter(*, by_filter_responses, sheet_index=None):
    """A service whose ``.get`` serves the sheet-index lookup and ``.getByDataFilter`` the data."""
    sheets = MagicMock(name="sheets_v4")
    # ``.get`` still serves the addressing sheet-index lookups (gridrange_to_a1 needs them).
    get_rec = _GetRecorder([], sheet_index or _INDEX)
    sheets.spreadsheets.return_value.get = get_rec
    by_rec = _ByDataFilterRecorder(by_filter_responses)
    sheets.spreadsheets.return_value.getByDataFilter = by_rec
    services = SheetsServices(sheets=sheets, drive=None)
    return services, by_rec


class TestDescribeDataFilters:
    def test_developer_metadata_lookup_reads_via_get_by_data_filter(self):
        # SPEC §6 P2: a developerMetadataLookup selector reads via getByDataFilter; the region's
        # GridRange (and A1 label + CF intersect) is derived from the RETURNED block.
        payload = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff"},
                    # block anchored at A2 (startRow=1), one row, two cols.
                    "data": [_block(1, 0, [{"values": [_cell(value="10"), _cell(value="hi")]}])],
                    "conditionalFormats": [_CF_A, _CF_Z],
                    "tables": [],
                    "bandedRanges": [],
                    "protectedRanges": [],
                }
            ]
        }
        services, by_rec = _service_with_data_filter(by_filter_responses=[payload])
        out = describe(
            services,
            SHEET_ID,
            data_filters=[{"developerMetadataLookup": {"metadataKey": "block:totals"}}],
        )
        # The selector is passed straight through into the request body's dataFilters.
        assert by_rec.calls[0]["body"]["dataFilters"] == [
            {"developerMetadataLookup": {"metadataKey": "block:totals"}}
        ]
        assert by_rec.calls[0]["body"]["includeGridData"] is True
        assert "conditionalFormats" in by_rec.calls[0]["fields"]

        assert len(out["regions"]) == 1
        region = out["regions"][0]
        # Derived from the returned block: A2:B2 (startRow=1, 1 row, 2 cols).
        assert region["range"] == "Cliff!A2:B2"
        assert region["cells"] == [
            {"a1": "A2", "value": "10"},
            {"a1": "B2", "value": "hi"},
        ]
        # CF intersect uses the derived range: col A (_CF_A, rows 2..100) intersects A2:B2; col Z does not.
        assert len(region["conditionalFormats"]) == 1
        assert region["conditionalFormats"][0]["ranges"] == ["Cliff!A2:A100"]

    def test_a1_selector_translated_to_gridrange_filter(self, monkeypatch):
        from gsheets.core import dataselector

        monkeypatch.setattr(
            dataselector,
            "a1_to_gridrange",
            lambda svc, sid, a1: {
                "sheetId": 0,
                "startRowIndex": 0,
                "endRowIndex": 1,
                "startColumnIndex": 0,
                "endColumnIndex": 1,
            },
        )
        payload = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff"},
                    "data": [_block(0, 0, [{"values": [_cell(value="x")]}])],
                }
            ]
        }
        services, by_rec = _service_with_data_filter(by_filter_responses=[payload])
        describe(services, SHEET_ID, data_filters=[{"a1": "Cliff!A1"}])
        assert by_rec.calls[0]["body"]["dataFilters"] == [
            {
                "gridRange": {
                    "sheetId": 0,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": 1,
                }
            }
        ]

    def test_ranges_and_data_filters_both_raises_conflicting_args(self):
        services, _ = _service_with_data_filter(by_filter_responses=[{}])
        with pytest.raises(SheetsError) as exc:
            describe(
                services,
                SHEET_ID,
                ["Cliff!A1"],
                data_filters=[{"a1": "Cliff!A1"}],
            )
        assert exc.value.code == "conflicting_args"

    def test_neither_ranges_nor_data_filters_raises_empty_ranges(self):
        services, _ = _service_with_data_filter(by_filter_responses=[{}])
        with pytest.raises(SheetsError) as exc:
            describe(services, SHEET_ID)
        assert exc.value.code == "empty_ranges"

    def test_max_cells_guard_applies_to_data_filter_path(self):
        payload = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff"},
                    "data": [_block(0, 0, [{"values": [_cell(value="a"), _cell(value="b"), _cell(value="c")]}])],
                }
            ]
        }
        services, _ = _service_with_data_filter(by_filter_responses=[payload])
        with pytest.raises(SheetsError) as exc:
            describe(
                services,
                SHEET_ID,
                data_filters=[{"gridRange": {"sheetId": 0}}],
                max_cells=2,
            )
        assert exc.value.code == "result_too_large"


class TestDescribeSheetIndexCache:
    """describe() opens its own re-entrant sheet_index_cache() scope, so resolving N ranges +
    per-region merges + CF rules issues exactly ONE sheet-index get even when called DIRECTLY (no
    adapter scope). Guards the N+1 regression where describe lacked the inner scope its siblings
    (read_conditional_formats, structure) have."""

    def test_multi_region_issues_one_sheet_index_get(self):
        merges = [
            {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1,
             "startColumnIndex": 0, "endColumnIndex": 2},
            {"sheetId": 0, "startRowIndex": 1, "endRowIndex": 2,
             "startColumnIndex": 0, "endColumnIndex": 2},
        ]
        payload = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff"},
                    "merges": merges,
                    "conditionalFormats": [_CF_A],
                    "data": [
                        _block(0, 0, [{"values": [_cell(value="x"), _cell(value="y")]}]),
                        _block(1, 0, [{"values": [_cell(value="z")]}]),
                    ],
                }
            ]
        }
        services, rec = _make_service(data_responses=[payload], sheet_index=_INDEX)
        describe(services, SHEET_ID, ["Cliff!A1:B2", "Cliff!A2:A100"])
        index_gets = [
            c for c in rec.calls if c.get("fields") == _GetRecorder._SHEET_INDEX_FIELDS
        ]
        assert len(index_gets) == 1
        # the data read itself is the single multi-range get (not multiplied either).
        assert len(rec.data_calls) == 1
