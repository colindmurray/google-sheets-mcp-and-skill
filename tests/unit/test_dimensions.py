"""Unit tests for ``gsheets.core.dimensions`` (DESIGN §X.7/§X.10/§X.13; analysis #7/#10/#13).

All tests run against a MOCKED Sheets service — no network. Two flavours:

- OUTBOUND-REQUEST assertions (golden-master): the EXACT ``batchUpdate`` request body each write
  action emits (``insertDimension`` / ``deleteDimension`` / ``moveDimension`` /
  ``appendDimension`` / ``autoResizeDimensions`` / ``updateDimensionProperties``), including the
  resolved ``sheetId``, the 0-based half-open ``DimensionRange``, and the auto fields mask for
  ``set_props``.
- READ assertions (golden-master serializer): representative metadata JSON (``rowMetadata`` /
  ``columnMetadata`` with ``hiddenByUser`` + a block ``startRow``/``startColumn`` origin) in ->
  exact ``{"hiddenRows": [...], "hiddenCols": [...]}`` out (absolute 0-based indices, sorted,
  de-duplicated).

Addressing (sheet-name -> sheetId) is the real implemented layer; its resolution is driven by a
``spreadsheets().get`` recorder returning a one-sheet index (``Sheet1``, sheetId 0). The
``read`` action ALSO calls ``spreadsheets().get`` (with ``ranges`` + the metadata mask); the smart
recorder distinguishes the two by inspecting the call kwargs so one recorder serves both.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import gsheets.core.dimensions  # ensure the submodule is importable as a unit
from gsheets.core.dimensions import dimensions
from gsheets.core.errors import SheetsError
from gsheets.core.service import SheetsServices

SPREADSHEET_ID = "<YOUR_SPREADSHEET_ID>"


# --------------------------------------------------------------------------- helpers


class _Recorder:
    """Callable recording its kwargs; ``.execute()`` yields the next queued response."""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        resp = self._responses.pop(0) if self._responses else {}
        request_obj = MagicMock(name="request")
        request_obj.execute.return_value = resp
        return request_obj


class _SmartGet:
    """A ``spreadsheets().get`` stand-in serving BOTH addressing and the ``read`` metadata get.

    Addressing calls use ``fields="sheets.properties(sheetId,title,index)"`` (no ``ranges``); the
    ``read`` action's call carries ``ranges=[...]`` and the metadata mask. We answer addressing
    calls with the sheet index and the metadata call with the queued metadata response, recording
    every call so a test can assert the read mask/ranges.
    """

    def __init__(self, sheets_index: list[dict], metadata_responses: list[dict]):
        self._sheets_index = sheets_index
        self._metadata = list(metadata_responses)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if "ranges" in kwargs:
            resp = self._metadata.pop(0) if self._metadata else {"sheets": []}
        else:
            resp = {"sheets": self._sheets_index}
        request_obj = MagicMock(name="request")
        request_obj.execute.return_value = resp
        return request_obj

    @property
    def metadata_calls(self) -> list[dict]:
        return [c for c in self.calls if "ranges" in c]


def _make_service(
    *,
    batch_replies: list[dict] | None = None,
    sheets_index: list[dict] | None = None,
    metadata_responses: list[dict] | None = None,
) -> tuple[SheetsServices, _SmartGet, _Recorder]:
    """Wire a mocked service with a smart ``get`` (addressing + read) and a ``batchUpdate`` recorder.

    Returns ``(services, get_rec, batch_rec)`` so a test can assert the captured batchUpdate body
    and/or the read get's mask/ranges.
    """
    if sheets_index is None:
        sheets_index = [{"properties": {"sheetId": 0, "title": "Sheet1", "index": 0}}]
    services = SheetsServices(sheets=MagicMock(name="sheets_v4"), drive=None)
    spreadsheets = services.sheets.spreadsheets.return_value
    get_rec = _SmartGet(sheets_index, list(metadata_responses or []))
    spreadsheets.get = get_rec
    batch_rec = _Recorder(list(batch_replies or [{}]))
    spreadsheets.batchUpdate = batch_rec
    return services, get_rec, batch_rec


def _batch_body(batch_rec: _Recorder) -> dict:
    """Return the single captured ``batchUpdate`` request body."""
    assert len(batch_rec.calls) == 1, f"expected 1 batchUpdate, got {len(batch_rec.calls)}"
    return batch_rec.calls[0]["body"]


def _only_request(batch_rec: _Recorder) -> dict:
    """Return the single request dict inside the one captured batchUpdate body."""
    requests = _batch_body(batch_rec)["requests"]
    assert len(requests) == 1, f"expected 1 request, got {len(requests)}"
    return requests[0]


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


# =========================================================================== dispatch


class TestDispatch:
    def test_unknown_action_raises(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(services, SPREADSHEET_ID, action="frobnicate", sheet="Sheet1")
        assert exc.value.code == "unknown_action"
        assert "frobnicate" in exc.value.message

    def test_unknown_param_raises(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="delete",
                sheet="Sheet1",
                params={"dimension": "ROWS", "start": 0, "end": 1, "bogus": 1},
            )
        assert exc.value.code == "unknown_param"
        assert "bogus" in exc.value.message

    def test_params_must_be_dict(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="delete",
                sheet="Sheet1",
                params=["not", "a", "dict"],
            )
        assert exc.value.code == "unknown_param"

    def test_missing_sheet_raises_on_every_action(self):
        """Every dimensions action targets one tab — a missing sheet is a hard error."""
        services, _, _ = _make_service()
        for action in (
            "insert",
            "delete",
            "move",
            "append",
            "auto_resize",
            "set_props",
            "read",
        ):
            with pytest.raises(SheetsError) as exc:
                dimensions(services, SPREADSHEET_ID, action=action, sheet=None)
            assert exc.value.code == "missing_sheet", action

    def test_none_params_treated_as_empty(self):
        """A None params for a span-requiring action surfaces missing_param, not a crash."""
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services, SPREADSHEET_ID, action="delete", sheet="Sheet1", params=None
            )
        assert exc.value.code == "missing_param"

    def test_api_error_is_classified(self):
        services, _, _ = _make_service()

        def boom(**kwargs):
            request_obj = MagicMock()
            request_obj.execute.side_effect = _make_http_error(403)
            return request_obj

        services.sheets.spreadsheets.return_value.batchUpdate = boom
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="delete",
                sheet="Sheet1",
                params={"dimension": "ROWS", "start": 0, "end": 1},
            )
        assert exc.value.code == "google_api_error"
        assert exc.value.status == 403


# =========================================================================== insert


class TestInsert:
    def test_insert_rows_request_and_return(self):
        services, _, batch_rec = _make_service()
        out = dimensions(
            services,
            SPREADSHEET_ID,
            action="insert",
            sheet="Sheet1",
            params={"dimension": "ROWS", "start": 5, "end": 8},
        )
        assert _only_request(batch_rec) == {
            "insertDimension": {
                "range": {
                    "sheetId": 0,
                    "dimension": "ROWS",
                    "startIndex": 5,
                    "endIndex": 8,
                }
            }
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SPREADSHEET_ID,
            "action": "insert",
            "sheet": "Sheet1",
            "dimension": "ROWS",
            "start": 5,
            "end": 8,
        }

    def test_insert_with_inherit_from_before(self):
        services, _, batch_rec = _make_service()
        dimensions(
            services,
            SPREADSHEET_ID,
            action="insert",
            sheet="Sheet1",
            params={
                "dimension": "COLUMNS",
                "start": 0,
                "end": 2,
                "inheritFromBefore": True,
            },
        )
        req = _only_request(batch_rec)["insertDimension"]
        assert req["inheritFromBefore"] is True
        assert req["range"]["dimension"] == "COLUMNS"

    def test_insert_requires_dimension(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="insert",
                sheet="Sheet1",
                params={"start": 0, "end": 1},
            )
        assert exc.value.code == "missing_param"

    def test_insert_bad_dimension(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="insert",
                sheet="Sheet1",
                params={"dimension": "DIAGONAL", "start": 0, "end": 1},
            )
        assert exc.value.code == "bad_param"

    def test_insert_requires_span(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="insert",
                sheet="Sheet1",
                params={"dimension": "ROWS", "start": 5},
            )
        assert exc.value.code == "missing_param"

    def test_insert_zero_width_span_rejected(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="insert",
                sheet="Sheet1",
                params={"dimension": "ROWS", "start": 5, "end": 5},
            )
        assert exc.value.code == "bad_param"

    def test_insert_negative_start_rejected(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="insert",
                sheet="Sheet1",
                params={"dimension": "ROWS", "start": -1, "end": 2},
            )
        assert exc.value.code == "bad_param"

    def test_insert_boolean_span_rejected(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="insert",
                sheet="Sheet1",
                params={"dimension": "ROWS", "start": True, "end": 2},
            )
        assert exc.value.code == "bad_param"


# =========================================================================== delete


class TestDelete:
    def test_delete_columns_request_and_return(self):
        services, _, batch_rec = _make_service()
        out = dimensions(
            services,
            SPREADSHEET_ID,
            action="delete",
            sheet="Sheet1",
            params={"dimension": "COLUMNS", "start": 2, "end": 4},
        )
        assert _only_request(batch_rec) == {
            "deleteDimension": {
                "range": {
                    "sheetId": 0,
                    "dimension": "COLUMNS",
                    "startIndex": 2,
                    "endIndex": 4,
                }
            }
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SPREADSHEET_ID,
            "action": "delete",
            "sheet": "Sheet1",
            "dimension": "COLUMNS",
            "start": 2,
            "end": 4,
        }


# =========================================================================== move


class TestMove:
    def test_move_request_and_return(self):
        services, _, batch_rec = _make_service()
        out = dimensions(
            services,
            SPREADSHEET_ID,
            action="move",
            sheet="Sheet1",
            params={
                "dimension": "ROWS",
                "start": 10,
                "end": 12,
                "destinationIndex": 3,
            },
        )
        assert _only_request(batch_rec) == {
            "moveDimension": {
                "source": {
                    "sheetId": 0,
                    "dimension": "ROWS",
                    "startIndex": 10,
                    "endIndex": 12,
                },
                "destinationIndex": 3,
            }
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SPREADSHEET_ID,
            "action": "move",
            "sheet": "Sheet1",
            "dimension": "ROWS",
            "start": 10,
            "end": 12,
            "destinationIndex": 3,
        }

    def test_move_destination_index_zero_is_valid(self):
        services, _, batch_rec = _make_service()
        dimensions(
            services,
            SPREADSHEET_ID,
            action="move",
            sheet="Sheet1",
            params={
                "dimension": "ROWS",
                "start": 10,
                "end": 12,
                "destinationIndex": 0,
            },
        )
        assert _only_request(batch_rec)["moveDimension"]["destinationIndex"] == 0

    def test_move_requires_destination_index(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="move",
                sheet="Sheet1",
                params={"dimension": "ROWS", "start": 10, "end": 12},
            )
        assert exc.value.code == "missing_param"

    def test_move_bad_destination_index(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="move",
                sheet="Sheet1",
                params={
                    "dimension": "ROWS",
                    "start": 10,
                    "end": 12,
                    "destinationIndex": -1,
                },
            )
        assert exc.value.code == "bad_param"


# =========================================================================== append


class TestAppend:
    def test_append_rows_request_and_return(self):
        services, _, batch_rec = _make_service()
        out = dimensions(
            services,
            SPREADSHEET_ID,
            action="append",
            sheet="Sheet1",
            params={"dimension": "ROWS", "length": 100},
        )
        # appendDimension carries sheetId DIRECTLY (not a DimensionRange) — no start/end.
        assert _only_request(batch_rec) == {
            "appendDimension": {
                "sheetId": 0,
                "dimension": "ROWS",
                "length": 100,
            }
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SPREADSHEET_ID,
            "action": "append",
            "sheet": "Sheet1",
            "dimension": "ROWS",
            "length": 100,
        }

    def test_append_requires_length(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="append",
                sheet="Sheet1",
                params={"dimension": "COLUMNS"},
            )
        assert exc.value.code == "missing_param"

    def test_append_zero_length_rejected(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="append",
                sheet="Sheet1",
                params={"dimension": "ROWS", "length": 0},
            )
        assert exc.value.code == "bad_param"

    def test_append_does_not_accept_span_keys(self):
        """append's params are {dimension,length} only — start/end are unknown keys here."""
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="append",
                sheet="Sheet1",
                params={"dimension": "ROWS", "length": 5, "start": 0},
            )
        assert exc.value.code == "unknown_param"


# =========================================================================== auto_resize


class TestAutoResize:
    def test_auto_resize_span_request_and_return(self):
        services, _, batch_rec = _make_service()
        out = dimensions(
            services,
            SPREADSHEET_ID,
            action="auto_resize",
            sheet="Sheet1",
            params={"dimension": "COLUMNS", "start": 0, "end": 3},
        )
        assert _only_request(batch_rec) == {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": 0,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": 3,
                }
            }
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SPREADSHEET_ID,
            "action": "auto_resize",
            "sheet": "Sheet1",
            "dimension": "COLUMNS",
            "start": 0,
            "end": 3,
        }

    def test_auto_resize_whole_sheet_omits_indices(self):
        """No start/end -> the DimensionRange spans the whole sheet (indices omitted)."""
        services, _, batch_rec = _make_service()
        out = dimensions(
            services,
            SPREADSHEET_ID,
            action="auto_resize",
            sheet="Sheet1",
            params={"dimension": "COLUMNS"},
        )
        assert _only_request(batch_rec) == {
            "autoResizeDimensions": {
                "dimensions": {"sheetId": 0, "dimension": "COLUMNS"}
            }
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SPREADSHEET_ID,
            "action": "auto_resize",
            "sheet": "Sheet1",
            "dimension": "COLUMNS",
        }
        assert "start" not in out and "end" not in out

    def test_auto_resize_partial_span_rejected(self):
        """Only one of start/end is ambiguous -> missing_param (needs both or neither)."""
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="auto_resize",
                sheet="Sheet1",
                params={"dimension": "COLUMNS", "start": 0},
            )
        assert exc.value.code == "missing_param"

    def test_auto_resize_requires_dimension(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="auto_resize",
                sheet="Sheet1",
                params={"start": 0, "end": 3},
            )
        assert exc.value.code == "missing_param"


# =========================================================================== set_props


class TestSetProps:
    def test_set_props_pixel_size_request_and_mask(self):
        services, _, batch_rec = _make_service()
        out = dimensions(
            services,
            SPREADSHEET_ID,
            action="set_props",
            sheet="Sheet1",
            params={"dimension": "COLUMNS", "start": 1, "end": 3, "pixelSize": 120},
        )
        assert _only_request(batch_rec) == {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": 0,
                    "dimension": "COLUMNS",
                    "startIndex": 1,
                    "endIndex": 3,
                },
                "properties": {"pixelSize": 120},
                "fields": "pixelSize",
            }
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SPREADSHEET_ID,
            "action": "set_props",
            "sheet": "Sheet1",
            "dimension": "COLUMNS",
            "start": 1,
            "end": 3,
            "appliedFields": "pixelSize",
            "pixelSize": 120,
        }

    def test_set_props_hidden_request_and_mask(self):
        services, _, batch_rec = _make_service()
        out = dimensions(
            services,
            SPREADSHEET_ID,
            action="set_props",
            sheet="Sheet1",
            params={"dimension": "ROWS", "start": 4, "end": 5, "hiddenByUser": True},
        )
        req = _only_request(batch_rec)["updateDimensionProperties"]
        assert req["properties"] == {"hiddenByUser": True}
        assert req["fields"] == "hiddenByUser"
        assert out["hiddenByUser"] is True
        assert out["appliedFields"] == "hiddenByUser"

    def test_set_props_both_fields_auto_masked(self):
        """Both subfields present -> the auto mask lists both, in payload insertion order."""
        services, _, batch_rec = _make_service()
        out = dimensions(
            services,
            SPREADSHEET_ID,
            action="set_props",
            sheet="Sheet1",
            params={
                "dimension": "ROWS",
                "start": 0,
                "end": 10,
                "pixelSize": 30,
                "hiddenByUser": False,
            },
        )
        req = _only_request(batch_rec)["updateDimensionProperties"]
        assert req["properties"] == {"pixelSize": 30, "hiddenByUser": False}
        assert req["fields"] == "pixelSize,hiddenByUser"
        assert out["pixelSize"] == 30
        assert out["hiddenByUser"] is False

    def test_set_props_empty_payload_rejected(self):
        """Neither pixelSize nor hiddenByUser -> a no-op write is refused."""
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="set_props",
                sheet="Sheet1",
                params={"dimension": "ROWS", "start": 0, "end": 10},
            )
        assert exc.value.code == "empty_payload"

    def test_set_props_negative_pixel_size_rejected(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="set_props",
                sheet="Sheet1",
                params={"dimension": "ROWS", "start": 0, "end": 1, "pixelSize": -5},
            )
        assert exc.value.code == "bad_param"

    def test_set_props_requires_span(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="set_props",
                sheet="Sheet1",
                params={"dimension": "ROWS", "pixelSize": 20},
            )
        assert exc.value.code == "missing_param"


# =========================================================================== read (hiddenByUser)


class TestRead:
    def test_read_mask_and_ranges_are_tight(self):
        """The read get must request ONLY the hiddenByUser metadata (no grid data) over the sheet."""
        services, get_rec, _ = _make_service(metadata_responses=[{"sheets": []}])
        dimensions(services, SPREADSHEET_ID, action="read", sheet="Sheet1")
        meta_calls = get_rec.metadata_calls
        assert len(meta_calls) == 1
        call = meta_calls[0]
        assert call["ranges"] == ["Sheet1"]
        assert call["fields"] == (
            "sheets(properties(sheetId,title),"
            "data(rowMetadata.hiddenByUser,columnMetadata.hiddenByUser,startRow,startColumn))"
        )
        # Never grid data.
        assert "includeGridData" not in call

    def test_read_collects_hidden_rows_and_cols_absolute(self):
        """Golden master: metadata JSON -> absolute 0-based hidden indices (block origin applied)."""
        metadata = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Sheet1"},
                    "data": [
                        {
                            "startRow": 10,
                            "startColumn": 2,
                            "rowMetadata": [
                                {"hiddenByUser": True},   # absolute row 10
                                {},                        # visible row 11
                                {"hiddenByUser": True},   # absolute row 12
                            ],
                            "columnMetadata": [
                                {},                        # visible col 2
                                {"hiddenByUser": True},   # absolute col 3
                            ],
                        }
                    ],
                }
            ]
        }
        services, _, _ = _make_service(metadata_responses=[metadata])
        out = dimensions(
            services,
            SPREADSHEET_ID,
            action="read",
            sheet="Sheet1",
            params={"range": "Sheet1!A11:Z13"},
        )
        assert out == {
            "ok": True,
            "spreadsheetId": SPREADSHEET_ID,
            "action": "read",
            "sheet": "Sheet1",
            "hiddenRows": [10, 12],
            "hiddenCols": [3],
        }

    def test_read_default_origin_is_zero(self):
        """A data block with no startRow/startColumn anchors at absolute index 0."""
        metadata = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Sheet1"},
                    "data": [
                        {
                            "rowMetadata": [
                                {},
                                {"hiddenByUser": True},   # absolute row 1
                            ],
                            "columnMetadata": [
                                {"hiddenByUser": True},   # absolute col 0
                            ],
                        }
                    ],
                }
            ]
        }
        services, _, _ = _make_service(metadata_responses=[metadata])
        out = dimensions(services, SPREADSHEET_ID, action="read", sheet="Sheet1")
        assert out["hiddenRows"] == [1]
        assert out["hiddenCols"] == [0]

    def test_read_passes_range_to_get_when_given(self):
        services, get_rec, _ = _make_service(metadata_responses=[{"sheets": []}])
        dimensions(
            services,
            SPREADSHEET_ID,
            action="read",
            sheet="Sheet1",
            params={"range": "Sheet1!A1:D100"},
        )
        assert get_rec.metadata_calls[0]["ranges"] == ["Sheet1!A1:D100"]

    def test_read_no_hidden_returns_empty_lists(self):
        metadata = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Sheet1"},
                    "data": [{"rowMetadata": [{}, {}], "columnMetadata": [{}]}],
                }
            ]
        }
        services, _, _ = _make_service(metadata_responses=[metadata])
        out = dimensions(services, SPREADSHEET_ID, action="read", sheet="Sheet1")
        assert out["hiddenRows"] == []
        assert out["hiddenCols"] == []

    def test_read_ignores_other_sheets_in_response(self):
        """Only the data of the requested sheet (by title) contributes hidden indices."""
        metadata = {
            "sheets": [
                {
                    "properties": {"sheetId": 7, "title": "Other"},
                    "data": [{"rowMetadata": [{"hiddenByUser": True}]}],
                },
                {
                    "properties": {"sheetId": 0, "title": "Sheet1"},
                    "data": [{"rowMetadata": [{}, {"hiddenByUser": True}]}],
                },
            ]
        }
        services, _, _ = _make_service(metadata_responses=[metadata])
        out = dimensions(services, SPREADSHEET_ID, action="read", sheet="Sheet1")
        assert out["hiddenRows"] == [1]  # from Sheet1, NOT Other's row 0

    def test_read_dedups_overlapping_blocks(self):
        """Overlapping data blocks must not double-count the same absolute hidden index."""
        metadata = {
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Sheet1"},
                    "data": [
                        {"startRow": 0, "rowMetadata": [{"hiddenByUser": True}]},
                        {"startRow": 0, "rowMetadata": [{"hiddenByUser": True}]},
                    ],
                }
            ]
        }
        services, _, _ = _make_service(metadata_responses=[metadata])
        out = dimensions(services, SPREADSHEET_ID, action="read", sheet="Sheet1")
        assert out["hiddenRows"] == [0]

    def test_read_api_error_is_classified(self):
        services = SheetsServices(sheets=MagicMock(name="sheets_v4"), drive=None)
        spreadsheets = services.sheets.spreadsheets.return_value

        # Addressing get succeeds (sheet index), but the metadata (ranged) get fails.
        sheets_index = [{"properties": {"sheetId": 0, "title": "Sheet1", "index": 0}}]

        def smart_get(**kwargs):
            request_obj = MagicMock()
            if "ranges" in kwargs:
                request_obj.execute.side_effect = _make_http_error(404)
            else:
                request_obj.execute.return_value = {"sheets": sheets_index}
            return request_obj

        spreadsheets.get = smart_get
        with pytest.raises(SheetsError) as exc:
            dimensions(services, SPREADSHEET_ID, action="read", sheet="Sheet1")
        assert exc.value.code == "google_api_error"
        assert exc.value.status == 404


# =========================================================================== addressing reuse


class TestSheetResolution:
    def test_named_sheet_resolves_to_its_id(self):
        """A non-default tab name resolves to its sheetId via the addressing cache."""
        services, _, batch_rec = _make_service(
            sheets_index=[
                {"properties": {"sheetId": 0, "title": "Sheet1", "index": 0}},
                {"properties": {"sheetId": 42, "title": "Data", "index": 1}},
            ]
        )
        dimensions(
            services,
            SPREADSHEET_ID,
            action="delete",
            sheet="Data",
            params={"dimension": "ROWS", "start": 0, "end": 1},
        )
        assert _only_request(batch_rec)["deleteDimension"]["range"]["sheetId"] == 42

    def test_unknown_sheet_raises_sheet_not_found(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="delete",
                sheet="Nope",
                params={"dimension": "ROWS", "start": 0, "end": 1},
            )
        assert exc.value.code == "sheet_not_found"


# =========================================================================== span/int coercion edges


class TestSpanCoercion:
    def test_non_numeric_span_string_rejected(self):
        """A non-numeric string ``start`` can't be coerced to int -> bad_param
        (dimensions.py:205-206). (Booleans are caught earlier by a dedicated guard.)"""
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="delete",
                sheet="Sheet1",
                params={"dimension": "ROWS", "start": "five", "end": 10},
            )
        assert exc.value.code == "bad_param"
        assert "integer" in exc.value.message

    def test_list_span_value_rejected(self):
        """A list ``end`` raises TypeError inside int() -> bad_param (dimensions.py:205-206)."""
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="delete",
                sheet="Sheet1",
                params={"dimension": "ROWS", "start": 0, "end": [10]},
            )
        assert exc.value.code == "bad_param"

    def test_numeric_string_span_is_coerced(self):
        """A numeric string span IS accepted (int("5") works) and flows into the request as an
        int — pins the happy side of the coercion the bad-param tests guard."""
        services, _, batch_rec = _make_service()
        dimensions(
            services,
            SPREADSHEET_ID,
            action="delete",
            sheet="Sheet1",
            params={"dimension": "ROWS", "start": "5", "end": "8"},
        )
        rng = _only_request(batch_rec)["deleteDimension"]["range"]
        assert rng["startIndex"] == 5
        assert rng["endIndex"] == 8


class TestRequireIntCoercion:
    def test_move_boolean_destination_index_rejected(self):
        """A boolean ``destinationIndex`` is rejected (a bool is not a valid int here)
        (dimensions.py:227)."""
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="move",
                sheet="Sheet1",
                params={
                    "dimension": "ROWS",
                    "start": 0,
                    "end": 2,
                    "destinationIndex": True,
                },
            )
        assert exc.value.code == "bad_param"
        assert "boolean" in exc.value.message

    def test_append_boolean_length_rejected(self):
        """A boolean ``length`` for append is rejected (dimensions.py:227)."""
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="append",
                sheet="Sheet1",
                params={"dimension": "ROWS", "length": True},
            )
        assert exc.value.code == "bad_param"

    def test_move_non_numeric_destination_index_rejected(self):
        """A non-numeric ``destinationIndex`` can't be coerced -> bad_param
        (dimensions.py:230-231)."""
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="move",
                sheet="Sheet1",
                params={
                    "dimension": "ROWS",
                    "start": 0,
                    "end": 2,
                    "destinationIndex": "top",
                },
            )
        assert exc.value.code == "bad_param"

    def test_append_list_length_rejected(self):
        """A list ``length`` raises TypeError inside int() -> bad_param (dimensions.py:230-231)."""
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            dimensions(
                services,
                SPREADSHEET_ID,
                action="append",
                sheet="Sheet1",
                params={"dimension": "ROWS", "length": [5]},
            )
        assert exc.value.code == "bad_param"


# =========================================================================== public symbol


def test_dimensions_is_the_module_public_symbol():
    """``dimensions`` is the single public symbol of this NEW pure-core module (DESIGN §X.7).

    (The ``gsheets.core`` package re-export lands in the separate ``core-exports`` integration
    unit per DESIGN §X.13 file-ownership, so this unit only pins the module-level surface.)
    """
    # ``core/__init__`` re-exports the function under the same name, which shadows the
    # ``gsheets.core.dimensions`` package *attribute* (and, per CPython's ``IMPORT_FROM``,
    # even ``import gsheets.core.dimensions as x`` then resolves to the function). Reach the
    # real module object through ``sys.modules`` via ``import_module`` to pin its surface.
    import importlib

    mod = importlib.import_module("gsheets.core.dimensions")

    assert mod.dimensions is dimensions
    assert callable(dimensions)
