"""Unit tests for ``gsheets.core.tables`` (DESIGN §X.0c, §X.3/§X.4; analysis #3).

All tests run against a MOCKED Sheets service — no network. Two flavours:

- GOLDEN-MASTER for :func:`serialize_table`: representative Google ``Table`` JSON in, assert
  the EXACT flattened read shape + terse condformat-style line out (incl. a DROPDOWN column
  whose ``dataValidationRule`` reuses the ``ValidationRule`` one-liner ``inspect`` surfaces).
- OUTBOUND-REQUEST assertions for the ``build_*`` builders: assert the exact ``addTable`` /
  ``updateTable`` / ``deleteTable`` ``batchUpdate`` request dict (including the AUTO fields mask
  on update and the ``ONE_OF_LIST`` validation conversion on DROPDOWN columns).

Addressing (A1 <-> GridRange) is the real implemented layer; its sheet-name resolution is
driven by wiring a ``spreadsheets().get`` recorder that returns a one-sheet index (``Sheet1``,
sheetId 0), so ``serialize_table`` resolves the GridRange -> ``Sheet1!A1:F500`` and the
builders resolve A1 -> GridRange deterministically.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gsheets.core import tables
from gsheets.core.errors import SheetsError
from gsheets.core.service import SheetsServices
from gsheets.core.tables import (
    build_add_table_request,
    build_delete_table_request,
    build_update_table_request,
    serialize_table,
)

GOLDEN_DIR = Path(__file__).parent / "golden"


def load_golden(name: str) -> dict:
    """Load a committed golden fixture (with or without the ``.json`` suffix)."""
    filename = name if name.endswith(".json") else f"{name}.json"
    return json.loads((GOLDEN_DIR / filename).read_text())


# --------------------------------------------------------------------------- helpers


class _Recorder:
    """Callable recording its kwargs; ``.execute()`` yields a queued response.

    Lets a test assert exactly what was sent to Google AND feed back a sheet index so the real
    addressing layer resolves names/ids without network.
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

    Default index: one sheet ``Sheet1`` (sheetId 0, index 0), which the real addressing layer
    uses to resolve both directions. The ``get`` recorder answers EVERY get call (the
    addressing cache may call it more than once).
    """
    if sheets_index is None:
        sheets_index = [{"properties": {"sheetId": 0, "title": "Sheet1", "index": 0}}]
    services = SheetsServices(sheets=MagicMock(name="sheets_v4"), drive=None)
    # The addressing layer issues ``spreadsheets().get(fields="sheets.properties(...)")``.
    rec = _Recorder([{"sheets": sheets_index}] * 8)
    services.sheets.spreadsheets.return_value.get = rec
    return services


SPREADSHEET_ID = "<YOUR_SPREADSHEET_ID>"


# =========================================================================== serialize


def test_serialize_table_golden():
    """Golden-master: Google Table JSON -> exact flattened shape + terse line."""
    golden = load_golden("table_serialize")
    services = _service_with_sheet_index()
    out = serialize_table(golden["table"], services, SPREADSHEET_ID)
    assert out == golden["expected"]


def test_serialize_table_line_dropdown_collapses_to_type_args():
    """A DROPDOWN column reads as ``Status:DROPDOWN(Open,Closed)`` in the terse line."""
    golden = load_golden("table_serialize")
    services = _service_with_sheet_index()
    out = serialize_table(golden["table"], services, SPREADSHEET_ID)
    assert "Status:DROPDOWN(Open,Closed)" in out["line"]
    # The structured column carries the SAME ValidationRule one-liner inspect surfaces.
    status_col = next(c for c in out["columns"] if c["name"] == "Status")
    assert status_col["validation"] == "ONE_OF_LIST(Open,Closed)"


def test_serialize_table_omits_unset_keys_and_empty_columns():
    """Unset ``tableId``/``name``/``range`` are omitted; columns is always present (possibly [])."""
    services = _service_with_sheet_index()
    out = serialize_table({"columnProperties": []}, services, SPREADSHEET_ID)
    assert "tableId" not in out
    assert "name" not in out
    assert "range" not in out
    assert out["columns"] == []
    # Line still renders deterministically with empty name/range/cols.
    assert out["line"] == 'table "" [] cols: '


def test_serialize_table_column_without_validation_has_no_validation_key():
    """A plain TEXT column carries name+type only — no ``validation`` key."""
    services = _service_with_sheet_index()
    table = {
        "tableId": "t1",
        "name": "T",
        "columnProperties": [{"columnName": "Region", "columnType": "TEXT"}],
    }
    out = serialize_table(table, services, SPREADSHEET_ID)
    assert out["columns"] == [{"name": "Region", "type": "TEXT"}]
    assert "validation" not in out["columns"][0]


def test_serialize_table_resolves_gridrange_to_a1():
    """The Google ``Table.range`` GridRange is resolved to a sheet-qualified A1 string."""
    services = _service_with_sheet_index()
    table = {
        "name": "T",
        "range": {
            "sheetId": 0,
            "startRowIndex": 1,
            "endRowIndex": 10,
            "startColumnIndex": 0,
            "endColumnIndex": 3,
        },
        "columnProperties": [],
    }
    out = serialize_table(table, services, SPREADSHEET_ID)
    assert out["range"] == "Sheet1!A2:C10"


def test_serialize_table_dropdown_one_of_range_validation_line():
    """A column validated against a range surfaces ``ONE_OF_RANGE(...)`` (source, not values)."""
    services = _service_with_sheet_index()
    table = {
        "name": "T",
        "columnProperties": [
            {
                "columnName": "Owner",
                "columnType": "DROPDOWN",
                "dataValidationRule": {
                    "condition": {
                        "type": "ONE_OF_RANGE",
                        "values": [{"userEnteredValue": "=Sheet1!Z1:Z10"}],
                    }
                },
            }
        ],
    }
    out = serialize_table(table, services, SPREADSHEET_ID)
    assert out["columns"][0]["validation"] == "ONE_OF_RANGE(Sheet1!Z1:Z10)"
    assert out["line"] == 'table "T" [] cols: Owner:DROPDOWN(Sheet1!Z1:Z10)'


def test_serialize_table_malformed_validation_is_skipped_not_fatal():
    """A column with a condition-less dataValidationRule still serializes (validation dropped)."""
    services = _service_with_sheet_index()
    table = {
        "name": "T",
        "columnProperties": [
            {"columnName": "X", "columnType": "DROPDOWN", "dataValidationRule": {}}
        ],
    }
    out = serialize_table(table, services, SPREADSHEET_ID)
    assert out["columns"][0] == {"name": "X", "type": "DROPDOWN"}


def test_serialize_table_rejects_non_dict():
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        serialize_table(["not", "a", "dict"], services, SPREADSHEET_ID)
    assert exc.value.code == "bad_table"


# =========================================================================== add_table


def test_build_add_table_request_basic():
    """addTable carries the resolved GridRange + columnProperties with positional indices."""
    services = _service_with_sheet_index()
    req = build_add_table_request(
        services,
        SPREADSHEET_ID,
        "Sheet1!A1:F500",
        {
            "name": "Sales",
            "columns": [
                {"name": "Region", "type": "TEXT"},
                {"name": "Units", "type": "DOUBLE"},
            ],
        },
    )
    assert req == {
        "addTable": {
            "table": {
                "name": "Sales",
                "range": {
                    "sheetId": 0,
                    "startRowIndex": 0,
                    "endRowIndex": 500,
                    "startColumnIndex": 0,
                    "endColumnIndex": 6,
                },
                "columnProperties": [
                    {"columnIndex": 0, "columnName": "Region", "columnType": "TEXT"},
                    {"columnIndex": 1, "columnName": "Units", "columnType": "DOUBLE"},
                ],
            }
        }
    }


def test_build_add_table_request_dropdown_converts_validation():
    """A DROPDOWN column's structured ValidationRule -> Google dataValidationRule."""
    services = _service_with_sheet_index()
    req = build_add_table_request(
        services,
        SPREADSHEET_ID,
        "Sheet1!A1:A100",
        {
            "name": "T",
            "columns": [
                {
                    "name": "Status",
                    "type": "DROPDOWN",
                    "validation": {"type": "ONE_OF_LIST", "values": ["Open", "Closed"]},
                }
            ],
        },
    )
    col = req["addTable"]["table"]["columnProperties"][0]
    assert col["columnIndex"] == 0
    assert col["columnName"] == "Status"
    assert col["columnType"] == "DROPDOWN"
    assert col["dataValidationRule"]["condition"] == {
        "type": "ONE_OF_LIST",
        "values": [{"userEnteredValue": "Open"}, {"userEnteredValue": "Closed"}],
    }
    # A Table column's dataValidationRule accepts ONLY ``condition`` — the cell-level
    # ``strict``/``showCustomUi`` subfields MUST be stripped or the API rejects the addTable
    # with ``Unknown name "strict"`` / ``Unknown name "showCustomUi"``.
    assert set(col["dataValidationRule"].keys()) == {"condition"}


def test_build_add_table_request_dropdown_requires_validation():
    """A DROPDOWN column without validation raises (per the Tables guide requirement)."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_add_table_request(
            services,
            SPREADSHEET_ID,
            "Sheet1!A1:A100",
            {"name": "T", "columns": [{"name": "Status", "type": "DROPDOWN"}]},
        )
    assert exc.value.code == "bad_table"
    assert "DROPDOWN" in exc.value.message


def test_build_add_table_request_requires_name():
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_add_table_request(services, SPREADSHEET_ID, "Sheet1!A1:F500", {})
    assert exc.value.code == "missing_param"


def test_build_add_table_request_rejects_unknown_column_type():
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_add_table_request(
            services,
            SPREADSHEET_ID,
            "Sheet1!A1:A10",
            {"name": "T", "columns": [{"name": "X", "type": "FANCY"}]},
        )
    assert exc.value.code == "bad_table"


def test_build_add_table_request_without_columns_omits_column_properties():
    services = _service_with_sheet_index()
    req = build_add_table_request(
        services, SPREADSHEET_ID, "Sheet1!A1:F500", {"name": "T"}
    )
    assert "columnProperties" not in req["addTable"]["table"]


# =========================================================================== update_table


def test_build_update_table_request_name_only_auto_masks():
    """update with name only -> fields mask is exactly ``name`` (tableId is not masked)."""
    services = _service_with_sheet_index()
    req = build_update_table_request(
        services, SPREADSHEET_ID, {"tableId": "abc", "name": "Renamed"}
    )
    assert req == {
        "updateTable": {
            "table": {"tableId": "abc", "name": "Renamed"},
            "fields": "name",
        }
    }


def test_build_update_table_request_range_resolves_and_masks():
    """update with a new range -> resolved GridRange + ``range`` in the mask."""
    services = _service_with_sheet_index()
    req = build_update_table_request(
        services, SPREADSHEET_ID, {"tableId": "abc", "range": "Sheet1!A1:C10"}
    )
    table = req["updateTable"]["table"]
    assert table["tableId"] == "abc"
    assert table["range"] == {
        "sheetId": 0,
        "startRowIndex": 0,
        "endRowIndex": 10,
        "startColumnIndex": 0,
        "endColumnIndex": 3,
    }
    assert req["updateTable"]["fields"] == "range"


def test_build_update_table_request_columns_mask_is_atomic():
    """updating columns masks the whole ``columnProperties`` array atomically."""
    services = _service_with_sheet_index()
    req = build_update_table_request(
        services,
        SPREADSHEET_ID,
        {"tableId": "abc", "columns": [{"name": "Region", "type": "TEXT"}]},
    )
    assert req["updateTable"]["table"]["columnProperties"] == [
        {"columnIndex": 0, "columnName": "Region", "columnType": "TEXT"}
    ]
    assert req["updateTable"]["fields"] == "columnProperties"


def test_build_update_table_request_multi_field_mask_order():
    """A multi-field update masks each changed field (insertion order)."""
    services = _service_with_sheet_index()
    req = build_update_table_request(
        services,
        SPREADSHEET_ID,
        {
            "tableId": "abc",
            "name": "N",
            "range": "Sheet1!A1:B2",
            "columns": [{"name": "C", "type": "TEXT"}],
        },
    )
    assert req["updateTable"]["fields"] == "name,range,columnProperties"


def test_build_update_table_request_requires_table_id():
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_update_table_request(services, SPREADSHEET_ID, {"name": "N"})
    assert exc.value.code == "missing_param"


def test_build_update_table_request_no_changes_is_empty_payload():
    """tableId only (nothing to change) refuses a no-op write."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_update_table_request(services, SPREADSHEET_ID, {"tableId": "abc"})
    assert exc.value.code == "empty_payload"


def test_build_update_table_request_empty_columns_list_masks_columnproperties():
    """An explicit empty columns list is a real change (clear columns) -> masked, not no-op."""
    services = _service_with_sheet_index()
    req = build_update_table_request(
        services, SPREADSHEET_ID, {"tableId": "abc", "columns": []}
    )
    assert req["updateTable"]["table"]["columnProperties"] == []
    assert req["updateTable"]["fields"] == "columnProperties"


# =========================================================================== delete_table


def test_build_delete_table_request():
    assert build_delete_table_request({"tableId": "abc"}) == {
        "deleteTable": {"tableId": "abc"}
    }


def test_build_delete_table_request_requires_table_id():
    with pytest.raises(SheetsError) as exc:
        build_delete_table_request({})
    assert exc.value.code == "missing_param"


# =========================================================================== round-trip


def test_round_trip_serialize_matches_built_dropdown_validation():
    """A DROPDOWN built via build_add_table_request serializes back to its one-liner.

    Closes the CRUD loop: structured validation -> Google dataValidationRule (write) ->
    serialize_table reads it back to the SAME ``ONE_OF_LIST(Open,Closed)`` one-liner.
    """
    services = _service_with_sheet_index()
    req = build_add_table_request(
        services,
        SPREADSHEET_ID,
        "Sheet1!A1:A100",
        {
            "name": "T",
            "columns": [
                {
                    "name": "Status",
                    "type": "DROPDOWN",
                    "validation": {"type": "ONE_OF_LIST", "values": ["Open", "Closed"]},
                }
            ],
        },
    )
    built_table = req["addTable"]["table"]
    # Give it an id + read it back through serialize_table.
    built_table["tableId"] = "rt"
    out = serialize_table(built_table, services, SPREADSHEET_ID)
    assert out["columns"][0]["validation"] == "ONE_OF_LIST(Open,Closed)"
