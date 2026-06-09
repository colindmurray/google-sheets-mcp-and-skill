"""Unit tests for ``gsheets.core.structure`` (DESIGN §3.3, §5.4).

All tests run against a MOCKED Sheets service — no network. A ``_Recorder`` captures the
kwargs/bodies sent to each Google API method so we can golden-master the OUTBOUND request
shape (batchUpdate request bodies, fields masks, developerMetadata search filters) as well as
the serialized RETURN dicts.

Sibling collaborators this unit calls but does NOT own (``a1_to_gridrange``,
``gridrange_to_a1``, ``build_fields_mask``, ``hex_to_color_style``, ``color_style_to_hex``)
are real implemented modules; addressing's sheet-name resolution is driven by wiring a
``spreadsheets().get`` recorder that returns a sheet index, OR monkeypatched where a test
wants to isolate from addressing entirely.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import sys

import gsheets.core.structure  # ensures the SUBMODULE is in sys.modules
from gsheets.core.errors import SheetsError
from gsheets.core.service import SheetsServices
from gsheets.core.structure import (
    capture_new_ids,
    manage_sheets,
    metadata,
    structure,
)

# ``gsheets.core.__init__`` re-exports the ``structure`` FUNCTION, which shadows the
# ``gsheets.core.structure`` submodule as a package attribute. Bind the real module object
# (for monkeypatching its module-level collaborators) via ``sys.modules`` so we never grab
# the function by accident.
structure_mod = sys.modules["gsheets.core.structure"]

SHEET_ID = "<TEST_SHEET_ID>"


# --------------------------------------------------------------------------- helpers


class _Recorder:
    """Callable recording its kwargs; returns an object whose ``.execute()`` yields a
    queued response. Lets a test assert exactly what was sent to Google."""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        resp = self._responses.pop(0) if self._responses else {}
        request_obj = MagicMock(name="request")
        request_obj.execute.return_value = resp
        return request_obj


def _make_service(*, account_email: str | None = None) -> SheetsServices:
    sheets = MagicMock(name="sheets_v4")
    return SheetsServices(sheets=sheets, drive=None, account_email=account_email)


def _wire_spreadsheets_method(
    services: SheetsServices, method: str, responses: list[dict]
) -> _Recorder:
    """Attach a recorder to ``spreadsheets().<method>`` and return it."""
    rec = _Recorder(responses)
    setattr(services.sheets.spreadsheets.return_value, method, rec)
    return rec


def _wire_developer_metadata_method(
    services: SheetsServices, method: str, responses: list[dict]
) -> _Recorder:
    """Attach a recorder to ``spreadsheets().developerMetadata().<method>``."""
    rec = _Recorder(responses)
    dm_api = services.sheets.spreadsheets.return_value.developerMetadata.return_value
    setattr(dm_api, method, rec)
    return rec


def _patch_addressing(monkeypatch, *, sheet_id: int = 0, title: str = "Cliff") -> None:
    """Isolate from the addressing layer: resolve any sheet to ``sheet_id``, any
    GridRange back to a stable A1 string."""

    def fake_a1_to_gridrange(services, spreadsheet_id, a1):
        return {"sheetId": sheet_id, "_a1": a1}

    def fake_gridrange_to_a1(services, spreadsheet_id, gr):
        # Bare sheetId -> the title; otherwise echo a stable A1 marker.
        if set(gr.keys()) == {"sheetId"}:
            return title
        return f"{title}!GR"

    monkeypatch.setattr(structure_mod, "a1_to_gridrange", fake_a1_to_gridrange)
    monkeypatch.setattr(structure_mod, "gridrange_to_a1", fake_gridrange_to_a1)


def _make_http_error(status: int = 403):
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = status
    resp.reason = "Forbidden"
    content = (
        b'{"error": {"code": %d, "status": "PERMISSION_DENIED", "message": "nope"}}'
        % status
    )
    return HttpError(resp=resp, content=content)


# =========================================================================== capture_new_ids


class TestCaptureNewIds:
    def test_empty_replies_all_empty_buckets(self):
        assert capture_new_ids([]) == {
            "sheetIds": [],
            "chartIds": [],
            "namedRangeIds": [],
            "protectedRangeIds": [],
            "metadataIds": [],
        }

    def test_add_sheet_nested_under_properties(self):
        replies = [{"addSheet": {"properties": {"sheetId": 7, "title": "New"}}}]
        assert capture_new_ids(replies)["sheetIds"] == [7]

    def test_duplicate_sheet_also_yields_sheet_id(self):
        replies = [{"duplicateSheet": {"properties": {"sheetId": 9}}}]
        assert capture_new_ids(replies)["sheetIds"] == [9]

    def test_add_chart_named_protected_metadata(self):
        replies = [
            {"addChart": {"chart": {"chartId": 99}}},
            {"addNamedRange": {"namedRange": {"namedRangeId": "abc"}}},
            {"addProtectedRange": {"protectedRange": {"protectedRangeId": 3}}},
            {"createDeveloperMetadata": {"developerMetadata": {"metadataId": 12}}},
        ]
        out = capture_new_ids(replies)
        assert out["chartIds"] == [99]
        assert out["namedRangeIds"] == ["abc"]
        assert out["protectedRangeIds"] == [3]
        assert out["metadataIds"] == [12]

    def test_multiple_same_kind_preserve_order(self):
        replies = [
            {"addSheet": {"properties": {"sheetId": 1}}},
            {"addSheet": {"properties": {"sheetId": 2}}},
        ]
        assert capture_new_ids(replies)["sheetIds"] == [1, 2]

    def test_irrelevant_and_malformed_replies_ignored(self):
        replies = [
            {},
            {"updateCells": {}},  # no id-bearing key
            "not-a-dict",
            {"addSheet": {"properties": {}}},  # missing sheetId
        ]
        assert capture_new_ids(replies) == {
            "sheetIds": [],
            "chartIds": [],
            "namedRangeIds": [],
            "protectedRangeIds": [],
            "metadataIds": [],
        }


# =========================================================================== structure(read)

# A representative whole-spreadsheet structural payload (golden master input).
_READ_PAYLOAD = {
    "namedRanges": [
        {"name": "config", "namedRangeId": "abc", "range": {"sheetId": 0}},
    ],
    "sheets": [
        {
            "properties": {
                "sheetId": 0,
                "title": "Cliff",
                "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 2},
                "tabColorStyle": {"rgbColor": {"red": 0.2588, "green": 0.5216, "blue": 0.9569}},
            },
            "merges": [{"sheetId": 0, "startRowIndex": 1, "endRowIndex": 4}],
            "protectedRanges": [
                {
                    "protectedRangeId": 1,
                    "range": {"sheetId": 0},
                    "description": "header",
                    "editors": {"users": ["a@b.com"]},
                    "warningOnly": False,
                }
            ],
            "rowGroups": [
                {"range": {"startIndex": 10, "endIndex": 20}, "depth": 1, "collapsed": False}
            ],
        },
        {
            "properties": {
                "sheetId": 5,
                "title": "Data",
                "gridProperties": {},
            },
        },
    ],
}


class TestStructureRead:
    def test_whole_spreadsheet_envelope_shape(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "get", [_READ_PAYLOAD])

        out = structure(services, SHEET_ID, action="read")

        # Top-level scalar fields + a sheets LIST (shape-stable envelope).
        assert out["ok"] is True
        assert out["spreadsheetId"] == SHEET_ID
        assert isinstance(out["sheets"], list)
        assert len(out["sheets"]) == 2
        # namedRanges are spreadsheet-scoped (top level).
        assert out["namedRanges"] == [
            {"name": "config", "namedRangeId": "abc", "range": "Cliff"}
        ]
        # The mask requests only structural subfields and NEVER grid data.
        fields = rec.calls[0]["fields"]
        assert "namedRanges" in fields
        assert "merges" in fields
        assert "protectedRanges" in fields
        assert "rowGroups" in fields and "columnGroups" in fields
        assert "rowData" not in fields  # never grid data
        assert "includeGridData" not in rec.calls[0]

    def test_per_sheet_structural_fields(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        _wire_spreadsheets_method(services, "get", [_READ_PAYLOAD])

        out = structure(services, SHEET_ID, action="read")
        cliff = out["sheets"][0]

        assert cliff["sheet"] == "Cliff"
        assert cliff["sheetId"] == 0
        assert cliff["frozenRows"] == 1
        assert cliff["frozenCols"] == 2
        assert cliff["tabColor"] == "#4285F4"
        assert cliff["merges"] == ["Cliff!GR"]
        assert cliff["protectedRanges"] == [
            {
                "protectedRangeId": 1,
                "range": "Cliff",
                "description": "header",
                "editors": ["a@b.com"],
                "warningOnly": False,
            }
        ]
        assert cliff["dimensionGroups"] == [
            {"dimension": "ROWS", "start": 10, "end": 20, "depth": 1, "collapsed": False}
        ]

    def test_second_sheet_defaults(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        _wire_spreadsheets_method(services, "get", [_READ_PAYLOAD])

        out = structure(services, SHEET_ID, action="read")
        data = out["sheets"][1]
        # Sheet with no grid props / merges / protected ranges still shape-stable.
        assert data["sheet"] == "Data"
        assert data["frozenRows"] == 0
        assert data["frozenCols"] == 0
        assert data["merges"] == []
        assert data["protectedRanges"] == []
        assert data["dimensionGroups"] == []
        assert "tabColor" not in data

    def test_read_one_sheet_still_a_list(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        _wire_spreadsheets_method(services, "get", [_READ_PAYLOAD])

        out = structure(services, SHEET_ID, action="read", sheet="Data")
        assert isinstance(out["sheets"], list)
        assert len(out["sheets"]) == 1
        assert out["sheets"][0]["sheet"] == "Data"
        # namedRanges remain top-level even when filtered to one sheet.
        assert "namedRanges" in out

    def test_read_unknown_sheet_raises(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        _wire_spreadsheets_method(services, "get", [_READ_PAYLOAD])
        with pytest.raises(SheetsError) as exc:
            structure(services, SHEET_ID, action="read", sheet="Nope")
        assert exc.value.code == "sheet_not_found"

    def test_read_never_passes_sheet_param_requirement(self, monkeypatch):
        # read with sheet=None must NOT raise missing_sheet (it is optional for read).
        services = _make_service()
        _patch_addressing(monkeypatch)
        _wire_spreadsheets_method(services, "get", [{"sheets": []}])
        out = structure(services, SHEET_ID, action="read")
        assert out["sheets"] == []
        assert out["namedRanges"] == []

    def test_read_column_group(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        payload = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff", "gridProperties": {}},
                    "columnGroups": [
                        {"range": {"startIndex": 3, "endIndex": 8}, "depth": 2, "collapsed": True}
                    ],
                }
            ]
        }
        _wire_spreadsheets_method(services, "get", [payload])
        out = structure(services, SHEET_ID, action="read")
        assert out["sheets"][0]["dimensionGroups"] == [
            {"dimension": "COLUMNS", "start": 3, "end": 8, "depth": 2, "collapsed": True}
        ]

    def test_read_http_error_classified(self, monkeypatch):
        services = _make_service()
        bad = MagicMock()
        bad.execute.side_effect = _make_http_error(404)
        services.sheets.spreadsheets.return_value.get.return_value = bad
        with pytest.raises(SheetsError) as exc:
            structure(services, SHEET_ID, action="read")
        assert exc.value.status == 404


# =========================================================================== structure mutators


class TestStructureMerge:
    def test_merge_default_type(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(services, SHEET_ID, action="merge", range="Cliff!A2:A4")
        req = rec.calls[0]["body"]["requests"][0]["mergeCells"]
        assert req["mergeType"] == "MERGE_ALL"
        assert req["range"]["sheetId"] == 0
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "merge",
            "range": "Cliff!A2:A4",
            "mergeType": "MERGE_ALL",
        }

    def test_merge_columns(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        structure(
            services,
            SHEET_ID,
            action="merge",
            range="Cliff!A2:C4",
            params={"mergeType": "MERGE_COLUMNS"},
        )
        assert rec.calls[0]["body"]["requests"][0]["mergeCells"]["mergeType"] == "MERGE_COLUMNS"

    def test_bad_merge_type_raises(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        _wire_spreadsheets_method(services, "batchUpdate", [{}])
        with pytest.raises(SheetsError) as exc:
            structure(
                services,
                SHEET_ID,
                action="merge",
                range="Cliff!A2:C4",
                params={"mergeType": "BOGUS"},
            )
        assert exc.value.code == "unknown_param"

    def test_merge_requires_range(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure(services, SHEET_ID, action="merge")
        assert exc.value.code == "bad_range"


class TestStructureUnmerge:
    def test_unmerge(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(services, SHEET_ID, action="unmerge", range="Cliff!A2:A4")
        assert "unmergeCells" in rec.calls[0]["body"]["requests"][0]
        assert out["action"] == "unmerge"
        assert out["range"] == "Cliff!A2:A4"


class TestStructureNamedRanges:
    def test_add_named_captures_id(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(
            services,
            "batchUpdate",
            [{"replies": [{"addNamedRange": {"namedRange": {"namedRangeId": "nr1"}}}]}],
        )
        out = structure(
            services,
            SHEET_ID,
            action="add_named",
            range="Cliff!AS986:AS1000",
            params={"name": "config"},
        )
        req = rec.calls[0]["body"]["requests"][0]["addNamedRange"]["namedRange"]
        assert req["name"] == "config"
        assert req["range"]["sheetId"] == 0
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "add_named",
            "name": "config",
            "range": "Cliff!AS986:AS1000",
            "namedRangeId": "nr1",
        }

    def test_add_named_requires_name(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        _wire_spreadsheets_method(services, "batchUpdate", [{}])
        with pytest.raises(SheetsError) as exc:
            structure(services, SHEET_ID, action="add_named", range="Cliff!A1:A2")
        assert exc.value.code == "missing_param"

    def test_delete_named_by_id(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(
            services,
            SHEET_ID,
            action="delete_named",
            params={"namedRangeId": "nr1"},
        )
        assert rec.calls[0]["body"]["requests"][0] == {
            "deleteNamedRange": {"namedRangeId": "nr1"}
        }
        assert out["namedRangeId"] == "nr1"

    def test_delete_named_by_name_resolves_id(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        # First a get() resolves the name -> id, then a batchUpdate deletes it.
        get_rec = _wire_spreadsheets_method(
            services,
            "get",
            [{"namedRanges": [{"name": "config", "namedRangeId": "nrX"}]}],
        )
        bu_rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(
            services, SHEET_ID, action="delete_named", params={"name": "config"}
        )
        assert get_rec.calls  # name resolution happened
        assert bu_rec.calls[0]["body"]["requests"][0] == {
            "deleteNamedRange": {"namedRangeId": "nrX"}
        }
        assert out["namedRangeId"] == "nrX"

    def test_delete_named_unknown_name_raises(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        _wire_spreadsheets_method(services, "get", [{"namedRanges": []}])
        with pytest.raises(SheetsError) as exc:
            structure(
                services, SHEET_ID, action="delete_named", params={"name": "missing"}
            )
        assert exc.value.code == "named_range_not_found"

    def test_delete_named_needs_id_or_name(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure(services, SHEET_ID, action="delete_named")
        assert exc.value.code == "missing_param"


class TestStructureProtect:
    def test_protect_full_params(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(
            services,
            "batchUpdate",
            [{"replies": [{"addProtectedRange": {"protectedRange": {"protectedRangeId": 42}}}]}],
        )
        out = structure(
            services,
            SHEET_ID,
            action="protect",
            range="Cliff!A1:D1",
            params={"description": "header", "editors": ["a@b.com"], "warningOnly": True},
        )
        pr = rec.calls[0]["body"]["requests"][0]["addProtectedRange"]["protectedRange"]
        assert pr["description"] == "header"
        assert pr["warningOnly"] is True
        assert pr["editors"] == {"users": ["a@b.com"]}
        assert pr["range"]["sheetId"] == 0
        assert out["protectedRangeId"] == 42

    def test_protect_minimal(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{"replies": []}])
        out = structure(services, SHEET_ID, action="protect", range="Cliff!A1:D1")
        pr = rec.calls[0]["body"]["requests"][0]["addProtectedRange"]["protectedRange"]
        assert set(pr.keys()) == {"range"}  # no optional params
        assert out["protectedRangeId"] is None

    def test_unprotect(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(
            services, SHEET_ID, action="unprotect", params={"protectedRangeId": 42}
        )
        assert rec.calls[0]["body"]["requests"][0] == {
            "deleteProtectedRange": {"protectedRangeId": 42}
        }
        assert out["protectedRangeId"] == 42

    def test_unprotect_requires_id(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure(services, SHEET_ID, action="unprotect")
        assert exc.value.code == "missing_param"


class TestStructureFreeze:
    def test_freeze_rows_and_cols_builds_mask(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(
            services,
            SHEET_ID,
            action="freeze",
            sheet="Cliff",
            params={"rows": 1, "cols": 2},
        )
        req = rec.calls[0]["body"]["requests"][0]["updateSheetProperties"]
        assert req["properties"]["sheetId"] == 0
        assert req["properties"]["gridProperties"] == {
            "frozenRowCount": 1,
            "frozenColumnCount": 2,
        }
        # Auto fields mask covers exactly the gridProperties subfields.
        assert req["fields"] == "gridProperties(frozenRowCount,frozenColumnCount)"
        assert out["frozenRows"] == 1
        assert out["frozenCols"] == 2

    def test_freeze_rows_only(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(
            services, SHEET_ID, action="freeze", sheet="Cliff", params={"rows": 3}
        )
        req = rec.calls[0]["body"]["requests"][0]["updateSheetProperties"]
        assert req["properties"]["gridProperties"] == {"frozenRowCount": 3}
        assert req["fields"] == "gridProperties.frozenRowCount"
        assert out["frozenRows"] == 3
        assert "frozenCols" not in out

    def test_freeze_requires_sheet(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure(services, SHEET_ID, action="freeze", params={"rows": 1})
        assert exc.value.code == "missing_sheet"

    def test_freeze_requires_a_value(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        with pytest.raises(SheetsError) as exc:
            structure(services, SHEET_ID, action="freeze", sheet="Cliff", params={})
        assert exc.value.code == "missing_param"


class TestStructureTabColor:
    def test_tab_color_hex(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(
            services,
            SHEET_ID,
            action="tab_color",
            sheet="Cliff",
            params={"color": "#4285F4"},
        )
        req = rec.calls[0]["body"]["requests"][0]["updateSheetProperties"]
        # Writes go through ColorStyle, never the deprecated flat Color.
        assert "tabColorStyle" in req["properties"]
        assert "rgbColor" in req["properties"]["tabColorStyle"]
        # tabColorStyle is an atomic-leaf -> masked at the parent.
        assert req["fields"] == "tabColorStyle"
        assert out["tabColor"] == "#4285F4"

    def test_tab_color_theme(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(
            services,
            SHEET_ID,
            action="tab_color",
            sheet="Cliff",
            params={"color": "theme:ACCENT1"},
        )
        req = rec.calls[0]["body"]["requests"][0]["updateSheetProperties"]
        assert req["properties"]["tabColorStyle"] == {"themeColor": "ACCENT1"}
        assert out["tabColor"] == "theme:ACCENT1"

    def test_tab_color_bad_color_raises(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        with pytest.raises(SheetsError) as exc:
            structure(
                services,
                SHEET_ID,
                action="tab_color",
                sheet="Cliff",
                params={"color": "not-a-color"},
            )
        assert exc.value.code == "bad_color"

    def test_tab_color_requires_color(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        with pytest.raises(SheetsError) as exc:
            structure(services, SHEET_ID, action="tab_color", sheet="Cliff", params={})
        assert exc.value.code == "missing_param"


class TestStructureGroup:
    def test_group_rows(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(
            services,
            SHEET_ID,
            action="group",
            sheet="Cliff",
            params={"dimension": "ROWS", "start": 10, "end": 20},
        )
        req = rec.calls[0]["body"]["requests"][0]["addDimensionGroup"]["range"]
        assert req == {
            "sheetId": 0,
            "dimension": "ROWS",
            "startIndex": 10,
            "endIndex": 20,
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "group",
            "sheet": "Cliff",
            "dimension": "ROWS",
            "start": 10,
            "end": 20,
        }

    def test_ungroup_columns(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(
            services,
            SHEET_ID,
            action="ungroup",
            sheet="Cliff",
            params={"dimension": "COLUMNS", "start": 3, "end": 8},
        )
        assert "deleteDimensionGroup" in rec.calls[0]["body"]["requests"][0]
        assert out["dimension"] == "COLUMNS"
        assert out["action"] == "ungroup"

    def test_group_bad_dimension(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        with pytest.raises(SheetsError) as exc:
            structure(
                services,
                SHEET_ID,
                action="group",
                sheet="Cliff",
                params={"dimension": "DIAGONAL", "start": 0, "end": 1},
            )
        assert exc.value.code == "missing_param"

    def test_group_missing_bounds(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        with pytest.raises(SheetsError) as exc:
            structure(
                services,
                SHEET_ID,
                action="group",
                sheet="Cliff",
                params={"dimension": "ROWS"},
            )
        assert exc.value.code == "missing_param"


class TestStructureValidation:
    def test_unknown_action(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure(services, SHEET_ID, action="frobnicate")
        assert exc.value.code == "unknown_action"

    def test_unknown_param_rejected(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure(
                services,
                SHEET_ID,
                action="merge",
                range="Cliff!A1:B2",
                params={"mergeType": "MERGE_ALL", "bogus": 1},
            )
        assert exc.value.code == "unknown_param"

    def test_mutate_http_error_classified(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        bad = MagicMock()
        bad.execute.side_effect = _make_http_error(403)
        services.sheets.spreadsheets.return_value.batchUpdate.return_value = bad
        with pytest.raises(SheetsError) as exc:
            structure(services, SHEET_ID, action="merge", range="Cliff!A1:B2")
        assert exc.value.code == "google_api_error"
        assert exc.value.status == 403


# =========================================================================== manage_sheets


class TestManageSheets:
    def test_add_minimal(self, monkeypatch):
        services = _make_service()
        rec = _wire_spreadsheets_method(
            services,
            "batchUpdate",
            [{"replies": [{"addSheet": {"properties": {"sheetId": 7, "title": "New", "index": 3}}}]}],
        )
        out = manage_sheets(services, SHEET_ID, action="add")
        # Empty properties dict still issues an addSheet (Google assigns defaults).
        assert rec.calls[0]["body"]["requests"][0] == {"addSheet": {"properties": {}}}
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "add",
            "sheet": {"sheetId": 7, "title": "New", "index": 3},
        }

    def test_add_with_params(self, monkeypatch):
        services = _make_service()
        rec = _wire_spreadsheets_method(
            services,
            "batchUpdate",
            [{"replies": [{"addSheet": {"properties": {"sheetId": 8, "title": "T", "index": 1}}}]}],
        )
        manage_sheets(
            services,
            SHEET_ID,
            action="add",
            params={"title": "T", "index": 1, "rows": 100, "cols": 12},
        )
        props = rec.calls[0]["body"]["requests"][0]["addSheet"]["properties"]
        assert props["title"] == "T"
        assert props["index"] == 1
        assert props["gridProperties"] == {"rowCount": 100, "columnCount": 12}

    def test_delete_resolves_sheet_id(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch, sheet_id=5)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = manage_sheets(services, SHEET_ID, action="delete", sheet="Old")
        assert rec.calls[0]["body"]["requests"][0] == {"deleteSheet": {"sheetId": 5}}
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "delete",
            "sheet": {"sheetId": 5, "title": "Old"},
        }

    def test_delete_requires_sheet(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            manage_sheets(services, SHEET_ID, action="delete")
        assert exc.value.code == "missing_sheet"

    def test_duplicate_captures_new_id(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch, sheet_id=2)
        rec = _wire_spreadsheets_method(
            services,
            "batchUpdate",
            [{"replies": [{"duplicateSheet": {"properties": {"sheetId": 11, "title": "Copy", "index": 4}}}]}],
        )
        out = manage_sheets(
            services,
            SHEET_ID,
            action="duplicate",
            sheet="Cliff",
            params={"newName": "Copy", "newIndex": 4},
        )
        req = rec.calls[0]["body"]["requests"][0]["duplicateSheet"]
        assert req["sourceSheetId"] == 2
        assert req["newSheetName"] == "Copy"
        assert req["insertSheetIndex"] == 4
        assert out["sheet"] == {"sheetId": 11, "title": "Copy", "index": 4}

    def test_rename_builds_mask(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch, sheet_id=0)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = manage_sheets(
            services, SHEET_ID, action="rename", sheet="Cliff", params={"newName": "Cliffs"}
        )
        req = rec.calls[0]["body"]["requests"][0]["updateSheetProperties"]
        assert req["properties"] == {"sheetId": 0, "title": "Cliffs"}
        assert req["fields"] == "title"
        assert out["sheet"] == {"sheetId": 0, "title": "Cliffs"}

    def test_rename_requires_new_name(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        with pytest.raises(SheetsError) as exc:
            manage_sheets(services, SHEET_ID, action="rename", sheet="Cliff", params={})
        assert exc.value.code == "missing_param"

    def test_reorder_builds_mask(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch, sheet_id=0)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = manage_sheets(
            services, SHEET_ID, action="reorder", sheet="Cliff", params={"newIndex": 2}
        )
        req = rec.calls[0]["body"]["requests"][0]["updateSheetProperties"]
        assert req["properties"] == {"sheetId": 0, "index": 2}
        assert req["fields"] == "index"
        assert out["sheet"] == {"sheetId": 0, "title": "Cliff", "index": 2}

    def test_reorder_requires_index(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        with pytest.raises(SheetsError) as exc:
            manage_sheets(services, SHEET_ID, action="reorder", sheet="Cliff", params={})
        assert exc.value.code == "missing_param"

    def test_unknown_action(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            manage_sheets(services, SHEET_ID, action="vaporize")
        assert exc.value.code == "unknown_action"

    def test_unknown_param(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            manage_sheets(services, SHEET_ID, action="add", params={"bogus": 1})
        assert exc.value.code == "unknown_param"

    def test_http_error_classified(self, monkeypatch):
        services = _make_service()
        bad = MagicMock()
        bad.execute.side_effect = _make_http_error(429)
        services.sheets.spreadsheets.return_value.batchUpdate.return_value = bad
        with pytest.raises(SheetsError) as exc:
            manage_sheets(services, SHEET_ID, action="add")
        assert exc.value.status == 429


# =========================================================================== metadata


class TestMetadata:
    def test_create_dimension_anchor_captures_id(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch, sheet_id=0)
        rec = _wire_spreadsheets_method(
            services,
            "batchUpdate",
            [{"replies": [{"createDeveloperMetadata": {"developerMetadata": {"metadataId": 12}}}]}],
        )
        out = metadata(
            services,
            SHEET_ID,
            action="create",
            key="anchor",
            value="wk1",
            location={"sheet": "Cliff", "dimension": "ROWS", "start": 10, "end": 11},
        )
        dm = rec.calls[0]["body"]["requests"][0]["createDeveloperMetadata"]["developerMetadata"]
        assert dm["metadataKey"] == "anchor"
        assert dm["metadataValue"] == "wk1"
        assert dm["visibility"] == "DOCUMENT"
        assert dm["location"] == {
            "dimensionRange": {
                "sheetId": 0,
                "dimension": "ROWS",
                "startIndex": 10,
                "endIndex": 11,
            }
        }
        assert out["action"] == "create"
        assert out["metadata"][0]["metadataId"] == 12
        assert out["metadata"][0]["key"] == "anchor"

    def test_create_whole_sheet_anchor(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch, sheet_id=4)
        rec = _wire_spreadsheets_method(
            services,
            "batchUpdate",
            [{"replies": [{"createDeveloperMetadata": {"developerMetadata": {"metadataId": 1}}}]}],
        )
        metadata(
            services,
            SHEET_ID,
            action="create",
            key="k",
            value="v",
            location={"sheet": "Cliff"},
        )
        dm = rec.calls[0]["body"]["requests"][0]["createDeveloperMetadata"]["developerMetadata"]
        assert dm["location"] == {"sheetId": 4}

    def test_create_spreadsheet_anchor(self, monkeypatch):
        services = _make_service()
        rec = _wire_spreadsheets_method(
            services,
            "batchUpdate",
            [{"replies": [{"createDeveloperMetadata": {"developerMetadata": {"metadataId": 2}}}]}],
        )
        metadata(services, SHEET_ID, action="create", key="k", value="v", location={})
        dm = rec.calls[0]["body"]["requests"][0]["createDeveloperMetadata"]["developerMetadata"]
        assert dm["location"] == {"spreadsheet": True}

    def test_create_no_location_defaults_to_spreadsheet(self, monkeypatch):
        services = _make_service()
        rec = _wire_spreadsheets_method(
            services,
            "batchUpdate",
            [{"replies": [{"createDeveloperMetadata": {"developerMetadata": {"metadataId": 3}}}]}],
        )
        metadata(services, SHEET_ID, action="create", key="k", value="v")
        dm = rec.calls[0]["body"]["requests"][0]["createDeveloperMetadata"]["developerMetadata"]
        assert dm["location"] == {"spreadsheet": True}

    def test_create_requires_key(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            metadata(services, SHEET_ID, action="create", value="v")
        assert exc.value.code == "missing_param"

    def test_create_unknown_location_key_raises(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            metadata(
                services,
                SHEET_ID,
                action="create",
                key="k",
                location={"sheet": "Cliff", "bogus": 1},
            )
        assert exc.value.code == "unknown_param"

    def test_create_partial_dimension_anchor_raises(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            metadata(
                services,
                SHEET_ID,
                action="create",
                key="k",
                location={"sheet": "Cliff", "dimension": "ROWS", "start": 1},  # no end
            )
        assert exc.value.code == "missing_param"

    def test_read_by_key(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch, sheet_id=0, title="Cliff")
        rec = _wire_developer_metadata_method(
            services,
            "search",
            [
                {
                    "matchedDeveloperMetadata": [
                        {
                            "developerMetadata": {
                                "metadataId": 12,
                                "metadataKey": "anchor",
                                "metadataValue": "wk1",
                                "visibility": "DOCUMENT",
                                "location": {
                                    "dimensionRange": {
                                        "sheetId": 0,
                                        "dimension": "ROWS",
                                        "startIndex": 10,
                                        "endIndex": 11,
                                    }
                                },
                            }
                        }
                    ]
                }
            ],
        )
        out = metadata(services, SHEET_ID, action="read", key="anchor")
        body = rec.calls[0]["body"]
        assert body == {
            "dataFilters": [{"developerMetadataLookup": {"metadataKey": "anchor"}}]
        }
        assert out["metadata"] == [
            {
                "metadataId": 12,
                "key": "anchor",
                "value": "wk1",
                "visibility": "DOCUMENT",
                "location": {
                    "sheet": "Cliff",
                    "dimension": "ROWS",
                    "start": 10,
                    "end": 11,
                },
            }
        ]

    def test_read_all_uses_document_filter(self, monkeypatch):
        services = _make_service()
        rec = _wire_developer_metadata_method(
            services, "search", [{"matchedDeveloperMetadata": []}]
        )
        out = metadata(services, SHEET_ID, action="read")
        body = rec.calls[0]["body"]
        assert body["dataFilters"][0]["developerMetadataLookup"]["locationType"] == "SPREADSHEET"
        assert out["metadata"] == []

    def test_read_by_id_uses_get(self, monkeypatch):
        services = _make_service()
        rec = _wire_developer_metadata_method(
            services,
            "get",
            [
                {
                    "metadataId": 7,
                    "metadataKey": "k",
                    "metadataValue": "v",
                    "visibility": "DOCUMENT",
                    "location": {"spreadsheet": True},
                }
            ],
        )
        out = metadata(services, SHEET_ID, action="read", metadata_id=7)
        assert rec.calls[0]["metadataId"] == 7
        assert out["metadata"][0]["metadataId"] == 7
        # spreadsheet-anchored location flattens to {}.
        assert out["metadata"][0]["location"] == {}

    def test_update_by_id_builds_mask(self, monkeypatch):
        services = _make_service()
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = metadata(
            services, SHEET_ID, action="update", metadata_id=12, value="wk2"
        )
        req = rec.calls[0]["body"]["requests"][0]["updateDeveloperMetadata"]
        assert req["dataFilters"] == [
            {"developerMetadataLookup": {"metadataId": 12}}
        ]
        assert req["developerMetadata"] == {"metadataValue": "wk2"}
        assert req["fields"] == "metadataValue"
        assert out["metadata"] == [{"metadataId": 12, "value": "wk2"}]

    def test_update_key_and_value(self, monkeypatch):
        services = _make_service()
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        metadata(
            services, SHEET_ID, action="update", metadata_id=3, key="k2", value="v2"
        )
        req = rec.calls[0]["body"]["requests"][0]["updateDeveloperMetadata"]
        assert req["developerMetadata"] == {"metadataKey": "k2", "metadataValue": "v2"}
        assert req["fields"] == "metadataKey,metadataValue"

    def test_update_requires_id(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            metadata(services, SHEET_ID, action="update", value="x")
        assert exc.value.code == "missing_param"

    def test_update_requires_change(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            metadata(services, SHEET_ID, action="update", metadata_id=1)
        assert exc.value.code == "empty_payload"

    def test_delete_by_id(self, monkeypatch):
        services = _make_service()
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = metadata(services, SHEET_ID, action="delete", metadata_id=12)
        req = rec.calls[0]["body"]["requests"][0]["deleteDeveloperMetadata"]
        assert req["dataFilter"] == {"developerMetadataLookup": {"metadataId": 12}}
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "delete",
            "metadata": [{"metadataId": 12}],
        }

    def test_delete_requires_id(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            metadata(services, SHEET_ID, action="delete")
        assert exc.value.code == "missing_param"

    def test_unknown_action(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            metadata(services, SHEET_ID, action="obliterate")
        assert exc.value.code == "unknown_action"

    def test_read_http_error_classified(self, monkeypatch):
        services = _make_service()
        dm_api = services.sheets.spreadsheets.return_value.developerMetadata.return_value
        bad = MagicMock()
        bad.execute.side_effect = _make_http_error(403)
        dm_api.search.return_value = bad
        with pytest.raises(SheetsError) as exc:
            metadata(services, SHEET_ID, action="read")
        assert exc.value.code == "google_api_error"


# =========================================================================== boundary


def test_module_is_transport_free():
    """The structure unit must not import any transport/CLI/pydantic symbol (DESIGN §1)."""
    import sys

    import gsheets.core.structure  # noqa: F401

    forbidden = {"fastmcp", "mcp", "argparse", "pydantic"}
    # gsheets.core.structure itself must not have pulled these in at import time.
    src = sys.modules["gsheets.core.structure"].__dict__
    assert "fastmcp" not in src
    assert "FastMCP" not in src
    assert "argparse" not in src
