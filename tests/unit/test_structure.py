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
            # v0.2 §X.3/§X.4/§X.9/§X.16 — new add-id buckets.
            "tableIds": [],
            "bandedRangeIds": [],
            "filterViewIds": [],
            "slicerIds": [],
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
            "tableIds": [],
            "bandedRangeIds": [],
            "filterViewIds": [],
            "slicerIds": [],
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


# =========================================================================== v0.2 read (features)


def _patch_addressing_everywhere(monkeypatch, *, sheet_id: int = 0, title: str = "Cliff") -> None:
    """Patch the addressing layer in ``structure`` AND in every serializer/builder module that
    imports ``a1_to_gridrange`` / ``gridrange_to_a1`` into its own namespace.

    The new feature serializers (``tables``/``slicers``) and write builders
    (``tables``/``banding``/``filters``) resolve ranges via addressing functions bound at THEIR
    module import time, so isolating from the real addressing layer means patching each of those
    bound names too (the base ``_patch_addressing`` only covers ``structure`` itself)."""
    _patch_addressing(monkeypatch, sheet_id=sheet_id, title=title)

    def fake_a1_to_gridrange(services, spreadsheet_id, a1):
        return {"sheetId": sheet_id, "_a1": a1}

    def fake_gridrange_to_a1(services, spreadsheet_id, gr):
        if set(gr.keys()) == {"sheetId"}:
            return title
        return f"{title}!GR"

    from gsheets.core import banding as banding_mod
    from gsheets.core import filters as filters_mod
    from gsheets.core import slicers as slicers_mod
    from gsheets.core import tables as tables_mod

    # tables: imports both a1_to_gridrange and gridrange_to_a1.
    monkeypatch.setattr(tables_mod, "a1_to_gridrange", fake_a1_to_gridrange)
    monkeypatch.setattr(tables_mod, "gridrange_to_a1", fake_gridrange_to_a1)
    # banding: imports a1_to_gridrange.
    monkeypatch.setattr(banding_mod, "a1_to_gridrange", fake_a1_to_gridrange)
    # slicers: imports gridrange_to_a1 (read serializer) AND a1_to_gridrange (write builders:
    # dataRange + anchor resolution).
    monkeypatch.setattr(slicers_mod, "gridrange_to_a1", fake_gridrange_to_a1)
    monkeypatch.setattr(slicers_mod, "a1_to_gridrange", fake_a1_to_gridrange)
    # filters builders resolve via the structure handler (already patched); its serializers take
    # a pre-resolved A1 string, so no addressing import to patch there.
    _ = filters_mod  # imported for symmetry/clarity; nothing to patch.


# A representative whole-spreadsheet payload exercising the five new sheet-scoped feature reads.
_FEATURE_READ_PAYLOAD = {
    "namedRanges": [],
    "sheets": [
        {
            "properties": {"sheetId": 0, "title": "Cliff", "gridProperties": {}},
            "tables": [
                {
                    "tableId": "t1",
                    "name": "Sales",
                    "range": {"sheetId": 0},
                    "columnProperties": [
                        {"columnIndex": 0, "columnName": "Region", "columnType": "TEXT"},
                    ],
                }
            ],
            "basicFilter": {
                "range": {"sheetId": 0},
                "criteria": {"1": {"hiddenValues": ["Closed"]}},
            },
            "filterViews": [
                {"filterViewId": 123, "title": "Open only", "range": {"sheetId": 0}}
            ],
            "bandedRanges": [
                {
                    "bandedRangeId": 7,
                    "range": {"sheetId": 0},
                    "rowProperties": {
                        "firstBandColorStyle": {
                            "rgbColor": {"red": 1.0, "green": 1.0, "blue": 1.0}
                        }
                    },
                }
            ],
            "slicers": [
                {
                    "slicerId": 4,
                    "spec": {
                        "title": "Region",
                        "dataRange": {"sheetId": 0},
                        "columnIndex": 0,
                    },
                }
            ],
        }
    ],
}


class TestStructureReadFeatures:
    def test_read_mask_includes_new_feature_fields(self, monkeypatch):
        services = _make_service()
        _patch_addressing_everywhere(monkeypatch)
        rec = _wire_spreadsheets_method(services, "get", [_FEATURE_READ_PAYLOAD])
        structure(services, SHEET_ID, action="read")
        fields = rec.calls[0]["fields"]
        for token in (
            "tables",
            "basicFilter",
            "filterViews",
            "bandedRanges",
            "slicers",
        ):
            assert token in fields
        # Still no grid data.
        assert "rowData" not in fields
        assert "includeGridData" not in rec.calls[0]

    def test_tables_attached_and_serialized(self, monkeypatch):
        services = _make_service()
        _patch_addressing_everywhere(monkeypatch)
        _wire_spreadsheets_method(services, "get", [_FEATURE_READ_PAYLOAD])
        cliff = structure(services, SHEET_ID, action="read")["sheets"][0]
        assert len(cliff["tables"]) == 1
        table = cliff["tables"][0]
        assert table["tableId"] == "t1"
        assert table["name"] == "Sales"
        assert table["range"] == "Cliff"  # bare-sheetId GridRange resolves to title
        assert "line" in table

    def test_basic_filter_single_or_null(self, monkeypatch):
        services = _make_service()
        _patch_addressing_everywhere(monkeypatch)
        _wire_spreadsheets_method(services, "get", [_FEATURE_READ_PAYLOAD])
        cliff = structure(services, SHEET_ID, action="read")["sheets"][0]
        assert cliff["basicFilter"] is not None
        assert cliff["basicFilter"]["range"] == "Cliff"
        assert "line" in cliff["basicFilter"]

    def test_filter_views_banding_slicers_attached(self, monkeypatch):
        services = _make_service()
        _patch_addressing_everywhere(monkeypatch)
        _wire_spreadsheets_method(services, "get", [_FEATURE_READ_PAYLOAD])
        cliff = structure(services, SHEET_ID, action="read")["sheets"][0]
        assert cliff["filterViews"][0]["filterViewId"] == 123
        assert cliff["bandedRanges"][0]["bandedRangeId"] == 7
        assert cliff["bandedRanges"][0]["rowBanding"]["first"] == "#FFFFFF"
        assert cliff["slicers"][0]["slicerId"] == 4

    def test_absent_features_default_to_empty_or_null(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        payload = {
            "sheets": [
                {"properties": {"sheetId": 0, "title": "Cliff", "gridProperties": {}}}
            ]
        }
        _wire_spreadsheets_method(services, "get", [payload])
        cliff = structure(services, SHEET_ID, action="read")["sheets"][0]
        assert cliff["tables"] == []
        assert cliff["basicFilter"] is None
        assert cliff["filterViews"] == []
        assert cliff["bandedRanges"] == []
        assert cliff["slicers"] == []


# =========================================================================== v0.2 tables write


class TestStructureTables:
    def test_add_table_captures_id(self, monkeypatch):
        services = _make_service()
        _patch_addressing_everywhere(monkeypatch)
        rec = _wire_spreadsheets_method(
            services,
            "batchUpdate",
            [{"replies": [{"addTable": {"table": {"tableId": "tNEW"}}}]}],
        )
        out = structure(
            services,
            SHEET_ID,
            action="add_table",
            range="Cliff!A1:B10",
            params={"name": "Sales", "columns": [{"name": "Region", "type": "TEXT"}]},
        )
        assert "addTable" in rec.calls[0]["body"]["requests"][0]
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "add_table",
            "range": "Cliff!A1:B10",
            "tableId": "tNEW",
        }

    def test_add_table_requires_range(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure(services, SHEET_ID, action="add_table", params={"name": "X"})
        assert exc.value.code == "bad_range"

    def test_update_table_dispatches(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(
            services,
            SHEET_ID,
            action="update_table",
            params={"tableId": "t1", "name": "Renamed"},
        )
        req = rec.calls[0]["body"]["requests"][0]["updateTable"]
        assert req["table"]["tableId"] == "t1"
        assert "fields" in req  # auto mask
        assert out["action"] == "update_table"
        assert out["tableId"] == "t1"

    def test_delete_table_dispatches(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(
            services, SHEET_ID, action="delete_table", params={"tableId": "t1"}
        )
        assert rec.calls[0]["body"]["requests"][0] == {"deleteTable": {"tableId": "t1"}}
        assert out["tableId"] == "t1"


# =========================================================================== v0.2 banding write


class TestStructureBanding:
    def test_add_banding_captures_id(self, monkeypatch):
        services = _make_service()
        _patch_addressing_everywhere(monkeypatch)
        rec = _wire_spreadsheets_method(
            services,
            "batchUpdate",
            [{"replies": [{"addBanding": {"bandedRange": {"bandedRangeId": 7}}}]}],
        )
        out = structure(
            services,
            SHEET_ID,
            action="add_banding",
            range="Cliff!A1:F500",
            params={"rowBanding": {"first": "#FFFFFF", "second": "#E8F0FE"}},
        )
        assert "addBanding" in rec.calls[0]["body"]["requests"][0]
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "add_banding",
            "range": "Cliff!A1:F500",
            "bandedRangeId": 7,
        }

    def test_add_banding_requires_range(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure(
                services,
                SHEET_ID,
                action="add_banding",
                params={"rowBanding": {"first": "#FFFFFF"}},
            )
        assert exc.value.code == "bad_range"

    def test_update_banding_auto_mask(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(
            services,
            SHEET_ID,
            action="update_banding",
            params={"bandedRangeId": 7, "rowBanding": {"first": "#000000"}},
        )
        req = rec.calls[0]["body"]["requests"][0]["updateBanding"]
        assert req["bandedRange"]["bandedRangeId"] == 7
        assert "fields" in req
        assert out["bandedRangeId"] == 7

    def test_delete_banding(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(
            services, SHEET_ID, action="delete_banding", params={"bandedRangeId": 7}
        )
        assert rec.calls[0]["body"]["requests"][0] == {
            "deleteBanding": {"bandedRangeId": 7}
        }
        assert out["bandedRangeId"] == 7


# =========================================================================== v0.2 filters write


class TestStructureFilters:
    def test_set_basic_filter(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(
            services,
            SHEET_ID,
            action="set_basic_filter",
            range="Cliff!A1:F500",
            params={"sorted": [{"col": "C", "order": "ASCENDING"}]},
        )
        req = rec.calls[0]["body"]["requests"][0]["setBasicFilter"]["filter"]
        assert req["range"]["sheetId"] == 0
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "set_basic_filter",
            "range": "Cliff!A1:F500",
        }

    def test_set_basic_filter_requires_range(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure(services, SHEET_ID, action="set_basic_filter")
        assert exc.value.code == "bad_range"

    def test_clear_basic_filter_requires_sheet(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure(services, SHEET_ID, action="clear_basic_filter")
        assert exc.value.code == "missing_sheet"

    def test_clear_basic_filter(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(
            services, SHEET_ID, action="clear_basic_filter", sheet="Cliff"
        )
        assert rec.calls[0]["body"]["requests"][0] == {
            "clearBasicFilter": {"sheetId": 0}
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "clear_basic_filter",
            "sheet": "Cliff",
        }

    def test_add_filter_view_captures_id(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(
            services,
            "batchUpdate",
            [{"replies": [{"addFilterView": {"filter": {"filterViewId": 55}}}]}],
        )
        out = structure(
            services,
            SHEET_ID,
            action="add_filter_view",
            range="Cliff!A1:F500",
            params={"title": "Open only"},
        )
        req = rec.calls[0]["body"]["requests"][0]["addFilterView"]["filter"]
        assert req["title"] == "Open only"
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "add_filter_view",
            "range": "Cliff!A1:F500",
            "filterViewId": 55,
        }

    def test_update_filter_view_resolves_range_to_grid(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(
            services,
            SHEET_ID,
            action="update_filter_view",
            params={
                "filterViewId": 55,
                "title": "Renamed",
                "range": "Cliff!A1:G900",
            },
        )
        req = rec.calls[0]["body"]["requests"][0]["updateFilterView"]
        assert req["filter"]["filterViewId"] == 55
        # The range was resolved to a GridRange and folded into the mask.
        assert req["filter"]["range"]["sheetId"] == 0
        assert "range" in req["fields"]
        assert out["filterViewId"] == 55

    def test_delete_filter_view(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(
            services, SHEET_ID, action="delete_filter_view", params={"filterViewId": 55}
        )
        assert rec.calls[0]["body"]["requests"][0] == {
            "deleteFilterView": {"filterId": 55}
        }
        assert out["filterViewId"] == 55

    def test_delete_filter_view_requires_id(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure(services, SHEET_ID, action="delete_filter_view", params={})
        assert exc.value.code == "missing_param"


# =========================================================================== v0.2 slicers write


# A two-sheet index so the REAL addressing layer resolves both the slicer's data range and its
# single-cell anchor (on a different tab) without network. ``Data`` (sheetId 0) is the data tab;
# ``Dash`` (sheetId 1) is the anchor tab.
_SLICER_SHEET_INDEX = {
    "sheets": [
        {"properties": {"sheetId": 0, "title": "Data", "index": 0}},
        {"properties": {"sheetId": 1, "title": "Dash", "index": 1}},
    ]
}


def _wire_slicer_services(batch_responses: list[dict]) -> tuple[SheetsServices, _Recorder]:
    """Wire a service whose ``get`` answers a two-sheet index (for real addressing) and whose
    ``batchUpdate`` records calls / returns ``batch_responses``. Returns ``(services, bu_rec)``."""
    services = _make_service()
    # The addressing cache may call get() more than once; answer every call with the index.
    _wire_spreadsheets_method(services, "get", [_SLICER_SHEET_INDEX] * 16)
    bu_rec = _wire_spreadsheets_method(services, "batchUpdate", batch_responses)
    return services, bu_rec


class TestStructureSlicers:
    def test_add_slicer_captures_id_and_builds_body(self):
        services, bu_rec = _wire_slicer_services(
            [{"replies": [{"addSlicer": {"slicer": {"slicerId": 4}}}]}]
        )
        out = structure(
            services,
            SHEET_ID,
            action="add_slicer",
            range="Data!A1:F500",
            params={"title": "Region", "columnIndex": 0, "anchor": "Dash!I1"},
        )
        request = bu_rec.calls[0]["body"]["requests"][0]
        assert "addSlicer" in request
        slicer = request["addSlicer"]["slicer"]
        # The top-level range became the data range; the anchor resolved to a GridCoordinate on
        # the OTHER tab (sheetId 1).
        assert slicer["spec"]["dataRange"]["sheetId"] == 0
        assert slicer["position"]["overlayPosition"]["anchorCell"] == {
            "sheetId": 1,
            "rowIndex": 0,
            "columnIndex": 8,
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "add_slicer",
            "slicerId": 4,
        }

    def test_add_slicer_data_range_may_come_from_params(self):
        services, bu_rec = _wire_slicer_services(
            [{"replies": [{"addSlicer": {"slicer": {"slicerId": 9}}}]}]
        )
        out = structure(
            services,
            SHEET_ID,
            action="add_slicer",
            params={"dataRange": "Data!A1:C10", "anchor": "Dash!A1"},
        )
        slicer = bu_rec.calls[0]["body"]["requests"][0]["addSlicer"]["slicer"]
        assert slicer["spec"]["dataRange"]["sheetId"] == 0
        assert out["slicerId"] == 9

    def test_add_slicer_requires_a_data_range(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure(
                services, SHEET_ID, action="add_slicer", params={"anchor": "Dash!A1"}
            )
        assert exc.value.code == "bad_range"

    def test_update_slicer_builds_auto_mask(self):
        services, bu_rec = _wire_slicer_services([{}])
        out = structure(
            services,
            SHEET_ID,
            action="update_slicer",
            params={"slicerId": 4, "title": "Renamed"},
        )
        req = bu_rec.calls[0]["body"]["requests"][0]["updateSlicerSpec"]
        assert req["slicerId"] == 4
        assert req["spec"] == {"title": "Renamed"}
        assert req["fields"] == "title"
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "update_slicer",
            "slicerId": 4,
        }

    def test_update_slicer_range_from_top_level_resolves(self):
        services, bu_rec = _wire_slicer_services([{}])
        structure(
            services,
            SHEET_ID,
            action="update_slicer",
            range="Data!A1:C10",
            params={"slicerId": 4},
        )
        req = bu_rec.calls[0]["body"]["requests"][0]["updateSlicerSpec"]
        assert req["spec"]["dataRange"]["sheetId"] == 0
        assert req["fields"] == "dataRange"

    def test_update_slicer_requires_id(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure(services, SHEET_ID, action="update_slicer", params={"title": "X"})
        assert exc.value.code == "missing_param"

    def test_delete_slicer_uses_delete_embedded_object(self):
        services, bu_rec = _wire_slicer_services([{}])
        out = structure(
            services, SHEET_ID, action="delete_slicer", params={"slicerId": 4}
        )
        assert bu_rec.calls[0]["body"]["requests"][0] == {
            "deleteEmbeddedObject": {"objectId": 4}
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "delete_slicer",
            "slicerId": 4,
        }

    def test_delete_slicer_requires_id(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure(services, SHEET_ID, action="delete_slicer", params={})
        assert exc.value.code == "missing_param"

    def test_add_slicer_rejects_unknown_param(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure(
                services,
                SHEET_ID,
                action="add_slicer",
                range="Data!A1:F500",
                params={"anchor": "Dash!A1", "bogus": 1},
            )
        assert exc.value.code == "unknown_param"


# =========================================================================== v0.2 spreadsheet_props


class TestStructureSpreadsheetProps:
    def test_set_title_locale_timezone_no_sheet(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = structure(
            services,
            SHEET_ID,
            action="spreadsheet_props",
            params={
                "title": "Budget",
                "locale": "en_US",
                "timeZone": "America/New_York",
            },
        )
        req = rec.calls[0]["body"]["requests"][0]["updateSpreadsheetProperties"]
        assert req["properties"] == {
            "title": "Budget",
            "locale": "en_US",
            "timeZone": "America/New_York",
        }
        # Auto fields mask covers exactly the three set properties.
        for token in ("title", "locale", "timeZone"):
            assert token in req["fields"]
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "spreadsheet_props",
            "title": "Budget",
            "locale": "en_US",
            "timeZone": "America/New_York",
        }

    def test_partial_props_masks_only_given(self, monkeypatch):
        services = _make_service()
        _patch_addressing(monkeypatch)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        structure(
            services, SHEET_ID, action="spreadsheet_props", params={"title": "Only"}
        )
        req = rec.calls[0]["body"]["requests"][0]["updateSpreadsheetProperties"]
        assert req["properties"] == {"title": "Only"}
        assert req["fields"] == "title"

    def test_empty_props_refused(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure(services, SHEET_ID, action="spreadsheet_props", params={})
        assert exc.value.code == "empty_payload"

    def test_unknown_prop_rejected(self):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure(
                services,
                SHEET_ID,
                action="spreadsheet_props",
                params={"title": "X", "bogus": 1},
            )
        assert exc.value.code == "unknown_param"


# =========================================================================== v0.2 capture ids


class TestCaptureNewIdsV02:
    def test_table_banding_filter_view_ids(self):
        replies = [
            {"addTable": {"table": {"tableId": "tABC"}}},
            {"addBanding": {"bandedRange": {"bandedRangeId": 7}}},
            {"addFilterView": {"filter": {"filterViewId": 55}}},
        ]
        out = capture_new_ids(replies)
        assert out["tableIds"] == ["tABC"]
        assert out["bandedRangeIds"] == [7]
        assert out["filterViewIds"] == [55]

    def test_slicer_id_captured(self):
        replies = [{"addSlicer": {"slicer": {"slicerId": 4}}}]
        assert capture_new_ids(replies)["slicerIds"] == [4]


# =========================================================================== capture_new_ids: broken path

# The reply-id specs walk a 2-step path (e.g. addSheet -> properties -> sheetId). If an
# intermediate value is the wrong shape mid-walk, the capture must degrade to "no id" rather
# than raise — exercising the path-abort branch (structure.py:88-90).


class TestCaptureNewIdsBrokenPath:
    def test_nested_non_dict_aborts_path_and_yields_no_id(self):
        # ``addSheet`` exists and is a dict, but ``properties`` is a string, so the walk to
        # ``properties.sheetId`` hits a non-dict before the last step -> no sheetId captured.
        replies = [{"addSheet": {"properties": "not-a-dict"}}]
        assert capture_new_ids(replies)["sheetIds"] == []

    def test_chart_path_with_non_dict_chart_yields_no_id(self):
        # ``addChart`` is a dict but ``chart`` is a list -> walk aborts at step 1.
        replies = [{"addChart": {"chart": ["unexpected"]}}]
        assert capture_new_ids(replies)["chartIds"] == []


# =========================================================================== _require_params non-dict


class TestRequireParamsNonDict:
    def test_non_dict_params_rejected_before_dispatch(self):
        """A non-None, non-dict ``params`` raises unknown_param (structure.py:111)."""
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure(
                services, SHEET_ID, action="merge", range="Cliff!A1:B2", params=["nope"]
            )
        assert exc.value.code == "unknown_param"
        assert "must be a dict" in exc.value.message


# =========================================================================== read degradations


class TestStructureReadDegradations:
    def test_unrecognized_tab_color_style_is_swallowed(self, monkeypatch):
        """A tabColorStyle dict that ``color_style_to_hex`` can't parse is dropped, NOT raised
        (structure.py:389-390): the read never fails over one weird color."""
        services = _make_service()
        _patch_addressing(monkeypatch)
        payload = {
            "sheets": [
                {
                    "properties": {
                        "sheetId": 0,
                        "title": "Cliff",
                        "gridProperties": {},
                        # Neither a themeColor nor any rgb channel -> ValueError inside.
                        "tabColorStyle": {"unknownColorKind": "x"},
                    }
                }
            ]
        }
        _wire_spreadsheets_method(services, "get", [payload])
        cliff = structure(services, SHEET_ID, action="read")["sheets"][0]
        # The bad color is silently omitted; the rest of the sheet still serializes.
        assert "tabColor" not in cliff
        assert cliff["sheet"] == "Cliff"

    def test_unresolvable_gridrange_degrades_to_none(self, monkeypatch):
        """When ``gridrange_to_a1`` raises SheetsError, a feature range degrades to None rather
        than aborting the read (structure.py:495-496 via _safe_gridrange_to_a1)."""

        def boom_gridrange_to_a1(services, spreadsheet_id, gr):
            raise SheetsError("sheet_not_found", "cannot resolve")

        def fake_a1_to_gridrange(services, spreadsheet_id, a1):
            return {"sheetId": 0, "_a1": a1}

        monkeypatch.setattr(structure_mod, "a1_to_gridrange", fake_a1_to_gridrange)
        monkeypatch.setattr(structure_mod, "gridrange_to_a1", boom_gridrange_to_a1)
        services = _make_service()
        payload = {
            "namedRanges": [
                {"name": "config", "namedRangeId": "abc", "range": {"sheetId": 0}}
            ],
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff", "gridProperties": {}},
                    "merges": [{"sheetId": 0, "startRowIndex": 1, "endRowIndex": 4}],
                }
            ],
        }
        _wire_spreadsheets_method(services, "get", [payload])
        out = structure(services, SHEET_ID, action="read")
        # An unresolvable named-range range still degrades to None (not an exception)...
        assert out["namedRanges"][0]["range"] is None
        # ...but an unresolvable merge is DROPPED from the list, never emitted as a null
        # (ISSUES.md #1: nulls crash the MCP model and leak useless entries to the CLI).
        assert out["sheets"][0]["merges"] == []

    def test_null_and_unresolvable_merge_entries_are_filtered(self, monkeypatch):
        """A merges array with null / unresolvable entries yields only the resolvable A1 strings
        (ISSUES.md #1) — no nulls so the MCP StructureResult never explodes one-error-per-null."""

        def selective_gridrange_to_a1(services, spreadsheet_id, gr):
            # A valid merge dict resolves; a null / dict-without-sheetId raises (caught upstream).
            if isinstance(gr, dict) and gr.get("startRowIndex") == 1:
                return "Cliff!A2:D4"
            raise SheetsError("bad_range", "unresolvable")

        monkeypatch.setattr(structure_mod, "gridrange_to_a1", selective_gridrange_to_a1)
        services = _make_service()
        payload = {
            "namedRanges": [],
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Cliff", "gridProperties": {}},
                    # one good merge + a null + a junk dict (mirrors the sparse array from #1)
                    "merges": [
                        {"sheetId": 0, "startRowIndex": 1, "endRowIndex": 4},
                        None,
                        {"unexpected": "shape"},
                    ],
                }
            ],
        }
        _wire_spreadsheets_method(services, "get", [payload])
        out = structure(services, SHEET_ID, action="read")
        assert out["sheets"][0]["merges"] == ["Cliff!A2:D4"]
        # The MCP mirror model validates without error (the regression that made #1 blocking).
        from gsheets.models import StructureResult

        StructureResult.model_validate(out)

    def test_feature_range_a1_non_dict_is_none(self):
        """A non-dict GridRange passed to the feature-range resolver yields None directly,
        never calling addressing (structure.py:485)."""
        services = _make_service()
        assert (
            structure_mod._feature_range_a1(services, SHEET_ID, "not-a-grid-range")
            is None
        )
        assert structure_mod._feature_range_a1(services, SHEET_ID, None) is None


# =========================================================================== resolve named-range HTTP error


class TestResolveNamedRangeHttpError:
    def test_delete_named_by_name_classifies_get_error(self, monkeypatch):
        """The narrow get that resolves a named-range NAME -> id classifies an HttpError
        (structure.py:602-603)."""
        services = _make_service()
        _patch_addressing(monkeypatch)
        bad = MagicMock()
        bad.execute.side_effect = _make_http_error(404)
        services.sheets.spreadsheets.return_value.get.return_value = bad
        with pytest.raises(SheetsError) as exc:
            structure(
                services, SHEET_ID, action="delete_named", params={"name": "config"}
            )
        assert exc.value.code == "google_api_error"
        assert exc.value.status == 404


# =========================================================================== _added_sheet_props edges


class TestAddedSheetProps:
    def test_non_dict_reply_skipped(self):
        """A non-dict entry in replies is skipped while scanning for the add/duplicate reply
        (structure.py:1265)."""
        resp = {
            "replies": [
                "garbage",
                {"addSheet": {"properties": {"sheetId": 3, "title": "T", "index": 1}}},
            ]
        }
        assert structure_mod._added_sheet_props(resp) == {
            "sheetId": 3,
            "title": "T",
            "index": 1,
        }

    def test_no_add_reply_yields_all_none(self):
        """When no addSheet/duplicateSheet reply is present, all id fields default to None
        (structure.py:1274) — keeps the add/duplicate return shape stable."""
        assert structure_mod._added_sheet_props({"replies": [{"updateCells": {}}]}) == {
            "sheetId": None,
            "title": None,
            "index": None,
        }

    def test_add_with_empty_replies_returns_none_props(self, monkeypatch):
        """End-to-end: an addSheet whose reply carries no properties surfaces an all-None
        sheet dict rather than crashing."""
        services = _make_service()
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{"replies": []}])
        out = manage_sheets(services, SHEET_ID, action="add")
        assert rec.calls  # the add was issued
        assert out["sheet"] == {"sheetId": None, "title": None, "index": None}


# =========================================================================== metadata location edges


class TestMetadataLocationEdges:
    def test_validate_location_non_dict_rejected(self):
        """A non-dict, non-None ``location`` raises unknown_param (structure.py:1350)."""
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            metadata(
                services, SHEET_ID, action="create", key="k", location=["not", "a", "dict"]
            )
        assert exc.value.code == "unknown_param"
        assert "must be a dict" in exc.value.message

    def test_create_dimension_anchor_bad_dimension_rejected(self, monkeypatch):
        """A dimension anchor whose dimension is neither ROWS nor COLUMNS raises unknown_param
        (structure.py:1378) — before any sheetId resolution."""
        services = _make_service()
        _patch_addressing(monkeypatch)
        with pytest.raises(SheetsError) as exc:
            metadata(
                services,
                SHEET_ID,
                action="create",
                key="k",
                location={
                    "sheet": "Cliff",
                    "dimension": "DIAGONAL",
                    "start": 0,
                    "end": 1,
                },
            )
        assert exc.value.code == "unknown_param"
        assert "ROWS" in exc.value.message

    def test_location_to_metadata_location_whole_sheet_missing_sheet(self):
        """A whole-sheet anchor with no 'sheet' (and no dimension/start/end) raises
        missing_param (structure.py:1393)."""
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            structure_mod._location_to_metadata_location(
                services, SHEET_ID, {"sheet": ""}
            )
        assert exc.value.code == "missing_param"

    def test_metadata_location_to_public_non_dict_is_empty(self):
        """A non-dict Google location flattens to {} (structure.py:1403)."""
        services = _make_service()
        assert (
            structure_mod._metadata_location_to_public(services, SHEET_ID, "bad") == {}
        )

    def test_metadata_location_to_public_bare_sheet_id(self, monkeypatch):
        """A bare ``{"sheetId": N}`` Google location resolves to ``{"sheet": <title>}``
        (structure.py:1417)."""
        _patch_addressing(monkeypatch, sheet_id=0, title="Cliff")
        services = _make_service()
        out = structure_mod._metadata_location_to_public(
            services, SHEET_ID, {"sheetId": 0}
        )
        assert out == {"sheet": "Cliff"}

    def test_read_by_id_whole_sheet_location_resolves_title(self, monkeypatch):
        """End-to-end: a read whose entry is anchored to a whole sheet surfaces
        ``location={"sheet": <title>}`` (exercises the bare-sheetId public mapping)."""
        _patch_addressing(monkeypatch, sheet_id=0, title="Cliff")
        services = _make_service()
        rec = _wire_developer_metadata_method(
            services,
            "get",
            [
                {
                    "metadataId": 9,
                    "metadataKey": "k",
                    "metadataValue": "v",
                    "visibility": "DOCUMENT",
                    "location": {"sheetId": 0},
                }
            ],
        )
        out = metadata(services, SHEET_ID, action="read", metadata_id=9)
        assert rec.calls
        assert out["metadata"][0]["location"] == {"sheet": "Cliff"}


# =========================================================================== _safe_sheet_title


class TestSafeSheetTitle:
    def test_none_sheet_id_is_none(self):
        services = _make_service()
        assert structure_mod._safe_sheet_title(services, SHEET_ID, None) is None

    def test_unresolvable_sheet_id_degrades_to_none(self, monkeypatch):
        """When ``gridrange_to_a1`` raises SheetsError resolving a sheetId, the title degrades
        to None rather than propagating (structure.py:1424,1427-1428)."""

        def boom(services, spreadsheet_id, gr):
            raise SheetsError("sheet_not_found", "no such sheet")

        monkeypatch.setattr(structure_mod, "gridrange_to_a1", boom)
        services = _make_service()
        assert structure_mod._safe_sheet_title(services, SHEET_ID, 999) is None

    def test_resolvable_sheet_id_returns_title(self, monkeypatch):
        _patch_addressing(monkeypatch, sheet_id=0, title="Cliff")
        services = _make_service()
        assert structure_mod._safe_sheet_title(services, SHEET_ID, 0) == "Cliff"


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
