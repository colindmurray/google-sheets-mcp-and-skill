"""Unit tests for ``gsheets.core.formula_patterns`` (SPEC §4 — formula_patterns).

``formula_patterns`` fetches ONLY formulas (``valueRenderOption=FORMULA``), column-major
(``majorDimension=COLUMNS``), then per column dedupes to distinct formula TEMPLATES with relative
row refs normalized to ``{r}`` (and ``{r±k}`` for off-by-k refs), the row span(s) each covers, and
(optionally) one sample computed value. A column whose formulas do not reduce cleanly (the
normalized template does not round-trip back to every original formula) is emitted VERBATIM with
``reduced=false`` — ``read_values --render formula`` stays the lossless ground truth.

These tests run against a MOCKED service: a ``_BatchGetRecorder`` serves the FORMULA pass (and an
optional FORMATTED pass for the sample) plus the sheet-index get the addressing layer issues. They
pin:

* the FORMULA-only, COLUMNS-major outbound request shape (no computed bloat);
* per-column dedup into templates with ``{r}`` normalization and row spans;
* a non-reducible column emitted verbatim with ``reduced=false``;
* the optional sample computed value (and ``sample=False`` skipping the FORMATTED pass);
* guards (empty ranges, bad range before any API call, classified HttpError).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gsheets.core.errors import SheetsError
from gsheets.core.formula_patterns import formula_patterns
from gsheets.core.service import SheetsServices

GOLDEN_DIR = Path(__file__).parent / "golden"


def load_golden(name: str) -> dict:
    filename = name if name.endswith(".json") else f"{name}.json"
    return json.loads((GOLDEN_DIR / filename).read_text())


# --------------------------------------------------------------------------- helpers


class _BatchGetRecorder:
    """Records ``batchGet(**kwargs)`` calls; dispatches FORMULA vs FORMATTED by render option.

    The FORMULA pass pops ``formula_responses``; the FORMATTED (sample) pass pops
    ``formatted_responses``. Both are column-major ``valueRanges`` payloads. A separate
    ``get`` recorder (the sheet-index lookup the addressing layer issues) is wired alongside.
    """

    def __init__(self, formula_responses, formatted_responses):
        self._formula = list(formula_responses)
        self._formatted = list(formatted_responses)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("valueRenderOption") == "FORMATTED_VALUE":
            resp = self._formatted.pop(0) if self._formatted else {}
        else:
            resp = self._formula.pop(0) if self._formula else {}
        req = MagicMock(name="request")
        req.execute.return_value = resp
        return req


_SHEET_INDEX_FIELDS = "sheets.properties(sheetId,title,index)"

_INDEX = {
    "sheets": [
        {"properties": {"sheetId": 0, "title": "Cliff", "index": 0}},
        {"properties": {"sheetId": 7, "title": "Plan", "index": 1}},
    ]
}


def _make_service(*, formula_responses=None, formatted_responses=None, account_email=None):
    sheets = MagicMock(name="sheets_v4")
    batch = _BatchGetRecorder(formula_responses or [], formatted_responses or [])
    sheets.spreadsheets.return_value.values.return_value.batchGet = batch

    def _get(**kwargs):
        req = MagicMock()
        if kwargs.get("fields") == _SHEET_INDEX_FIELDS:
            req.execute.return_value = _INDEX
        else:
            req.execute.return_value = {}
        return req

    sheets.spreadsheets.return_value.get = _get
    services = SheetsServices(sheets=sheets, drive=None, account_email=account_email)
    return services, batch


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


def _vr(a1, columns):
    """A column-major batchGet RESPONSE wrapping one valueRange (``columns`` = list of columns)."""
    return {
        "valueRanges": [
            {"range": a1, "majorDimension": "COLUMNS", "values": columns}
        ]
    }


# =========================================================================== request shape


class TestRequestShape:
    def test_formula_only_columns_major(self):
        # K3:K5 = =SUM(J3:R3), =SUM(J4:R4), =SUM(J5:R5)
        formulas = _vr(
            "Cliff!K3:K5",
            [["=SUM(J3:R3)", "=SUM(J4:R4)", "=SUM(J5:R5)"]],
        )
        services, rec = _make_service(formula_responses=[formulas])
        formula_patterns(services, SHEET_ID, ["Cliff!K3:K5"], sample=False)
        # Exactly one FORMULA pass (sample=False ⇒ no FORMATTED pass).
        assert len(rec.calls) == 1
        sent = rec.calls[0]
        assert sent["spreadsheetId"] == SHEET_ID
        assert sent["ranges"] == ["Cliff!K3:K5"]
        assert sent["valueRenderOption"] == "FORMULA"
        assert sent["majorDimension"] == "COLUMNS"

    def test_sample_issues_a_formatted_pass(self):
        formulas = _vr("Cliff!K3:K5", [["=SUM(J3:R3)", "=SUM(J4:R4)", "=SUM(J5:R5)"]])
        formatted = _vr("Cliff!K3:K5", [["185", "190", "200"]])
        services, rec = _make_service(
            formula_responses=[formulas], formatted_responses=[formatted]
        )
        formula_patterns(services, SHEET_ID, ["Cliff!K3:K5"], sample=True)
        assert len(rec.calls) == 2
        assert rec.calls[0]["valueRenderOption"] == "FORMULA"
        assert rec.calls[1]["valueRenderOption"] == "FORMATTED_VALUE"
        assert rec.calls[1]["majorDimension"] == "COLUMNS"


# =========================================================================== dedup / normalize


class TestDedup:
    def test_single_template_relative_row(self):
        formulas = _vr(
            "Cliff!K3:K5",
            [["=SUM(J3:R3)", "=SUM(J4:R4)", "=SUM(J5:R5)"]],
        )
        formatted = _vr("Cliff!K3:K5", [["185", "190", "200"]])
        services, _ = _make_service(
            formula_responses=[formulas], formatted_responses=[formatted]
        )
        out = formula_patterns(services, SHEET_ID, ["Cliff!K3:K5"])
        assert out["ok"] is True
        assert out["spreadsheetId"] == SHEET_ID
        assert len(out["columns"]) == 1
        col = out["columns"][0]
        assert col["col"] == "Cliff!K"
        assert col["reduced"] is True
        assert col["templates"] == [
            {
                "formula": "=SUM(J{r}:R{r})",
                "rows": "3:5",
                "cells": 3,
                "sample": {"a1": "K3", "value": "185"},
            }
        ]

    def test_two_templates_split_by_span(self):
        # K3:K4 = SUM template; K5 = a different self-referential template.
        formulas = _vr(
            "Cliff!K3:K5",
            [["=SUM(J3:R3)", "=SUM(J4:R4)", "=IFERROR(K4+1,0)"]],
        )
        services, _ = _make_service(formula_responses=[formulas], formatted_responses=[{}])
        out = formula_patterns(services, SHEET_ID, ["Cliff!K3:K5"], sample=False)
        col = out["columns"][0]
        assert col["reduced"] is True
        assert [t["formula"] for t in col["templates"]] == [
            "=SUM(J{r}:R{r})",
            "=IFERROR(K{r-1}+1,0)",
        ]
        assert [t["rows"] for t in col["templates"]] == ["3:4", "5:5"]
        assert [t["cells"] for t in col["templates"]] == [2, 1]

    def test_blank_cells_in_column_are_skipped(self):
        # A column with a leading blank cell (empty string) then formulas.
        formulas = _vr(
            "Cliff!K3:K5",
            [["", "=SUM(J4:R4)", "=SUM(J5:R5)"]],
        )
        services, _ = _make_service(formula_responses=[formulas], formatted_responses=[{}])
        out = formula_patterns(services, SHEET_ID, ["Cliff!K3:K5"], sample=False)
        col = out["columns"][0]
        assert col["templates"] == [
            {"formula": "=SUM(J{r}:R{r})", "rows": "4:5", "cells": 2}
        ]

    def test_literal_only_column_has_no_templates(self):
        # A column with only literal values (no formulas) yields an empty templates list.
        formulas = _vr("Cliff!A3:A5", [["1", "2", "3"]])
        services, _ = _make_service(formula_responses=[formulas], formatted_responses=[{}])
        out = formula_patterns(services, SHEET_ID, ["Cliff!A3:A5"], sample=False)
        col = out["columns"][0]
        assert col["col"] == "Cliff!A"
        assert col["templates"] == []
        assert col["reduced"] is True

    def test_absolute_row_ref_kept_verbatim(self):
        # An absolute row ($1) does NOT shift down the column, so it stays literal in the
        # template; the relative col-row ref normalizes to {r}.
        formulas = _vr(
            "Cliff!K3:K4",
            [["=J3/J$1", "=J4/J$1"]],
        )
        services, _ = _make_service(formula_responses=[formulas], formatted_responses=[{}])
        out = formula_patterns(services, SHEET_ID, ["Cliff!K3:K4"], sample=False)
        col = out["columns"][0]
        assert col["reduced"] is True
        assert col["templates"][0]["formula"] == "=J{r}/J$1"


# =========================================================================== non-reducible


class TestNonReducible:
    def test_non_reducible_column_emitted_verbatim(self):
        # Two formulas that do NOT share a single shifting template (different absolute targets
        # that don't move uniformly with the row) → emitted verbatim, reduced=false.
        formulas = _vr(
            "Cliff!K3:K4",
            [["=A1+B2", "=Z9+Q3"]],
        )
        services, _ = _make_service(formula_responses=[formulas], formatted_responses=[{}])
        out = formula_patterns(services, SHEET_ID, ["Cliff!K3:K4"], sample=False)
        col = out["columns"][0]
        assert col["reduced"] is False
        # verbatim: one template entry per cell, the raw formula and its own 1-row span.
        assert [t["formula"] for t in col["templates"]] == ["=A1+B2", "=Z9+Q3"]
        assert [t["rows"] for t in col["templates"]] == ["3:3", "4:4"]


# =========================================================================== multi-column


class TestMultiColumn:
    def test_multiple_columns_in_one_range(self):
        formulas = _vr(
            "Cliff!K3:L4",
            [
                ["=SUM(J3:R3)", "=SUM(J4:R4)"],  # column K
                ["=K3*2", "=K4*2"],  # column L
            ],
        )
        services, _ = _make_service(formula_responses=[formulas], formatted_responses=[{}])
        out = formula_patterns(services, SHEET_ID, ["Cliff!K3:L4"], sample=False)
        assert [c["col"] for c in out["columns"]] == ["Cliff!K", "Cliff!L"]
        assert out["columns"][0]["templates"][0]["formula"] == "=SUM(J{r}:R{r})"
        assert out["columns"][1]["templates"][0]["formula"] == "=K{r}*2"


# =========================================================================== guards


class TestGuards:
    def test_empty_ranges_raises(self):
        services, _ = _make_service()
        with pytest.raises(SheetsError) as ei:
            formula_patterns(services, SHEET_ID, [])
        assert ei.value.code == "empty_ranges"

    def test_bad_range_raises_before_api(self):
        services, rec = _make_service()
        with pytest.raises(SheetsError) as ei:
            formula_patterns(services, SHEET_ID, [""])
        assert ei.value.code == "bad_range"
        assert rec.calls == []

    def test_http_error_classified(self):
        sheets = MagicMock(name="sheets_v4")

        def _batch(**kwargs):
            req = MagicMock()
            req.execute.side_effect = _make_http_error(404)
            return req

        def _get(**kwargs):
            req = MagicMock()
            req.execute.return_value = _INDEX
            return req

        sheets.spreadsheets.return_value.values.return_value.batchGet = _batch
        sheets.spreadsheets.return_value.get = _get
        services = SheetsServices(sheets=sheets, drive=None)
        with pytest.raises(SheetsError) as ei:
            formula_patterns(services, SHEET_ID, ["Cliff!K3:K5"])
        assert ei.value.code == "google_api_error"
        assert ei.value.status == 404


# =========================================================================== golden master


def test_formula_patterns_golden():
    """Golden-master (SPEC §4.5): the dedup + normalization (incl. a non-reducible column).

    Drives the committed ``formula_patterns`` fixture (a multi-column FORMULA + FORMATTED
    column-major pair) through the mocked service and asserts the full output byte-for-byte:
    a clean two-template column, a literal-only column, and a non-reducible column. Changing the
    dedup/normalization output must update this fixture in the same change.
    """
    golden = load_golden("formula_patterns")
    services, _ = _make_service(
        formula_responses=[golden["formula_response"]],
        formatted_responses=[golden["formatted_response"]],
    )
    out = formula_patterns(services, golden["spreadsheetId"], golden["ranges"])
    assert out == golden["expected"]
