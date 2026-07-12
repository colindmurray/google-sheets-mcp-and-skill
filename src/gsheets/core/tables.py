"""Native Sheets Tables (``Table``) read serialization + add/update/delete write builders.

Covers DESIGN §X.0c and §X.3/§X.4. Native Tables went GA in 2024 and are exposed in the
v4 REST API as a per-sheet ``tables`` array. This module owns:

- :func:`serialize_table` — flatten a Google ``Table`` (``name`` / ``range`` /
  ``columnProperties{columnName,columnType,dataValidationRule}``) into the terse, flattened,
  round-trippable read shape (condformat line style). A ``DROPDOWN`` column's
  ``dataValidationRule`` is rendered as the SAME ``ValidationRule`` one-liner that ``inspect``
  surfaces, by reusing ``rules.validation_to_rule``.
- :func:`build_add_table_request` / :func:`build_update_table_request` /
  :func:`build_delete_table_request` — return ready-to-send ``batchUpdate`` request dicts
  (``addTable`` / ``updateTable`` / ``deleteTable``). ``update`` auto-builds its ``fields``
  mask from the payload via :func:`gsheets.core.fieldsmask.build_fields_mask`. These are
  consumed by ``core/structure.py``'s new ``add_table`` / ``update_table`` / ``delete_table``
  actions (which own the action->handler dispatch); the ``addTable`` reply's ``tableId`` is
  captured by ``structure.capture_new_ids`` (its ``_REPLY_ID_SPECS`` is extended there).

Boundary (DESIGN §1, §5.2): PURE core. Imports only stdlib + sibling core modules. Must NEVER
import ``fastmcp``, ``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models``.

Range handling mirrors the condformat boundary: :func:`serialize_table` resolves a Google
``Table.range`` ``GridRange`` -> A1 via ``addressing.gridrange_to_a1`` (it holds a
``SheetsServices`` handle); the ``build_*`` request builders resolve the caller's A1 ``range``
-> ``GridRange`` via ``addressing.a1_to_gridrange`` before emitting the request.
"""

from __future__ import annotations

from .addressing import a1_to_gridrange, gridrange_to_a1
from .colors import color_style_to_hex, hex_to_color_style
from .errors import SheetsError
from .fieldsmask import build_fields_mask
from .rules import rule_to_validation, validation_to_rule
from .service import SheetsServices

#: ``Table.rowsProperties`` table-native band/header/footer colors, as
#: ``(google *ColorStyle field, terse round-trip key)`` pairs. Each Google field carries a
#: ``ColorStyle`` (verified against the v4 discovery ``TableRowsProperties`` schema). The read
#: serializer flattens each to a hex / ``theme:`` string under the terse key; the write builder
#: accepts the terse key (hex / ``theme:``) and emits the Google ``*ColorStyle`` field. Keeping
#: read output and write input keyed identically makes ``rowsProperties`` round-trippable.
_ROWS_COLOR_FIELDS: tuple[tuple[str, str], ...] = (
    ("headerColorStyle", "header"),
    ("firstBandColorStyle", "firstBand"),
    ("secondBandColorStyle", "secondBand"),
    ("footerColorStyle", "footer"),
)

#: ``Table.columnProperties[].columnType`` write-enum — the real v4 discovery values for
#: ``TableColumnProperties.columnType`` (minus ``COLUMN_TYPE_UNSPECIFIED``, which is the
#: server default, not a caller-selectable write target). A ``DROPDOWN`` column REQUIRES a
#: ``dataValidationRule`` with a ``ONE_OF_LIST`` condition.
COLUMN_TYPES: frozenset[str] = frozenset(
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

#: Legacy/deprecated column-type spellings this module once accepted, mapped to their real
#: discovery-enum target. Applied in :func:`_build_column_properties` BEFORE validation so old
#: callers keep working. ``SMART_CHIP`` is deliberately absent — it has no single target and is
#: rejected with a message naming the specific ``*_CHIP`` types.
COLUMN_TYPE_ALIASES: dict[str, str] = {
    "DATETIME": "DATE_TIME",
    "CHECKBOX": "BOOLEAN",
    "RATING": "RATINGS_CHIP",
}

#: The concrete ``*_CHIP`` column types, named in the ``SMART_CHIP`` rejection message.
_CHIP_TYPES: tuple[str, ...] = (
    "FILES_CHIP",
    "PEOPLE_CHIP",
    "FINANCE_CHIP",
    "PLACE_CHIP",
    "RATINGS_CHIP",
)


# ===========================================================================
# serialize: Google Table -> flattened read shape + terse line
# ===========================================================================


def serialize_table(
    table: dict, services: SheetsServices, spreadsheet_id: str
) -> dict:
    """Flatten a Google ``Table`` into the terse, round-trippable read shape (DESIGN §X.0c).

    Produces::

        { "tableId": "abc", "name": "Sales", "range": "Sheet1!A1:F500",
          "columns": [ { "name": "Region", "type": "TEXT" },
                       { "name": "Status", "type": "DROPDOWN",
                         "validation": "ONE_OF_LIST(Open,Closed)" } ],
          "rowsProperties": { "header": "#FFCDD2", "secondBand": "theme:ACCENT2" },
          "line": 'table "Sales" [Sheet1!A1:F500] cols: Region:TEXT, Status:DROPDOWN(Open,Closed)' }

    A column's ``dataValidationRule`` (present on ``DROPDOWN`` columns) is rendered to the SAME
    ``ValidationRule`` one-liner ``inspect`` surfaces, by reusing ``rules.validation_to_rule``.
    Unset keys are omitted (token efficiency); per-column ``validation`` is attached only when
    present. The ``Table.range`` ``GridRange`` is resolved to a sheet-qualified A1 string.
    ``Table.rowsProperties`` (header/band/footer colors) is flattened to terse
    ``{header, firstBand, secondBand, footer}`` hex / ``theme:`` values (present colors only) and
    attached only when the table carries at least one — a colorless table omits the key entirely.

    Args:
        table: A Google ``Table`` dict (``tableId`` / ``name`` / ``range`` /
            ``columnProperties``).
        services: The authed handle (used to resolve ``range`` ``GridRange`` -> A1).
        spreadsheet_id: Target spreadsheet id.

    Returns:
        The flattened table dict described above (``line`` always present; ``columns`` always
        present, possibly empty).
    """
    if not isinstance(table, dict):
        raise SheetsError(
            "bad_table", f"table must be a dict, got {type(table).__name__}"
        )

    out: dict = {}
    table_id = table.get("tableId")
    if table_id is not None:
        out["tableId"] = table_id

    name = table.get("name")
    if name is not None:
        out["name"] = name

    a1_range = None
    gr = table.get("range")
    if isinstance(gr, dict):
        a1_range = gridrange_to_a1(services, spreadsheet_id, gr)
    if a1_range is not None:
        out["range"] = a1_range

    columns: list[dict] = []
    for col in table.get("columnProperties", []) or []:
        columns.append(_serialize_column(col))
    out["columns"] = columns

    rows_props = _serialize_rows_properties(table.get("rowsProperties"))
    if rows_props:
        out["rowsProperties"] = rows_props

    out["line"] = _serialize_table_line(name, a1_range, columns, rows_props)
    return out


def _serialize_rows_properties(rows_props: object) -> dict | None:
    """Flatten a Google ``Table.rowsProperties`` to terse ``{header, firstBand, ...}`` hex/theme.

    Each present ``*ColorStyle`` (``headerColorStyle`` / ``firstBandColorStyle`` /
    ``secondBandColorStyle`` / ``footerColorStyle``) is flattened via
    ``colors.color_style_to_hex`` (uppercase ``#RRGGBB`` or ``theme:NAME``) under its terse key.
    Absent colors are omitted (token efficiency). A malformed ``*ColorStyle`` is skipped rather
    than raising, so a partially formed table never breaks an otherwise-valid read (mirrors the
    forgiving DROPDOWN validation handling). Returns ``None`` when nothing is present, so
    ``serialize_table`` omits the ``rowsProperties`` key entirely for a colorless table.
    """
    if not isinstance(rows_props, dict):
        return None
    out: dict = {}
    for gfield, key in _ROWS_COLOR_FIELDS:
        color_style = rows_props.get(gfield)
        if isinstance(color_style, dict):
            try:
                out[key] = color_style_to_hex(color_style)
            except ValueError:
                continue
    return out or None


def _serialize_column(col: dict) -> dict:
    """Flatten one ``Table.columnProperties[]`` entry to ``{name, type, validation?}``."""
    if not isinstance(col, dict):
        raise SheetsError(
            "bad_table", f"column must be a dict, got {type(col).__name__}"
        )
    entry: dict = {}
    col_name = col.get("columnName")
    if col_name is not None:
        entry["name"] = col_name
    col_type = col.get("columnType")
    if col_type is not None:
        entry["type"] = col_type

    dv = col.get("dataValidationRule")
    if isinstance(dv, dict):
        # A column's dataValidationRule carries its condition under ``condition`` (matching a
        # cell ``DataValidationRule``). Reuse the validation round-trip so a DROPDOWN column's
        # one-liner is IDENTICAL to what ``inspect`` surfaces for the same validation.
        validation = _column_validation_line(dv)
        if validation is not None:
            entry["validation"] = validation
    return entry


def _column_validation_line(dv: dict) -> str | None:
    """Render a column's ``dataValidationRule`` to the ``ValidationRule`` one-liner.

    Returns ``None`` (rather than raising) for a malformed/condition-less rule so a partially
    formed table never breaks an otherwise-valid read.
    """
    try:
        structured = validation_to_rule(dv)
    except SheetsError:
        return None
    return _validation_one_liner(structured)


def _validation_one_liner(rule: dict) -> str:
    """Terse human one-liner for a structured ``ValidationRule`` (matches ``rules`` output).

    Examples: ``"ONE_OF_LIST(Open,Closed)"``, ``"ONE_OF_RANGE(Sheet1!Z1:Z10)"``,
    ``"BOOLEAN"``, ``"NUMBER_BETWEEN(0,100)"``. Mirrors ``rules._validation_one_liner`` so the
    DROPDOWN column line equals the cell-validation line ``inspect`` emits.
    """
    rule_type = rule.get("type", "")
    source = rule.get("source")
    if source is not None:
        return f"{rule_type}({source})"
    values = rule.get("values")
    if values:
        return f"{rule_type}({','.join(_scalar_to_str(v) for v in values)})"
    return rule_type


def _scalar_to_str(value: object) -> str:
    """Stringify a validation value (ints stay bare, no trailing ``.0``)."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _serialize_table_line(
    name: str | None,
    a1_range: str | None,
    columns: list[dict],
    rows_props: dict | None = None,
) -> str:
    """Build the terse condformat-style line for a table.

    Form: ``table "Sales" [Sheet1!A1:F500] cols: Region:TEXT, Status:DROPDOWN(Open,Closed)``.
    A DROPDOWN (or otherwise validated) column appends its validation values as
    ``Name:TYPE(v1,v2)``; other columns are ``Name:TYPE``. When the table carries
    ``rowsProperties`` (header/band/footer colors), a ``rows: header=#RRGGBB, ...`` segment is
    appended — present colors only — so a colorless table's line is byte-for-byte unchanged.
    """
    name_part = f'"{name}"' if name is not None else '""'
    range_part = f"[{a1_range}]" if a1_range is not None else "[]"
    col_parts = [_column_line_token(c) for c in columns]
    cols_part = ", ".join(col_parts)
    line = f"table {name_part} {range_part} cols: {cols_part}"
    if rows_props:
        rows_part = ", ".join(f"{key}={rows_props[key]}" for _, key in _ROWS_COLOR_FIELDS if key in rows_props)
        line = f"{line} rows: {rows_part}"
    return line


def _column_line_token(col: dict) -> str:
    """Render one column for the terse line: ``Name:TYPE`` or ``Name:TYPE(v1,v2)``.

    A column carrying a ``validation`` one-liner (e.g. ``ONE_OF_LIST(Open,Closed)``) collapses
    to ``Name:TYPE(Open,Closed)`` — the type label plus the validation's parenthesised args —
    so a DROPDOWN reads as ``Status:DROPDOWN(Open,Closed)``.
    """
    col_name = col.get("name", "")
    col_type = col.get("type", "")
    validation = col.get("validation")
    if validation:
        args = _validation_args(validation)
        if args is not None:
            return f"{col_name}:{col_type}({args})"
    return f"{col_name}:{col_type}"


def _validation_args(validation: str) -> str | None:
    """Pull the parenthesised argument list out of a validation one-liner.

    ``"ONE_OF_LIST(Open,Closed)"`` -> ``"Open,Closed"``; a no-arg one-liner (``"BOOLEAN"``)
    yields ``None`` so it never appends empty parens to the column token.
    """
    open_paren = validation.find("(")
    if open_paren == -1 or not validation.endswith(")"):
        return None
    return validation[open_paren + 1 : -1]


# ===========================================================================
# build: A1 + structured columns -> Google batchUpdate request dicts
# ===========================================================================


def build_add_table_request(
    services: SheetsServices,
    spreadsheet_id: str,
    range: str,
    params: dict,
) -> dict:
    """Build an ``addTable`` ``batchUpdate`` request (DESIGN §X.3 add_table).

    ``params`` is
    ``{"name": str, "columns": [{"name", "type", "validation"?}, …], "rowsProperties"?}`` over
    the A1 ``range``. A ``DROPDOWN`` column REQUIRES a ``ONE_OF_LIST`` ``validation`` (the
    structured ``ValidationRule`` shape, e.g. ``{"type": "ONE_OF_LIST", "values": ["Open",
    "Closed"]}``), which is converted to a Google ``dataValidationRule`` via
    ``rules.rule_to_validation``. Optional ``rowsProperties`` is
    ``{"header"?, "firstBand"?, "secondBand"?, "footer"?}`` of hex / ``theme:`` colors, converted
    to the Google ``*ColorStyle`` fields. The caller (``structure.add_table``) captures the new
    ``tableId`` from the reply.

    Args:
        services: The authed handle (resolves the A1 ``range`` -> ``GridRange``).
        spreadsheet_id: Target spreadsheet id.
        range: A1 range the table spans.
        params: ``{"name", "columns"}`` (see above).

    Returns:
        ``{"addTable": {"table": {...}}}`` — ready for ``spreadsheets.batchUpdate``.
    """
    name = params.get("name")
    if not name:
        raise SheetsError("missing_param", "add_table requires params={'name': <str>}")

    grid_range = a1_to_gridrange(services, spreadsheet_id, range)
    table: dict = {"name": name, "range": grid_range}

    column_props = _build_column_properties(params.get("columns"))
    if column_props is not None:
        table["columnProperties"] = column_props

    rows_props = _build_rows_properties(params.get("rowsProperties"))
    if rows_props:
        table["rowsProperties"] = rows_props

    return {"addTable": {"table": table}}


def build_update_table_request(
    services: SheetsServices,
    spreadsheet_id: str,
    params: dict,
) -> dict:
    """Build an ``updateTable`` ``batchUpdate`` request with an AUTO fields mask (DESIGN §X.3).

    ``params`` is ``{"tableId": str, "name"?, "columns"?, "range"?, "rowsProperties"?}``. Only
    the supplied fields are masked (via :func:`gsheets.core.fieldsmask.build_fields_mask`) so
    unspecified table properties are never wiped. ``columnProperties`` is an atomic leaf for
    masking (Google treats the column-properties array as one field); ``rowsProperties`` is NOT
    atomic — only the specific ``*ColorStyle`` colors supplied are masked (so unspecified band
    colors survive). An empty payload (``tableId`` only) raises ``empty_payload`` — refuse a
    no-op.

    Args:
        services: The authed handle (resolves ``range`` -> ``GridRange`` when present).
        spreadsheet_id: Target spreadsheet id.
        params: ``{"tableId", "name"?, "columns"?, "range"?}``.

    Returns:
        ``{"updateTable": {"table": {...}, "fields": "<mask>"}}``.
    """
    table_id = params.get("tableId")
    if not table_id:
        raise SheetsError(
            "missing_param", "update_table requires params={'tableId': <str>}"
        )

    table: dict = {"tableId": table_id}
    # The masked payload mirrors ``table`` but EXCLUDES the immutable ``tableId`` so the auto
    # mask covers only the fields actually being changed.
    masked: dict = {}

    if params.get("name") is not None:
        table["name"] = params["name"]
        masked["name"] = params["name"]

    if params.get("range") is not None:
        grid_range = a1_to_gridrange(services, spreadsheet_id, params["range"])
        table["range"] = grid_range
        # ``range`` is one logical field on ``updateTable`` — the whole GridRange is replaced
        # atomically, so the mask must emit ``range``, NOT its ``range(sheetId,...)`` subfields
        # (which Google would reject / partially-apply). Mask it via a scalar sentinel so
        # build_fields_mask treats ``range`` as a leaf rather than recursing the dict.
        masked["range"] = True

    if "columns" in params and params["columns"] is not None:
        column_props = _build_column_properties(params["columns"])
        # ``columnProperties`` masks atomically (the whole column array is one field).
        table["columnProperties"] = column_props or []
        masked["columnProperties"] = table["columnProperties"]

    if params.get("rowsProperties") is not None:
        rows_props = _build_rows_properties(params["rowsProperties"]) or {}
        table["rowsProperties"] = rows_props
        # ``rowsProperties`` is NOT atomic — its children are ``*ColorStyle`` leaves. Feeding the
        # Google-shaped dict to build_fields_mask yields ``rowsProperties.headerColorStyle`` (one
        # color) or ``rowsProperties(headerColorStyle,footerColorStyle)`` (several), so only the
        # colors actually supplied are masked and the rest are never wiped. An empty dict
        # (``rowsProperties: {}``) contributes no token — a no-op that neither sets nor clears.
        masked["rowsProperties"] = rows_props

    fields = build_fields_mask(masked)  # raises empty_payload when nothing to change
    return {"updateTable": {"table": table, "fields": fields}}


def build_delete_table_request(params: dict) -> dict:
    """Build a ``deleteTable`` ``batchUpdate`` request (DESIGN §X.3 delete_table).

    ``params`` is ``{"tableId": str}`` — addresses the table by id (no range needed).

    Args:
        params: ``{"tableId": <str>}``.

    Returns:
        ``{"deleteTable": {"tableId": <str>}}``.
    """
    table_id = params.get("tableId")
    if not table_id:
        raise SheetsError(
            "missing_param", "delete_table requires params={'tableId': <str>}"
        )
    return {"deleteTable": {"tableId": table_id}}


# --- rows-property construction (write side) -----------------------------------------


def _build_rows_properties(rows_props: object) -> dict | None:
    """Build a Google ``Table.rowsProperties`` from terse ``{header, firstBand, ...}`` colors.

    Each supplied terse key (``header`` / ``firstBand`` / ``secondBand`` / ``footer``) carries a
    hex (``#RRGGBB``) or ``theme:NAME`` string, converted to the matching Google ``*ColorStyle``
    via ``colors.hex_to_color_style`` (writes always use ``ColorStyle``, never the deprecated
    flat ``Color``). Returns ``None`` when ``rows_props`` is ``None`` (caller omits the field);
    an empty / colorless mapping yields ``{}`` (the caller decides whether to attach it).

    Unknown keys are ignored (only the four recognised band/header/footer colors are honored).
    A bad color value raises ``SheetsError('bad_table', …)`` naming the offending key.
    """
    if rows_props is None:
        return None
    if not isinstance(rows_props, dict):
        raise SheetsError(
            "bad_table",
            f"rowsProperties must be a dict, got {type(rows_props).__name__}",
        )
    out: dict = {}
    for gfield, key in _ROWS_COLOR_FIELDS:
        value = rows_props.get(key)
        if value is None:
            continue
        try:
            out[gfield] = hex_to_color_style(value)
        except ValueError as exc:
            raise SheetsError("bad_table", f"rowsProperties.{key}: {exc}") from exc
    return out


# --- column-property construction (write side) ---------------------------------------


def _build_column_properties(columns: object) -> list[dict] | None:
    """Build Google ``Table.columnProperties`` from the structured ``columns`` list.

    Each column is ``{"name": str, "type": str, "validation"?: ValidationRule}``. Returns
    ``None`` when ``columns`` is ``None`` (caller omits the field). An empty list yields ``[]``.
    A ``DROPDOWN`` column MUST carry a ``ONE_OF_LIST`` ``validation`` (per the Tables guide);
    its structured ``ValidationRule`` is converted via ``rules.rule_to_validation``.

    The Google ``columnProperties`` entry uses ``columnIndex`` (0-based positional, derived
    from list order), ``columnName``, ``columnType``, and (when validated) ``dataValidationRule``.
    """
    if columns is None:
        return None
    if not isinstance(columns, (list, tuple)):
        raise SheetsError(
            "bad_table",
            f"columns must be a list, got {type(columns).__name__}",
        )

    out: list[dict] = []
    for i, col in enumerate(columns):
        if not isinstance(col, dict):
            raise SheetsError("bad_table", f"columns[{i}] must be a dict")
        col_name = col.get("name")
        if not col_name:
            raise SheetsError("bad_table", f"columns[{i}] requires a 'name'")
        col_type = col.get("type")
        if not col_type:
            raise SheetsError("bad_table", f"columns[{i}] requires a 'type'")
        # Map legacy spellings (DATETIME/CHECKBOX/RATING) to the real discovery enum BEFORE
        # validation so old callers keep working.
        col_type = COLUMN_TYPE_ALIASES.get(col_type, col_type)
        if col_type == "SMART_CHIP":
            raise SheetsError(
                "bad_table",
                f"columns[{i}] type 'SMART_CHIP' is not a column type — choose the specific "
                f"chip type: {', '.join(_CHIP_TYPES)}",
            )
        if col_type not in COLUMN_TYPES:
            raise SheetsError(
                "bad_table",
                f"columns[{i}] type {col_type!r} must be one of {sorted(COLUMN_TYPES)}",
            )

        entry: dict = {
            "columnIndex": i,
            "columnName": col_name,
            "columnType": col_type,
        }

        validation = col.get("validation")
        if col_type == "DROPDOWN" and validation is None:
            raise SheetsError(
                "bad_table",
                f"columns[{i}] is DROPDOWN — it requires a ONE_OF_LIST 'validation'",
            )
        if validation is not None:
            entry["dataValidationRule"] = _build_column_validation(i, validation)

        out.append(entry)
    return out


def _build_column_validation(i: int, validation: object) -> dict:
    """Convert a column's structured ``ValidationRule`` to a Table-column ``dataValidationRule``.

    A *cell* ``DataValidationRule`` (what ``rules.rule_to_validation`` emits) carries
    ``condition`` + ``strict`` + ``showCustomUi``. A *Table column's*
    ``columnProperties[].dataValidationRule`` accepts ONLY ``condition`` — the API rejects the
    extra keys with ``Unknown name "strict"`` / ``Unknown name "showCustomUi"``. So reuse the
    shared converter for the condition shape, then keep only ``condition`` (drop the cell-only
    ``strict``/``showCustomUi`` subfields).
    """
    if not isinstance(validation, dict):
        raise SheetsError(
            "bad_table",
            f"columns[{i}] 'validation' must be a structured ValidationRule dict",
        )
    google = rule_to_validation(validation)
    condition = google.get("condition")
    if not isinstance(condition, dict):
        raise SheetsError(
            "bad_table",
            f"columns[{i}] 'validation' produced no condition",
        )
    return {"condition": condition}
