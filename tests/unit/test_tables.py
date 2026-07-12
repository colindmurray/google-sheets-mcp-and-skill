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


def test_serialize_table_rejects_non_dict_column():
    """A non-dict entry inside ``columnProperties`` raises ``bad_table`` (per-column guard)."""
    services = _service_with_sheet_index()
    table = {
        "name": "T",
        "columnProperties": [{"columnName": "Region", "columnType": "TEXT"}, "nope"],
    }
    with pytest.raises(SheetsError) as exc:
        serialize_table(table, services, SPREADSHEET_ID)
    assert exc.value.code == "bad_table"
    assert "column must be a dict" in exc.value.message


def test_serialize_table_boolean_validation_renders_bare_type_no_parens():
    """A BOOLEAN-validated column reads as a bare ``Name:CHECKBOX`` — no empty parens appended.

    The validation one-liner for a no-value condition is the bare type (``BOOLEAN``); the column
    line token must NOT append ``()`` for a no-arg one-liner, so the column reads ``Flag:CHECKBOX``
    and the structured ``validation`` carries the bare ``BOOLEAN``.
    """
    services = _service_with_sheet_index()
    table = {
        "name": "T",
        "columnProperties": [
            {
                "columnName": "Flag",
                "columnType": "CHECKBOX",
                "dataValidationRule": {"condition": {"type": "BOOLEAN"}},
            }
        ],
    }
    out = serialize_table(table, services, SPREADSHEET_ID)
    assert out["columns"][0]["validation"] == "BOOLEAN"
    # No empty parens in the terse line — the no-arg one-liner collapses to ``Flag:CHECKBOX``.
    assert out["line"] == 'table "T" [] cols: Flag:CHECKBOX'


def test_serialize_table_dropdown_integral_float_value_renders_without_dot_zero():
    """An integral-float validation value (``5.0``) stringifies bare (``5``), not ``5.0``."""
    services = _service_with_sheet_index()
    table = {
        "name": "T",
        "columnProperties": [
            {
                "columnName": "Size",
                "columnType": "DROPDOWN",
                "dataValidationRule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        # A literal numeric userEnteredValue passes through to the scalar
                        # stringifier; an integral float must drop the trailing ``.0``.
                        "values": [{"userEnteredValue": 5.0}, {"userEnteredValue": 10.0}],
                    }
                },
            }
        ],
    }
    out = serialize_table(table, services, SPREADSHEET_ID)
    assert out["columns"][0]["validation"] == "ONE_OF_LIST(5,10)"
    assert "Size:DROPDOWN(5,10)" in out["line"]


def test_serialize_table_dropdown_bool_value_renders_true_false():
    """A literal bool validation value renders as ``TRUE``/``FALSE`` (not ``True``/``False``)."""
    services = _service_with_sheet_index()
    table = {
        "name": "T",
        "columnProperties": [
            {
                "columnName": "Active",
                "columnType": "DROPDOWN",
                "dataValidationRule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [
                            {"userEnteredValue": True},
                            {"userEnteredValue": False},
                        ],
                    }
                },
            }
        ],
    }
    out = serialize_table(table, services, SPREADSHEET_ID)
    assert out["columns"][0]["validation"] == "ONE_OF_LIST(TRUE,FALSE)"


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


@pytest.mark.parametrize(
    "col_type",
    [
        "DOUBLE",
        "CURRENCY",
        "PERCENT",
        "DATE",
        "TIME",
        "DATE_TIME",
        "TEXT",
        "BOOLEAN",
        "FILES_CHIP",
        "PEOPLE_CHIP",
        "FINANCE_CHIP",
        "PLACE_CHIP",
        "RATINGS_CHIP",
    ],
)
def test_build_add_table_request_accepts_each_valid_column_type(col_type):
    """Every real discovery-enum column type (DROPDOWN aside) round-trips into the request."""
    services = _service_with_sheet_index()
    req = build_add_table_request(
        services,
        SPREADSHEET_ID,
        "Sheet1!A1:A10",
        {"name": "T", "columns": [{"name": "C", "type": col_type}]},
    )
    assert req["addTable"]["table"]["columnProperties"][0]["columnType"] == col_type


@pytest.mark.parametrize(
    ("legacy", "resolved"),
    [
        ("DATETIME", "DATE_TIME"),
        ("CHECKBOX", "BOOLEAN"),
        ("RATING", "RATINGS_CHIP"),
    ],
)
def test_build_add_table_request_maps_legacy_type_aliases(legacy, resolved):
    """Legacy spellings map to their real discovery enum BEFORE the API call (old callers work)."""
    services = _service_with_sheet_index()
    req = build_add_table_request(
        services,
        SPREADSHEET_ID,
        "Sheet1!A1:A10",
        {"name": "T", "columns": [{"name": "C", "type": legacy}]},
    )
    assert req["addTable"]["table"]["columnProperties"][0]["columnType"] == resolved


def test_build_add_table_request_rejects_smart_chip_naming_chip_types():
    """SMART_CHIP has no single target -> rejected pre-flight, naming the specific *_CHIP types."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_add_table_request(
            services,
            SPREADSHEET_ID,
            "Sheet1!A1:A10",
            {"name": "T", "columns": [{"name": "X", "type": "SMART_CHIP"}]},
        )
    assert exc.value.code == "bad_table"
    # The message points the caller at the concrete chip types.
    for chip in ("FILES_CHIP", "PEOPLE_CHIP", "FINANCE_CHIP", "PLACE_CHIP", "RATINGS_CHIP"):
        assert chip in exc.value.message


def test_build_add_table_request_rejects_dropped_invalid_types():
    """Types this module once wrongly accepted (raw DATETIME alias aside) are no longer valid.

    ``DATETIME``/``CHECKBOX``/``RATING`` now alias to real types; the enum itself must NOT carry
    the invalid spellings, so a raw ``DATETIME`` in the *enum* is gone (only the alias survives).
    """
    services = _service_with_sheet_index()
    # A genuinely unknown value still raises pre-flight.
    with pytest.raises(SheetsError) as exc:
        build_add_table_request(
            services,
            SPREADSHEET_ID,
            "Sheet1!A1:A10",
            {"name": "T", "columns": [{"name": "X", "type": "NOT_A_TYPE"}]},
        )
    assert exc.value.code == "bad_table"


def test_column_types_enum_is_the_real_discovery_set():
    """The write-enum is exactly the real discovery values (minus UNSPECIFIED); no legacy junk."""
    assert tables.COLUMN_TYPES == frozenset(
        {
            "DOUBLE",
            "CURRENCY",
            "PERCENT",
            "DATE",
            "TIME",
            "DATE_TIME",
            "TEXT",
            "BOOLEAN",
            "DROPDOWN",
            "FILES_CHIP",
            "PEOPLE_CHIP",
            "FINANCE_CHIP",
            "PLACE_CHIP",
            "RATINGS_CHIP",
        }
    )
    # The old invalid spellings are gone from the enum itself.
    for bad in ("DATETIME", "CHECKBOX", "SMART_CHIP", "RATING"):
        assert bad not in tables.COLUMN_TYPES


def test_serialize_table_passes_through_genuine_chip_and_boolean_types():
    """Read serializer passes columnType through unchanged for BOOLEAN/DATE_TIME/chip tables."""
    services = _service_with_sheet_index()
    table = {
        "name": "T",
        "columnProperties": [
            {"columnName": "Flag", "columnType": "BOOLEAN"},
            {"columnName": "When", "columnType": "DATE_TIME"},
            {"columnName": "Owner", "columnType": "PEOPLE_CHIP"},
            {"columnName": "Score", "columnType": "RATINGS_CHIP"},
        ],
    }
    out = serialize_table(table, services, SPREADSHEET_ID)
    assert [c["type"] for c in out["columns"]] == [
        "BOOLEAN",
        "DATE_TIME",
        "PEOPLE_CHIP",
        "RATINGS_CHIP",
    ]


def test_build_add_table_request_without_columns_omits_column_properties():
    services = _service_with_sheet_index()
    req = build_add_table_request(
        services, SPREADSHEET_ID, "Sheet1!A1:F500", {"name": "T"}
    )
    assert "columnProperties" not in req["addTable"]["table"]


def test_build_add_table_request_non_list_columns_rejected():
    """``columns`` that is not a list/tuple raises ``bad_table`` (a dict is NOT a column list)."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_add_table_request(
            services,
            SPREADSHEET_ID,
            "Sheet1!A1:F500",
            {"name": "T", "columns": {"name": "Region", "type": "TEXT"}},
        )
    assert exc.value.code == "bad_table"
    assert "columns must be a list" in exc.value.message


def test_build_add_table_request_non_dict_column_entry_rejected():
    """A non-dict entry in ``columns`` raises ``bad_table`` naming the offending index."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_add_table_request(
            services,
            SPREADSHEET_ID,
            "Sheet1!A1:F500",
            {"name": "T", "columns": [{"name": "Region", "type": "TEXT"}, "nope"]},
        )
    assert exc.value.code == "bad_table"
    assert "columns[1]" in exc.value.message


def test_build_add_table_request_column_missing_name_rejected():
    """A column with no ``name`` raises ``bad_table`` (every column needs a name)."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_add_table_request(
            services,
            SPREADSHEET_ID,
            "Sheet1!A1:F500",
            {"name": "T", "columns": [{"type": "TEXT"}]},
        )
    assert exc.value.code == "bad_table"
    assert "requires a 'name'" in exc.value.message


def test_build_add_table_request_column_missing_type_rejected():
    """A column with no ``type`` raises ``bad_table`` (every column needs a type)."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_add_table_request(
            services,
            SPREADSHEET_ID,
            "Sheet1!A1:F500",
            {"name": "T", "columns": [{"name": "Region"}]},
        )
    assert exc.value.code == "bad_table"
    assert "requires a 'type'" in exc.value.message


def test_build_add_table_request_validation_must_be_dict():
    """A non-dict ``validation`` payload raises ``bad_table`` (must be a structured rule dict).

    Uses a non-DROPDOWN column so the DROPDOWN-requires-validation guard is not the trigger —
    the failure must come from the validation-shape check inside ``_build_column_validation``.
    """
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_add_table_request(
            services,
            SPREADSHEET_ID,
            "Sheet1!A1:A10",
            {
                "name": "T",
                "columns": [
                    {"name": "X", "type": "TEXT", "validation": "ONE_OF_LIST(A,B)"}
                ],
            },
        )
    assert exc.value.code == "bad_table"
    assert "structured ValidationRule dict" in exc.value.message


def test_build_column_validation_non_dict_validation_rejected():
    """``_build_column_validation`` rejects a non-dict validation directly (defensive guard).

    The public ``build_add_table_request`` path also hits this, but exercising the helper pins
    the exact ``bad_table`` raised when the structured validation is not a dict at all.
    """
    with pytest.raises(SheetsError) as exc:
        tables._build_column_validation(2, "ONE_OF_LIST(A,B)")
    assert exc.value.code == "bad_table"
    assert "columns[2]" in exc.value.message
    assert "structured ValidationRule dict" in exc.value.message


def test_build_column_validation_no_condition_rejected():
    """A converter result lacking a ``condition`` dict raises ``bad_table`` (no-condition guard).

    ``rule_to_validation`` normally always emits a ``condition``; this pins the defensive branch
    that protects the Table-column path — a column ``dataValidationRule`` MUST carry a condition —
    by handing the helper a (monkeypatched) converter that returns no condition.
    """
    import gsheets.core.tables as tables_mod

    orig = tables_mod.rule_to_validation
    tables_mod.rule_to_validation = lambda rule: {"strict": True}  # no ``condition`` key
    try:
        with pytest.raises(SheetsError) as exc:
            tables_mod._build_column_validation(0, {"type": "ONE_OF_LIST", "values": ["A"]})
    finally:
        tables_mod.rule_to_validation = orig
    assert exc.value.code == "bad_table"
    assert "produced no condition" in exc.value.message


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


# ===================================================================== rowsProperties colors


def test_serialize_table_rows_properties_golden():
    """Golden-master: a Table with header/band/footer colors -> exact flattened shape + line."""
    golden = load_golden("table_serialize_rowscolors")
    services = _service_with_sheet_index()
    out = serialize_table(golden["table"], services, SPREADSHEET_ID)
    assert out == golden["expected"]


def test_serialize_table_rows_properties_flattens_rgb_and_theme():
    """Each *ColorStyle flattens to hex (rgbColor) or theme:NAME (themeColor)."""
    services = _service_with_sheet_index()
    table = {
        "tableId": "t",
        "name": "T",
        "rowsProperties": {
            "headerColorStyle": {"rgbColor": {"red": 1.0, "green": 0.0, "blue": 0.0}},
            "secondBandColorStyle": {"themeColor": "ACCENT1"},
        },
    }
    out = serialize_table(table, services, SPREADSHEET_ID)
    assert out["rowsProperties"] == {"header": "#FF0000", "secondBand": "theme:ACCENT1"}
    # Only present colors appear in the terse line segment (firstBand/footer absent).
    assert out["line"].endswith(" rows: header=#FF0000, secondBand=theme:ACCENT1")


def test_serialize_table_colorless_table_has_no_rows_properties_key():
    """A table without rowsProperties omits the key entirely and leaves the line unchanged."""
    services = _service_with_sheet_index()
    table = {
        "tableId": "t",
        "name": "Sales",
        "range": {
            "sheetId": 0,
            "startRowIndex": 0,
            "endRowIndex": 500,
            "startColumnIndex": 0,
            "endColumnIndex": 6,
        },
        "columnProperties": [{"columnName": "Region", "columnType": "TEXT"}],
    }
    out = serialize_table(table, services, SPREADSHEET_ID)
    assert "rowsProperties" not in out
    # No 'rows:' segment appended — line is byte-for-byte the pre-feature form.
    assert out["line"] == 'table "Sales" [Sheet1!A1:F500] cols: Region:TEXT'
    assert " rows: " not in out["line"]


def test_serialize_table_empty_rows_properties_omitted():
    """An empty rowsProperties dict (no colors set) omits the key — nothing to surface."""
    services = _service_with_sheet_index()
    out = serialize_table(
        {"tableId": "t", "name": "T", "rowsProperties": {}}, services, SPREADSHEET_ID
    )
    assert "rowsProperties" not in out
    assert " rows: " not in out["line"]


def test_serialize_table_rows_properties_skips_malformed_color():
    """A malformed *ColorStyle is skipped (not fatal); valid siblings still flatten."""
    services = _service_with_sheet_index()
    table = {
        "tableId": "t",
        "name": "T",
        "rowsProperties": {
            "headerColorStyle": {"nonsense": True},  # neither themeColor nor rgb channels
            "footerColorStyle": {"rgbColor": {"red": 0.0, "green": 0.0, "blue": 0.0}},
        },
    }
    out = serialize_table(table, services, SPREADSHEET_ID)
    assert out["rowsProperties"] == {"footer": "#000000"}


def test_build_add_table_request_rows_properties_emits_colorstyle():
    """add_table with rowsProperties emits Google *ColorStyle fields (hex -> rgbColor)."""
    services = _service_with_sheet_index()
    req = build_add_table_request(
        services,
        SPREADSHEET_ID,
        "Sheet1!A1:C100",
        {
            "name": "T",
            "rowsProperties": {"header": "#FF0000", "footer": "theme:ACCENT2"},
        },
    )
    assert req["addTable"]["table"]["rowsProperties"] == {
        "headerColorStyle": {"rgbColor": {"red": 1.0, "green": 0.0, "blue": 0.0}},
        "footerColorStyle": {"themeColor": "ACCENT2"},
    }


def test_build_add_table_request_without_rows_properties_omits_key():
    """add_table without rowsProperties never attaches the key (colorless table unaffected)."""
    services = _service_with_sheet_index()
    req = build_add_table_request(
        services, SPREADSHEET_ID, "Sheet1!A1:C100", {"name": "T"}
    )
    assert "rowsProperties" not in req["addTable"]["table"]


def test_build_add_table_request_empty_rows_properties_omits_key():
    """An empty rowsProperties mapping resolves to nothing and is not attached."""
    services = _service_with_sheet_index()
    req = build_add_table_request(
        services, SPREADSHEET_ID, "Sheet1!A1:C100", {"name": "T", "rowsProperties": {}}
    )
    assert "rowsProperties" not in req["addTable"]["table"]


def test_build_update_table_request_rows_properties_masks_supplied_colors_only():
    """update with two band colors masks exactly those two *ColorStyle leaves (group form)."""
    services = _service_with_sheet_index()
    req = build_update_table_request(
        services,
        SPREADSHEET_ID,
        {
            "tableId": "abc",
            "rowsProperties": {"header": "#FFCDD2", "footer": "#E0E0E0"},
        },
    )
    assert req["updateTable"]["table"]["rowsProperties"] == {
        "headerColorStyle": {
            "rgbColor": {"red": 1.0, "green": 205 / 255, "blue": 210 / 255}
        },
        "footerColorStyle": {
            "rgbColor": {"red": 224 / 255, "green": 224 / 255, "blue": 224 / 255}
        },
    }
    assert (
        req["updateTable"]["fields"]
        == "rowsProperties(headerColorStyle,footerColorStyle)"
    )


def test_build_update_table_request_single_rows_color_dotted_mask():
    """update with one band color emits the dotted single-leaf mask (unspecified survive)."""
    services = _service_with_sheet_index()
    req = build_update_table_request(
        services,
        SPREADSHEET_ID,
        {"tableId": "abc", "rowsProperties": {"secondBand": "theme:ACCENT3"}},
    )
    assert req["updateTable"]["table"]["rowsProperties"] == {
        "secondBandColorStyle": {"themeColor": "ACCENT3"}
    }
    assert req["updateTable"]["fields"] == "rowsProperties.secondBandColorStyle"


def test_build_update_table_request_rows_properties_with_other_fields_mask_order():
    """rowsProperties masks alongside name in payload insertion order."""
    services = _service_with_sheet_index()
    req = build_update_table_request(
        services,
        SPREADSHEET_ID,
        {"tableId": "abc", "name": "N", "rowsProperties": {"header": "#000000"}},
    )
    assert req["updateTable"]["fields"] == "name,rowsProperties.headerColorStyle"


def test_build_update_table_request_empty_rows_properties_is_noop_payload():
    """tableId + empty rowsProperties contributes no token -> refuse a no-op write."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_update_table_request(
            services, SPREADSHEET_ID, {"tableId": "abc", "rowsProperties": {}}
        )
    assert exc.value.code == "empty_payload"


def test_build_rows_properties_rejects_non_dict():
    with pytest.raises(SheetsError) as exc:
        tables._build_rows_properties(["#FF0000"])
    assert exc.value.code == "bad_table"


def test_build_rows_properties_rejects_bad_color():
    with pytest.raises(SheetsError) as exc:
        tables._build_rows_properties({"header": "not-a-color"})
    assert exc.value.code == "bad_table"
    assert "rowsProperties.header" in str(exc.value)


def test_build_rows_properties_ignores_unknown_keys():
    """Only the four recognised colors are honored; stray keys are ignored, not emitted."""
    out = tables._build_rows_properties({"header": "#FF0000", "bogus": "#00FF00"})
    assert out == {"headerColorStyle": {"rgbColor": {"red": 1.0, "green": 0.0, "blue": 0.0}}}


# =========================================================================== round-trip


def test_round_trip_rows_properties_hex_and_theme():
    """rowsProperties built via add_table serializes back to the SAME terse colors.

    Closes the CRUD loop for T2.6: terse {header, secondBand} colors -> Google *ColorStyle
    (write) -> serialize_table reads them back to the identical hex / theme strings.
    """
    services = _service_with_sheet_index()
    req = build_add_table_request(
        services,
        SPREADSHEET_ID,
        "Sheet1!A1:C100",
        {
            "name": "T",
            "rowsProperties": {"header": "#FFCDD2", "secondBand": "theme:ACCENT2"},
        },
    )
    built_table = req["addTable"]["table"]
    built_table["tableId"] = "rt"
    out = serialize_table(built_table, services, SPREADSHEET_ID)
    assert out["rowsProperties"] == {"header": "#FFCDD2", "secondBand": "theme:ACCENT2"}


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
