"""Unit tests for ``gsheets.core.pivot`` (DESIGN §X.0b, §X.6 — Feature #6).

Golden-master style: representative Google ``PivotTable`` JSON in -> exact flattened dict /
terse line out. All against a MOCKED service (no network): the ONLY service interaction is the
``source`` ``GridRange`` -> A1 resolution (``gridrange_to_a1``), which issues the cached
``sheets.properties(sheetId,title,index)`` lookup. A ``_GetRecorder`` (mirroring
``test_reads``) answers that lookup with a fixed sheet index and records the call.

This module is pure test scaffolding: stdlib + ``pytest`` only; it never imports
``fastmcp``/``mcp``/``argparse``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gsheets.core.pivot import serialize_pivot
from gsheets.core.service import SheetsServices

# --------------------------------------------------------------------------- helpers


class _GetRecorder:
    """Answers the cached ``sheets.properties(sheetId,title,index)`` lookup for addressing.

    Any ``get`` whose ``fields`` matches the sheet-index mask returns the queued sheet index;
    everything else returns ``{}`` (the pivot serializer never issues a data get of its own).
    """

    _SHEET_INDEX_FIELDS = "sheets.properties(sheetId,title,index)"

    def __init__(self, sheet_index_response):
        self._sheet_index = sheet_index_response
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        resp = self._sheet_index if kwargs.get("fields") == self._SHEET_INDEX_FIELDS else {}
        request_obj = MagicMock(name="request")
        request_obj.execute.return_value = resp
        return request_obj


# Sheet index with the two tabs our fixtures reference (offsets are into the source range, so
# the tab names only matter for the source resolution).
_SHEET_INDEX = {
    "sheets": [
        {"properties": {"sheetId": 0, "title": "Dash", "index": 0}},
        {"properties": {"sheetId": 1, "title": "Data", "index": 1}},
    ]
}

SHEET_ID = "<TEST_SHEET_ID>"


def _make_service(sheet_index=None):
    """Build a ``SheetsServices`` whose ``spreadsheets().get`` routes to a ``_GetRecorder``."""
    sheets = MagicMock(name="sheets_v4")
    rec = _GetRecorder(sheet_index or _SHEET_INDEX)
    sheets.spreadsheets.return_value.get = rec
    return SheetsServices(sheets=sheets, drive=None), rec


# A representative Google ``PivotTable`` over Data!A1:F500: a "Data" tab is sheetId 1.
# source GridRange: rows [0,500), cols [0,6) -> Data!A1:F500.
_PIVOT_SOURCE = {
    "sheetId": 1,
    "startRowIndex": 0,
    "endRowIndex": 500,
    "startColumnIndex": 0,
    "endColumnIndex": 6,
}


def _golden_pivot() -> dict:
    """The canonical mixed pivot: a row, a column, a value, and a legacy-criteria filter."""
    return {
        "source": dict(_PIVOT_SOURCE),
        "rows": [
            {
                "sourceColumnOffset": 0,
                "showTotals": True,
                "sortOrder": "ASCENDING",
                "label": "Region",
            }
        ],
        "columns": [
            {
                "sourceColumnOffset": 2,
                "showTotals": True,
                "sortOrder": "SORT_ORDER_UNSPECIFIED",
                "label": "Quarter",
            }
        ],
        "values": [
            {
                "summarizeFunction": "SUM",
                "name": "Sum of Sales",
                "sourceColumnOffset": 4,
            }
        ],
        "criteria": {
            "1": {"visibleValues": ["X", "Y"]},
        },
        "valueLayout": "HORIZONTAL",
    }


# =========================================================================== golden master


class TestGoldenMaster:
    def test_full_pivot_flattens_to_exact_dict(self):
        services, _ = _make_service()
        out = serialize_pivot(_golden_pivot(), services, SHEET_ID)
        assert out == {
            "source": "Data!A1:F500",
            "rows": [
                {
                    "field": "Region",
                    "sourceColumnOffset": 0,
                    "showTotals": True,
                    "sortOrder": "ASCENDING",
                }
            ],
            "columns": [
                {
                    "field": "Quarter",
                    "sourceColumnOffset": 2,
                    "showTotals": True,
                }
            ],
            "values": [
                {
                    "name": "Sum of Sales",
                    "sourceColumnOffset": 4,
                    "summarize": "SUM",
                }
            ],
            "filters": [
                {
                    "sourceColumnOffset": 1,
                    "visibleValues": ["X", "Y"],
                }
            ],
            "valueLayout": "HORIZONTAL",
            "line": "pivot <- Data!A1:F500 | rows: Region | cols: Quarter "
            "| values: SUM(Sum of Sales) | filters: col1[X,Y]",
        }

    def test_terse_line_exact(self):
        services, _ = _make_service()
        out = serialize_pivot(_golden_pivot(), services, SHEET_ID)
        assert out["line"] == (
            "pivot <- Data!A1:F500 | rows: Region | cols: Quarter "
            "| values: SUM(Sum of Sales) | filters: col1[X,Y]"
        )

    def test_unspecified_sort_order_dropped(self):
        services, _ = _make_service()
        out = serialize_pivot(_golden_pivot(), services, SHEET_ID)
        # Quarter's SORT_ORDER_UNSPECIFIED must NOT surface a sortOrder key.
        assert "sortOrder" not in out["columns"][0]
        # Region's explicit ASCENDING must survive.
        assert out["rows"][0]["sortOrder"] == "ASCENDING"


# =========================================================================== source resolution


class TestSourceResolution:
    def test_source_gridrange_resolved_to_a1(self):
        services, rec = _make_service()
        out = serialize_pivot(_golden_pivot(), services, SHEET_ID)
        assert out["source"] == "Data!A1:F500"
        # The ONLY service call was the cached sheet-index lookup for addressing.
        assert any(
            c.get("fields") == "sheets.properties(sheetId,title,index)" for c in rec.calls
        )

    def test_missing_source_omits_key(self):
        services, _ = _make_service()
        pivot = _golden_pivot()
        del pivot["source"]
        out = serialize_pivot(pivot, services, SHEET_ID)
        assert "source" not in out
        # Line still renders, just without the source arrow.
        assert out["line"].startswith("pivot | rows:")

    def test_source_resolved_via_services_not_passthrough(self):
        # A different tab (Dash, sheetId 0) must resolve to that tab's name.
        services, _ = _make_service()
        pivot = _golden_pivot()
        pivot["source"] = {
            "sheetId": 0,
            "startRowIndex": 0,
            "endRowIndex": 10,
            "startColumnIndex": 0,
            "endColumnIndex": 2,
        }
        out = serialize_pivot(pivot, services, SHEET_ID)
        assert out["source"] == "Dash!A1:B10"


# =========================================================================== filters


class TestFilters:
    def test_legacy_criteria_map_flattened_and_sorted(self):
        services, _ = _make_service()
        pivot = _golden_pivot()
        # Out-of-order keys must come back ascending by offset.
        pivot["criteria"] = {
            "3": {"visibleValues": ["Closed"]},
            "1": {"visibleValues": ["X"]},
        }
        out = serialize_pivot(pivot, services, SHEET_ID)
        assert out["filters"] == [
            {"sourceColumnOffset": 1, "visibleValues": ["X"]},
            {"sourceColumnOffset": 3, "visibleValues": ["Closed"]},
        ]

    def test_criteria_without_visible_values_keeps_offset_only(self):
        services, _ = _make_service()
        pivot = _golden_pivot()
        # A condition-only filter (no visibleValues) -> just the offset, no visibleValues key.
        pivot["criteria"] = {"2": {"condition": {"type": "NUMBER_GREATER"}}}
        out = serialize_pivot(pivot, services, SHEET_ID)
        assert out["filters"] == [{"sourceColumnOffset": 2}]

    def test_modern_filter_specs_flattened(self):
        services, _ = _make_service()
        pivot = _golden_pivot()
        del pivot["criteria"]
        pivot["filterSpecs"] = [
            {
                "columnOffsetIndex": 1,
                "filterCriteria": {"visibleValues": ["X", "Y"]},
            }
        ]
        out = serialize_pivot(pivot, services, SHEET_ID)
        assert out["filters"] == [{"sourceColumnOffset": 1, "visibleValues": ["X", "Y"]}]

    def test_no_filters_omits_key(self):
        services, _ = _make_service()
        pivot = _golden_pivot()
        del pivot["criteria"]
        out = serialize_pivot(pivot, services, SHEET_ID)
        assert "filters" not in out
        assert "filters:" not in out["line"]


# =========================================================================== values


class TestValues:
    def test_value_without_summarize_renders_by_name(self):
        services, _ = _make_service()
        pivot = _golden_pivot()
        pivot["values"] = [{"name": "Raw", "sourceColumnOffset": 4}]
        out = serialize_pivot(pivot, services, SHEET_ID)
        assert out["values"] == [{"name": "Raw", "sourceColumnOffset": 4}]
        assert "values: Raw" in out["line"]

    def test_unspecified_summarize_dropped(self):
        services, _ = _make_service()
        pivot = _golden_pivot()
        pivot["values"] = [
            {
                "name": "Raw",
                "sourceColumnOffset": 4,
                "summarizeFunction": "PIVOT_STANDARD_VALUE_FUNCTION_UNSPECIFIED",
            }
        ]
        out = serialize_pivot(pivot, services, SHEET_ID)
        assert "summarize" not in out["values"][0]

    def test_formula_value_surfaced(self):
        services, _ = _make_service()
        pivot = _golden_pivot()
        pivot["values"] = [
            {"name": "Margin", "formula": "=Sales-Cost", "summarizeFunction": "SUM"}
        ]
        out = serialize_pivot(pivot, services, SHEET_ID)
        assert out["values"] == [
            {"name": "Margin", "formula": "=Sales-Cost", "summarize": "SUM"}
        ]

    def test_multiple_values_in_line(self):
        services, _ = _make_service()
        pivot = _golden_pivot()
        pivot["values"] = [
            {"name": "Sales", "sourceColumnOffset": 4, "summarizeFunction": "SUM"},
            {"name": "Units", "sourceColumnOffset": 5, "summarizeFunction": "COUNTA"},
        ]
        out = serialize_pivot(pivot, services, SHEET_ID)
        assert "values: SUM(Sales), COUNTA(Units)" in out["line"]


# =========================================================================== groups / labels


class TestGroups:
    def test_data_source_column_reference_used_as_field(self):
        services, _ = _make_service()
        pivot = _golden_pivot()
        # No sourceColumnOffset, no label -> fall back to the data-source column reference name.
        pivot["rows"] = [
            {
                "dataSourceColumnReference": {"name": "Region"},
                "showTotals": False,
            }
        ]
        out = serialize_pivot(pivot, services, SHEET_ID)
        assert out["rows"] == [{"field": "Region", "showTotals": False}]
        assert "rows: Region" in out["line"]

    def test_label_less_group_falls_back_to_col_offset_in_line(self):
        services, _ = _make_service()
        pivot = _golden_pivot()
        pivot["rows"] = [{"sourceColumnOffset": 3, "showTotals": True}]
        out = serialize_pivot(pivot, services, SHEET_ID)
        assert out["rows"] == [{"sourceColumnOffset": 3, "showTotals": True}]
        assert "rows: col3" in out["line"]

    def test_show_totals_false_preserved(self):
        services, _ = _make_service()
        pivot = _golden_pivot()
        pivot["rows"] = [{"label": "Region", "sourceColumnOffset": 0, "showTotals": False}]
        out = serialize_pivot(pivot, services, SHEET_ID)
        assert out["rows"][0]["showTotals"] is False


# =========================================================================== valueLayout / edges


class TestValueLayoutAndEdges:
    def test_value_layout_defaults_to_horizontal(self):
        services, _ = _make_service()
        pivot = _golden_pivot()
        del pivot["valueLayout"]
        out = serialize_pivot(pivot, services, SHEET_ID)
        assert out["valueLayout"] == "HORIZONTAL"

    def test_value_layout_vertical_preserved(self):
        services, _ = _make_service()
        pivot = _golden_pivot()
        pivot["valueLayout"] = "VERTICAL"
        out = serialize_pivot(pivot, services, SHEET_ID)
        assert out["valueLayout"] == "VERTICAL"

    def test_empty_pivot_minimal_output(self):
        services, _ = _make_service()
        out = serialize_pivot({}, services, SHEET_ID)
        # No source, no dimensions -> only valueLayout + a bare line.
        assert out == {"valueLayout": "HORIZONTAL", "line": "pivot"}

    def test_empty_collections_omit_keys(self):
        services, _ = _make_service()
        pivot = {
            "source": dict(_PIVOT_SOURCE),
            "rows": [],
            "columns": [],
            "values": [],
            "criteria": {},
        }
        out = serialize_pivot(pivot, services, SHEET_ID)
        assert set(out) == {"source", "valueLayout", "line"}
        assert out["line"] == "pivot <- Data!A1:F500"


# =========================================================================== purity


class TestPurity:
    def test_module_is_boundary_pure(self):
        """The pivot module must not have dragged any transport/CLI/pydantic symbol in."""
        import sys

        import gsheets.core.pivot  # noqa: F401

        forbidden = {"fastmcp", "mcp", "argparse", "pydantic"}
        leaked = forbidden & set(sys.modules)
        # ``mcp``/``pydantic`` may already be present from sibling adapter tests in the shared
        # interpreter; the authoritative check is the subprocess boundary guard. Here we only
        # assert the pivot module itself exposes no such symbol at module scope.
        mod = sys.modules["gsheets.core.pivot"]
        assert not any(hasattr(mod, name) for name in forbidden)
        _ = leaked  # referenced for clarity; real enforcement is test_boundary_guard


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
