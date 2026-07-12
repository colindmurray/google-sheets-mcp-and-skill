"""Unit tests for ``gsheets.core.describe`` — the compact per-column PROFILER (T4.1, audit #15).

BREAKING (v0.6): ``describe`` no longer returns per-cell ``cells``/``regions``. It issues ONE
tight-mask ``spreadsheets.get`` (``effectiveValue`` + ``effectiveFormat.numberFormat.type`` +
``userEnteredValue.formulaValue`` + ``formattedValue``, plus ``merges``), AGGREGATES the grid to
per-column statistics, and DISCARDS every per-cell value. The full per-cell read is ``inspect``.

These tests run against a MOCKED service: one ``_GetRecorder`` serves both the profiler data get
and the sheet-index get the addressing layer issues. They pin:

* the ONE data get's outbound mask (tight profiler mask; never the heavy per-cell/CF/table blob);
* per-column type inference (number/date/text/bool/formula-heavy/empty);
* density + formulaRatio + distinctSample + header extraction;
* ``max_cells`` raises ``result_too_large`` BEFORE the data get for a bounded range;
* NO cache (each call re-issues the get);
* a golden-master profile.
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
    data-response queue.
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

_INDEX = {
    "sheets": [
        {"properties": {"sheetId": 0, "title": "Cliff", "index": 0}},
        {"properties": {"sheetId": 7, "title": "Plan", "index": 1}},
    ]
}


def _num(n, *, number_format=None):
    """A number cell (optionally with an effectiveFormat.numberFormat.type)."""
    cell: dict = {"effectiveValue": {"numberValue": n}, "formattedValue": str(n)}
    if number_format is not None:
        cell["effectiveFormat"] = {"numberFormat": {"type": number_format}}
    return cell


def _text(s):
    return {"effectiveValue": {"stringValue": s}, "formattedValue": s}


def _bool(b):
    return {"effectiveValue": {"boolValue": b}, "formattedValue": "TRUE" if b else "FALSE"}


def _formula(formula, computed, *, formatted=None):
    """A formula cell: userEnteredValue.formulaValue + an effective (computed) value."""
    ev = {"numberValue": computed} if isinstance(computed, (int, float)) else {
        "stringValue": str(computed)
    }
    return {
        "userEnteredValue": {"formulaValue": formula},
        "effectiveValue": ev,
        "formattedValue": formatted if formatted is not None else str(computed),
    }


def _date(serial, display):
    return {
        "effectiveValue": {"numberValue": serial},
        "effectiveFormat": {"numberFormat": {"type": "DATE"}},
        "formattedValue": display,
    }


def _block(start_row, start_col, rowdata):
    return {"startRow": start_row, "startColumn": start_col, "rowData": rowdata}


def _payload(rowdata, *, start_row=0, start_col=0, sheet_id=0, title="Cliff", merges=None):
    sheet: dict = {
        "properties": {"sheetId": sheet_id, "title": title},
        "data": [_block(start_row, start_col, rowdata)],
    }
    if merges is not None:
        sheet["merges"] = merges
    return {"sheets": [sheet]}


def _row(*cells):
    return {"values": list(cells)}


# =========================================================================== mask


class TestDescribeMask:
    def _run(self):
        payload = _payload([_row(_text("x"))])
        services, rec = _make_service(data_responses=[payload], sheet_index=_INDEX)
        describe(services, SHEET_ID, "Cliff!A1:B2")
        return rec.data_calls[0]

    def test_one_get_scoped_to_the_range(self):
        sent = self._run()
        assert sent["spreadsheetId"] == SHEET_ID
        assert sent["ranges"] == ["Cliff!A1:B2"]
        # The profiler does NOT use includeGridData (a tight field mask returns grid data anyway).
        assert "includeGridData" not in sent

    def test_mask_is_the_tight_profiler_mask(self):
        fields = self._run()["fields"]
        # enough to type + detect a formula + label a column
        assert "effectiveValue" in fields
        assert "userEnteredValue" in fields  # formulaValue presence
        assert "numberFormat" in fields  # date-vs-number discrimination
        assert "formattedValue" in fields
        assert "merges" in fields
        # NEVER the heavy per-cell/structure facets the old describe pulled
        assert "userEnteredFormat" not in fields
        assert "dataValidation" not in fields
        assert "conditionalFormats" not in fields
        assert "tables" not in fields
        assert "protectedRanges" not in fields


# =========================================================================== envelope


class TestDescribeEnvelope:
    def test_top_level_shape(self):
        payload = _payload(
            [
                _row(_text("Name"), _text("Score")),
                _row(_text("Ann"), _num(10)),
                _row(_text("Bob"), _num(20)),
            ]
        )
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        out = describe(services, SHEET_ID, "Cliff!A1:B3")
        assert out["ok"] is True
        assert out["spreadsheetId"] == SHEET_ID
        assert out["sheet"] == "Cliff"
        assert out["range"] == "Cliff!A1:B3"
        assert out["rows"] == 3
        assert out["cols"] == 2
        assert "regions" not in out
        assert "cells" not in out
        assert isinstance(out["columns"], list) and len(out["columns"]) == 2

    def test_absolute_column_letters_from_start_offset(self):
        # A block anchored at C3 -> the first profiled column is "C", not "A".
        payload = _payload([_row(_text("h"), _num(1))], start_row=2, start_col=2)
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        out = describe(services, SHEET_ID, "Cliff!C3:D3", header=False)
        assert [c["col"] for c in out["columns"]] == ["C", "D"]


# =========================================================================== type inference


class TestColumnTypeInference:
    def _profile(self, rowdata, **kw):
        payload = _payload(rowdata)
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        return describe(services, SHEET_ID, "Cliff!A1:D100", **kw)

    def test_number_date_text_bool_columns(self):
        out = self._profile(
            [
                _row(_text("num"), _text("when"), _text("who"), _text("flag")),
                _row(_num(1), _date(45000, "2023-03-15"), _text("ann"), _bool(True)),
                _row(_num(2), _date(45001, "2023-03-16"), _text("bob"), _bool(False)),
            ]
        )
        types = {c["col"]: c["type"] for c in out["columns"]}
        assert types["A"] == "number"
        assert types["B"] == "date"
        assert types["C"] == "text"
        assert types["D"] == "bool"

    def test_formula_heavy_column(self):
        out = self._profile(
            [
                _row(_text("total")),
                _row(_formula("=SUM(B1:B9)", 10)),
                _row(_formula("=SUM(C1:C9)", 20)),
                _row(_num(5)),  # a lone literal — still a majority of formulas -> formula-heavy
            ]
        )
        col = out["columns"][0]
        assert col["type"] == "formula-heavy"
        # formulaRatio = 2 formulas / 3 non-empty
        assert col["formulaRatio"] == round(2 / 3, 3)

    def test_empty_column(self):
        # Column B never carries a value across the data rows -> "empty".
        out = self._profile(
            [
                _row(_text("A-header"), _text("B-header")),
                _row(_num(1), {}),
                _row(_num(2), {}),
            ]
        )
        types = {c["col"]: c["type"] for c in out["columns"]}
        assert types["A"] == "number"
        assert types["B"] == "empty"
        b = next(c for c in out["columns"] if c["col"] == "B")
        assert b["density"] == 0.0
        assert b["formulaRatio"] == 0.0
        assert "distinctSample" not in b


# =========================================================================== density / ratio / sample


class TestDensityRatioSample:
    def test_density_and_distinct_sample(self):
        # A col: 2 of 3 data rows filled -> density 0.667; distinct sample of the 2 values.
        payload = _payload(
            [
                _row(_text("cat")),
                _row(_text("red")),
                _row({}),
                _row(_text("red")),
            ]
        )
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        out = describe(services, SHEET_ID, "Cliff!A1:A4")
        col = out["columns"][0]
        assert col["density"] == round(2 / 3, 3)
        # "red" appears twice but the sample is DISTINCT.
        assert col["distinctSample"] == ["red"]

    def test_distinct_sample_capped_at_five(self):
        rows = [_row(_text("hdr"))] + [_row(_text(f"v{i}")) for i in range(8)]
        payload = _payload(rows)
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        out = describe(services, SHEET_ID, "Cliff!A1:A9")
        assert out["columns"][0]["distinctSample"] == ["v0", "v1", "v2", "v3", "v4"]

    def test_density_over_all_rows_when_header_false(self):
        payload = _payload([_row(_num(1)), _row(_num(2)), _row({})])
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        out = describe(services, SHEET_ID, "Cliff!A1:A3", header=False)
        # header=False -> all 3 rows are data; 2 filled -> 0.667.
        assert out["columns"][0]["density"] == round(2 / 3, 3)


# =========================================================================== header extraction


class TestHeaderExtraction:
    def test_header_taken_from_first_row_and_excluded_from_stats(self):
        payload = _payload(
            [
                _row(_text("Score")),
                _row(_num(10)),
                _row(_num(20)),
            ]
        )
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        out = describe(services, SHEET_ID, "Cliff!A1:A3")
        col = out["columns"][0]
        assert col["header"] == "Score"
        assert col["type"] == "number"  # header excluded, so no "text" contamination
        assert col["density"] == 1.0  # both data rows filled

    def test_no_header_key_when_header_false(self):
        payload = _payload([_row(_num(10)), _row(_num(20))])
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        out = describe(services, SHEET_ID, "Cliff!A1:A2", header=False)
        assert "header" not in out["columns"][0]

    def test_empty_header_cell_omits_header_key(self):
        payload = _payload([_row({}), _row(_num(10))])
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        out = describe(services, SHEET_ID, "Cliff!A1:A2")
        assert "header" not in out["columns"][0]


# =========================================================================== merges


class TestDescribeMerges:
    def test_merges_surfaced_when_present(self):
        merges = [
            {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1,
             "startColumnIndex": 0, "endColumnIndex": 2}
        ]
        payload = _payload([_row(_text("x"), _text("y"))], merges=merges)
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        out = describe(services, SHEET_ID, "Cliff!A1:B1", header=False)
        assert out["merges"] == ["Cliff!A1:B1"]

    def test_merges_key_absent_when_none(self):
        payload = _payload([_row(_text("x"))])
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        out = describe(services, SHEET_ID, "Cliff!A1")
        assert "merges" not in out


# =========================================================================== max_cells


class TestMaxCells:
    def test_bounded_range_raises_before_any_data_fetch(self):
        # A1:C100 = 300 cells > max_cells=2. The profiler must refuse BEFORE the data get: the ONLY
        # get issued is the sheet-index lookup (for a1_to_gridrange); no profiler data get fires.
        services, rec = _make_service(data_responses=[], sheet_index=_INDEX)
        with pytest.raises(SheetsError) as ei:
            describe(services, SHEET_ID, "Cliff!A1:C100", max_cells=2)
        assert ei.value.code == "result_too_large"
        # No data get was issued (the grid was never fetched).
        assert rec.data_calls == []

    def test_bounded_range_under_cap_fetches_and_profiles(self):
        payload = _payload([_row(_num(1)), _row(_num(2))])
        services, rec = _make_service(data_responses=[payload], sheet_index=_INDEX)
        out = describe(services, SHEET_ID, "Cliff!A1:A3", max_cells=10)
        assert out["ok"] is True
        assert len(rec.data_calls) == 1

    def test_unbounded_range_guarded_post_fetch(self):
        # A whole-column range (A:A) can't be sized before the fetch; the guard uses the actual
        # returned extent (3 rows x 1 col = 3 > 2).
        payload = _payload([_row(_num(1)), _row(_num(2)), _row(_num(3))])
        services, _ = _make_service(data_responses=[payload], sheet_index=_INDEX)
        with pytest.raises(SheetsError) as ei:
            describe(services, SHEET_ID, "Cliff!A:A", max_cells=2)
        assert ei.value.code == "result_too_large"

    def test_max_cells_zero_or_negative_raises(self):
        services, _ = _make_service(sheet_index=_INDEX)
        with pytest.raises(SheetsError) as ei:
            describe(services, SHEET_ID, "Cliff!A1", max_cells=0)
        assert ei.value.code == "bad_max_cells"


# =========================================================================== guards


class TestGuards:
    def test_bad_range_raises_before_api(self):
        services, rec = _make_service(sheet_index=_INDEX)
        with pytest.raises(SheetsError) as ei:
            describe(services, SHEET_ID, "")
        assert ei.value.code == "bad_range"
        assert rec.data_calls == []

    def test_no_data_raises(self):
        services, _ = _make_service(data_responses=[{"sheets": []}], sheet_index=_INDEX)
        with pytest.raises(SheetsError) as ei:
            describe(services, SHEET_ID, "Cliff!A1")
        assert ei.value.code == "no_data"

    def test_http_error_classified(self):
        def _boom(**kwargs):
            if kwargs.get("fields") == "sheets.properties(sheetId,title,index)":
                req = MagicMock()
                req.execute.return_value = _INDEX
                return req
            req = MagicMock()
            req.execute.side_effect = _make_http_error(404)
            return req

        sheets = MagicMock(name="sheets_v4")
        sheets.spreadsheets.return_value.get = _boom
        services = SheetsServices(sheets=sheets, drive=None)
        with pytest.raises(SheetsError) as ei:
            describe(services, SHEET_ID, "Cliff!A1:B2")
        assert ei.value.code == "google_api_error"
        assert ei.value.status == 404

    def test_no_cache_reissues_get_each_call(self):
        payload = _payload([_row(_text("x"))])
        services, rec = _make_service(
            data_responses=[payload, payload], sheet_index=_INDEX
        )
        describe(services, SHEET_ID, "Cliff!A1")
        describe(services, SHEET_ID, "Cliff!A1")
        assert len(rec.data_calls) == 2

    def test_multi_read_issues_one_sheet_index_get(self):
        # describe opens its own re-entrant sheet_index_cache: the range resolution + each merge's
        # gridrange_to_a1 share ONE sheet-index get even for a direct core caller.
        merges = [
            {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1,
             "startColumnIndex": 0, "endColumnIndex": 1},
            {"sheetId": 0, "startRowIndex": 1, "endRowIndex": 2,
             "startColumnIndex": 0, "endColumnIndex": 1},
        ]
        payload = _payload([_row(_num(1)), _row(_num(2))], merges=merges)
        services, rec = _make_service(data_responses=[payload], sheet_index=_INDEX)
        describe(services, SHEET_ID, "Cliff!A1:A2")
        index_gets = [
            c for c in rec.calls if c.get("fields") == _GetRecorder._SHEET_INDEX_FIELDS
        ]
        assert len(index_gets) == 1
        assert len(rec.data_calls) == 1


# =========================================================================== golden master


def test_describe_profile_golden():
    """Golden-master: a realistic profiler response -> the EXACT compact profile.

    Drives the committed ``describe_profile`` fixture (a tight-mask ``spreadsheets.get`` response
    with a header row and mixed number/date/text/formula columns) through the mocked service and
    asserts the full ``describe`` output byte-for-byte. Changing the profiler's shape must update
    this fixture in the same change.
    """
    golden = load_golden("describe_profile")
    services, _ = _make_service(
        data_responses=[golden["response"]], sheet_index=golden["sheet_index"]
    )
    out = describe(
        services,
        golden["spreadsheetId"],
        golden["range"],
        **golden.get("kwargs", {}),
    )
    assert out == golden["expected"]
