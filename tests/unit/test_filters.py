"""Unit tests for ``gsheets.core.filters`` (DESIGN §X.0d, §X.3/§X.4, feature #4).

Two halves, both PURE (no network — these functions take A1 strings / resolved GridRanges and
plain params, never a live service):

* **Serializers** (``serialize_basic_filter`` / ``serialize_filter_view``): GOLDEN-MASTER style —
  representative Sheets-API ``BasicFilter`` / ``FilterView`` JSON in, assert the EXACT flattened
  dict + terse condformat-style ``line`` out. Covers the newer ``filterSpecs`` form, the legacy
  ``criteria`` map, ``visibleValues`` normalization, sort order words, and absolute column-index
  -> letter conversion.
* **Request builders** (``build_*_request``): assert the EXACT ``batchUpdate`` request body,
  including the AUTO-built ``fields`` mask on ``updateFilterView`` and col-letter -> absolute
  column-index conversion. ``services``/``spreadsheet_id`` are unused by the builders (uniform
  signature), so ``None`` is passed.

Condition strings reuse the SAME condformat condition (de)serializer, so a filter condition
reads/round-trips exactly like a CF condition (``NUMBER_GREATER(0)``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gsheets.core import filters
from gsheets.core.errors import SheetsError
from gsheets.core.filters import (
    build_add_filter_view_request,
    build_clear_basic_filter_request,
    build_delete_filter_view_request,
    build_set_basic_filter_request,
    build_update_filter_view_request,
    serialize_basic_filter,
    serialize_filter_view,
)

GOLDEN_DIR = Path(__file__).parent / "golden"

# Builders ignore services/spreadsheet_id; pass sentinels so a misuse would surface loudly.
_SVC = None
_SID = "<TEST_SPREADSHEET_ID>"


def load_golden() -> dict:
    return json.loads((GOLDEN_DIR / "filters_serialize.json").read_text())


# =========================================================================== column helpers


class TestColumnIndexLetter:
    @pytest.mark.parametrize(
        "index,letter",
        [(0, "A"), (1, "B"), (2, "C"), (25, "Z"), (26, "AA"), (27, "AB"), (701, "ZZ")],
    )
    def test_index_to_col_roundtrip(self, index, letter):
        assert filters._index_to_col(index) == letter
        assert filters._col_to_index(letter) == index

    def test_col_to_index_lowercase_and_dollar(self):
        assert filters._col_to_index("b") == 1
        assert filters._col_to_index("$C") == 2

    def test_index_to_col_negative_raises(self):
        with pytest.raises(SheetsError) as exc:
            filters._index_to_col(-1)
        assert exc.value.code == "bad_filter"

    def test_index_to_col_bool_raises(self):
        # bool is an int subclass — guard against True/False slipping in as a column index.
        with pytest.raises(SheetsError):
            filters._index_to_col(True)

    def test_col_to_index_empty_raises(self):
        with pytest.raises(SheetsError):
            filters._col_to_index("")

    def test_col_to_index_non_letters_raises(self):
        with pytest.raises(SheetsError) as exc:
            filters._col_to_index("B2")
        assert exc.value.code == "bad_filter"


# =========================================================================== serialize golden


class TestSerializeBasicFilterGolden:
    def test_basic_filter_golden(self):
        g = load_golden()["basic_filter"]
        out = serialize_basic_filter(g["google"], g["range_a1"])
        assert out == g["expected"]

    def test_basic_filter_line_exact(self):
        g = load_golden()["basic_filter"]
        out = serialize_basic_filter(g["google"], g["range_a1"])
        assert (
            out["line"]
            == "basicFilter [Sheet1!A1:F500] sort C asc | B: hide Closed, NUMBER_GREATER(0)"
        )

    def test_legacy_criteria_map_golden(self):
        g = load_golden()["basic_filter_legacy_criteria"]
        out = serialize_basic_filter(g["google"], g["range_a1"])
        assert out == g["expected"]

    def test_legacy_criteria_sorted_by_column_index(self):
        # Map order is non-deterministic; the serializer must emit ascending column order.
        bf = {"criteria": {"5": {"hiddenValues": ["z"]}, "0": {"hiddenValues": ["a"]}}}
        out = serialize_basic_filter(bf, "Sheet1!A1:Z10")
        cols = [c["col"] for c in out["criteria"]]
        assert cols == ["A", "F"]

    def test_no_sort_no_criteria_line_is_head_only(self):
        out = serialize_basic_filter({}, "Sheet1!A1:F500")
        assert out == {"range": "Sheet1!A1:F500", "line": "basicFilter [Sheet1!A1:F500]"}

    def test_range_omitted_when_none(self):
        out = serialize_basic_filter({"sortSpecs": [{"dimensionIndex": 0}]}, None)
        assert "range" not in out
        assert out["line"] == "basicFilter sort A asc"

    def test_descending_sort_word(self):
        out = serialize_basic_filter(
            {"sortSpecs": [{"dimensionIndex": 1, "sortOrder": "DESCENDING"}]},
            "S!A1:B2",
        )
        assert out["sorted"] == [{"col": "B", "order": "DESCENDING"}]
        assert "sort B desc" in out["line"]

    def test_visible_values_normalized(self):
        bf = {"filterSpecs": [{"columnIndex": 1, "filterCriteria": {"visibleValues": ["Open"]}}]}
        out = serialize_basic_filter(bf, "S!A1:F9")
        assert out["criteria"] == [{"col": "B", "visible": ["Open"]}]
        assert out["line"] == "basicFilter [S!A1:F9] B: show Open"

    def test_filterspecs_win_over_legacy_on_conflict(self):
        bf = {
            "filterSpecs": [{"columnIndex": 1, "filterCriteria": {"hiddenValues": ["new"]}}],
            "criteria": {"1": {"hiddenValues": ["old"]}},
        }
        out = serialize_basic_filter(bf, "S!A1:F9")
        assert out["criteria"] == [{"col": "B", "hidden": ["new"]}]

    def test_sortspec_without_index_skipped(self):
        out = serialize_basic_filter({"sortSpecs": [{"sortOrder": "ASCENDING"}]}, "S!A1:B2")
        assert "sorted" not in out

    def test_condition_reuses_condformat_serializer(self):
        bf = {
            "filterSpecs": [
                {
                    "columnIndex": 0,
                    "filterCriteria": {
                        "condition": {
                            "type": "TEXT_CONTAINS",
                            "values": [{"userEnteredValue": "done"}],
                        }
                    },
                }
            ]
        }
        out = serialize_basic_filter(bf, "S!A1:A9")
        assert out["criteria"][0]["condition"] == "TEXT_CONTAINS(done)"

    def test_not_a_dict_raises(self):
        with pytest.raises(SheetsError) as exc:
            serialize_basic_filter(["nope"], "S!A1:B2")  # type: ignore[arg-type]
        assert exc.value.code == "bad_filter"


class TestSerializeFilterViewGolden:
    def test_filter_view_golden(self):
        g = load_golden()["filter_view"]
        out = serialize_filter_view(g["google"], g["range_a1"])
        assert out == g["expected"]

    def test_filter_view_line_exact(self):
        g = load_golden()["filter_view"]
        out = serialize_filter_view(g["google"], g["range_a1"])
        assert (
            out["line"]
            == 'filterView 123 "Open only" [Sheet1!A1:F500] sort C desc | B: hide Closed'
        )

    def test_title_omitted_when_absent(self):
        fv = {"filterViewId": 7}
        out = serialize_filter_view(fv, "S!A1:B2")
        assert "title" not in out
        assert out["line"] == "filterView 7 [S!A1:B2]"

    def test_id_only_when_no_range(self):
        out = serialize_filter_view({"filterViewId": 7, "title": "X"}, None)
        assert out == {
            "filterViewId": 7,
            "title": "X",
            "line": 'filterView 7 "X"',
        }

    def test_not_a_dict_raises(self):
        with pytest.raises(SheetsError):
            serialize_filter_view("nope", "S!A1:B2")  # type: ignore[arg-type]


# =========================================================================== builders


_GR = {
    "sheetId": 0,
    "startRowIndex": 0,
    "endRowIndex": 500,
    "startColumnIndex": 0,
    "endColumnIndex": 6,
}


class TestBuildSetBasicFilter:
    def test_full_request_body(self):
        req = build_set_basic_filter_request(
            _SVC,
            _SID,
            _GR,
            {
                "sorted": [{"col": "C", "order": "ASCENDING"}],
                "criteria": [
                    {"col": "B", "hidden": ["Closed"], "condition": "NUMBER_GREATER(0)"}
                ],
            },
        )
        assert req == {
            "setBasicFilter": {
                "filter": {
                    "range": _GR,
                    "sortSpecs": [{"dimensionIndex": 2, "sortOrder": "ASCENDING"}],
                    "filterSpecs": [
                        {
                            "columnIndex": 1,
                            "filterCriteria": {
                                "hiddenValues": ["Closed"],
                                "condition": {
                                    "type": "NUMBER_GREATER",
                                    "values": [{"userEnteredValue": "0"}],
                                },
                            },
                        }
                    ],
                }
            }
        }

    def test_range_only(self):
        req = build_set_basic_filter_request(_SVC, _SID, _GR, {})
        assert req == {"setBasicFilter": {"filter": {"range": _GR}}}

    def test_structured_condition_dict_accepted(self):
        req = build_set_basic_filter_request(
            _SVC,
            _SID,
            _GR,
            {"criteria": [{"col": "A", "condition": {"type": "BLANK"}}]},
        )
        crit = req["setBasicFilter"]["filter"]["filterSpecs"][0]["filterCriteria"]
        assert crit == {"condition": {"type": "BLANK"}}

    def test_bad_grid_range_raises(self):
        with pytest.raises(SheetsError) as exc:
            build_set_basic_filter_request(_SVC, _SID, {"no": "sheetid"}, {})
        assert exc.value.code == "bad_range"

    def test_unknown_sort_order_raises(self):
        with pytest.raises(SheetsError) as exc:
            build_set_basic_filter_request(
                _SVC, _SID, _GR, {"sorted": [{"col": "A", "order": "SIDEWAYS"}]}
            )
        assert exc.value.code == "bad_filter"

    def test_sort_missing_col_raises(self):
        with pytest.raises(SheetsError):
            build_set_basic_filter_request(_SVC, _SID, _GR, {"sorted": [{"order": "ASCENDING"}]})

    def test_criterion_without_anything_raises(self):
        with pytest.raises(SheetsError) as exc:
            build_set_basic_filter_request(_SVC, _SID, _GR, {"criteria": [{"col": "B"}]})
        assert exc.value.code == "bad_filter"

    def test_col_letter_to_absolute_index(self):
        req = build_set_basic_filter_request(
            _SVC, _SID, _GR, {"criteria": [{"col": "AA", "hidden": ["x"]}]}
        )
        assert req["setBasicFilter"]["filter"]["filterSpecs"][0]["columnIndex"] == 26


class TestBuildClearBasicFilter:
    def test_body(self):
        assert build_clear_basic_filter_request(_SVC, _SID, 5) == {
            "clearBasicFilter": {"sheetId": 5}
        }

    def test_sheet_id_zero_is_valid(self):
        assert build_clear_basic_filter_request(_SVC, _SID, 0) == {
            "clearBasicFilter": {"sheetId": 0}
        }

    def test_none_sheet_id_raises(self):
        with pytest.raises(SheetsError) as exc:
            build_clear_basic_filter_request(_SVC, _SID, None)
        assert exc.value.code == "missing_param"


class TestBuildAddFilterView:
    def test_full_body(self):
        req = build_add_filter_view_request(
            _SVC,
            _SID,
            _GR,
            {"title": "Open only", "criteria": [{"col": "B", "hidden": ["Closed"]}]},
        )
        assert req == {
            "addFilterView": {
                "filter": {
                    "range": _GR,
                    "filterSpecs": [
                        {"columnIndex": 1, "filterCriteria": {"hiddenValues": ["Closed"]}}
                    ],
                    "title": "Open only",
                }
            }
        }

    def test_title_optional(self):
        req = build_add_filter_view_request(_SVC, _SID, _GR, {})
        assert req == {"addFilterView": {"filter": {"range": _GR}}}


class TestBuildUpdateFilterView:
    def test_title_and_criteria_mask(self):
        req = build_update_filter_view_request(
            _SVC,
            _SID,
            {"filterViewId": 123, "title": "New", "criteria": [{"col": "B", "hidden": ["X"]}]},
        )
        assert req == {
            "updateFilterView": {
                "filter": {
                    "filterViewId": 123,
                    "title": "New",
                    "filterSpecs": [
                        {"columnIndex": 1, "filterCriteria": {"hiddenValues": ["X"]}}
                    ],
                },
                "fields": "title,filterSpecs",
            }
        }

    def test_id_is_not_in_mask(self):
        req = build_update_filter_view_request(
            _SVC, _SID, {"filterViewId": 9, "title": "Z"}
        )
        assert req["updateFilterView"]["fields"] == "title"
        assert req["updateFilterView"]["filter"]["filterViewId"] == 9

    def test_sorted_only_mask(self):
        req = build_update_filter_view_request(
            _SVC, _SID, {"filterViewId": 9, "sorted": [{"col": "A"}]}
        )
        assert req["updateFilterView"]["fields"] == "sortSpecs"
        assert req["updateFilterView"]["filter"]["sortSpecs"] == [{"dimensionIndex": 0}]

    def test_range_move_via_grid_range_kwarg(self):
        req = build_update_filter_view_request(
            _SVC, _SID, {"filterViewId": 9, "title": "Z"}, grid_range=_GR
        )
        f = req["updateFilterView"]
        assert f["filter"]["range"] == _GR
        # range is a nested GridRange -> grouped mask; title is a leaf.
        assert "title" in f["fields"]
        assert "range(" in f["fields"]

    def test_empty_criteria_list_masks_filterspecs_to_empty(self):
        # Passing criteria=[] explicitly is a request to CLEAR the filter specs (empty array
        # is a present, non-no-op value once paired with the mask).
        req = build_update_filter_view_request(
            _SVC, _SID, {"filterViewId": 9, "criteria": []}
        )
        assert req["updateFilterView"]["filter"]["filterSpecs"] == []
        assert req["updateFilterView"]["fields"] == "filterSpecs"

    def test_missing_filter_view_id_raises(self):
        with pytest.raises(SheetsError) as exc:
            build_update_filter_view_request(_SVC, _SID, {"title": "X"})
        assert exc.value.code == "missing_param"

    def test_nothing_to_update_raises_empty_payload(self):
        with pytest.raises(SheetsError) as exc:
            build_update_filter_view_request(_SVC, _SID, {"filterViewId": 9})
        assert exc.value.code == "empty_payload"


class TestBuildDeleteFilterView:
    def test_body(self):
        assert build_delete_filter_view_request(_SVC, _SID, 123) == {
            "deleteFilterView": {"filterId": 123}
        }

    def test_none_raises(self):
        with pytest.raises(SheetsError) as exc:
            build_delete_filter_view_request(_SVC, _SID, None)
        assert exc.value.code == "missing_param"


# =========================================================================== round-trip parity


class TestSerializeBuildParity:
    """A read line's structured criteria fed back into a builder reproduce the Google body."""

    def test_basic_filter_read_then_write_roundtrip(self):
        google = load_golden()["basic_filter"]["google"]
        serialized = serialize_basic_filter(google, "Sheet1!A1:F500")
        # Feed the flattened sorted/criteria back into the builder over the same GridRange.
        rebuilt = build_set_basic_filter_request(
            _SVC,
            _SID,
            google["range"],
            {"sorted": serialized["sorted"], "criteria": serialized["criteria"]},
        )
        filt = rebuilt["setBasicFilter"]["filter"]
        assert filt["sortSpecs"] == google["sortSpecs"]
        assert filt["filterSpecs"] == google["filterSpecs"]
