"""Unit tests for ``gsheets.core.slicers`` (DESIGN §X.0f; analysis #16).

All tests run against a MOCKED Sheets service — no network. The headline is a GOLDEN-MASTER
for :func:`serialize_slicer`: representative Google ``Slicer`` JSON in, assert the EXACT
flattened read shape + terse condformat-style line out. Slicers are READ-ONLY in v1 (write stays
in the ``batch`` escape hatch), so there are no ``build_*`` request builders to assert.

Addressing (GridRange/GridCoordinate -> A1) is the real implemented layer; its sheet-name
resolution is driven by wiring a ``spreadsheets().get`` recorder that returns a TWO-sheet index
(``Data`` sheetId 0, ``Dash`` sheetId 1), so ``serialize_slicer`` resolves the data range to
``Data!A1:F500`` and the anchor cell (on a different tab) to ``Dash!I1`` deterministically.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gsheets.core import slicers
from gsheets.core.errors import SheetsError
from gsheets.core.service import SheetsServices
from gsheets.core.slicers import (
    build_add_slicer_request,
    build_delete_slicer_request,
    build_update_slicer_request,
    serialize_slicer,
)

GOLDEN_DIR = Path(__file__).parent / "golden"


def load_golden(name: str) -> dict:
    """Load a committed golden fixture (with or without the ``.json`` suffix)."""
    filename = name if name.endswith(".json") else f"{name}.json"
    return json.loads((GOLDEN_DIR / filename).read_text())


# --------------------------------------------------------------------------- helpers


class _Recorder:
    """Callable recording its kwargs; ``.execute()`` yields a queued response.

    Lets a test feed back a sheet index so the real addressing layer resolves names/ids
    without network (the addressing cache may call ``get`` more than once).
    """

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        resp = self._responses.pop(0) if self._responses else {}
        request_obj = MagicMock(name="request")
        request_obj.execute.return_value = resp
        return request_obj


def _service_with_sheet_index(
    *, sheets_index: list[dict] | None = None
) -> SheetsServices:
    """Build a mocked service whose ``spreadsheets().get`` returns a sheet index.

    Default index: two sheets — ``Data`` (sheetId 0, the slicer's data tab) and ``Dash``
    (sheetId 1, the slicer's anchor tab) — which the real addressing layer uses to resolve both
    directions. The ``get`` recorder answers EVERY get call.
    """
    if sheets_index is None:
        sheets_index = [
            {"properties": {"sheetId": 0, "title": "Data", "index": 0}},
            {"properties": {"sheetId": 1, "title": "Dash", "index": 1}},
        ]
    services = SheetsServices(sheets=MagicMock(name="sheets_v4"), drive=None)
    rec = _Recorder([{"sheets": sheets_index}] * 16)
    services.sheets.spreadsheets.return_value.get = rec
    return services


SPREADSHEET_ID = "<YOUR_SPREADSHEET_ID>"


# =========================================================================== serialize


def test_serialize_slicer_golden():
    """Golden-master: Google Slicer JSON -> exact flattened shape + terse line."""
    golden = load_golden("slicer_serialize")
    services = _service_with_sheet_index(sheets_index=golden["sheets_index"])
    out = serialize_slicer(golden["slicer"], services, SPREADSHEET_ID)
    assert out == golden["expected"]


def test_serialize_slicer_resolves_data_range_to_a1():
    """The slicer's ``spec.dataRange`` GridRange resolves to a sheet-qualified A1 string."""
    services = _service_with_sheet_index()
    slicer = {
        "slicerId": 1,
        "spec": {
            "dataRange": {
                "sheetId": 0,
                "startRowIndex": 1,
                "endRowIndex": 10,
                "startColumnIndex": 0,
                "endColumnIndex": 3,
            }
        },
    }
    out = serialize_slicer(slicer, services, SPREADSHEET_ID)
    assert out["range"] == "Data!A2:C10"


def test_serialize_slicer_anchor_on_different_sheet_resolves_name_and_a1():
    """The anchor GridCoordinate resolves its sheetId to a NAME on a different tab -> Dash!I1."""
    services = _service_with_sheet_index()
    slicer = {
        "slicerId": 4,
        "spec": {
            "title": "Region",
            "dataRange": {
                "sheetId": 0,
                "startRowIndex": 0,
                "endRowIndex": 500,
                "startColumnIndex": 0,
                "endColumnIndex": 6,
            },
            "columnIndex": 0,
        },
        "position": {
            "overlayPosition": {
                "anchorCell": {"sheetId": 1, "rowIndex": 0, "columnIndex": 8}
            }
        },
    }
    out = serialize_slicer(slicer, services, SPREADSHEET_ID)
    assert out["anchor"] == {"sheet": "Dash", "row": 0, "col": 8}
    # 0-based row 0 / col 8 -> 1-based A1 cell I1, qualified by the anchor sheet name.
    assert out["line"].endswith("@ Dash!I1")


def test_serialize_slicer_column_index_zero_is_surfaced():
    """columnIndex 0 is meaningful and must be surfaced (presence, not truthiness)."""
    services = _service_with_sheet_index()
    slicer = {"slicerId": 2, "spec": {"columnIndex": 0}}
    out = serialize_slicer(slicer, services, SPREADSHEET_ID)
    assert out["columnIndex"] == 0
    assert "col 0" in out["line"]


def test_serialize_slicer_criterion_reuses_condition_serializer():
    """A slicer's filterCriteria condition renders via the SHARED condformat serializer."""
    services = _service_with_sheet_index()
    slicer = {
        "slicerId": 3,
        "spec": {
            "title": "Sales",
            "columnIndex": 1,
            "filterCriteria": {
                "condition": {
                    "type": "NUMBER_GREATER",
                    "values": [{"userEnteredValue": "0"}],
                }
            },
        },
    }
    out = serialize_slicer(slicer, services, SPREADSHEET_ID)
    assert out["criteria"] == "NUMBER_GREATER(0)"
    assert out["line"].endswith("-> NUMBER_GREATER(0)")


def test_serialize_slicer_criterion_hidden_and_visible_values():
    """hiddenValues / visibleValues normalize to terse ``hide``/``show`` facets joined by '; '."""
    services = _service_with_sheet_index()
    slicer = {
        "slicerId": 5,
        "spec": {
            "filterCriteria": {
                "hiddenValues": ["Closed", "Void"],
                "visibleValues": ["Open"],
            }
        },
    }
    out = serialize_slicer(slicer, services, SPREADSHEET_ID)
    assert out["criteria"] == "hide Closed,Void; show Open"


def test_serialize_slicer_criterion_condition_and_hidden_combine():
    """A condition AND hidden values combine (condition first), joined by '; '."""
    services = _service_with_sheet_index()
    slicer = {
        "slicerId": 6,
        "spec": {
            "filterCriteria": {
                "condition": {
                    "type": "TEXT_CONTAINS",
                    "values": [{"userEnteredValue": "x"}],
                },
                "hiddenValues": ["q"],
            }
        },
    }
    out = serialize_slicer(slicer, services, SPREADSHEET_ID)
    assert out["criteria"] == "TEXT_CONTAINS(x); hide q"


def test_serialize_slicer_omits_unset_keys():
    """Unset slicerId/title/range/columnIndex/anchor/criteria are omitted; line still renders."""
    services = _service_with_sheet_index()
    out = serialize_slicer({}, services, SPREADSHEET_ID)
    for absent in ("slicerId", "title", "range", "columnIndex", "anchor", "criteria"):
        assert absent not in out
    # An empty slicer still produces a deterministic, parseable line head.
    assert out["line"] == "slicer"


def test_serialize_slicer_empty_filter_criteria_emits_no_criteria_key():
    """An empty filterCriteria (no facet) emits no ``criteria`` key (and no '-> ' on the line)."""
    services = _service_with_sheet_index()
    slicer = {"slicerId": 7, "spec": {"title": "T", "filterCriteria": {}}}
    out = serialize_slicer(slicer, services, SPREADSHEET_ID)
    assert "criteria" not in out
    assert "->" not in out["line"]


def test_serialize_slicer_anchor_without_sheet_id_uses_bare_cell():
    """An anchor cell lacking a sheetId renders a bare A1 cell (no sheet prefix) in the line."""
    services = _service_with_sheet_index()
    slicer = {
        "slicerId": 8,
        "spec": {},
        "position": {
            "overlayPosition": {"anchorCell": {"rowIndex": 2, "columnIndex": 1}}
        },
    }
    out = serialize_slicer(slicer, services, SPREADSHEET_ID)
    assert out["anchor"] == {"row": 2, "col": 1}
    # No sheet on the anchor -> bare B3 (0-based row 2 -> 1-based 3, col 1 -> B).
    assert out["line"].endswith("@ B3")


def test_serialize_slicer_anchor_quotes_sheet_name_with_space():
    """An anchor sheet whose title needs quoting renders 'My Dash'!I1 in the line."""
    services = _service_with_sheet_index(
        sheets_index=[
            {"properties": {"sheetId": 0, "title": "Data", "index": 0}},
            {"properties": {"sheetId": 2, "title": "My Dash", "index": 1}},
        ]
    )
    slicer = {
        "slicerId": 9,
        "spec": {},
        "position": {
            "overlayPosition": {
                "anchorCell": {"sheetId": 2, "rowIndex": 0, "columnIndex": 8}
            }
        },
    }
    out = serialize_slicer(slicer, services, SPREADSHEET_ID)
    assert out["anchor"]["sheet"] == "My Dash"
    assert out["line"].endswith("@ 'My Dash'!I1")


def test_serialize_slicer_dangling_anchor_sheet_id_falls_back_to_raw_id():
    """An anchor sheetId matching no sheet falls back to the raw id (never breaks the read)."""
    services = _service_with_sheet_index()
    slicer = {
        "slicerId": 10,
        "spec": {},
        "position": {
            "overlayPosition": {
                "anchorCell": {"sheetId": 999, "rowIndex": 0, "columnIndex": 0}
            }
        },
    }
    out = serialize_slicer(slicer, services, SPREADSHEET_ID)
    # Unknown sheetId -> raw id surfaced under ``sheet`` (a dangling/cross-sheet anchor).
    assert out["anchor"]["sheet"] == 999
    # The raw int id stringifies to a digit-leading token, which A1 addressing quotes -> '999'!A1.
    assert out["line"].endswith("@ '999'!A1")


def test_serialize_slicer_no_anchor_omits_anchor_and_at_segment():
    """No position/overlay/anchorCell -> no ``anchor`` key and no '@ ...' on the line."""
    services = _service_with_sheet_index()
    slicer = {"slicerId": 11, "spec": {"title": "T", "columnIndex": 0}}
    out = serialize_slicer(slicer, services, SPREADSHEET_ID)
    assert "anchor" not in out
    assert "@" not in out["line"]
    assert out["line"] == 'slicer 11 "T" col 0'


def test_serialize_slicer_anchor_top_left_row0_col0():
    """A top-left anchor (row 0, col 0) is fully surfaced and renders @ <Sheet>!A1."""
    services = _service_with_sheet_index()
    slicer = {
        "slicerId": 12,
        "spec": {},
        "position": {
            "overlayPosition": {
                "anchorCell": {"sheetId": 1, "rowIndex": 0, "columnIndex": 0}
            }
        },
    }
    out = serialize_slicer(slicer, services, SPREADSHEET_ID)
    assert out["anchor"] == {"sheet": "Dash", "row": 0, "col": 0}
    assert out["line"].endswith("@ Dash!A1")


def test_serialize_slicer_rejects_non_dict():
    """A non-dict slicer raises ``bad_slicer`` (never silently returns garbage)."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        serialize_slicer(["not", "a", "dict"], services, SPREADSHEET_ID)
    assert exc.value.code == "bad_slicer"


def test_serialize_slicer_spec_not_a_dict_is_tolerated():
    """A slicer whose ``spec`` is missing/None degrades to just the id + bare line."""
    services = _service_with_sheet_index()
    out = serialize_slicer({"slicerId": 13, "spec": None}, services, SPREADSHEET_ID)
    assert out == {"slicerId": 13, "line": "slicer 13"}


def test_serialize_slicer_full_line_order_is_id_title_col_range_anchor_criteria():
    """The terse line keeps the canonical segment order: id, title, col, [range], @ anchor -> crit."""
    golden = load_golden("slicer_serialize")
    services = _service_with_sheet_index(sheets_index=golden["sheets_index"])
    out = serialize_slicer(golden["slicer"], services, SPREADSHEET_ID)
    assert (
        out["line"]
        == 'slicer 4 "Region" col 0 [Data!A1:F500] @ Dash!I1 -> ONE_OF_LIST(West,East)'
    )


def test_serialize_slicer_is_json_serializable():
    """The flattened output is plain JSON-serializable (no Google nesting leaks through)."""
    golden = load_golden("slicer_serialize")
    services = _service_with_sheet_index(sheets_index=golden["sheets_index"])
    out = serialize_slicer(golden["slicer"], services, SPREADSHEET_ID)
    # Round-trips through json without error and is byte-stable.
    assert json.loads(json.dumps(out)) == out


# =========================================================================== build: add


def test_build_add_slicer_request_golden():
    """Golden-master: flat slicer spec -> exact Google ``addSlicer`` batchUpdate request dict.

    The two A1 references resolve through the REAL addressing layer (driven by the two-sheet
    index): ``Data!A1:F500`` -> the data GridRange; the single-cell anchor ``Dash!I1`` -> a
    GridCoordinate on the OTHER tab. The ``ONE_OF_LIST(West,East)`` criterion condition is built
    by the SHARED condformat condition builder.
    """
    golden = load_golden("slicer_addslicer_request")
    services = _service_with_sheet_index(sheets_index=golden["sheets_index"])
    request = build_add_slicer_request(services, SPREADSHEET_ID, golden["spec"])
    assert request == golden["expected"]


def test_build_add_slicer_request_resolves_anchor_to_grid_coordinate():
    """A single-cell A1 ``anchor`` collapses to a ``GridCoordinate`` (sheetId/rowIndex/columnIndex)."""
    services = _service_with_sheet_index()
    request = build_add_slicer_request(
        services,
        SPREADSHEET_ID,
        {"dataRange": "Data!A1:C10", "anchor": "Dash!I1"},
    )
    anchor = request["addSlicer"]["slicer"]["position"]["overlayPosition"]["anchorCell"]
    assert anchor == {"sheetId": 1, "rowIndex": 0, "columnIndex": 8}


def test_build_add_slicer_request_column_index_zero_kept():
    """``columnIndex`` 0 is meaningful and must survive into the spec (presence, not truthiness)."""
    services = _service_with_sheet_index()
    request = build_add_slicer_request(
        services,
        SPREADSHEET_ID,
        {"dataRange": "Data!A1:C10", "anchor": "Dash!A1", "columnIndex": 0},
    )
    assert request["addSlicer"]["slicer"]["spec"]["columnIndex"] == 0


def test_build_add_slicer_request_hidden_values_criteria():
    """A ``criteria`` with hidden values builds a ``FilterCriteria.hiddenValues`` (no condition)."""
    services = _service_with_sheet_index()
    request = build_add_slicer_request(
        services,
        SPREADSHEET_ID,
        {
            "dataRange": "Data!A1:C10",
            "anchor": "Dash!A1",
            "criteria": {"hidden": ["Closed", "Void"]},
        },
    )
    fc = request["addSlicer"]["slicer"]["spec"]["filterCriteria"]
    assert fc == {"hiddenValues": ["Closed", "Void"]}


def test_build_add_slicer_request_requires_data_range():
    """Missing ``dataRange`` raises ``missing_param`` (never builds a rangeless slicer)."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_add_slicer_request(services, SPREADSHEET_ID, {"anchor": "Dash!A1"})
    assert exc.value.code == "missing_param"


def test_build_add_slicer_request_requires_anchor():
    """Missing ``anchor`` raises ``missing_param`` (a slicer must be positioned)."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_add_slicer_request(
            services, SPREADSHEET_ID, {"dataRange": "Data!A1:C10"}
        )
    assert exc.value.code == "missing_param"


def test_build_add_slicer_request_rejects_multi_cell_anchor():
    """A multi-cell range passed as ``anchor`` raises ``bad_slicer`` (anchor must be ONE cell)."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_add_slicer_request(
            services,
            SPREADSHEET_ID,
            {"dataRange": "Data!A1:C10", "anchor": "Dash!I1:J5"},
        )
    assert exc.value.code == "bad_slicer"


def test_build_add_slicer_request_rejects_non_dict_spec():
    """A non-dict spec raises ``bad_slicer``."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_add_slicer_request(services, SPREADSHEET_ID, ["not", "a", "dict"])
    assert exc.value.code == "bad_slicer"


# =========================================================================== build: update


def test_build_update_slicer_request_auto_fields_mask():
    """``update`` builds an auto fields mask covering ONLY the changed keys (title + columnIndex)."""
    services = _service_with_sheet_index()
    request = build_update_slicer_request(
        services, SPREADSHEET_ID, 4, {"title": "New", "columnIndex": 2}
    )
    spec = request["updateSlicerSpec"]
    assert spec["slicerId"] == 4
    assert spec["spec"] == {"title": "New", "columnIndex": 2}
    # The mask covers exactly the two changed leaf fields (insertion order).
    assert spec["fields"] == "title,columnIndex"


def test_build_update_slicer_request_data_range_masks_atomically():
    """A changed ``dataRange`` masks as the WHOLE ``dataRange`` leaf, not its GridRange subfields."""
    services = _service_with_sheet_index()
    request = build_update_slicer_request(
        services, SPREADSHEET_ID, 4, {"dataRange": "Data!A1:C10"}
    )
    spec = request["updateSlicerSpec"]
    assert spec["fields"] == "dataRange"
    # The built spec carries the resolved GridRange (not the sentinel used only for masking).
    assert spec["spec"]["dataRange"]["sheetId"] == 0


def test_build_update_slicer_request_criteria_masks_atomically():
    """A changed ``criteria`` masks as the whole ``filterCriteria`` leaf and builds the condition."""
    services = _service_with_sheet_index()
    request = build_update_slicer_request(
        services,
        SPREADSHEET_ID,
        4,
        {"criteria": {"condition": "NUMBER_GREATER(0)"}},
    )
    spec = request["updateSlicerSpec"]
    assert spec["fields"] == "filterCriteria"
    assert spec["spec"]["filterCriteria"]["condition"]["type"] == "NUMBER_GREATER"


def test_build_update_slicer_request_requires_slicer_id():
    """A ``None`` slicer id raises ``missing_param``."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_update_slicer_request(services, SPREADSHEET_ID, None, {"title": "X"})
    assert exc.value.code == "missing_param"


def test_build_update_slicer_request_empty_spec_refused():
    """An empty spec (nothing to change) raises ``empty_payload`` (refuse a no-op)."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_update_slicer_request(services, SPREADSHEET_ID, 4, {})
    assert exc.value.code == "empty_payload"


# =========================================================================== build: delete


def test_build_delete_slicer_request_uses_embedded_object_id_space():
    """Delete maps to ``deleteEmbeddedObject`` (slicers share the embedded-object id space)."""
    assert build_delete_slicer_request(4) == {
        "deleteEmbeddedObject": {"objectId": 4}
    }


def test_build_delete_slicer_request_requires_id():
    """A ``None`` slicer id raises ``missing_param``."""
    with pytest.raises(SheetsError) as exc:
        build_delete_slicer_request(None)
    assert exc.value.code == "missing_param"
