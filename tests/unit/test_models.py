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
# v0.2 extensions (DESIGN §Extensions): rich reads, new structure keys, new result models.
# --------------------------------------------------------------------------------------


def test_inspect_cell_rich_text_hyperlink_pivot():
    # A cell carrying textFormatRuns + hyperlink + a pivot anchor (all per-cell-only).
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "sheet": "Dash",
        "range": "Dash!A1:A1",
        "rows": 1,
        "cols": 1,
        "merges": [],
        "compact": False,
        "cells": [
            {
                "a1": "A1",
                "value": "Click here then plain",
                "runs": [
                    {
                        "start": 0,
                        "text": "Click here",
                        "format": {"bold": True, "fg": "#1155CC"},
                        "link": "https://x",
                    },
                    {"start": 11, "text": "then plain"},
                ],
                "hyperlink": "https://x",
                "pivot": {
                    "source": "Data!A1:F500",
                    "rows": [
                        {"field": "Region", "sourceColumnOffset": 0, "showTotals": True}
                    ],
                    "columns": [{"field": "Quarter", "sourceColumnOffset": 2}],
                    "values": [
                        {"name": "Sum of Sales", "sourceColumnOffset": 4, "summarize": "SUM"}
                    ],
                    "filters": [{"sourceColumnOffset": 1, "visibleValues": ["X", "Y"]}],
                    "valueLayout": "HORIZONTAL",
                    "line": "pivot A1 <- Data!A1:F500",
                },
            },
            {"a1": "A2"},
        ],
    }
    m = models.InspectResult.model_validate(data)
    cell = m.cells[0]
    assert cell.runs[0].text == "Click here"
    assert cell.runs[0].format.bold is True and cell.runs[0].link == "https://x"
    assert cell.runs[1].start == 11 and cell.runs[1].format is None
    assert cell.hyperlink == "https://x"
    assert cell.pivot.source == "Data!A1:F500"
    assert cell.pivot.rows[0].field == "Region"
    assert cell.pivot.values[0].summarize == "SUM"
    assert cell.pivot.filters[0].visibleValues == ["X", "Y"]
    # A cell with no rich data leaves the new fields unset (per-cell-only emission).
    assert m.cells[1].runs is None and m.cells[1].hyperlink is None


def test_run_carries_rich_text_in_compact_mode():
    data = {
        "a1Range": "A1:A1",
        "value": "Click here",
        "runs": [{"start": 0, "text": "Click here", "link": "https://x"}],
        "hyperlink": "https://x",
    }
    r = models.Run.model_validate(data)
    assert r.runs[0].link == "https://x" and r.hyperlink == "https://x"


def test_structure_read_new_sheet_scoped_keys():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "namedRanges": [],
        "sheets": [
            {
                "sheet": "Sheet1",
                "sheetId": 0,
                "merges": [],
                "frozenRows": 1,
                "frozenCols": 0,
                "tables": [
                    {
                        "tableId": "abc",
                        "name": "Sales",
                        "range": "Sheet1!A1:F500",
                        "columns": [
                            {"name": "Region", "type": "TEXT"},
                            {
                                "name": "Status",
                                "type": "DROPDOWN",
                                "validation": "ONE_OF_LIST(Open,Closed)",
                            },
                        ],
                        "line": 'table "Sales" [Sheet1!A1:F500]',
                    }
                ],
                "basicFilter": {
                    "range": "Sheet1!A1:F500",
                    "sorted": [{"col": "C", "order": "ASCENDING"}],
                    "criteria": [
                        {"col": "B", "hidden": ["Closed"], "condition": "NUMBER_GREATER(0)"}
                    ],
                    "line": "basicFilter [Sheet1!A1:F500]",
                },
                "filterViews": [
                    {
                        "filterViewId": 123,
                        "title": "Open only",
                        "range": "Sheet1!A1:F500",
                        "criteria": [{"col": "B", "hidden": ["Closed"]}],
                        "line": 'filterView 123 "Open only"',
                    }
                ],
                "bandedRanges": [
                    {
                        "bandedRangeId": 7,
                        "range": "Sheet1!A1:F500",
                        "rowBanding": {
                            "header": "#4285F4",
                            "first": "#FFFFFF",
                            "second": "#E8F0FE",
                            "footer": None,
                        },
                        "columnBanding": None,
                        "line": "banding 7 [Sheet1!A1:F500]",
                    }
                ],
                "slicers": [
                    {
                        "slicerId": 4,
                        "title": "Region",
                        "range": "Data!A1:F500",
                        "columnIndex": 0,
                        "anchor": {"sheet": "Dash", "row": 0, "col": 8},
                        "criteria": "ONE_OF_LIST(X,Y)",
                        "line": 'slicer 4 "Region" col 0',
                    }
                ],
            }
        ],
    }
    m = models.StructureResult.model_validate(data)
    s = m.sheets[0]
    assert s.tables[0].columns[1].validation == "ONE_OF_LIST(Open,Closed)"
    assert s.basicFilter.sorted[0].col == "C"
    assert s.basicFilter.criteria[0].condition == "NUMBER_GREATER(0)"
    assert s.filterViews[0].filterViewId == 123
    assert s.bandedRanges[0].rowBanding.header == "#4285F4"
    assert s.bandedRanges[0].columnBanding is None
    assert s.slicers[0].anchor.col == 8 and s.slicers[0].columnIndex == 0
    # extra "line" keys survive the round-trip (extra="allow").
    assert m.sheets[0].tables[0].model_dump()["line"] == 'table "Sales" [Sheet1!A1:F500]'


def test_overview_gains_locale_and_timezone():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "title": "Budget",
        "locale": "en_US",
        "timeZone": "America/New_York",
        "sheets": [],
        "namedRanges": [],
    }
    _roundtrips(models.OverviewResult, data)
    m = models.OverviewResult.model_validate(data)
    assert m.locale == "en_US" and m.timeZone == "America/New_York"


def test_data_ops_find_replace_result():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "action": "find_replace",
        "sheet": "Sheet1",
        "valuesChanged": 3,
        "formulasChanged": 0,
        "rowsChanged": 2,
        "sheetsChanged": 1,
        "occurrencesChanged": 3,
    }
    _roundtrips(models.DataOpsResult, data)
    m = models.DataOpsResult.model_validate(data)
    assert "find_replace" in m.terse and "3 occurrences" in m.terse


def test_data_ops_copy_paste_and_delete_duplicates():
    cp = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "action": "copy_paste",
        "source": "Sheet1!A1:B2",
        "destination": "Sheet1!D1:E2",
    }
    m1 = models.DataOpsResult.model_validate(cp)
    assert "A1:B2 -> Sheet1!D1:E2" in m1.terse

    dd = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "action": "delete_duplicates",
        "range": "Sheet1!A1:A100",
        "duplicatesRemoved": 5,
    }
    _roundtrips(models.DataOpsResult, dd)
    assert "5 duplicates" in models.DataOpsResult.model_validate(dd).terse


def test_dimensions_write_and_read():
    write = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "action": "insert",
        "sheet": "Sheet1",
        "dimension": "ROWS",
        "start": 0,
        "end": 3,
    }
    _roundtrips(models.DimensionsResult, write)
    assert "insert ROWS[0:3] on Sheet1" in models.DimensionsResult.model_validate(write).terse

    read = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "action": "read",
        "sheet": "Sheet1",
        "hiddenRows": [3, 4, 5],
        "hiddenCols": [],
    }
    _roundtrips(models.DimensionsResult, read)
    m = models.DimensionsResult.model_validate(read)
    assert m.hiddenRows == [3, 4, 5] and "3 hidden row" in m.terse


def test_dimensions_move_echoes_destination():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "action": "move",
        "sheet": "Sheet1",
        "dimension": "COLUMNS",
        "start": 2,
        "end": 4,
        "destinationIndex": 0,
    }
    m = models.DimensionsResult.model_validate(data)
    assert "-> 0" in m.terse


def test_comments_result():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "comments": [
            {
                "id": "AAAA",
                "author": "Jane Doe",
                "content": "please verify Q3",
                "created": "2026-05-01T00:00:00Z",
                "modified": "2026-05-02T00:00:00Z",
                "resolved": False,
                "quoted": "1234",
                "anchorRaw": "kix.opaque",
                "replies": [{"author": "Bob", "content": "done", "action": "resolve"}],
                "line": 'comment AAAA by Jane Doe',
            }
        ],
    }
    m = models.CommentsResult.model_validate(data)
    c = m.comments[0]
    assert c.author == "Jane Doe" and c.quoted == "1234"
    assert c.anchorRaw == "kix.opaque"
    assert c.replies[0].action == "resolve"
    assert "1 comment" in m.terse and "AAAA by Jane Doe" in m.terse
    # empty comments → terse fallback
    assert "no comments" in models.CommentsResult.model_validate(
        {"ok": True, "comments": []}
    ).terse


def test_model_for_wraps_new_extension_fns():
    for name, cls in (
        ("data_ops", models.DataOpsResult),
        ("dimensions", models.DimensionsResult),
        ("comments", models.CommentsResult),
    ):
        assert isinstance(models.model_for(name, {"ok": True, "action": "x"}), cls)


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
        # v0.2 extension top-level core fns (DESIGN §Extensions)
        "data_ops",
        "dimensions",
        "comments",
        # v0.2 cross-file + export extensions (DESIGN §3.x / §3.3)
        "export",
        "read_many",
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


# --------------------------------------------------------------------------------------
# Terse-rendering branches: pin the exact ``content`` summary for each result model's
# under-exercised code paths (the base fallback, the empty/None branches, every NewIds
# bucket, the cross-file fan-out lines). These are the lines the MCP adapter ships as the
# human/AI-facing ``content`` block, so each assertion pins the literal rendering.
# --------------------------------------------------------------------------------------


def test_base_result_terse_fallback_is_classname_and_ok():
    # ``_Result`` itself (and any subclass that does NOT override ``terse``) falls back to the
    # class name plus the ``ok`` flag so a model is never content-less (DESIGN §3.1).
    assert models._Result(ok=True).terse == "_Result(ok=True)"
    assert models._Result(ok=False).terse == "_Result(ok=False)"


def test_read_values_terse_lists_each_range_row_count():
    # Per-range "N rows" rendering; values may be absent (None) -> counted as 0.
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "render": "formula",
        "ranges": [
            {"range": "Cliff!A1:B3", "values": [["=A1"], ["x"], ["y"]]},
            {"range": "Cliff!Z1:Z1"},  # no values key at all -> 0 rows, not a crash
        ],
    }
    m = models.ReadValuesResult.model_validate(data)
    terse = m.terse
    assert terse == (
        "read_values render=formula\n"
        "  Cliff!A1:B3: 3 rows\n"
        "  Cliff!Z1:Z1: 0 rows"
    )


def test_inspect_compact_terse_appends_merges_line():
    # Compact (runs) path WITH merges -> the trailing "merges:" line (models.py:628).
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "sheet": "Cliff",
        "range": "Cliff!A1:B2",
        "rows": 2,
        "cols": 2,
        "compact": True,
        "merges": ["Cliff!A1:A2", "Cliff!B1:B2"],
        "runs": [{"a1Range": "A1:A2", "value": "merged", "formula": None}],
    }
    m = models.InspectResult.model_validate(data)
    terse = m.terse
    assert "1 runs" in terse
    assert "  A1:A2: 'merged'" in terse
    assert terse.endswith("  merges: Cliff!A1:A2, Cliff!B1:B2")


def test_inspect_compact_terse_prefers_formula_over_value():
    # In a compact run the formula (when present) wins over the value in the rendering.
    data = {
        "ok": True,
        "range": "Cliff!A1:A1",
        "rows": 1,
        "cols": 1,
        "compact": True,
        "merges": [],
        "runs": [{"a1Range": "A1", "value": "10", "formula": "=SUM(B:B)"}],
    }
    m = models.InspectResult.model_validate(data)
    assert "  A1: '=SUM(B:B)'" in m.terse


def test_append_result_terse_none_updates_falls_back():
    # No ``updates`` block -> the generic "appended rows" fallback (models.py:703).
    m = models.AppendResult.model_validate(
        {"ok": True, "spreadsheetId": "<YOUR_SPREADSHEET_ID>"}
    )
    assert m.updates is None
    assert m.terse == "appended rows"


def test_format_result_terse_renders_range_and_fields():
    m = models.FormatResult.model_validate(
        {
            "ok": True,
            "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
            "range": "Cliff!A1:A10",
            "appliedFields": "userEnteredFormat.backgroundColorStyle,borders",
        }
    )
    assert m.terse == (
        "formatted Cliff!A1:A10: userEnteredFormat.backgroundColorStyle,borders"
    )


def test_set_validation_terse_cleared_when_validation_none():
    # validation absent -> "cleared validation on <range>" (models.py:769); the round-trip of a
    # clear ack carries only the range.
    cleared = models.SetValidationResult.model_validate(
        {"ok": True, "spreadsheetId": "<YOUR_SPREADSHEET_ID>", "range": "Cliff!A2:A100"}
    )
    assert cleared.validation is None
    assert cleared.terse == "cleared validation on Cliff!A2:A100"


def test_batch_result_terse_captures_every_newids_bucket():
    # Exercise EACH NewIds bucket branch in the terse rendering (models.py:889/891/893/895):
    # chartIds, namedRangeIds, protectedRangeIds, metadataIds (sheetIds covered elsewhere).
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "replies": [{}, {}, {}],
        "newIds": {
            "sheetIds": [7],
            "chartIds": [42],
            "namedRangeIds": ["nr_abc"],
            "protectedRangeIds": [3],
            "metadataIds": [99],
        },
    }
    m = models.BatchResult.model_validate(data)
    terse = m.terse
    assert terse.startswith("batch: 3 reply(ies) | newIds: ")
    # Every populated bucket is summarized, in declaration order.
    assert "sheets=[7]" in terse
    assert "charts=[42]" in terse
    assert "named=['nr_abc']" in terse
    assert "protected=[3]" in terse
    assert "metadata=[99]" in terse


def test_batch_result_terse_omits_newids_when_all_buckets_empty():
    # A NewIds with every bucket empty captures nothing -> no " | newIds:" suffix.
    m = models.BatchResult.model_validate(
        {"ok": True, "replies": [{}], "newIds": {}}
    )
    assert m.terse == "batch: 1 reply(ies)"


def test_batch_result_terse_no_newids_block():
    # newIds absent entirely -> bare reply count.
    m = models.BatchResult.model_validate({"ok": True, "replies": []})
    assert m.terse == "batch: 0 reply(ies)"


def test_dimensions_terse_length_branch_for_append():
    # An append-style verb echoes ``length`` (not start/end) -> " xN" span (models.py:994-995).
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "action": "append",
        "sheet": "Sheet1",
        "dimension": "ROWS",
        "length": 10,
    }
    m = models.DimensionsResult.model_validate(data)
    # start/end absent so the [a:b] span is skipped in favor of the xN length form.
    assert m.terse == "dimensions append ROWS x10 on Sheet1"


def test_dimensions_terse_dimension_only_no_span_numbers():
    # dimension present but neither start/end NOR length -> bare dimension token, no span.
    data = {
        "ok": True,
        "action": "resize",
        "sheet": "Sheet1",
        "dimension": "COLUMNS",
        "pixelSize": 120,
    }
    m = models.DimensionsResult.model_validate(data)
    assert m.terse == "dimensions resize COLUMNS on Sheet1"


def test_comments_terse_deleted_branch():
    m = models.CommentsResult.model_validate(
        {"ok": True, "spreadsheetId": "<YOUR_SPREADSHEET_ID>", "commentId": "C1", "deleted": True}
    )
    assert m.terse == "deleted comment C1"


def test_comments_terse_resolved_branch():
    m = models.CommentsResult.model_validate(
        {
            "ok": True,
            "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
            "commentId": "C2",
            "resolved": True,
            "reply": {"author": "Bob", "content": "lgtm", "action": "resolve"},
        }
    )
    # resolved takes precedence over the reply branch.
    assert m.terse == "resolved comment C2"


def test_comments_terse_reply_branch():
    m = models.CommentsResult.model_validate(
        {
            "ok": True,
            "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
            "commentId": "C3",
            "reply": {"author": "Ann", "content": "thanks"},
        }
    )
    assert m.terse == "replied to comment C3"


def test_comments_terse_created_branch():
    m = models.CommentsResult.model_validate(
        {
            "ok": True,
            "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
            "comment": {"id": "C4", "author": "Ann", "content": "new thread"},
        }
    )
    assert m.terse == "created comment C4: 'new thread'"


def test_comments_terse_created_branch_empty_content():
    # A created comment with no content renders the empty string (the ``or ''`` fallback).
    m = models.CommentsResult.model_validate(
        {"ok": True, "comment": {"id": "C5", "author": "Ann"}}
    )
    assert m.terse == "created comment C5: ''"


def test_export_result_terse_renders_path_and_bytes():
    data = {
        "ok": True,
        "spreadsheetId": "<YOUR_SPREADSHEET_ID>",
        "format": "csv",
        "mimeType": "text/csv",
        "path": "/tmp/out.csv",
        "bytes": 2048,
    }
    _roundtrips(models.ExportResult, data)
    m = models.ExportResult.model_validate(data)
    assert m.terse == "exported csv -> /tmp/out.csv (2048 bytes)"


def test_read_many_terse_summary_values_and_error_entries():
    # All three result-entry shapes in ONE fan-out: a summary success, a values success, and a
    # captured per-file error (models.py:1086-1101). Pins the exact per-entry rendering and that
    # a top-level ok:True still surfaces the embedded ERROR line.
    data = {
        "ok": True,
        "mode": "values",
        "count": 3,
        "results": [
            {
                "ok": True,
                "spreadsheetId": "SID_SUMMARY",
                "title": "Budget",
                "sheets": [{"sheetId": 0}, {"sheetId": 1}],
            },
            {
                "ok": True,
                "spreadsheetId": "SID_VALUES",
                "ranges": [
                    {"range": "Sheet1!A1:B2", "values": [["a", "b"], ["c", "d"]]},
                    {"range": "Sheet1!C1", "values": [["x"]]},
                ],
            },
            {
                "spreadsheetId": "SID_BAD",
                "ok": False,
                "error": {"code": "google_api_error", "message": "not found"},
            },
        ],
    }
    m = models.ReadManyResult.model_validate(data)
    lines = m.terse.splitlines()
    assert lines[0] == "read_many mode=values: 3 result(s)"
    # summary entry: title + sheet count.
    assert lines[1] == "  SID_SUMMARY: Budget (2 sheet(s))"
    # values entry: total rows across N ranges (2 + 1 = 3 rows over 2 ranges).
    assert lines[2] == "  SID_VALUES: 3 row(s) across 2 range(s)"
    # error entry: the captured failure is surfaced inline.
    assert lines[3] == "  SID_BAD: ERROR google_api_error: not found"


def test_read_many_terse_untitled_summary_and_missing_id():
    # A summary entry with no title -> "(untitled)"; a malformed entry with no id -> "?".
    data = {
        "ok": True,
        "mode": "summary",
        "count": 1,
        "results": [{"ok": True, "sheets": []}],  # no spreadsheetId, no title
    }
    m = models.ReadManyResult.model_validate(data)
    assert m.terse == "read_many mode=summary: 1 result(s)\n  ?: (untitled) (0 sheet(s))"
