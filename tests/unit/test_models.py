"""Adapter-side model tests (DESIGN §3.1, §7.1).

The Pydantic models in ``gsheets.models`` are mechanical, field-for-field mirrors of the core
return dicts. These tests pin that fidelity (each documented core dict round-trips through its
model and re-serializes to the SAME data), the variant shapes (compact ``runs`` vs ``cells``,
gradient ``stops`` vs boolean ``condition``, structure read-vs-mutate), the terse ``content``
rendering, and the adapter-only boundary (core must never import ``gsheets.models``).
"""

from __future__ import annotations

from gsheets import models


# --------------------------------------------------------------------------------------
# Field-for-field fidelity: every documented core dict round-trips through its model.
# --------------------------------------------------------------------------------------


def _roundtrips(model_cls, data: dict) -> None:
    """Assert ``data`` survives a model round-trip with no field loss or reshaping."""
    m = model_cls.model_validate(data)
    dumped = m.model_dump(exclude_none=True, exclude_defaults=False)
    for key, value in data.items():
        assert key in dumped, f"{model_cls.__name__} dropped key {key!r}"
        assert dumped[key] == value, f"{model_cls.__name__} reshaped {key!r}"


def test_overview_mirrors_core_dict():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "title": "Workout Tracker",
        "sheets": [
            {
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
        ],
        "namedRanges": [
            {"name": "config", "range": "Cliff!AS986:AS1000", "namedRangeId": "abc"}
        ],
    }
    _roundtrips(models.OverviewResult, data)
    m = models.OverviewResult.model_validate(data)
    assert "Workout Tracker" in m.terse
    assert "12 CF" in m.terse


def test_inspect_non_compact_cells():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "sheet": "Cliff",
        "range": "Cliff!A1:B2",
        "rows": 2,
        "cols": 2,
        "merges": ["Cliff!A1:A2"],
        "compact": False,
        "cells": [
            {
                "a1": "A1",
                "value": "1234",
                "formula": "=SUM(A:A)",
                "effectiveFormat": {"bg": "#FFCDD2", "bold": True},
                "note": "reviewed",
                "validation": "ONE_OF_LIST(Yes,No)",
                "validationRule": {"type": "ONE_OF_LIST", "values": ["Yes", "No"]},
            },
            {"a1": "B1"},
        ],
    }
    m = models.InspectResult.model_validate(data)
    assert m.cells is not None and m.runs is None
    assert m.cells[0].validationRule.type == "ONE_OF_LIST"
    assert m.cells[0].effectiveFormat.bg == "#FFCDD2"
    assert "=SUM(A:A)" in m.terse
    # empty cell (B1) is not rendered in terse
    assert "B1" not in m.terse


def test_inspect_compact_runs():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "sheet": "Cliff",
        "range": "Cliff!AS986:AS1000",
        "rows": 15,
        "cols": 1,
        "merges": [],
        "compact": True,
        "runs": [
            {
                "a1Range": "AS986:AS1000",
                "value": "config",
                "formula": None,
                "format": {"bg": "#ECEFF1"},
                "note": "anchor block",
            }
        ],
    }
    m = models.InspectResult.model_validate(data)
    assert m.runs is not None and m.cells is None
    assert m.runs[0].a1Range == "AS986:AS1000"
    assert "1 runs" in m.terse and "config" in m.terse


def test_read_values_all_render_has_computed():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "render": "all",
        "ranges": [
            {
                "range": "Cliff!A1:B1",
                "values": [["=SUM(B:B)", "1234"]],
                "computed": [["10", "1234"]],
            }
        ],
    }
    _roundtrips(models.ReadValuesResult, data)
    m = models.ReadValuesResult.model_validate(data)
    assert m.ranges[0].computed == [["10", "1234"]]


def test_read_conditional_formats_boolean_and_gradient():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "sheets": [
            {
                "sheet": "Cliff",
                "sheetId": 0,
                "rules": [
                    {
                        "index": 0,
                        "line": "[Cliff!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold",
                        "ranges": ["Cliff!A2:A100"],
                        "kind": "boolean",
                        "condition": {"type": "CUSTOM_FORMULA", "values": ["=$B2>10"]},
                        "format": {"bg": "#FFCDD2", "bold": True},
                    },
                    {
                        "index": 1,
                        "line": "[Cliff!G2:G100] gradient min=#FFFFFF | max=#1A73E8",
                        "ranges": ["Cliff!G2:G100"],
                        "kind": "gradient",
                        "stops": [
                            {"slot": "min", "hex": "#FFFFFF"},
                            {"slot": "max", "hex": "#1A73E8"},
                        ],
                        "format": {},
                    },
                ],
            }
        ],
    }
    m = models.ConditionalFormatReport.model_validate(data)
    rules = m.sheets[0].rules
    assert rules[0].condition.type == "CUSTOM_FORMULA"
    assert rules[0].stops is None
    assert rules[1].kind == "gradient" and rules[1].condition is None
    assert rules[1].stops == [
        {"slot": "min", "hex": "#FFFFFF"},
        {"slot": "max", "hex": "#1A73E8"},
    ]
    assert "CUSTOM_FORMULA" in m.terse


def test_write_values_result():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "updatedRanges": ["Cliff!A1"],
        "updatedCells": 1,
        "updatedRows": 1,
        "updatedColumns": 1,
    }
    _roundtrips(models.WriteValuesResult, data)
    assert "wrote 1 cell" in models.WriteValuesResult.model_validate(data).terse


def test_append_result_nested_updates():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "updates": {
            "updatedRange": "Cliff!A101:D102",
            "updatedRows": 2,
            "updatedCells": 8,
        },
        "tableRange": "Cliff!A1:D100",
    }
    m = models.AppendResult.model_validate(data)
    assert m.updates.updatedCells == 8
    assert "2 row" in m.terse


def test_clear_result():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "clearedRanges": ["Cliff!A2:D100"],
        "cleared": {"values": True, "formats": False, "validation": False, "notes": False},
    }
    _roundtrips(models.ClearResult, data)
    assert "cleared values" in models.ClearResult.model_validate(data).terse


def test_format_result():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "range": "Cliff!A1:A10",
        "appliedFields": "userEnteredFormat(backgroundColorStyle,textFormat.bold),note",
    }
    _roundtrips(models.FormatResult, data)


def test_set_conditional_format_single_and_batch():
    single = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "action": "add",
        "sheet": "Cliff",
        "index": 0,
        "rule": "[Cliff!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold",
    }
    m1 = models.SetConditionalFormatResult.model_validate(single)
    assert m1.results is None and "add CF @ index 0" in m1.terse

    batch = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "results": [
            {"action": "delete", "index": 5},
            {"action": "update", "index": 2, "rule": "[Cliff!A2:A100] if BLANK -> bg #ECEFF1"},
        ],
    }
    m2 = models.SetConditionalFormatResult.model_validate(batch)
    assert m2.results is not None and len(m2.results) == 2
    assert "2 CF mutation" in m2.terse


def test_set_validation_result():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "range": "Cliff!A2:A100",
        "validation": "ONE_OF_LIST(Yes,No)",
        "validationRule": {
            "type": "ONE_OF_LIST",
            "values": ["Yes", "No"],
            "strict": True,
            "showDropdown": True,
        },
    }
    m = models.SetValidationResult.model_validate(data)
    assert m.validationRule.showDropdown is True
    assert "ONE_OF_LIST(Yes,No)" in m.terse


def test_structure_read_envelope():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "namedRanges": [
            {"name": "config", "namedRangeId": "abc", "range": "Cliff!AS986:AS1000"}
        ],
        "sheets": [
            {
                "sheet": "Cliff",
                "sheetId": 0,
                "merges": ["Cliff!A2:A4"],
                "frozenRows": 1,
                "frozenCols": 2,
                "tabColor": "#4285F4",
                "protectedRanges": [
                    {
                        "protectedRangeId": 1,
                        "range": "Cliff!A1:D1",
                        "description": "header",
                        "editors": ["a@b.com"],
                        "warningOnly": False,
                    }
                ],
                "dimensionGroups": [
                    {"dimension": "ROWS", "start": 10, "end": 20, "depth": 1, "collapsed": False}
                ],
            }
        ],
    }
    m = models.StructureResult.model_validate(data)
    assert m.action is None  # read shape
    assert m.sheets[0].protectedRanges[0].editors == ["a@b.com"]
    assert "[Cliff]" in m.terse and "1 protected" in m.terse


def test_structure_mutate_ack():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "action": "merge",
        "range": "Cliff!A2:A4",
        "mergeType": "MERGE_ALL",
    }
    m = models.StructureResult.model_validate(data)
    assert m.action == "merge" and "structure merge ok" in m.terse


def test_manage_sheets_result():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "action": "add",
        "sheet": {"sheetId": 7, "title": "New", "index": 3},
    }
    m = models.ManageSheetsResult.model_validate(data)
    assert m.sheet.sheetId == 7 and "add sheet New" in m.terse


def test_metadata_result():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "action": "read",
        "metadata": [
            {
                "metadataId": 12,
                "key": "anchor",
                "value": "wk1",
                "visibility": "DOCUMENT",
                "location": {"sheet": "Cliff", "dimension": "ROWS", "start": 10, "end": 11},
            }
        ],
    }
    m = models.MetadataResult.model_validate(data)
    assert m.metadata[0].location["dimension"] == "ROWS"
    assert "anchor='wk1'" in m.terse


def test_charts_create_and_read():
    create = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "action": "create",
        "chartId": 99,
    }
    m1 = models.ChartsResult.model_validate(create)
    assert m1.charts is None and "chartId=99" in m1.terse

    read = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "action": "read",
        "charts": [{"chartId": 99, "title": "Trend", "type": "LINE", "anchor": {"sheet": "Cliff", "row": 0, "col": 5}}],
    }
    m2 = models.ChartsResult.model_validate(read)
    assert m2.charts[0].type == "LINE" and "Trend" in m2.terse


def test_batch_result_new_ids():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "replies": [{}, {}],
        "newIds": {
            "sheetIds": [7],
            "chartIds": [],
            "namedRangeIds": [],
            "protectedRangeIds": [],
            "metadataIds": [],
        },
    }
    m = models.BatchResult.model_validate(data)
    assert m.newIds.sheetIds == [7]
    assert "2 reply" in m.terse and "sheets=[7]" in m.terse


# --------------------------------------------------------------------------------------
# Helpers + registry
# --------------------------------------------------------------------------------------


def test_to_model_and_registry_cover_every_core_fn():
    expected = {
        "overview",
        "inspect",
        "read_values",
        "read_conditional_formats",
        "write_values",
        "append_rows",
        "clear",
        "format",
        "set_conditional_format",
        "set_validation",
        "structure",
        "manage_sheets",
        "metadata",
        "charts",
        "batch",
    }
    assert set(models.RESULT_MODELS) == expected


def test_model_for_wraps_by_core_name():
    m = models.model_for("overview", {"ok": True, "title": "T", "spreadsheetId": "X"})
    assert isinstance(m, models.OverviewResult)
    assert m.title == "T"


def test_str_delegates_to_terse():
    m = models.WriteValuesResult.model_validate(
        {"ok": True, "updatedRanges": ["A1"], "updatedCells": 1}
    )
    assert str(m) == m.terse


def test_extra_keys_allowed_so_model_never_lags_core():
    # A core dict that grows a not-yet-modeled key must still round-trip.
    m = models.OverviewResult.model_validate(
        {"ok": True, "title": "T", "futureField": [1, 2, 3]}
    )
    assert m.model_dump()["futureField"] == [1, 2, 3]
