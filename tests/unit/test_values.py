"""Unit tests for ``gsheets.core.values`` (DESIGN §3.3, §5.5).

All tests run against a MOCKED Sheets service — no network. The mock records the request
bodies passed to each Google API method so we can golden-master the OUTBOUND request shape
(value render options, value input options, batchClear/updateCells bodies) as well as the
serialized RETURN dicts.

Sibling collaborators that this unit calls but does NOT own (``a1_to_gridrange``,
``build_fields_mask`` — implemented by other build units) are patched where exercised, so
these tests stay isolated from those units' on-disk state.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gsheets.core import values as values_mod
from gsheets.core.errors import SheetsError
from gsheets.core.service import SheetsServices
from gsheets.core.values import (
    append_rows,
    clear,
    pad_jagged,
    read_values,
    write_values,
)


# --------------------------------------------------------------------------- helpers


class _Recorder:
    """A callable that records its kwargs and returns an object whose ``.execute()``
    yields a queued response. Lets a test assert exactly what was sent to Google."""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        resp = self._responses.pop(0) if self._responses else {}
        request_obj = MagicMock(name="request")
        request_obj.execute.return_value = resp
        return request_obj


def _make_service(*, account_email: str | None = None) -> tuple[SheetsServices, dict]:
    """Build a SheetsServices whose chained accessors route to per-method ``_Recorder``s.

    Returns ``(services, recorders)`` where ``recorders`` is a dict you populate via
    :func:`_wire` before calling core. Each recorder captures the kwargs of its API method.
    """
    sheets = MagicMock(name="sheets_v4")
    services = SheetsServices(sheets=sheets, drive=None, account_email=account_email)
    return services, {}


def _wire_values_method(services: SheetsServices, method: str, responses: list[dict]) -> _Recorder:
    """Attach a recorder to ``spreadsheets().values().<method>`` and return it."""
    rec = _Recorder(responses)
    values_api = services.sheets.spreadsheets.return_value.values.return_value
    setattr(values_api, method, rec)
    return rec


def _wire_spreadsheets_method(services: SheetsServices, method: str, responses: list[dict]) -> _Recorder:
    """Attach a recorder to ``spreadsheets().<method>`` and return it."""
    rec = _Recorder(responses)
    setattr(services.sheets.spreadsheets.return_value, method, rec)
    return rec


def _make_http_error(status: int = 403):
    """A minimal stand-in for ``googleapiclient.errors.HttpError`` that classify can read."""
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


# =========================================================================== pad_jagged


class TestPadJagged:
    def test_pads_to_max_width(self):
        assert pad_jagged([[1, 2, 3], [4], [5, 6]]) == [[1, 2, 3], [4, "", ""], [5, 6, ""]]

    def test_explicit_width_wider_than_data(self):
        assert pad_jagged([[1], [2, 3]], width=4) == [[1, "", "", ""], [2, 3, "", ""]]

    def test_explicit_width_equal_to_data(self):
        assert pad_jagged([["a", "b"]], width=2) == [["a", "b"]]

    def test_empty_input_returns_empty(self):
        assert pad_jagged([]) == []

    def test_already_rectangular_is_unchanged(self):
        rect = [[1, 2], [3, 4]]
        assert pad_jagged(rect) == [[1, 2], [3, 4]]

    def test_does_not_mutate_input(self):
        src = [[1], [2, 3]]
        pad_jagged(src)
        assert src == [[1], [2, 3]]

    def test_row_of_single_empty_grid(self):
        assert pad_jagged([[], [], []]) == [[], [], []]


# =========================================================================== read_values


class TestReadValues:
    def test_plain_uses_formatted_value_and_pads(self):
        services, _ = _make_service()
        rec = _wire_values_method(
            services,
            "batchGet",
            [{"valueRanges": [{"range": "Cliff!A1:C2", "values": [["a", "b", "c"], ["d"]]}]}],
        )
        out = read_values(services, SHEET_ID, ["Cliff!A1:C2"], render="plain")

        assert rec.calls[0]["valueRenderOption"] == "FORMATTED_VALUE"
        assert rec.calls[0]["ranges"] == ["Cliff!A1:C2"]
        assert rec.calls[0]["spreadsheetId"] == SHEET_ID
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "render": "plain",
            "major": "rows",
            "ranges": [
                {
                    "range": "Cliff!A1:C2",
                    "values": [["a", "b", "c"], ["d", "", ""]],
                }
            ],
        }

    def test_unformatted_render_option(self):
        services, _ = _make_service()
        rec = _wire_values_method(
            services, "batchGet", [{"valueRanges": [{"range": "S!A1", "values": [[1]]}]}]
        )
        read_values(services, SHEET_ID, ["S!A1"], render="unformatted")
        assert rec.calls[0]["valueRenderOption"] == "UNFORMATTED_VALUE"

    def test_formula_render_passthrough_literal(self):
        # Under FORMULA render, a non-formula cell returns its literal value, not a formula.
        services, _ = _make_service()
        _wire_values_method(
            services,
            "batchGet",
            [{"valueRanges": [{"range": "S!A1:A2", "values": [["=SUM(B:B)"], ["1234"]]}]}],
        )
        out = read_values(services, SHEET_ID, ["S!A1:A2"], render="formula")
        assert out["ranges"][0]["values"] == [["=SUM(B:B)"], ["1234"]]
        assert "computed" not in out["ranges"][0]

    def test_all_issues_two_passes_with_correct_options(self):
        services, _ = _make_service()
        rec = _wire_values_method(
            services,
            "batchGet",
            [
                {"valueRanges": [{"range": "S!A1:B1", "values": [["=SUM(B:B)", "=A1*2"]]}]},
                {"valueRanges": [{"range": "S!A1:B1", "values": [["1234", "2468"]]}]},
            ],
        )
        out = read_values(services, SHEET_ID, ["S!A1:B1"], render="all")

        # Two passes: FORMULA first (-> values), FORMATTED_VALUE second (-> computed).
        assert [c["valueRenderOption"] for c in rec.calls] == [
            "FORMULA",
            "FORMATTED_VALUE",
        ]
        entry = out["ranges"][0]
        assert entry["values"] == [["=SUM(B:B)", "=A1*2"]]
        assert entry["computed"] == [["1234", "2468"]]

    def test_all_pads_both_to_common_rectangle(self):
        # GOLDEN: the two render passes have DIFFERENT jagged extents; core must pad BOTH to
        # the element-wise-max rectangle so values[r][c] and computed[r][c] are index-aligned.
        services, _ = _make_service()
        formula_pass = [["=A", "=B"], ["=C"]]  # 2 rows, widths 2 and 1
        formatted_pass = [["1"], ["3", "4"], ["5"]]  # 3 rows, widths 1, 2, 1
        _wire_values_method(
            services,
            "batchGet",
            [
                {"valueRanges": [{"range": "S!A1:B3", "values": formula_pass}]},
                {"valueRanges": [{"range": "S!A1:B3", "values": formatted_pass}]},
            ],
        )
        out = read_values(services, SHEET_ID, ["S!A1:B3"], render="all")
        entry = out["ranges"][0]
        # Common rectangle = 3 rows x 2 cols for BOTH arrays.
        assert entry["values"] == [["=A", "=B"], ["=C", ""], ["", ""]]
        assert entry["computed"] == [["1", ""], ["3", "4"], ["5", ""]]
        # Index alignment invariant: equal dimensions across both arrays.
        assert len(entry["values"]) == len(entry["computed"]) == 3
        assert all(
            len(v) == len(c) == 2
            for v, c in zip(entry["values"], entry["computed"])
        )

    def test_multi_range(self):
        services, _ = _make_service()
        _wire_values_method(
            services,
            "batchGet",
            [
                {
                    "valueRanges": [
                        {"range": "S!A1", "values": [["x"]]},
                        {"range": "S!B1:B2", "values": [["y"], ["z"]]},
                    ]
                }
            ],
        )
        out = read_values(services, SHEET_ID, ["S!A1", "S!B1:B2"], render="plain")
        assert len(out["ranges"]) == 2
        assert out["ranges"][1]["values"] == [["y"], ["z"]]

    # ---- major (SPEC §6 P3): majorDimension passthrough ---------------------------------

    def test_major_defaults_to_rows_and_is_not_sent(self):
        # Default rows: Google's own default, so core does NOT send a majorDimension param
        # (keeps the request minimal and the existing golden calls unchanged).
        services, _ = _make_service()
        rec = _wire_values_method(
            services, "batchGet", [{"valueRanges": [{"range": "S!A1", "values": [["x"]]}]}]
        )
        read_values(services, SHEET_ID, ["S!A1"], render="plain")
        assert "majorDimension" not in rec.calls[0]

    def test_major_columns_sends_column_major(self):
        services, _ = _make_service()
        rec = _wire_values_method(
            services,
            "batchGet",
            [{"valueRanges": [{"range": "S!A1:B2", "values": [["a", "d"], ["b", "e"]]}]}],
        )
        out = read_values(services, SHEET_ID, ["S!A1:B2"], render="plain", major="columns")
        assert rec.calls[0]["majorDimension"] == "COLUMNS"
        assert out["major"] == "columns"
        # The grid is whatever Google returns (column-major), padded to a rectangle.
        assert out["ranges"][0]["values"] == [["a", "d"], ["b", "e"]]

    def test_major_rows_explicit_sends_rows(self):
        services, _ = _make_service()
        rec = _wire_values_method(
            services, "batchGet", [{"valueRanges": [{"range": "S!A1", "values": [["x"]]}]}]
        )
        out = read_values(services, SHEET_ID, ["S!A1"], render="plain", major="rows")
        # Explicit rows is the default; core omits the param (rows is Google's default).
        assert "majorDimension" not in rec.calls[0]
        assert out["major"] == "rows"

    def test_major_applies_to_both_passes_of_render_all(self):
        services, _ = _make_service()
        rec = _wire_values_method(
            services,
            "batchGet",
            [
                {"valueRanges": [{"range": "S!A1:B1", "values": [["=A"], ["=B"]]}]},
                {"valueRanges": [{"range": "S!A1:B1", "values": [["1"], ["2"]]}]},
            ],
        )
        read_values(services, SHEET_ID, ["S!A1:B1"], render="all", major="columns")
        assert [c["majorDimension"] for c in rec.calls] == ["COLUMNS", "COLUMNS"]

    def test_bad_major_raises(self):
        services, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            read_values(services, SHEET_ID, ["S!A1"], render="plain", major="diagonal")
        assert exc.value.code == "bad_major"

    # ---- data_filters (SPEC §6 P2): metadata-addressed reads via batchGetByDataFilter --------

    def test_data_filters_developer_metadata_lookup_uses_by_data_filter(self):
        # A symbolic developerMetadataLookup selector reads via batchGetByDataFilter and the wrapped
        # valueRange is unwrapped to the same flat shape as a literal-ranges read.
        services, _ = _make_service()
        rec = _wire_values_method(
            services,
            "batchGetByDataFilter",
            [
                {
                    "valueRanges": [
                        {
                            "dataFilters": [
                                {"developerMetadataLookup": {"metadataKey": "block:totals"}}
                            ],
                            "valueRange": {"range": "S!A1:B1", "values": [["x", "y"]]},
                        }
                    ]
                }
            ],
        )
        out = read_values(
            services,
            SHEET_ID,
            data_filters=[{"developerMetadataLookup": {"metadataKey": "block:totals"}}],
        )
        # The selector is passed straight through into the request body's dataFilters.
        assert rec.calls[0]["body"]["dataFilters"] == [
            {"developerMetadataLookup": {"metadataKey": "block:totals"}}
        ]
        assert rec.calls[0]["body"]["valueRenderOption"] == "FORMATTED_VALUE"
        assert out["ranges"][0]["values"] == [["x", "y"]]

    def test_data_filters_a1_selector_resolved_to_gridrange(self, monkeypatch):
        # An {"a1": ...} selector is resolved to a GridRange via addressing (the SAME conversion
        # the literal path uses), then sent as a gridRange dataFilter.
        from gsheets.core import dataselector

        monkeypatch.setattr(
            dataselector,
            "a1_to_gridrange",
            lambda services, sid, a1: {
                "sheetId": 0,
                "startRowIndex": 0,
                "endRowIndex": 1,
                "startColumnIndex": 0,
                "endColumnIndex": 2,
            },
        )
        services, _ = _make_service()
        rec = _wire_values_method(
            services,
            "batchGetByDataFilter",
            [
                {
                    "valueRanges": [
                        {"valueRange": {"range": "S!A1:B1", "values": [["x", "y"]]}}
                    ]
                }
            ],
        )
        read_values(services, SHEET_ID, data_filters=[{"a1": "S!A1:B1"}])
        assert rec.calls[0]["body"]["dataFilters"] == [
            {
                "gridRange": {
                    "sheetId": 0,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": 2,
                }
            }
        ]

    def test_data_filters_and_ranges_both_raises_conflicting_args(self):
        services, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            read_values(
                services,
                SHEET_ID,
                ["S!A1"],
                data_filters=[{"a1": "S!A1"}],
            )
        assert exc.value.code == "conflicting_args"

    def test_neither_ranges_nor_data_filters_raises_empty_ranges(self):
        services, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            read_values(services, SHEET_ID)
        assert exc.value.code == "empty_ranges"

    def test_data_filters_render_all_two_passes_by_data_filter(self):
        services, _ = _make_service()
        rec = _wire_values_method(
            services,
            "batchGetByDataFilter",
            [
                {"valueRanges": [{"valueRange": {"range": "S!A1", "values": [["=A"]]}}]},
                {"valueRanges": [{"valueRange": {"range": "S!A1", "values": [["1"]]}}]},
            ],
        )
        out = read_values(
            services,
            SHEET_ID,
            data_filters=[{"gridRange": {"sheetId": 0}}],
            render="all",
        )
        assert [c["body"]["valueRenderOption"] for c in rec.calls] == [
            "FORMULA",
            "FORMATTED_VALUE",
        ]
        assert out["ranges"][0]["values"] == [["=A"]]
        assert out["ranges"][0]["computed"] == [["1"]]

    def test_data_filters_empty_list_raises_bad_data_filters(self):
        services, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            read_values(services, SHEET_ID, data_filters=[])
        # An empty data_filters list with no ranges falls through to empty_ranges (nothing to read).
        assert exc.value.code == "empty_ranges"

    # ---- diff_only (ISSUES.md #12): de-duplicate ``computed`` against ``values`` --------

    def test_diff_only_omits_computed_when_range_fully_static(self):
        # A staticized region: every FORMATTED cell equals its FORMULA-pass literal, so
        # ``computed`` is pure duplication and is dropped entirely.
        services, _ = _make_service()
        _wire_values_method(
            services,
            "batchGet",
            [
                {"valueRanges": [{"range": "S!A1:B1", "values": [["185", "200"]]}]},
                {"valueRanges": [{"range": "S!A1:B1", "values": [["185", "200"]]}]},
            ],
        )
        out = read_values(services, SHEET_ID, ["S!A1:B1"], render="all", diff_only=True)
        entry = out["ranges"][0]
        assert entry["values"] == [["185", "200"]]
        assert "computed" not in entry  # nothing differs -> computed omitted

    def test_diff_only_keeps_differing_cells_and_nulls_the_equal_ones(self):
        # One live formula (computed differs) + one static literal (computed equal): the sparse
        # ``computed`` keeps the differing cell and nulls the equal one, preserving alignment.
        services, _ = _make_service()
        _wire_values_method(
            services,
            "batchGet",
            [
                {"valueRanges": [{"range": "S!A1:B1", "values": [["=SUM(B:B)", "200"]]}]},
                {"valueRanges": [{"range": "S!A1:B1", "values": [["1234", "200"]]}]},
            ],
        )
        out = read_values(services, SHEET_ID, ["S!A1:B1"], render="all", diff_only=True)
        entry = out["ranges"][0]
        assert entry["values"] == [["=SUM(B:B)", "200"]]
        assert entry["computed"] == [["1234", None]]
        # Still index-aligned: equal dimensions, holes mean "same as values".
        assert len(entry["values"][0]) == len(entry["computed"][0])

    def test_diff_only_treats_typed_literal_equal_to_its_formatted_string(self):
        # FORMULA render yields the typed number 185; FORMATTED yields "185". They are the same
        # value, so diff_only must treat them as equal (str-normalized) and drop ``computed``.
        services, _ = _make_service()
        _wire_values_method(
            services,
            "batchGet",
            [
                {"valueRanges": [{"range": "S!A1:B1", "values": [[185, "hello"]]}]},
                {"valueRanges": [{"range": "S!A1:B1", "values": [["185", "hello"]]}]},
            ],
        )
        out = read_values(services, SHEET_ID, ["S!A1:B1"], render="all", diff_only=True)
        assert "computed" not in out["ranges"][0]

    def test_diff_only_is_noop_for_non_all_render(self):
        # diff_only only governs the computed pass; with render!="all" there is no computed and
        # the flag is a silent no-op (no error, no computed key).
        services, _ = _make_service()
        _wire_values_method(
            services, "batchGet", [{"valueRanges": [{"range": "S!A1", "values": [["x"]]}]}]
        )
        out = read_values(services, SHEET_ID, ["S!A1"], render="plain", diff_only=True)
        assert out["ranges"][0] == {"range": "S!A1", "values": [["x"]]}

    def test_default_all_still_emits_full_computed(self):
        # Regression guard: default (diff_only=False) keeps the full duplicate computed matrix.
        services, _ = _make_service()
        _wire_values_method(
            services,
            "batchGet",
            [
                {"valueRanges": [{"range": "S!A1:B1", "values": [["185", "200"]]}]},
                {"valueRanges": [{"range": "S!A1:B1", "values": [["185", "200"]]}]},
            ],
        )
        out = read_values(services, SHEET_ID, ["S!A1:B1"], render="all")
        assert out["ranges"][0]["computed"] == [["185", "200"]]

    # ---- max_cells (ISSUES.md #13): fail fast instead of blowing the client token cap ----

    def test_max_cells_raises_result_too_large_when_exceeded(self):
        services, _ = _make_service()
        _wire_values_method(
            services,
            "batchGet",
            [{"valueRanges": [{"range": "S!A1:C2", "values": [["a", "b", "c"], ["d", "e", "f"]]}]}],
        )
        with pytest.raises(SheetsError) as exc:
            read_values(services, SHEET_ID, ["S!A1:C2"], render="plain", max_cells=5)
        assert exc.value.code == "result_too_large"
        assert "6" in exc.value.message  # the actual cell count is surfaced

    def test_max_cells_at_limit_passes(self):
        services, _ = _make_service()
        _wire_values_method(
            services,
            "batchGet",
            [{"valueRanges": [{"range": "S!A1:C2", "values": [["a", "b", "c"], ["d", "e", "f"]]}]}],
        )
        out = read_values(services, SHEET_ID, ["S!A1:C2"], render="plain", max_cells=6)
        assert out["ranges"][0]["values"] == [["a", "b", "c"], ["d", "e", "f"]]

    def test_max_cells_counts_across_all_ranges(self):
        services, _ = _make_service()
        _wire_values_method(
            services,
            "batchGet",
            [
                {
                    "valueRanges": [
                        {"range": "S!A1", "values": [["x"]]},
                        {"range": "S!B1:B2", "values": [["y"], ["z"]]},
                    ]
                }
            ],
        )
        with pytest.raises(SheetsError) as exc:
            read_values(services, SHEET_ID, ["S!A1", "S!B1:B2"], render="plain", max_cells=2)
        assert exc.value.code == "result_too_large"

    def test_max_cells_none_is_unlimited(self):
        services, _ = _make_service()
        _wire_values_method(
            services,
            "batchGet",
            [{"valueRanges": [{"range": "S!A1:C2", "values": [["a", "b", "c"], ["d", "e", "f"]]}]}],
        )
        out = read_values(services, SHEET_ID, ["S!A1:C2"], render="plain", max_cells=None)
        assert out["ok"] is True

    def test_bad_max_cells_raises(self):
        services, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            read_values(services, SHEET_ID, ["S!A1"], render="plain", max_cells=0)
        assert exc.value.code == "bad_max_cells"

    def test_empty_value_range_yields_empty_values(self):
        services, _ = _make_service()
        _wire_values_method(
            services, "batchGet", [{"valueRanges": [{"range": "S!Z1:Z9"}]}]
        )
        out = read_values(services, SHEET_ID, ["S!Z1:Z9"], render="plain")
        assert out["ranges"][0]["values"] == []

    def test_bad_render_raises(self):
        services, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            read_values(services, SHEET_ID, ["S!A1"], render="bogus")
        assert exc.value.code == "bad_render"

    def test_empty_ranges_raises(self):
        services, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            read_values(services, SHEET_ID, [], render="plain")
        assert exc.value.code == "empty_ranges"

    def test_http_error_is_classified(self):
        services, _ = _make_service()
        values_api = services.sheets.spreadsheets.return_value.values.return_value
        bad = MagicMock()
        bad.execute.side_effect = _make_http_error(403)
        values_api.batchGet.return_value = bad
        with pytest.raises(SheetsError) as exc:
            read_values(services, SHEET_ID, ["S!A1"], render="plain")
        assert exc.value.code == "google_api_error"
        assert exc.value.status == 403


# =========================================================================== write_values


class TestWriteValues:
    def test_user_entered_default_and_body_shape(self):
        services, _ = _make_service()
        rec = _wire_values_method(
            services,
            "batchUpdate",
            [
                {
                    "totalUpdatedCells": 1,
                    "totalUpdatedRows": 1,
                    "totalUpdatedColumns": 1,
                    "responses": [{"updatedRange": "Cliff!A1"}],
                }
            ],
        )
        out = write_values(
            services,
            SHEET_ID,
            [{"range": "Cliff!A1", "values": [["=SUM(B:B)"]]}],
        )
        body = rec.calls[0]["body"]
        assert body["valueInputOption"] == "USER_ENTERED"
        assert body["data"] == [{"range": "Cliff!A1", "values": [["=SUM(B:B)"]]}]
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "updatedRanges": ["Cliff!A1"],
            "updatedCells": 1,
            "updatedRows": 1,
            "updatedColumns": 1,
        }

    def test_raw_input_option(self):
        services, _ = _make_service()
        rec = _wire_values_method(
            services, "batchUpdate", [{"responses": [{"updatedRange": "S!A1"}]}]
        )
        write_values(services, SHEET_ID, [{"range": "S!A1", "values": [["x"]]}], input="raw")
        assert rec.calls[0]["body"]["valueInputOption"] == "RAW"

    def test_multi_range_totals_aggregated_from_responses(self):
        services, _ = _make_service()
        # No top-level totals -> core sums per-response counts.
        _wire_values_method(
            services,
            "batchUpdate",
            [
                {
                    "responses": [
                        {"updatedRange": "S!A1", "updatedCells": 1, "updatedRows": 1, "updatedColumns": 1},
                        {"updatedRange": "S!B1:B2", "updatedCells": 2, "updatedRows": 2, "updatedColumns": 1},
                    ]
                }
            ],
        )
        out = write_values(
            services,
            SHEET_ID,
            [
                {"range": "S!A1", "values": [["a"]]},
                {"range": "S!B1:B2", "values": [["b"], ["c"]]},
            ],
        )
        assert out["updatedRanges"] == ["S!A1", "S!B1:B2"]
        assert out["updatedCells"] == 3
        assert out["updatedRows"] == 3
        assert out["updatedColumns"] == 2

    def test_empty_data_raises(self):
        services, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            write_values(services, SHEET_ID, [])
        assert exc.value.code == "empty_payload"

    def test_bad_input_raises(self):
        services, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            write_values(services, SHEET_ID, [{"range": "S!A1", "values": [["x"]]}], input="weird")
        assert exc.value.code == "bad_input"

    def test_http_error_is_classified(self):
        services, _ = _make_service()
        values_api = services.sheets.spreadsheets.return_value.values.return_value
        bad = MagicMock()
        bad.execute.side_effect = _make_http_error(404)
        values_api.batchUpdate.return_value = bad
        with pytest.raises(SheetsError) as exc:
            write_values(services, SHEET_ID, [{"range": "S!A1", "values": [["x"]]}])
        assert exc.value.status == 404


# =========================================================================== append_rows


class TestAppendRows:
    def test_insert_rows_option_and_return_shape(self):
        services, _ = _make_service()
        rec = _wire_values_method(
            services,
            "append",
            [
                {
                    "tableRange": "Cliff!A1:D100",
                    "updates": {
                        "updatedRange": "Cliff!A101:D102",
                        "updatedRows": 2,
                        "updatedCells": 8,
                    },
                }
            ],
        )
        out = append_rows(
            services,
            SHEET_ID,
            "Cliff!A1:D100",
            [["w", "x", "y", "z"], ["1", "2", "3", "4"]],
        )
        call = rec.calls[0]
        assert call["valueInputOption"] == "USER_ENTERED"
        assert call["insertDataOption"] == "INSERT_ROWS"
        assert call["range"] == "Cliff!A1:D100"
        assert call["body"] == {"values": [["w", "x", "y", "z"], ["1", "2", "3", "4"]]}
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "updates": {
                "updatedRange": "Cliff!A101:D102",
                "updatedRows": 2,
                "updatedCells": 8,
            },
            "tableRange": "Cliff!A1:D100",
        }

    def test_raw_input(self):
        services, _ = _make_service()
        rec = _wire_values_method(services, "append", [{"updates": {}}])
        append_rows(services, SHEET_ID, "S!A1", [["x"]], input="raw")
        assert rec.calls[0]["valueInputOption"] == "RAW"

    def test_empty_values_raises(self):
        services, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            append_rows(services, SHEET_ID, "S!A1", [])
        assert exc.value.code == "empty_payload"

    def test_missing_updates_degrades_gracefully(self):
        services, _ = _make_service()
        _wire_values_method(services, "append", [{}])
        out = append_rows(services, SHEET_ID, "S!A1", [["x"]])
        assert out["updates"] == {"updatedRange": None, "updatedRows": 0, "updatedCells": 0}
        assert out["tableRange"] is None

    def test_http_error_is_classified(self):
        services, _ = _make_service()
        values_api = services.sheets.spreadsheets.return_value.values.return_value
        bad = MagicMock()
        bad.execute.side_effect = _make_http_error(403)
        values_api.append.return_value = bad
        with pytest.raises(SheetsError):
            append_rows(services, SHEET_ID, "S!A1", [["x"]])


# =========================================================================== clear


class TestClear:
    def test_values_only_uses_batch_clear(self):
        services, _ = _make_service()
        rec = _wire_values_method(services, "batchClear", [{}])
        # Also wire batchUpdate so an accidental call would be observable.
        bu = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        out = clear(services, SHEET_ID, ["Cliff!A2:D100"])

        assert rec.calls[0]["body"] == {"ranges": ["Cliff!A2:D100"]}
        assert bu.calls == []  # no structural batchUpdate when only values cleared
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "clearedRanges": ["Cliff!A2:D100"],
            "cleared": {
                "values": True,
                "formats": False,
                "validation": False,
                "notes": False,
            },
        }

    def test_formats_routes_through_fields_mask_and_update_cells(self, monkeypatch):
        services, _ = _make_service()
        # Patch the sibling collaborators this unit calls but does not own.
        grid = {
            "sheetId": 0,
            "startRowIndex": 1,
            "endRowIndex": 100,
            "startColumnIndex": 0,
            "endColumnIndex": 4,
        }
        monkeypatch.setattr(values_mod, "a1_to_gridrange", lambda s, sid, a1: dict(grid))
        captured_payload = {}

        def fake_mask(payload):
            captured_payload.update(payload)
            return "userEnteredFormat"

        monkeypatch.setattr(values_mod, "build_fields_mask", fake_mask)

        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])
        # values=False so only the structural path runs.
        out = clear(services, SHEET_ID, ["Cliff!A2:D100"], values=False, formats=True)

        # The payload handed to build_fields_mask must contain ONLY the requested subfield.
        assert captured_payload == {"userEnteredFormat": {}}
        body = rec.calls[0]["body"]
        assert body == {
            "requests": [
                {"updateCells": {"range": grid, "fields": "userEnteredFormat"}}
            ]
        }
        assert out["cleared"] == {
            "values": False,
            "formats": True,
            "validation": False,
            "notes": False,
        }

    def test_validation_and_notes_payload(self, monkeypatch):
        services, _ = _make_service()
        monkeypatch.setattr(
            values_mod, "a1_to_gridrange", lambda s, sid, a1: {"sheetId": 1}
        )
        seen = {}

        def fake_mask(payload):
            seen.update(payload)
            return "dataValidation,note"

        monkeypatch.setattr(values_mod, "build_fields_mask", fake_mask)
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])

        clear(
            services,
            SHEET_ID,
            ["S!A1:A9"],
            values=False,
            validation=True,
            notes=True,
        )
        # validation -> dataValidation:{}, notes -> note:""
        assert seen == {"dataValidation": {}, "note": ""}
        assert rec.calls[0]["body"]["requests"][0]["updateCells"]["fields"] == "dataValidation,note"

    def test_values_plus_structural_issues_both_calls(self, monkeypatch):
        services, _ = _make_service()
        monkeypatch.setattr(
            values_mod, "a1_to_gridrange", lambda s, sid, a1: {"sheetId": 0}
        )
        monkeypatch.setattr(values_mod, "build_fields_mask", lambda p: "userEnteredFormat")
        bc = _wire_values_method(services, "batchClear", [{}])
        bu = _wire_spreadsheets_method(services, "batchUpdate", [{}])

        clear(services, SHEET_ID, ["S!A1:B2"], values=True, formats=True)

        assert len(bc.calls) == 1  # values cleared
        assert len(bu.calls) == 1  # formats cleared

    def test_multi_range_structural_builds_one_request_per_range(self, monkeypatch):
        services, _ = _make_service()
        monkeypatch.setattr(
            values_mod,
            "a1_to_gridrange",
            lambda s, sid, a1: {"sheetId": 0, "tag": a1},
        )
        monkeypatch.setattr(values_mod, "build_fields_mask", lambda p: "userEnteredFormat")
        rec = _wire_spreadsheets_method(services, "batchUpdate", [{}])

        clear(services, SHEET_ID, ["S!A1", "S!B1"], values=False, formats=True)
        reqs = rec.calls[0]["body"]["requests"]
        assert len(reqs) == 2
        assert reqs[0]["updateCells"]["range"]["tag"] == "S!A1"
        assert reqs[1]["updateCells"]["range"]["tag"] == "S!B1"

    def test_empty_ranges_raises(self):
        services, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            clear(services, SHEET_ID, [])
        assert exc.value.code == "empty_ranges"

    def test_nothing_to_clear_raises(self):
        services, _ = _make_service()
        with pytest.raises(SheetsError) as exc:
            clear(
                services,
                SHEET_ID,
                ["S!A1"],
                values=False,
                formats=False,
                validation=False,
                notes=False,
            )
        assert exc.value.code == "empty_payload"

    def test_http_error_is_classified(self):
        services, _ = _make_service()
        values_api = services.sheets.spreadsheets.return_value.values.return_value
        bad = MagicMock()
        bad.execute.side_effect = _make_http_error(403)
        values_api.batchClear.return_value = bad
        with pytest.raises(SheetsError) as exc:
            clear(services, SHEET_ID, ["S!A1"])
        assert exc.value.code == "google_api_error"
