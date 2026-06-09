"""Unit tests for ``gsheets.core.dataops`` (DESIGN §X.2/§X.11/§X.14/§X.15; analysis #2/#11/#14/#15).

All tests run against a MOCKED Sheets service — no network. Two flavours:

- OUTBOUND-REQUEST assertions: golden-master the EXACT ``batchUpdate`` request body each action
  emits (``findReplace`` / ``deleteDuplicates`` / ``trimWhitespace`` / ``sortRange`` /
  ``textToColumns`` / ``autoFill`` / ``copyPaste`` / ``cutPaste``), including resolved
  ``GridRange``s, col-letter -> index conversion, and scope selection.
- RETURN-SUMMARY assertions: golden-master the exact return dict, including the reply summaries
  surfaced from ``replies[]`` (``occurrencesChanged`` & friends, ``duplicatesRemoved``,
  ``cellsChangedCount``).

Addressing (A1 <-> GridRange) is the real implemented layer; its sheet-name resolution is driven
by a ``spreadsheets().get`` recorder returning a one-sheet index (``Sheet1``, sheetId 0). The
``spreadsheets().batchUpdate`` recorder both captures the outbound body AND feeds back a queued
reply so the summary path is exercised.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import gsheets.core.dataops  # ensure the submodule is importable as a unit
from gsheets.core.dataops import data_ops
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


def _make_service(
    *,
    batch_replies: list[dict] | None = None,
    sheets_index: list[dict] | None = None,
) -> tuple[SheetsServices, _Recorder, _Recorder]:
    """Wire a mocked service with separate ``get`` (addressing) and ``batchUpdate`` recorders.

    Returns ``(services, get_rec, batch_rec)`` so a test can assert the captured batchUpdate body.
    The ``get`` recorder answers EVERY addressing call (the addressing cache may call it more than
    once across multiple A1 resolutions in one action).
    """
    if sheets_index is None:
        sheets_index = [{"properties": {"sheetId": 0, "title": "Sheet1", "index": 0}}]
    services = SheetsServices(sheets=MagicMock(name="sheets_v4"), drive=None)
    spreadsheets = services.sheets.spreadsheets.return_value
    get_rec = _Recorder([{"sheets": sheets_index}] * 12)
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
            data_ops(services, SPREADSHEET_ID, action="frobnicate")
        assert exc.value.code == "unknown_action"
        assert "frobnicate" in exc.value.message

    def test_unknown_param_raises(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="trim_whitespace",
                params={"range": "Sheet1!A1:A10", "bogus": 1},
            )
        assert exc.value.code == "unknown_param"
        assert "bogus" in exc.value.message

    def test_params_must_be_dict(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services, SPREADSHEET_ID, action="trim_whitespace", params=["not", "a", "dict"]
            )
        assert exc.value.code == "unknown_param"

    def test_none_params_treated_as_empty(self):
        """A None params for a range-requiring action surfaces missing_param, not a crash."""
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(services, SPREADSHEET_ID, action="trim_whitespace", params=None)
        assert exc.value.code == "missing_param"

    def test_api_error_is_classified(self):
        services, _, _ = _make_service()

        def boom(**kwargs):
            request_obj = MagicMock()
            request_obj.execute.side_effect = _make_http_error(403)
            return request_obj

        services.sheets.spreadsheets.return_value.batchUpdate = boom
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="trim_whitespace",
                params={"range": "Sheet1!A1:A10"},
            )
        assert exc.value.code == "google_api_error"
        assert exc.value.status == 403


# =========================================================================== find_replace


class TestFindReplace:
    def test_find_replace_range_scope_request_and_summary(self):
        services, _, batch_rec = _make_service(
            batch_replies=[
                {
                    "replies": [
                        {
                            "findReplace": {
                                "valuesChanged": 3,
                                "formulasChanged": 1,
                                "rowsChanged": 2,
                                "sheetsChanged": 1,
                                "occurrencesChanged": 4,
                            }
                        }
                    ]
                }
            ]
        )
        out = data_ops(
            services,
            SPREADSHEET_ID,
            action="find_replace",
            params={
                "find": "foo",
                "replacement": "bar",
                "matchCase": True,
                "includeFormulas": True,
                "range": "Sheet1!A1:B10",
            },
        )
        assert _only_request(batch_rec) == {
            "findReplace": {
                "find": "foo",
                "replacement": "bar",
                "matchCase": True,
                "includeFormulas": True,
                "range": {
                    "sheetId": 0,
                    "startRowIndex": 0,
                    "endRowIndex": 10,
                    "startColumnIndex": 0,
                    "endColumnIndex": 2,
                },
            }
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SPREADSHEET_ID,
            "action": "find_replace",
            "range": "Sheet1!A1:B10",
            "valuesChanged": 3,
            "formulasChanged": 1,
            "rowsChanged": 2,
            "sheetsChanged": 1,
            "occurrencesChanged": 4,
        }

    def test_find_replace_sheet_scope_uses_sheet_id(self):
        services, _, batch_rec = _make_service()
        out = data_ops(
            services,
            SPREADSHEET_ID,
            action="find_replace",
            params={"find": "x", "replacement": "y", "sheet": "Sheet1"},
        )
        req = _only_request(batch_rec)["findReplace"]
        assert req["sheetId"] == 0
        assert "range" not in req and "allSheets" not in req
        assert out["sheet"] == "Sheet1"
        # Absent counts default to 0.
        assert out["occurrencesChanged"] == 0

    def test_find_replace_all_sheets_scope(self):
        services, _, batch_rec = _make_service()
        out = data_ops(
            services,
            SPREADSHEET_ID,
            action="find_replace",
            params={"find": "x", "replacement": "y", "allSheets": True},
        )
        req = _only_request(batch_rec)["findReplace"]
        assert req["allSheets"] is True
        assert "range" not in req and "sheetId" not in req
        assert out["allSheets"] is True

    def test_find_replace_regex_flag_passthrough(self):
        services, _, batch_rec = _make_service()
        data_ops(
            services,
            SPREADSHEET_ID,
            action="find_replace",
            params={
                "find": r"\d+",
                "replacement": "N",
                "searchByRegex": True,
                "matchEntireCell": True,
                "allSheets": True,
            },
        )
        req = _only_request(batch_rec)["findReplace"]
        assert req["searchByRegex"] is True
        assert req["matchEntireCell"] is True

    def test_find_replace_requires_find(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="find_replace",
                params={"replacement": "y", "allSheets": True},
            )
        assert exc.value.code == "missing_param"
        assert "find" in exc.value.message

    def test_find_replace_requires_replacement(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="find_replace",
                params={"find": "x", "allSheets": True},
            )
        assert exc.value.code == "missing_param"
        assert "replacement" in exc.value.message

    def test_find_replace_empty_replacement_is_allowed(self):
        """Replacing with the empty string (a delete) is valid — not missing_param."""
        services, _, batch_rec = _make_service()
        data_ops(
            services,
            SPREADSHEET_ID,
            action="find_replace",
            params={"find": "x", "replacement": "", "allSheets": True},
        )
        assert _only_request(batch_rec)["findReplace"]["replacement"] == ""

    def test_find_replace_no_scope_raises(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="find_replace",
                params={"find": "x", "replacement": "y"},
            )
        assert exc.value.code == "conflicting_args"

    def test_find_replace_multiple_scopes_raises(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="find_replace",
                params={
                    "find": "x",
                    "replacement": "y",
                    "range": "Sheet1!A1:A2",
                    "allSheets": True,
                },
            )
        assert exc.value.code == "conflicting_args"


# =========================================================================== delete_duplicates


class TestDeleteDuplicates:
    def test_delete_duplicates_basic_request_and_summary(self):
        # Google's real DeleteDuplicatesResponse field is ``duplicatesRemovedCount`` (verified
        # live). Mock the REAL field name so this guards the parse — using the wrong key here is
        # exactly what let the "always 0" bug slip past the mocked suite originally.
        services, _, batch_rec = _make_service(
            batch_replies=[
                {"replies": [{"deleteDuplicates": {"duplicatesRemovedCount": 5}}]}
            ]
        )
        out = data_ops(
            services,
            SPREADSHEET_ID,
            action="delete_duplicates",
            params={"range": "Sheet1!A1:C100"},
        )
        assert _only_request(batch_rec) == {
            "deleteDuplicates": {
                "range": {
                    "sheetId": 0,
                    "startRowIndex": 0,
                    "endRowIndex": 100,
                    "startColumnIndex": 0,
                    "endColumnIndex": 3,
                }
            }
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SPREADSHEET_ID,
            "action": "delete_duplicates",
            "range": "Sheet1!A1:C100",
            "duplicatesRemoved": 5,
        }

    def test_delete_duplicates_comparison_columns_letters(self):
        """Column letters become single-column DimensionRanges (absolute 0-based index)."""
        services, _, batch_rec = _make_service()
        data_ops(
            services,
            SPREADSHEET_ID,
            action="delete_duplicates",
            params={"range": "Sheet1!A1:D100", "comparisonColumns": ["A", "C"]},
        )
        req = _only_request(batch_rec)["deleteDuplicates"]
        assert req["comparisonColumns"] == [
            {"sheetId": 0, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            {"sheetId": 0, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
        ]

    def test_delete_duplicates_comparison_columns_ints(self):
        services, _, batch_rec = _make_service()
        data_ops(
            services,
            SPREADSHEET_ID,
            action="delete_duplicates",
            params={"range": "Sheet1!A1:D100", "comparisonColumns": [1]},
        )
        req = _only_request(batch_rec)["deleteDuplicates"]
        assert req["comparisonColumns"] == [
            {"sheetId": 0, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2}
        ]

    def test_delete_duplicates_requires_range(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(services, SPREADSHEET_ID, action="delete_duplicates", params={})
        assert exc.value.code == "missing_param"

    def test_delete_duplicates_bad_comparison_column_letter(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="delete_duplicates",
                params={"range": "Sheet1!A1:D100", "comparisonColumns": ["A1"]},
            )
        assert exc.value.code == "bad_param"

    def test_delete_duplicates_bool_column_rejected(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="delete_duplicates",
                params={"range": "Sheet1!A1:D100", "comparisonColumns": [True]},
            )
        assert exc.value.code == "bad_param"


# =========================================================================== trim_whitespace


class TestTrimWhitespace:
    def test_trim_whitespace_request_and_summary(self):
        services, _, batch_rec = _make_service(
            batch_replies=[{"replies": [{"trimWhitespace": {"cellsChangedCount": 7}}]}]
        )
        out = data_ops(
            services,
            SPREADSHEET_ID,
            action="trim_whitespace",
            params={"range": "Sheet1!A1:A50"},
        )
        assert _only_request(batch_rec) == {
            "trimWhitespace": {
                "range": {
                    "sheetId": 0,
                    "startRowIndex": 0,
                    "endRowIndex": 50,
                    "startColumnIndex": 0,
                    "endColumnIndex": 1,
                }
            }
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SPREADSHEET_ID,
            "action": "trim_whitespace",
            "range": "Sheet1!A1:A50",
            "cellsChangedCount": 7,
        }

    def test_trim_whitespace_empty_reply_defaults_zero(self):
        services, _, _ = _make_service(batch_replies=[{}])
        out = data_ops(
            services,
            SPREADSHEET_ID,
            action="trim_whitespace",
            params={"range": "Sheet1!A1:A50"},
        )
        assert out["cellsChangedCount"] == 0


# =========================================================================== sort_range


class TestSortRange:
    def test_sort_range_request_and_summary(self):
        services, _, batch_rec = _make_service()
        out = data_ops(
            services,
            SPREADSHEET_ID,
            action="sort_range",
            params={
                "range": "Sheet1!A2:D100",
                "specs": [
                    {"col": "B", "order": "ASCENDING"},
                    {"col": "D", "order": "DESCENDING"},
                ],
            },
        )
        # dimensionIndex is RELATIVE to the sorted range's first column (A => 0).
        assert _only_request(batch_rec) == {
            "sortRange": {
                "range": {
                    "sheetId": 0,
                    "startRowIndex": 1,
                    "endRowIndex": 100,
                    "startColumnIndex": 0,
                    "endColumnIndex": 4,
                },
                "sortSpecs": [
                    {"dimensionIndex": 1, "sortOrder": "ASCENDING"},
                    {"dimensionIndex": 3, "sortOrder": "DESCENDING"},
                ],
            }
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SPREADSHEET_ID,
            "action": "sort_range",
            "range": "Sheet1!A2:D100",
            "specs": [
                {"col": "B", "order": "ASCENDING"},
                {"col": "D", "order": "DESCENDING"},
            ],
        }

    def test_sort_range_relative_index_with_offset_start(self):
        """When the range starts at column C, sorting column C is dimensionIndex 0."""
        services, _, batch_rec = _make_service()
        data_ops(
            services,
            SPREADSHEET_ID,
            action="sort_range",
            params={"range": "Sheet1!C1:E10", "specs": [{"col": "C"}]},
        )
        spec = _only_request(batch_rec)["sortRange"]["sortSpecs"][0]
        assert spec == {"dimensionIndex": 0, "sortOrder": "ASCENDING"}

    def test_sort_range_default_order_is_ascending(self):
        services, _, batch_rec = _make_service()
        data_ops(
            services,
            SPREADSHEET_ID,
            action="sort_range",
            params={"range": "Sheet1!A1:B10", "specs": [{"col": "A"}]},
        )
        assert (
            _only_request(batch_rec)["sortRange"]["sortSpecs"][0]["sortOrder"]
            == "ASCENDING"
        )

    def test_sort_range_requires_specs(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="sort_range",
                params={"range": "Sheet1!A1:B10"},
            )
        assert exc.value.code == "missing_param"

    def test_sort_range_spec_requires_col(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="sort_range",
                params={"range": "Sheet1!A1:B10", "specs": [{"order": "ASCENDING"}]},
            )
        assert exc.value.code == "missing_param"

    def test_sort_range_bad_order_raises(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="sort_range",
                params={"range": "Sheet1!A1:B10", "specs": [{"col": "A", "order": "UP"}]},
            )
        assert exc.value.code == "bad_param"

    def test_sort_range_col_outside_range_raises(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="sort_range",
                params={"range": "Sheet1!C1:E10", "specs": [{"col": "A"}]},
            )
        assert exc.value.code == "bad_param"


# =========================================================================== text_to_columns


class TestTextToColumns:
    def test_text_to_columns_with_delimiter_type(self):
        services, _, batch_rec = _make_service()
        out = data_ops(
            services,
            SPREADSHEET_ID,
            action="text_to_columns",
            params={"range": "Sheet1!A1:A20", "delimiterType": "COMMA"},
        )
        assert _only_request(batch_rec) == {
            "textToColumns": {
                "source": {
                    "sheetId": 0,
                    "startRowIndex": 0,
                    "endRowIndex": 20,
                    "startColumnIndex": 0,
                    "endColumnIndex": 1,
                },
                "delimiterType": "COMMA",
            }
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SPREADSHEET_ID,
            "action": "text_to_columns",
            "range": "Sheet1!A1:A20",
            "delimiterType": "COMMA",
        }

    def test_text_to_columns_bare_delimiter_implies_custom(self):
        services, _, batch_rec = _make_service()
        out = data_ops(
            services,
            SPREADSHEET_ID,
            action="text_to_columns",
            params={"range": "Sheet1!A1:A20", "delimiter": "|"},
        )
        req = _only_request(batch_rec)["textToColumns"]
        assert req["delimiterType"] == "CUSTOM"
        assert req["delimiter"] == "|"
        assert out["delimiterType"] == "CUSTOM"
        assert out["delimiter"] == "|"

    def test_text_to_columns_custom_with_explicit_type_and_delim(self):
        services, _, batch_rec = _make_service()
        data_ops(
            services,
            SPREADSHEET_ID,
            action="text_to_columns",
            params={
                "range": "Sheet1!A1:A20",
                "delimiterType": "CUSTOM",
                "delimiter": ";;",
            },
        )
        req = _only_request(batch_rec)["textToColumns"]
        assert req["delimiterType"] == "CUSTOM"
        assert req["delimiter"] == ";;"

    def test_text_to_columns_no_delimiter_omits_both(self):
        """Neither delimiter nor type -> the API auto-detects; we send only source."""
        services, _, batch_rec = _make_service()
        out = data_ops(
            services,
            SPREADSHEET_ID,
            action="text_to_columns",
            params={"range": "Sheet1!A1:A20"},
        )
        assert _only_request(batch_rec)["textToColumns"] == {
            "source": {
                "sheetId": 0,
                "startRowIndex": 0,
                "endRowIndex": 20,
                "startColumnIndex": 0,
                "endColumnIndex": 1,
            }
        }
        assert "delimiterType" not in out and "delimiter" not in out

    def test_text_to_columns_bad_delimiter_type(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="text_to_columns",
                params={"range": "Sheet1!A1:A20", "delimiterType": "TAB"},
            )
        assert exc.value.code == "bad_param"

    def test_text_to_columns_requires_range(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(services, SPREADSHEET_ID, action="text_to_columns", params={})
        assert exc.value.code == "missing_param"


# =========================================================================== auto_fill


class TestAutoFill:
    def test_auto_fill_single_range(self):
        services, _, batch_rec = _make_service()
        out = data_ops(
            services,
            SPREADSHEET_ID,
            action="auto_fill",
            params={"range": "Sheet1!A1:A20", "useAlternateSeries": True},
        )
        assert _only_request(batch_rec) == {
            "autoFill": {
                "useAlternateSeries": True,
                "range": {
                    "sheetId": 0,
                    "startRowIndex": 0,
                    "endRowIndex": 20,
                    "startColumnIndex": 0,
                    "endColumnIndex": 1,
                },
            }
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SPREADSHEET_ID,
            "action": "auto_fill",
            "range": "Sheet1!A1:A20",
            "useAlternateSeries": True,
        }

    def test_auto_fill_source_destination_rows(self):
        """A vertical extend infers ROWS and a positive fillLength."""
        services, _, batch_rec = _make_service()
        out = data_ops(
            services,
            SPREADSHEET_ID,
            action="auto_fill",
            params={"source": "Sheet1!A1:A2", "destination": "Sheet1!A1:A10"},
        )
        sad = _only_request(batch_rec)["autoFill"]["sourceAndDestination"]
        assert sad["source"] == {
            "sheetId": 0,
            "startRowIndex": 0,
            "endRowIndex": 2,
            "startColumnIndex": 0,
            "endColumnIndex": 1,
        }
        assert sad["dimension"] == "ROWS"
        assert sad["fillLength"] == 8  # 10 rows dest - 2 rows source
        assert out["source"] == "Sheet1!A1:A2"
        assert out["destination"] == "Sheet1!A1:A10"

    def test_auto_fill_source_destination_columns(self):
        """A horizontal extend infers COLUMNS."""
        services, _, batch_rec = _make_service()
        data_ops(
            services,
            SPREADSHEET_ID,
            action="auto_fill",
            params={"source": "Sheet1!A1:B1", "destination": "Sheet1!A1:F1"},
        )
        sad = _only_request(batch_rec)["autoFill"]["sourceAndDestination"]
        assert sad["dimension"] == "COLUMNS"
        assert sad["fillLength"] == 4  # 6 cols dest - 2 cols source

    def test_auto_fill_range_and_pair_conflict(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="auto_fill",
                params={
                    "range": "Sheet1!A1:A10",
                    "source": "Sheet1!A1:A2",
                    "destination": "Sheet1!A1:A10",
                },
            )
        assert exc.value.code == "conflicting_args"

    def test_auto_fill_no_args_raises(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(services, SPREADSHEET_ID, action="auto_fill", params={})
        assert exc.value.code == "missing_param"

    def test_auto_fill_pair_requires_both(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="auto_fill",
                params={"source": "Sheet1!A1:A2"},
            )
        assert exc.value.code == "missing_param"


# =========================================================================== copy_paste


class TestCopyPaste:
    def test_copy_paste_basic(self):
        services, _, batch_rec = _make_service()
        out = data_ops(
            services,
            SPREADSHEET_ID,
            action="copy_paste",
            params={
                "source": "Sheet1!A1:B2",
                "destination": "Sheet1!D1:E2",
                "pasteType": "PASTE_VALUES",
                "pasteOrientation": "NORMAL",
            },
        )
        assert _only_request(batch_rec) == {
            "copyPaste": {
                "source": {
                    "sheetId": 0,
                    "startRowIndex": 0,
                    "endRowIndex": 2,
                    "startColumnIndex": 0,
                    "endColumnIndex": 2,
                },
                "destination": {
                    "sheetId": 0,
                    "startRowIndex": 0,
                    "endRowIndex": 2,
                    "startColumnIndex": 3,
                    "endColumnIndex": 5,
                },
                "pasteType": "PASTE_VALUES",
                "pasteOrientation": "NORMAL",
            }
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SPREADSHEET_ID,
            "action": "copy_paste",
            "source": "Sheet1!A1:B2",
            "destination": "Sheet1!D1:E2",
            "pasteType": "PASTE_VALUES",
            "pasteOrientation": "NORMAL",
        }

    def test_copy_paste_defaults_omit_optional_keys(self):
        services, _, batch_rec = _make_service()
        out = data_ops(
            services,
            SPREADSHEET_ID,
            action="copy_paste",
            params={"source": "Sheet1!A1:B2", "destination": "Sheet1!D1:E2"},
        )
        req = _only_request(batch_rec)["copyPaste"]
        assert "pasteType" not in req and "pasteOrientation" not in req
        assert "pasteType" not in out and "pasteOrientation" not in out

    def test_copy_paste_requires_source(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="copy_paste",
                params={"destination": "Sheet1!D1:E2"},
            )
        assert exc.value.code == "missing_param"

    def test_copy_paste_bad_paste_type(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="copy_paste",
                params={
                    "source": "Sheet1!A1:B2",
                    "destination": "Sheet1!D1:E2",
                    "pasteType": "PASTE_EVERYTHING",
                },
            )
        assert exc.value.code == "bad_param"

    def test_copy_paste_bad_orientation(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="copy_paste",
                params={
                    "source": "Sheet1!A1:B2",
                    "destination": "Sheet1!D1:E2",
                    "pasteOrientation": "FLIP",
                },
            )
        assert exc.value.code == "bad_param"


# =========================================================================== cut_paste


class TestCutPaste:
    def test_cut_paste_destination_is_top_left_coordinate(self):
        """cutPaste destination is a GridCoordinate (top-left), not a full range."""
        services, _, batch_rec = _make_service()
        out = data_ops(
            services,
            SPREADSHEET_ID,
            action="cut_paste",
            params={
                "source": "Sheet1!A1:B2",
                "destination": "Sheet1!D5:E6",
                "pasteType": "PASTE_NORMAL",
            },
        )
        assert _only_request(batch_rec) == {
            "cutPaste": {
                "source": {
                    "sheetId": 0,
                    "startRowIndex": 0,
                    "endRowIndex": 2,
                    "startColumnIndex": 0,
                    "endColumnIndex": 2,
                },
                "destination": {"sheetId": 0, "rowIndex": 4, "columnIndex": 3},
                "pasteType": "PASTE_NORMAL",
            }
        }
        assert out == {
            "ok": True,
            "spreadsheetId": SPREADSHEET_ID,
            "action": "cut_paste",
            "source": "Sheet1!A1:B2",
            "destination": "Sheet1!D5:E6",
            "pasteType": "PASTE_NORMAL",
        }

    def test_cut_paste_single_cell_destination(self):
        services, _, batch_rec = _make_service()
        data_ops(
            services,
            SPREADSHEET_ID,
            action="cut_paste",
            params={"source": "Sheet1!A1:B2", "destination": "Sheet1!D5"},
        )
        dest = _only_request(batch_rec)["cutPaste"]["destination"]
        assert dest == {"sheetId": 0, "rowIndex": 4, "columnIndex": 3}

    def test_cut_paste_requires_destination(self):
        services, _, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            data_ops(
                services,
                SPREADSHEET_ID,
                action="cut_paste",
                params={"source": "Sheet1!A1:B2"},
            )
        assert exc.value.code == "missing_param"

    def test_cut_paste_omits_paste_type_when_default(self):
        services, _, batch_rec = _make_service()
        out = data_ops(
            services,
            SPREADSHEET_ID,
            action="cut_paste",
            params={"source": "Sheet1!A1:B2", "destination": "Sheet1!D5"},
        )
        assert "pasteType" not in _only_request(batch_rec)["cutPaste"]
        assert "pasteType" not in out


# =========================================================================== public symbol


def test_data_ops_is_the_module_public_symbol():
    """``data_ops`` is the single public symbol of this NEW pure-core module (DESIGN §X.2).

    (The ``gsheets.core`` package re-export lands in the separate ``core-exports`` integration
    unit per DESIGN §X.13 file-ownership, so this unit only pins the module-level surface.)
    """
    assert gsheets.core.dataops.data_ops is data_ops
    assert callable(data_ops)
