"""Unit tests for ``gsheets.core.reads`` (DESIGN §3.3, §10).

All tests run against a MOCKED Sheets service — no network. A small recorder captures the
kwargs sent to ``spreadsheets().get(...)`` so we can golden-master the OUTBOUND request shape
(the narrow ``overview`` mask, the tight ``inspect`` mask trimmed by the include_* flags, the
``conditionalFormats`` mask) as well as the serialized RETURN dicts.

For the serializers (compact rectangular runs, conditional-format rule lines, validation
round-trip) we use GOLDEN-MASTER style: representative Sheets-API JSON in, assert exact
serialized output out.

Sibling collaborators this unit calls but does NOT own:
    - ``validation_to_rule`` (from ``core.rules``, a sibling unit) is patched in the ``reads``
      namespace where ``inspect``'s validation path is exercised, so these tests stay isolated
      from that unit's on-disk state (it ships as a stub until its own build unit lands).
    - ``a1_to_gridrange`` / ``gridrange_to_a1`` (from ``core.addressing``) are already
      implemented on disk and are driven through a mocked sheet-index get; no patching needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gsheets.core import reads as reads_mod
from gsheets.core.errors import SheetsError
from gsheets.core.reads import (
    inspect,
    overview,
    read_conditional_formats,
)
from gsheets.core.service import SheetsServices


# --------------------------------------------------------------------------- helpers


class _GetRecorder:
    """Records each ``get(**kwargs)`` call and returns a queued response on ``.execute()``.

    A single recorder serves BOTH the data ``get`` (overview/inspect/CF) and the sheet-index
    ``get`` (the per-call cached ``sheets.properties(sheetId,title,index)`` lookup that
    ``gridrange_to_a1`` issues). It dispatches by the requested ``fields`` mask: any get whose
    fields == the sheet-index mask returns the queued sheet-index payload; everything else
    pops the data-response queue.
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
        request_obj = MagicMock(name="request")
        request_obj.execute.return_value = resp
        return request_obj

    @property
    def data_calls(self) -> list[dict]:
        """The get() calls that were NOT the sheet-index lookup (the real data reads)."""
        return [c for c in self.calls if c.get("fields") != self._SHEET_INDEX_FIELDS]


def _make_service(
    *,
    data_responses=None,
    sheet_index=None,
    account_email: str | None = None,
):
    """Build a SheetsServices whose ``spreadsheets().get`` routes to a ``_GetRecorder``."""
    sheets = MagicMock(name="sheets_v4")
    rec = _GetRecorder(data_responses or [], sheet_index or {"sheets": []})
    sheets.spreadsheets.return_value.get = rec
    services = SheetsServices(sheets=sheets, drive=None, account_email=account_email)
    return services, rec


# A reusable sheet-index payload for the addressing helpers (one tab "Cliff", id 0).
_CLIFF_INDEX = {
    "sheets": [{"properties": {"sheetId": 0, "title": "Cliff", "index": 0}}]
}


def _make_http_error(status: int = 403):
    """A minimal stand-in for ``googleapiclient.errors.HttpError``."""
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = status
    resp.reason = "Forbidden"
    content = (
        b'{"error": {"code": %d, "status": "PERMISSION_DENIED", "message": "nope"}}'
        % status
    )
    return HttpError(resp=resp, content=content)


SHEET_ID = "<TEST_SHEET_ID>"


# =========================================================================== overview


class TestOverview:
    def test_uses_narrow_count_yielding_mask(self):
        services, rec = _make_service(data_responses=[{"properties": {"title": "T"}}])
        overview(services, SHEET_ID)
        sent = rec.data_calls[0]
        fields = sent["fields"]
        # The mask MUST request the cheap length-yielding subfields, NOT whole rule bodies.
        assert "sheets.protectedRanges.protectedRangeId" in fields
        assert "sheets.conditionalFormats.ranges" in fields
        # And MUST NOT widen to the full rule / protected bodies (DESIGN §3.3 token note).
        assert "conditionalFormats.booleanRule" not in fields
        assert "conditionalFormats.gradientRule" not in fields
        assert "protectedRanges.editors" not in fields
        assert sent["spreadsheetId"] == SHEET_ID

    def test_counts_are_len_of_arrays(self):
        payload = {
            "properties": {"title": "Workout Tracker"},
            "sheets": [
                {
                    "properties": {
                        "sheetId": 0,
                        "title": "Cliff",
                        "index": 0,
                        "sheetType": "GRID",
                        "gridProperties": {
                            "rowCount": 1000,
                            "columnCount": 86,
                            "frozenRowCount": 1,
                            "frozenColumnCount": 2,
                        },
                        "tabColorStyle": {
                            "rgbColor": {
                                "red": 0.25882354,
                                "green": 0.52156866,
                                "blue": 0.95686275,
                            }
                        },
                    },
                    "protectedRanges": [{"protectedRangeId": 1}],
                    "conditionalFormats": [
                        {"ranges": [{"sheetId": 0}]} for _ in range(12)
                    ],
                }
            ],
        }
        services, _ = _make_service(data_responses=[payload])
        out = overview(services, SHEET_ID)

        assert out["ok"] is True
        assert out["spreadsheetId"] == SHEET_ID
        assert out["title"] == "Workout Tracker"
        assert len(out["sheets"]) == 1
        s = out["sheets"][0]
        assert s == {
            "sheetId": 0,
            "title": "Cliff",
            "index": 0,
            "type": "GRID",
            "rows": 1000,
            "cols": 86,
            "frozenRows": 1,
            "frozenCols": 2,
            "tabColor": "#4285F4",
            "protectedRangeCount": 1,
            "conditionalFormatCount": 12,
        }

    def test_missing_protected_and_cf_arrays_count_zero(self):
        payload = {
            "properties": {"title": "T"},
            "sheets": [
                {
                    "properties": {
                        "sheetId": 5,
                        "title": "Bare",
                        "index": 0,
                        "sheetType": "GRID",
                        "gridProperties": {"rowCount": 100, "columnCount": 26},
                    }
                }
            ],
        }
        services, _ = _make_service(data_responses=[payload])
        out = overview(services, SHEET_ID)
        s = out["sheets"][0]
        assert s["protectedRangeCount"] == 0
        assert s["conditionalFormatCount"] == 0
        # No tab color set -> the key is omitted (token efficiency).
        assert "tabColor" not in s
        # frozen defaults to 0 when Google omits the count.
        assert s["frozenRows"] == 0
        assert s["frozenCols"] == 0

    def test_named_ranges_resolved_to_a1(self):
        payload = {
            "properties": {"title": "T"},
            "sheets": [
                {
                    "properties": {
                        "sheetId": 0,
                        "title": "Cliff",
                        "index": 0,
                        "sheetType": "GRID",
                        "gridProperties": {"rowCount": 1000, "columnCount": 86},
                    }
                }
            ],
            "namedRanges": [
                {
                    "name": "config",
                    "namedRangeId": "abc",
                    "range": {
                        "sheetId": 0,
                        "startRowIndex": 985,
                        "endRowIndex": 1000,
                        "startColumnIndex": 44,
                        "endColumnIndex": 45,
                    },
                }
            ],
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = overview(services, SHEET_ID)
        assert out["namedRanges"] == [
            {"name": "config", "namedRangeId": "abc", "range": "Cliff!AS986:AS1000"}
        ]

    def test_http_error_classified(self):
        services, rec = _make_service()
        rec._data = []

        def _boom(**kwargs):
            req = MagicMock()
            req.execute.side_effect = _make_http_error(403)
            return req

        services.sheets.spreadsheets.return_value.get = _boom
        with pytest.raises(SheetsError) as ei:
            overview(services, SHEET_ID)
        assert ei.value.code == "google_api_error"
        assert ei.value.status == 403


# =========================================================================== inspect (mask)


class TestInspectFieldsMask:
    def _run(self, **flags):
        services, rec = _make_service(
            data_responses=[{"sheets": [{"properties": {"title": "Cliff"}, "data": [{}]}]}],
            sheet_index=_CLIFF_INDEX,
        )
        inspect(services, SHEET_ID, "Cliff!A1:B2", **flags)
        return rec.data_calls[0]["fields"]

    def test_full_mask_has_everything_and_never_grid_data(self):
        fields = self._run()
        assert "userEnteredValue" in fields
        assert "effectiveValue" in fields
        assert "formattedValue" in fields
        assert "userEnteredFormat" in fields
        assert "effectiveFormat" in fields
        assert "dataValidation" in fields
        assert "note" in fields
        assert "merges" in fields
        # Tight mask, never the heavy includeGridData blob.
        assert "includeGridData" not in fields

    def test_no_formulas_drops_user_entered_value(self):
        fields = self._run(include_formulas=False)
        assert "userEnteredValue" not in fields
        # Value still available via effective/formatted.
        assert "effectiveValue" in fields

    def test_no_user_entered_format_drops_it(self):
        fields = self._run(include_user_entered_format=False)
        assert "userEnteredFormat" not in fields
        assert "effectiveFormat" in fields

    def test_no_effective_format_drops_it(self):
        fields = self._run(include_effective_format=False)
        assert "effectiveFormat" not in fields
        assert "userEnteredFormat" in fields

    def test_no_validation_drops_data_validation(self):
        fields = self._run(include_validation=False)
        assert "dataValidation" not in fields

    def test_ranges_arg_passed(self):
        services, rec = _make_service(
            data_responses=[{"sheets": [{"properties": {"title": "Cliff"}, "data": [{}]}]}],
            sheet_index=_CLIFF_INDEX,
        )
        inspect(services, SHEET_ID, "Cliff!A1:B2")
        assert rec.data_calls[0]["ranges"] == ["Cliff!A1:B2"]


# =========================================================================== inspect (cells)


def _cell(value=None, formula=None, eff=None, uef=None, note=None, validation=None):
    """Build a Google CellData dict for fixtures (only the keys provided)."""
    cell: dict = {}
    if formula is not None:
        cell["userEnteredValue"] = {"formulaValue": formula}
    elif value is not None and not isinstance(value, str):
        cell["userEnteredValue"] = {"numberValue": value}
    if value is not None:
        cell["formattedValue"] = str(value)
    if eff is not None:
        cell["effectiveFormat"] = eff
    if uef is not None:
        cell["userEnteredFormat"] = uef
    if note is not None:
        cell["note"] = note
    if validation is not None:
        cell["dataValidation"] = validation
    return cell


class TestInspectCells:
    def test_values_and_formulas_side_by_side(self):
        rowdata = [
            {"values": [_cell(formula="=SUM(A:A)", value="1234"), _cell(value="hi")]},
        ]
        payload = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff"},
                    "data": [{"startRow": 0, "startColumn": 0, "rowData": rowdata}],
                }
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1:B1")

        assert out["ok"] is True
        assert out["sheet"] == "Cliff"
        assert out["range"] == "Cliff!A1:B1"
        assert out["rows"] == 1
        assert out["cols"] == 2
        assert out["compact"] is False
        assert out["cells"] == [
            {"a1": "A1", "value": "1234", "formula": "=SUM(A:A)"},
            {"a1": "B1", "value": "hi"},
        ]

    def test_literal_string_is_not_a_formula(self):
        # A user-entered string that does NOT start with "=" is a literal, never a formula.
        cell = {
            "userEnteredValue": {"stringValue": "=not actually"},
            "formattedValue": "=not actually",
        }
        # stringValue starting with "=" but userEnteredValue is a stringValue, not formula.
        rowdata = [{"values": [cell]}]
        payload = {
            "sheets": [
                {
                    "properties": {"title": "Cliff"},
                    "data": [{"rowData": rowdata}],
                }
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1")
        assert out["cells"][0] == {"a1": "A1", "value": "=not actually"}
        assert "formula" not in out["cells"][0]

    def test_formats_flattened(self):
        eff = {
            "backgroundColorStyle": {"rgbColor": {"red": 1.0, "green": 0.8039216, "blue": 0.8235294}},
            "textFormat": {"bold": True},
        }
        rowdata = [{"values": [_cell(value="x", eff=eff)]}]
        payload = {
            "sheets": [
                {"properties": {"title": "Cliff"}, "data": [{"rowData": rowdata}]}
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1")
        assert out["cells"][0]["effectiveFormat"] == {"bg": "#FFCDD2", "bold": True}

    def test_jagged_rows_padded_to_rectangle(self):
        rowdata = [
            {"values": [_cell(value="a"), _cell(value="b"), _cell(value="c")]},
            {"values": [_cell(value="d")]},
        ]
        payload = {
            "sheets": [
                {
                    "properties": {"title": "Cliff"},
                    "data": [{"startRow": 0, "startColumn": 0, "rowData": rowdata}],
                }
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1:C2")
        assert out["rows"] == 2
        assert out["cols"] == 3
        # Row 2 is jagged-filled: B2/C2 are emitted as bare a1-only cells.
        assert out["cells"] == [
            {"a1": "A1", "value": "a"},
            {"a1": "B1", "value": "b"},
            {"a1": "C1", "value": "c"},
            {"a1": "A2", "value": "d"},
            {"a1": "B2"},
            {"a1": "C2"},
        ]

    def test_a1_offset_respects_start_row_col(self):
        # A data block can start at a non-zero offset (range not anchored at A1).
        rowdata = [{"values": [_cell(value="v")]}]
        payload = {
            "sheets": [
                {
                    "properties": {"title": "Cliff"},
                    "data": [{"startRow": 6, "startColumn": 3, "rowData": rowdata}],
                }
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!D7")
        assert out["cells"] == [{"a1": "D7", "value": "v"}]

    def test_merges_resolved_to_a1(self):
        payload = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff"},
                    "merges": [
                        {
                            "sheetId": 0,
                            "startRowIndex": 1,
                            "endRowIndex": 4,
                            "startColumnIndex": 0,
                            "endColumnIndex": 1,
                        }
                    ],
                    "data": [{"rowData": []}],
                }
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1:D5")
        assert out["merges"] == ["Cliff!A2:A4"]

    def test_note_surfaced(self):
        rowdata = [{"values": [_cell(value="x", note="reviewed")]}]
        payload = {
            "sheets": [
                {"properties": {"title": "Cliff"}, "data": [{"rowData": rowdata}]}
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1")
        assert out["cells"][0]["note"] == "reviewed"

    def test_effective_value_typed_fallback_when_no_formatted(self):
        # A cell with only a typed effectiveValue (no formattedValue) still yields a value.
        cell = {"effectiveValue": {"numberValue": 42}}
        rowdata = [{"values": [cell]}]
        payload = {
            "sheets": [
                {"properties": {"title": "Cliff"}, "data": [{"rowData": rowdata}]}
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1")
        assert out["cells"][0] == {"a1": "A1", "value": 42}

    def test_no_sheets_returned_raises(self):
        services, _ = _make_service(
            data_responses=[{"sheets": []}], sheet_index=_CLIFF_INDEX
        )
        with pytest.raises(SheetsError) as ei:
            inspect(services, SHEET_ID, "Cliff!A1")
        assert ei.value.code == "no_data"

    def test_bad_range_raises_before_api(self):
        services, rec = _make_service()
        with pytest.raises(SheetsError) as ei:
            inspect(services, SHEET_ID, "")
        assert ei.value.code == "bad_range"
        # No data get was issued.
        assert rec.data_calls == []

    def test_http_error_classified(self):
        services, _ = _make_service(sheet_index=_CLIFF_INDEX)

        def _boom(**kwargs):
            req = MagicMock()
            req.execute.side_effect = _make_http_error(404)
            return req

        services.sheets.spreadsheets.return_value.get = _boom
        with pytest.raises(SheetsError) as ei:
            inspect(services, SHEET_ID, "Cliff!A1")
        assert ei.value.code == "google_api_error"
        assert ei.value.status == 404


# =========================================================================== inspect validation


class TestInspectValidation:
    def test_validation_round_trip_structured_and_terse(self, monkeypatch):
        # Patch the sibling validation_to_rule (owned by core.rules) in the reads namespace.
        def fake_validation_to_rule(google_validation):
            return {
                "type": "ONE_OF_LIST",
                "values": ["Yes", "No"],
                "strict": True,
                "showDropdown": True,
            }

        monkeypatch.setattr(reads_mod, "validation_to_rule", fake_validation_to_rule)

        google_validation = {
            "condition": {
                "type": "ONE_OF_LIST",
                "values": [{"userEnteredValue": "Yes"}, {"userEnteredValue": "No"}],
            },
            "strict": True,
            "showCustomUi": True,
        }
        rowdata = [{"values": [_cell(value="Yes", validation=google_validation)]}]
        payload = {
            "sheets": [
                {"properties": {"title": "Cliff"}, "data": [{"rowData": rowdata}]}
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1")
        cell = out["cells"][0]
        # The terse one-liner (token-cheap, human-facing).
        assert cell["validation"] == "ONE_OF_LIST(Yes,No)"
        # The structured rule round-trips straight into set_validation(rule=...).
        assert cell["validationRule"] == {
            "type": "ONE_OF_LIST",
            "values": ["Yes", "No"],
            "strict": True,
            "showDropdown": True,
        }

    def test_one_of_range_terse_uses_source(self, monkeypatch):
        monkeypatch.setattr(
            reads_mod,
            "validation_to_rule",
            lambda g: {"type": "ONE_OF_RANGE", "source": "Cliff!Z1:Z10"},
        )
        rowdata = [{"values": [_cell(value="x", validation={"condition": {}})]}]
        payload = {
            "sheets": [
                {"properties": {"title": "Cliff"}, "data": [{"rowData": rowdata}]}
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1")
        assert out["cells"][0]["validation"] == "ONE_OF_RANGE(Cliff!Z1:Z10)"

    def test_boolean_terse_has_no_args(self, monkeypatch):
        monkeypatch.setattr(
            reads_mod, "validation_to_rule", lambda g: {"type": "BOOLEAN"}
        )
        rowdata = [{"values": [_cell(value="TRUE", validation={"condition": {}})]}]
        payload = {
            "sheets": [
                {"properties": {"title": "Cliff"}, "data": [{"rowData": rowdata}]}
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1")
        assert out["cells"][0]["validation"] == "BOOLEAN"

    def test_validation_skipped_when_include_validation_false(self, monkeypatch):
        # Even if a cell carries validation, the flag-off path never calls the helper.
        called = {"n": 0}

        def _spy(g):
            called["n"] += 1
            return {"type": "BOOLEAN"}

        monkeypatch.setattr(reads_mod, "validation_to_rule", _spy)
        rowdata = [{"values": [_cell(value="x", validation={"condition": {}})]}]
        payload = {
            "sheets": [
                {"properties": {"title": "Cliff"}, "data": [{"rowData": rowdata}]}
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1", include_validation=False)
        assert called["n"] == 0
        assert "validation" not in out["cells"][0]
        assert "validationRule" not in out["cells"][0]


# =========================================================================== inspect compact runs


class TestInspectCompactRuns:
    def test_single_cell_degenerates_to_1x1(self):
        rowdata = [{"values": [_cell(value="config")]}]
        payload = {
            "sheets": [
                {
                    "properties": {"title": "Cliff"},
                    "data": [{"startRow": 6, "startColumn": 3, "rowData": rowdata}],
                }
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!D7", compact=True)
        assert out["compact"] is True
        assert "cells" not in out
        assert out["runs"] == [
            {"a1Range": "D7:D7", "value": "config", "formula": None, "format": {}}
        ]

    def test_vertical_block_collapses_to_one_run(self):
        # A 15-row identical config block in column AS collapses to a single AS986:AS1000 run
        # (DESIGN §3.3 — rectangular RLE collapses vertical blocks, unlike row-major RLE).
        eff = {"backgroundColorStyle": {"rgbColor": {"red": 0.0, "green": 0.0, "blue": 0.0}}}
        rowdata = [{"values": [_cell(value="config", eff=eff)]} for _ in range(15)]
        payload = {
            "sheets": [
                {
                    "properties": {"title": "Cliff"},
                    "data": [
                        {"startRow": 985, "startColumn": 44, "rowData": rowdata}
                    ],
                }
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!AS986:AS1000", compact=True)
        assert out["runs"] == [
            {
                "a1Range": "AS986:AS1000",
                "value": "config",
                "formula": None,
                "format": {"bg": "#000000"},
            }
        ]

    def test_mixed_horizontal_and_vertical_rectangle(self):
        # A 2x3 block of identical "X" cells -> a single A1:C2 rectangular run; a distinct
        # cell below stays its own 1x1 run. Golden-master of the rectangle grower.
        x = _cell(value="X")
        y = _cell(value="Y")
        rowdata = [
            {"values": [x, x, x]},
            {"values": [x, x, x]},
            {"values": [y, None, None]},
        ]
        # Google never returns ``None`` cells inline; model the third row as a short row
        # (jagged) so only A3 is present.
        rowdata[2] = {"values": [y]}
        payload = {
            "sheets": [
                {
                    "properties": {"title": "Cliff"},
                    "data": [{"startRow": 0, "startColumn": 0, "rowData": rowdata}],
                }
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1:C3", compact=True)
        assert out["runs"] == [
            {"a1Range": "A1:C2", "value": "X", "formula": None, "format": {}},
            {"a1Range": "A3:A3", "value": "Y", "formula": None, "format": {}},
        ]

    def test_empty_cells_dropped_from_runs(self):
        rowdata = [
            {"values": [_cell(value="a"), {}, _cell(value="a")]},
        ]
        payload = {
            "sheets": [
                {
                    "properties": {"title": "Cliff"},
                    "data": [{"rowData": rowdata}],
                }
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1:C1", compact=True)
        # The blank middle cell breaks the run -> two separate 1x1 runs (no merge across gap).
        assert out["runs"] == [
            {"a1Range": "A1:A1", "value": "a", "formula": None, "format": {}},
            {"a1Range": "C1:C1", "value": "a", "formula": None, "format": {}},
        ]

    def test_differing_notes_never_merge(self):
        # Two adjacent cells equal in value/format but with different notes must NOT merge.
        a = _cell(value="x", note="first")
        b = _cell(value="x", note="second")
        rowdata = [{"values": [a, b]}]
        payload = {
            "sheets": [
                {"properties": {"title": "Cliff"}, "data": [{"rowData": rowdata}]}
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1:B1", compact=True)
        assert out["runs"] == [
            {
                "a1Range": "A1:A1",
                "value": "x",
                "formula": None,
                "format": {},
                "note": "first",
            },
            {
                "a1Range": "B1:B1",
                "value": "x",
                "formula": None,
                "format": {},
                "note": "second",
            },
        ]

    def test_differing_validation_never_merges(self, monkeypatch):
        # Cells with differing validationRule must stay distinct runs (DESIGN §3.3).
        rules_iter = iter(
            [
                {"type": "BOOLEAN"},
                {"type": "ONE_OF_LIST", "values": ["Yes", "No"]},
            ]
        )
        monkeypatch.setattr(
            reads_mod, "validation_to_rule", lambda g: next(rules_iter)
        )
        a = _cell(value="x", validation={"condition": {"type": "BOOLEAN"}})
        b = _cell(value="x", validation={"condition": {"type": "ONE_OF_LIST"}})
        rowdata = [{"values": [a, b]}]
        payload = {
            "sheets": [
                {"properties": {"title": "Cliff"}, "data": [{"rowData": rowdata}]}
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1:B1", compact=True)
        assert len(out["runs"]) == 2
        assert out["runs"][0]["validationRule"] == {"type": "BOOLEAN"}
        assert out["runs"][1]["validationRule"] == {
            "type": "ONE_OF_LIST",
            "values": ["Yes", "No"],
        }

    def test_run_carries_formula(self):
        f = _cell(formula="=A1", value="1")
        rowdata = [{"values": [f, f]}]
        payload = {
            "sheets": [
                {"properties": {"title": "Cliff"}, "data": [{"rowData": rowdata}]}
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1:B1", compact=True)
        assert out["runs"] == [
            {"a1Range": "A1:B1", "value": "1", "formula": "=A1", "format": {}}
        ]


# =========================================================== read_conditional_formats


# A Google ConditionalFormatRule (boolean) as the API returns it, ranges as GridRange.
_GOOGLE_BOOLEAN_RULE = {
    "ranges": [
        {
            "sheetId": 0,
            "startRowIndex": 1,
            "endRowIndex": 100,
            "startColumnIndex": 0,
            "endColumnIndex": 1,
        }
    ],
    "booleanRule": {
        "condition": {
            "type": "CUSTOM_FORMULA",
            "values": [{"userEnteredValue": "=$B2>10"}],
        },
        "format": {
            "backgroundColorStyle": {
                "rgbColor": {"red": 1.0, "green": 0.8039216, "blue": 0.8235294}
            },
            "textFormat": {"bold": True},
        },
    },
}

# A Google ConditionalFormatRule (gradient) with min/mid/max.
_GOOGLE_GRADIENT_RULE = {
    "ranges": [
        {
            "sheetId": 0,
            "startRowIndex": 1,
            "endRowIndex": 100,
            "startColumnIndex": 7,
            "endColumnIndex": 8,
        }
    ],
    "gradientRule": {
        "minpoint": {
            "type": "MIN",
            "colorStyle": {
                "rgbColor": {"red": 0.95686275, "green": 0.2627451, "blue": 0.21176471}
            },
        },
        "midpoint": {
            "type": "NUMBER",
            "value": "50",
            "colorStyle": {
                "rgbColor": {"red": 1.0, "green": 0.92156863, "blue": 0.23137255}
            },
        },
        "maxpoint": {
            "type": "MAX",
            "colorStyle": {
                "rgbColor": {"red": 0.29803922, "green": 0.6862745, "blue": 0.3137255}
            },
        },
    },
}


class TestReadConditionalFormats:
    def _cf_payload(self, *rules, title="Cliff", sheet_id=0):
        return {
            "sheets": [
                {
                    "properties": {"sheetId": sheet_id, "title": title},
                    "conditionalFormats": list(rules),
                }
            ]
        }

    def test_uses_cf_mask(self):
        services, rec = _make_service(
            data_responses=[self._cf_payload()], sheet_index=_CLIFF_INDEX
        )
        read_conditional_formats(services, SHEET_ID)
        fields = rec.data_calls[0]["fields"]
        assert fields == "sheets(properties(sheetId,title),conditionalFormats)"

    def test_boolean_rule_serialized_with_index_and_structured_fields(self):
        services, _ = _make_service(
            data_responses=[self._cf_payload(_GOOGLE_BOOLEAN_RULE)],
            sheet_index=_CLIFF_INDEX,
        )
        out = read_conditional_formats(services, SHEET_ID)

        assert out["ok"] is True
        assert out["spreadsheetId"] == SHEET_ID
        assert len(out["sheets"]) == 1
        sheet = out["sheets"][0]
        assert sheet["sheet"] == "Cliff"
        assert sheet["sheetId"] == 0
        assert sheet["rules"] == [
            {
                "index": 0,
                "line": "[Cliff!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold",
                "ranges": ["Cliff!A2:A100"],
                "kind": "boolean",
                "condition": {"type": "CUSTOM_FORMULA", "values": ["=$B2>10"]},
                "format": {"bg": "#FFCDD2", "bold": True},
            }
        ]

    def test_gradient_rule_serialized_with_stops(self):
        services, _ = _make_service(
            data_responses=[self._cf_payload(_GOOGLE_GRADIENT_RULE)],
            sheet_index=_CLIFF_INDEX,
        )
        out = read_conditional_formats(services, SHEET_ID)
        rule = out["sheets"][0]["rules"][0]
        assert rule["index"] == 0
        assert rule["kind"] == "gradient"
        assert (
            rule["line"]
            == "[Cliff!H2:H100] gradient min=#F44336 | mid:num:50=#FFEB3B | max=#4CAF50"
        )
        assert rule["ranges"] == ["Cliff!H2:H100"]
        assert rule["stops"] == [
            {"slot": "min", "hexColor": "#F44336"},
            {"slot": "mid", "hexColor": "#FFEB3B", "interp": "num", "value": "50"},
            {"slot": "max", "hexColor": "#4CAF50"},
        ]
        # Gradient rules carry no structured `condition` key.
        assert "condition" not in rule

    def test_index_increments_in_array_order(self):
        services, _ = _make_service(
            data_responses=[
                self._cf_payload(_GOOGLE_BOOLEAN_RULE, _GOOGLE_GRADIENT_RULE)
            ],
            sheet_index=_CLIFF_INDEX,
        )
        out = read_conditional_formats(services, SHEET_ID)
        rules = out["sheets"][0]["rules"]
        assert [r["index"] for r in rules] == [0, 1]
        assert rules[0]["kind"] == "boolean"
        assert rules[1]["kind"] == "gradient"

    def test_multi_sheet_envelope_shape(self):
        payload = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff"},
                    "conditionalFormats": [_GOOGLE_BOOLEAN_RULE],
                },
                {
                    "properties": {"sheetId": 1, "title": "Other"},
                    "conditionalFormats": [],
                },
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = read_conditional_formats(services, SHEET_ID)
        # `sheets` is ALWAYS a list (shared envelope with structure(read)).
        assert [s["sheet"] for s in out["sheets"]] == ["Cliff", "Other"]
        assert out["sheets"][1]["rules"] == []

    def test_sheet_filter_restricts_to_one_tab(self):
        payload = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff"},
                    "conditionalFormats": [_GOOGLE_BOOLEAN_RULE],
                },
                {
                    "properties": {"sheetId": 1, "title": "Other"},
                    "conditionalFormats": [_GOOGLE_GRADIENT_RULE],
                },
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = read_conditional_formats(services, SHEET_ID, sheet="Other")
        assert len(out["sheets"]) == 1
        assert out["sheets"][0]["sheet"] == "Other"
        assert out["sheets"][0]["rules"][0]["kind"] == "gradient"

    def test_unknown_sheet_filter_raises(self):
        services, _ = _make_service(
            data_responses=[self._cf_payload(_GOOGLE_BOOLEAN_RULE)],
            sheet_index=_CLIFF_INDEX,
        )
        with pytest.raises(SheetsError) as ei:
            read_conditional_formats(services, SHEET_ID, sheet="Nope")
        assert ei.value.code == "sheet_not_found"

    def test_no_rules_yields_empty_rules_list(self):
        services, _ = _make_service(
            data_responses=[self._cf_payload()], sheet_index=_CLIFF_INDEX
        )
        out = read_conditional_formats(services, SHEET_ID)
        assert out["sheets"][0]["rules"] == []

    def test_multi_range_rule(self):
        rule = {
            "ranges": [
                {
                    "sheetId": 0,
                    "startRowIndex": 1,
                    "endRowIndex": 100,
                    "startColumnIndex": 3,
                    "endColumnIndex": 4,
                },
                {
                    "sheetId": 0,
                    "startRowIndex": 1,
                    "endRowIndex": 100,
                    "startColumnIndex": 5,
                    "endColumnIndex": 6,
                },
            ],
            "booleanRule": {
                "condition": {
                    "type": "TEXT_CONTAINS",
                    "values": [{"userEnteredValue": "done"}],
                },
                "format": {
                    "backgroundColorStyle": {
                        "rgbColor": {
                            "red": 0.78431374,
                            "green": 0.9019608,
                            "blue": 0.7882353,
                        }
                    }
                },
            },
        }
        services, _ = _make_service(
            data_responses=[self._cf_payload(rule)], sheet_index=_CLIFF_INDEX
        )
        out = read_conditional_formats(services, SHEET_ID)
        r = out["sheets"][0]["rules"][0]
        assert (
            r["line"]
            == "[Cliff!D2:D100,Cliff!F2:F100] if TEXT_CONTAINS(done) -> bg #C8E6C9"
        )
        assert r["ranges"] == ["Cliff!D2:D100", "Cliff!F2:F100"]

    def test_http_error_classified(self):
        services, _ = _make_service(sheet_index=_CLIFF_INDEX)

        def _boom(**kwargs):
            req = MagicMock()
            req.execute.side_effect = _make_http_error(403)
            return req

        services.sheets.spreadsheets.return_value.get = _boom
        with pytest.raises(SheetsError) as ei:
            read_conditional_formats(services, SHEET_ID)
        assert ei.value.code == "google_api_error"
        assert ei.value.status == 403


# ==================================================== overview locale / timeZone (§X.12)


class TestOverviewLocaleTimeZone:
    def test_mask_requests_locale_and_time_zone(self):
        services, rec = _make_service(data_responses=[{"properties": {"title": "T"}}])
        overview(services, SHEET_ID)
        fields = rec.data_calls[0]["fields"]
        # The widened properties mask now pulls title + locale + timeZone (§X.12).
        assert "properties(title,locale,timeZone)" in fields

    def test_locale_and_time_zone_surfaced_when_present(self):
        payload = {
            "properties": {
                "title": "T",
                "locale": "en_US",
                "timeZone": "America/New_York",
            }
        }
        services, _ = _make_service(data_responses=[payload])
        out = overview(services, SHEET_ID)
        assert out["locale"] == "en_US"
        assert out["timeZone"] == "America/New_York"

    def test_locale_and_time_zone_omitted_when_absent(self):
        services, _ = _make_service(data_responses=[{"properties": {"title": "T"}}])
        out = overview(services, SHEET_ID)
        assert "locale" not in out
        assert "timeZone" not in out


# ==================================================== inspect rich-text / hyperlink (§X.1)


class TestInspectRichTextMask:
    def _run(self, **flags):
        services, rec = _make_service(
            data_responses=[{"sheets": [{"properties": {"title": "Cliff"}, "data": [{}]}]}],
            sheet_index=_CLIFF_INDEX,
        )
        inspect(services, SHEET_ID, "Cliff!A1:B2", **flags)
        return rec.data_calls[0]["fields"]

    def test_base_mask_omits_rich_text_and_pivot_by_default(self):
        fields = self._run()
        assert "textFormatRuns" not in fields
        assert "hyperlink" not in fields
        assert "pivotTable" not in fields

    def test_include_rich_text_adds_runs_and_hyperlink(self):
        fields = self._run(include_rich_text=True)
        assert "textFormatRuns" in fields
        assert "hyperlink" in fields
        # Still tight — never the heavy grid blob.
        assert "includeGridData" not in fields

    def test_include_pivot_adds_pivot_table(self):
        fields = self._run(include_pivot=True)
        assert "pivotTable" in fields


class TestInspectRichText:
    def _inspect_cell(self, cell, *, flags=None, sheet_index=_CLIFF_INDEX):
        rowdata = [{"values": [cell]}]
        payload = {
            "sheets": [
                {"properties": {"title": "Cliff"}, "data": [{"rowData": rowdata}]}
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=sheet_index
        )
        return inspect(services, SHEET_ID, "Cliff!A1", **(flags or {}))

    def test_runs_attached_only_when_present(self):
        cell = {
            "formattedValue": "Click here then plain",
            "textFormatRuns": [
                {"startIndex": 0, "format": {"bold": True, "link": {"uri": "https://x"}}},
                {"startIndex": 10, "format": {}},
            ],
        }
        out = self._inspect_cell(cell, flags={"include_rich_text": True})
        c = out["cells"][0]
        assert c["runs"] == [
            {"start": 0, "text": "Click here", "format": {"bold": True}, "link": "https://x"},
            {"start": 10, "text": " then plain"},
        ]

    def test_runs_not_emitted_when_flag_off(self):
        cell = {
            "formattedValue": "abc",
            "textFormatRuns": [{"startIndex": 0, "format": {"bold": True}}],
        }
        out = self._inspect_cell(cell)  # flag off
        assert "runs" not in out["cells"][0]

    def test_hyperlink_attached_flat_only_when_set(self):
        cell = {"formattedValue": "link", "hyperlink": "https://example.com"}
        out = self._inspect_cell(cell, flags={"include_rich_text": True})
        assert out["cells"][0]["hyperlink"] == "https://example.com"

    def test_hyperlink_omitted_when_absent(self):
        cell = {"formattedValue": "plain"}
        out = self._inspect_cell(cell, flags={"include_rich_text": True})
        assert "hyperlink" not in out["cells"][0]

    def test_cell_with_only_runs_is_not_empty(self):
        # A cell carrying ONLY rich-text runs (no scalar value distinct from text) still emits.
        cell = {
            "formattedValue": "x",
            "textFormatRuns": [{"startIndex": 0, "format": {"italic": True}}],
        }
        out = self._inspect_cell(cell, flags={"include_rich_text": True})
        c = out["cells"][0]
        assert c["value"] == "x"
        assert c["runs"][0]["format"] == {"italic": True}


# ==================================================== inspect pivot (§X.6)


class TestInspectPivot:
    def test_pivot_attached_on_anchor_cell_with_source_resolved(self):
        pivot = {
            "source": {
                "sheetId": 0,
                "startRowIndex": 0,
                "endRowIndex": 500,
                "startColumnIndex": 0,
                "endColumnIndex": 6,
            },
            "rows": [{"sourceColumnOffset": 0, "showTotals": True}],
            "values": [{"summarizeFunction": "SUM", "sourceColumnOffset": 4, "name": "Sales"}],
            "valueLayout": "HORIZONTAL",
        }
        cell = {"formattedValue": "Region", "pivotTable": pivot}
        rowdata = [{"values": [cell]}]
        payload = {
            "sheets": [
                {"properties": {"title": "Cliff"}, "data": [{"rowData": rowdata}]}
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1", include_pivot=True)
        piv = out["cells"][0]["pivot"]
        # source GridRange resolved to A1 via the addressing layer.
        assert piv["source"] == "Cliff!A1:F500"
        assert piv["values"][0]["summarize"] == "SUM"
        assert "line" in piv

    def test_pivot_not_emitted_when_flag_off(self):
        cell = {"formattedValue": "x", "pivotTable": {"source": {"sheetId": 0}}}
        rowdata = [{"values": [cell]}]
        payload = {
            "sheets": [
                {"properties": {"title": "Cliff"}, "data": [{"rowData": rowdata}]}
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1")
        assert "pivot" not in out["cells"][0]


# ============================== compact runs do not merge on rich-text differences (§X.1)


class TestCompactRichTextNeverMerges:
    def test_differing_hyperlinks_never_merge(self):
        a = {"formattedValue": "x", "hyperlink": "https://a"}
        b = {"formattedValue": "x", "hyperlink": "https://b"}
        rowdata = [{"values": [a, b]}]
        payload = {
            "sheets": [
                {"properties": {"title": "Cliff"}, "data": [{"rowData": rowdata}]}
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1:B1", compact=True, include_rich_text=True)
        assert len(out["runs"]) == 2
        assert out["runs"][0]["hyperlink"] == "https://a"
        assert out["runs"][1]["hyperlink"] == "https://b"

    def test_differing_runs_never_merge(self):
        a = {
            "formattedValue": "x",
            "textFormatRuns": [{"startIndex": 0, "format": {"bold": True}}],
        }
        b = {
            "formattedValue": "x",
            "textFormatRuns": [{"startIndex": 0, "format": {"italic": True}}],
        }
        rowdata = [{"values": [a, b]}]
        payload = {
            "sheets": [
                {"properties": {"title": "Cliff"}, "data": [{"rowData": rowdata}]}
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(services, SHEET_ID, "Cliff!A1:B1", compact=True, include_rich_text=True)
        assert len(out["runs"]) == 2
        assert out["runs"][0]["runs"][0]["format"] == {"bold": True}
        assert out["runs"][1]["runs"][0]["format"] == {"italic": True}

    def test_identical_plain_cells_still_merge_with_flags_on(self):
        # Two plain cells (no runs/hyperlink/pivot) still merge even with the flags ON — the
        # extended _run_key degenerates to the base identity when those keys are absent.
        a = _cell(value="x")
        b = _cell(value="x")
        rowdata = [{"values": [a, b]}]
        payload = {
            "sheets": [
                {"properties": {"title": "Cliff"}, "data": [{"rowData": rowdata}]}
            ]
        }
        services, _ = _make_service(
            data_responses=[payload], sheet_index=_CLIFF_INDEX
        )
        out = inspect(
            services, SHEET_ID, "Cliff!A1:B1", compact=True,
            include_rich_text=True, include_pivot=True,
        )
        assert out["runs"] == [
            {"a1Range": "A1:B1", "value": "x", "formula": None, "format": {}}
        ]


# =========================================================== boundary: no transport imports


def test_reads_module_imports_no_transport():
    # A fresh SUBPROCESS is mandatory: the in-process sys.modules table is already polluted by
    # earlier tests in the full suite (test_models.py / test_mcp_server.py import pydantic/
    # fastmcp/mcp; test_cli.py imports argparse). An in-process `set(sys.modules)` check would
    # give a false FAIL even though `gsheets.core.reads`'s OWN import graph is transport-clean.
    # We shell out to a clean interpreter so the only thing that ran is `import gsheets.core.reads`.
    # The authoritative repo-wide guard lives in test_boundary_guard.py; this mirrors its
    # mechanism, scoped to the reads module specifically.
    import os
    import subprocess
    import sys

    src_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src")
    )
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src_dir + os.pathsep + existing if existing else src_dir

    code = (
        "import gsheets.core.reads, sys; "
        "forbidden = {'fastmcp', 'mcp', 'argparse', 'pydantic'}; "
        "leaked = sorted(forbidden & set(sys.modules)); "
        "assert not leaked, 'LEAKED: ' + ', '.join(leaked)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        "importing `gsheets.core.reads` in a clean interpreter pulled a transport/CLI/pydantic "
        "module into sys.modules (boundary violation, DESIGN §1).\n"
        f"--- child stdout ---\n{result.stdout}"
        f"--- child stderr ---\n{result.stderr}"
    )
