"""Row/column dimension ops + ``hiddenByUser`` read (DESIGN §X.7/§X.10/§X.13).

One dispatch fn :func:`dimensions` covers the row/column dimension verbs — a distinct,
high-frequency intent kept OFF ``manage_sheets`` (which is tab-level) on purpose (DESIGN
§X.7 decision: a separate pure module keeps both clean and keeps each integration unit
single-file). The write verbs each map to ONE ``batchUpdate`` request:

- ``insert`` -> ``insertDimension``
- ``delete`` -> ``deleteDimension``
- ``move`` -> ``moveDimension``
- ``append`` -> ``appendDimension``
- ``auto_resize`` -> ``autoResizeDimensions``
- ``set_props`` -> ``updateDimensionProperties`` (auto fields mask via
  ``build_fields_mask``)

The read verb is ``set_props``'s read-side counterpart:

- ``read`` -> ``spreadsheets.get`` over ``data(rowMetadata.hiddenByUser,
  columnMetadata.hiddenByUser, startRow, startColumn)`` -> ``{"hiddenRows":[idx,…],
  "hiddenCols":[idx,…]}`` (absolute 0-based indices).

Every op targets ONE tab; ``sheet`` is REQUIRED on every action and resolved to a
``sheetId`` via the addressing layer (``a1_to_gridrange(services, sid, sheet)["sheetId"]``,
the existing pattern). Each action consumes ONLY its documented ``params`` keys; an unknown
key raises ``SheetsError("unknown_param")`` (the typed surface stays strict — ``params`` is
NOT a raw escape hatch, exactly like ``structure``/``manage_sheets`` in DESIGN §3.3).

This is a NEW pure-core module: it imports ONLY stdlib + sibling core modules
(``addressing``/``errors``/``service``/``fieldsmask``) + ``googleapiclient`` errors. It must
NEVER import ``fastmcp``, ``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models`` (DESIGN
§1 boundary).
"""

from __future__ import annotations

from googleapiclient.errors import HttpError

from .addressing import a1_to_gridrange
from .errors import SheetsError, classify_google_error
from .fieldsmask import build_fields_mask
from .service import SheetsServices

# ---------------------------------------------------------------------------------------
# Action surface + per-action allowed params (LOCKED — DESIGN §X.7 dimensions table).
# ---------------------------------------------------------------------------------------

#: Every supported ``dimensions`` action.
_DIMENSIONS_ACTIONS = frozenset(
    {
        "insert",
        "delete",
        "move",
        "append",
        "auto_resize",
        "set_props",
        "read",
    }
)

#: Per-action allowed ``params`` keys (LOCKED). An unknown key -> ``unknown_param``.
_DIMENSIONS_PARAMS: dict[str, set[str]] = {
    "insert": {"dimension", "start", "end", "inheritFromBefore"},
    "delete": {"dimension", "start", "end"},
    "move": {"dimension", "start", "end", "destinationIndex"},
    "append": {"dimension", "length"},
    "auto_resize": {"dimension", "start", "end"},
    "set_props": {"dimension", "start", "end", "pixelSize", "hiddenByUser"},
    "read": {"range"},
}

#: Valid ``Dimension`` enum values.
_DIMENSIONS = frozenset({"ROWS", "COLUMNS"})


# ---------------------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------------------


def dimensions(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    action: str,
    sheet: str | None = None,
    params: dict | None = None,
) -> dict:
    """Insert/delete/move/append/auto-resize/set-props rows+cols, or read hidden ones.

    Dispatches over the row/column dimension verbs (DESIGN §X.7): ``insert``/``delete``/
    ``move``/``append`` -> ``insertDimension``/``deleteDimension``/``moveDimension``/
    ``appendDimension``; ``auto_resize`` -> ``autoResizeDimensions``; ``set_props`` ->
    ``updateDimensionProperties`` (auto fields mask); ``read`` -> a tight
    ``spreadsheets.get`` over ``rowMetadata``/``columnMetadata`` ``hiddenByUser``.

    Every action targets ONE tab, so ``sheet`` is REQUIRED on every action (a missing sheet
    raises ``SheetsError("missing_sheet")``). All start/end indices are 0-based half-open,
    matching the A1-aligned vocabulary the rest of core uses. Each action consumes only its
    documented ``params`` keys; an unknown key raises ``SheetsError("unknown_param")``.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        action: One of the ``dimensions`` actions (see DESIGN §X.7 table).
        sheet: Target tab name (REQUIRED for every action).
        params: Per-action parameter dict (see the LOCKED key table).

    Returns:
        For a write: ``{"ok": True, "spreadsheetId": ..., "action": ..., "sheet": ...,
        ...echoed geometry...}``. For ``read``: ``{"ok": True, "spreadsheetId": ...,
        "action": "read", "sheet": ..., "hiddenRows": [idx, ...], "hiddenCols": [idx, ...]}``.

    Raises:
        SheetsError: ``unknown_action``/``unknown_param``/``missing_param``/``missing_sheet``/
            ``bad_param`` on bad input; ``google_api_error`` on an API failure.
    """
    if action not in _DIMENSIONS_ACTIONS:
        raise SheetsError(
            "unknown_action",
            f"unknown dimensions action {action!r}; expected one of "
            f"{sorted(_DIMENSIONS_ACTIONS)}",
        )
    params = _require_params(action, params, _DIMENSIONS_PARAMS[action])
    sheet = _require_sheet(action, sheet)
    handler = _HANDLERS[action]
    return handler(services, spreadsheet_id, sheet, params)


# ---------------------------------------------------------------------------------------
# Shared helpers (kept local so the module stays dependency-light, DESIGN §X.7)
# ---------------------------------------------------------------------------------------


def _require_params(action: str, params: dict | None, allowed: set[str]) -> dict:
    """Return a validated ``params`` dict, rejecting unknown keys (DESIGN §3.3 pattern).

    An unknown key for the given ``action`` raises ``SheetsError("unknown_param")`` so the
    typed surface stays strict (``params`` is NOT a raw escape hatch). ``None`` is treated as
    an empty dict.
    """
    params = params or {}
    if not isinstance(params, dict):
        raise SheetsError(
            "unknown_param", f"params for action {action!r} must be a dict"
        )
    unknown = set(params) - allowed
    if unknown:
        raise SheetsError(
            "unknown_param",
            f"unknown params for action {action!r}: {sorted(unknown)}; "
            f"allowed: {sorted(allowed)}",
        )
    return params


def _require_sheet(action: str, sheet: str | None) -> str:
    """Return ``sheet`` or raise ``missing_sheet`` (every dimensions op targets one tab)."""
    if not sheet:
        raise SheetsError(
            "missing_sheet",
            f"action {action!r} targets one sheet — pass sheet=<tab name>",
        )
    return sheet


def _sheet_id_for(services: SheetsServices, spreadsheet_id: str, sheet: str) -> int:
    """Resolve a sheet NAME to its ``sheetId`` (reusing the addressing layer's cache)."""
    return a1_to_gridrange(services, spreadsheet_id, sheet)["sheetId"]


def _require_dimension(action: str, params: dict) -> str:
    """Return a validated ``dimension`` (``ROWS``/``COLUMNS``) or raise."""
    dimension = params.get("dimension")
    if dimension is None:
        raise SheetsError(
            "missing_param",
            f"action {action!r} requires params={{'dimension': 'ROWS'|'COLUMNS'}}",
        )
    if dimension not in _DIMENSIONS:
        raise SheetsError(
            "bad_param",
            f"dimension must be 'ROWS' or 'COLUMNS', got {dimension!r}",
        )
    return dimension


def _require_span(action: str, params: dict) -> tuple[int, int]:
    """Return a validated ``(start, end)`` 0-based half-open span or raise.

    Both endpoints are required; ``end`` must be strictly greater than ``start`` (a 0-width
    span is a no-op and rejected) and ``start`` must be non-negative.
    """
    start = params.get("start")
    end = params.get("end")
    if start is None or end is None:
        raise SheetsError(
            "missing_param",
            f"action {action!r} requires both 'start' and 'end' (0-based half-open) in params",
        )
    if isinstance(start, bool) or isinstance(end, bool):
        raise SheetsError("bad_param", "'start'/'end' must be integers, not booleans")
    try:
        start = int(start)
        end = int(end)
    except (TypeError, ValueError) as exc:
        raise SheetsError(
            "bad_param", f"'start'/'end' must be integers in {action!r}"
        ) from exc
    if start < 0:
        raise SheetsError("bad_param", f"'start' must be >= 0, got {start}")
    if end <= start:
        raise SheetsError(
            "bad_param",
            f"'end' ({end}) must be > 'start' ({start}) — a 0-width span is a no-op",
        )
    return start, end


def _require_int(action: str, params: dict, key: str, *, minimum: int = 0) -> int:
    """Return ``int(params[key])`` (>= ``minimum``) or raise ``missing_param``/``bad_param``."""
    value = params.get(key)
    if value is None:
        raise SheetsError(
            "missing_param", f"action {action!r} requires params={{{key!r}: <int>}}"
        )
    if isinstance(value, bool):
        raise SheetsError("bad_param", f"{key!r} must be an integer, not a boolean")
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise SheetsError("bad_param", f"{key!r} must be an integer") from exc
    if value < minimum:
        raise SheetsError("bad_param", f"{key!r} must be >= {minimum}, got {value}")
    return value


def _dimension_range(sheet_id: int, dimension: str, start: int, end: int) -> dict:
    """Build a Google ``DimensionRange`` (0-based, half-open)."""
    return {
        "sheetId": sheet_id,
        "dimension": dimension,
        "startIndex": start,
        "endIndex": end,
    }


def _batch_update(
    services: SheetsServices, spreadsheet_id: str, requests: list[dict]
) -> dict:
    """Issue one ``spreadsheets.batchUpdate`` and return the raw response (classifies errors)."""
    try:
        return (
            services.sheets.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests},
            )
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc


# ---------------------------------------------------------------------------------------
# insert
# ---------------------------------------------------------------------------------------


def _dim_insert(services, spreadsheet_id, sheet, params) -> dict:
    """``insertDimension`` over a ``DimensionRange`` (optional ``inheritFromBefore``)."""
    dimension = _require_dimension("insert", params)
    start, end = _require_span("insert", params)
    sheet_id = _sheet_id_for(services, spreadsheet_id, sheet)
    req_body: dict = {"range": _dimension_range(sheet_id, dimension, start, end)}
    if params.get("inheritFromBefore") is not None:
        req_body["inheritFromBefore"] = bool(params["inheritFromBefore"])
    _batch_update(services, spreadsheet_id, [{"insertDimension": req_body}])
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "insert",
        "sheet": sheet,
        "dimension": dimension,
        "start": start,
        "end": end,
    }


# ---------------------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------------------


def _dim_delete(services, spreadsheet_id, sheet, params) -> dict:
    """``deleteDimension`` over a ``DimensionRange``."""
    dimension = _require_dimension("delete", params)
    start, end = _require_span("delete", params)
    sheet_id = _sheet_id_for(services, spreadsheet_id, sheet)
    _batch_update(
        services,
        spreadsheet_id,
        [{"deleteDimension": {"range": _dimension_range(sheet_id, dimension, start, end)}}],
    )
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "delete",
        "sheet": sheet,
        "dimension": dimension,
        "start": start,
        "end": end,
    }


# ---------------------------------------------------------------------------------------
# move
# ---------------------------------------------------------------------------------------


def _dim_move(services, spreadsheet_id, sheet, params) -> dict:
    """``moveDimension``: move the ``source`` ``DimensionRange`` to ``destinationIndex``."""
    dimension = _require_dimension("move", params)
    start, end = _require_span("move", params)
    destination_index = _require_int("move", params, "destinationIndex")
    sheet_id = _sheet_id_for(services, spreadsheet_id, sheet)
    _batch_update(
        services,
        spreadsheet_id,
        [
            {
                "moveDimension": {
                    "source": _dimension_range(sheet_id, dimension, start, end),
                    "destinationIndex": destination_index,
                }
            }
        ],
    )
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "move",
        "sheet": sheet,
        "dimension": dimension,
        "start": start,
        "end": end,
        "destinationIndex": destination_index,
    }


# ---------------------------------------------------------------------------------------
# append
# ---------------------------------------------------------------------------------------


def _dim_append(services, spreadsheet_id, sheet, params) -> dict:
    """``appendDimension``: append ``length`` rows/cols to the end of the sheet.

    ``appendDimension`` carries ``sheetId`` directly (not a ``DimensionRange``); the new
    rows/cols are always added at the end, so there is no start/end span.
    """
    dimension = _require_dimension("append", params)
    length = _require_int("append", params, "length", minimum=1)
    sheet_id = _sheet_id_for(services, spreadsheet_id, sheet)
    _batch_update(
        services,
        spreadsheet_id,
        [
            {
                "appendDimension": {
                    "sheetId": sheet_id,
                    "dimension": dimension,
                    "length": length,
                }
            }
        ],
    )
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "append",
        "sheet": sheet,
        "dimension": dimension,
        "length": length,
    }


# ---------------------------------------------------------------------------------------
# auto_resize
# ---------------------------------------------------------------------------------------


def _dim_auto_resize(services, spreadsheet_id, sheet, params) -> dict:
    """``autoResizeDimensions``: auto-fit a span (omit start/end ⇒ whole sheet).

    ``AutoResizeDimensionsRequest`` carries a single ``DimensionRange`` under ``dimensions``.
    When neither ``start`` nor ``end`` is given the range spans the whole sheet (both indices
    omitted, per the API). Passing only ONE of start/end is rejected (an unbounded-on-one-side
    span is ambiguous here).
    """
    dimension = _require_dimension("auto_resize", params)
    start = params.get("start")
    end = params.get("end")
    sheet_id = _sheet_id_for(services, spreadsheet_id, sheet)

    dimension_range: dict = {"sheetId": sheet_id, "dimension": dimension}
    out: dict = {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "auto_resize",
        "sheet": sheet,
        "dimension": dimension,
    }
    if start is not None or end is not None:
        lo, hi = _require_span("auto_resize", params)
        dimension_range["startIndex"] = lo
        dimension_range["endIndex"] = hi
        out["start"] = lo
        out["end"] = hi

    _batch_update(
        services,
        spreadsheet_id,
        [{"autoResizeDimensions": {"dimensions": dimension_range}}],
    )
    return out


# ---------------------------------------------------------------------------------------
# set_props
# ---------------------------------------------------------------------------------------


def _dim_set_props(services, spreadsheet_id, sheet, params) -> dict:
    """``updateDimensionProperties``: set ``pixelSize``/``hiddenByUser`` over a span.

    The ``fields`` mask is AUTO-built from the present ``DimensionProperties`` keys via
    ``build_fields_mask`` (so an unspecified subfield is never wiped). At least one of
    ``pixelSize``/``hiddenByUser`` is required — an empty payload is a no-op and rejected.
    """
    dimension = _require_dimension("set_props", params)
    start, end = _require_span("set_props", params)
    sheet_id = _sheet_id_for(services, spreadsheet_id, sheet)

    properties: dict = {}
    if params.get("pixelSize") is not None:
        properties["pixelSize"] = _require_int(
            "set_props", params, "pixelSize", minimum=0
        )
    if params.get("hiddenByUser") is not None:
        properties["hiddenByUser"] = bool(params["hiddenByUser"])
    if not properties:
        raise SheetsError(
            "empty_payload",
            "set_props needs at least one of 'pixelSize'/'hiddenByUser' to change",
        )

    fields = build_fields_mask(properties)
    _batch_update(
        services,
        spreadsheet_id,
        [
            {
                "updateDimensionProperties": {
                    "range": _dimension_range(sheet_id, dimension, start, end),
                    "properties": properties,
                    "fields": fields,
                }
            }
        ],
    )
    out: dict = {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "set_props",
        "sheet": sheet,
        "dimension": dimension,
        "start": start,
        "end": end,
        "appliedFields": fields,
    }
    if "pixelSize" in properties:
        out["pixelSize"] = properties["pixelSize"]
    if "hiddenByUser" in properties:
        out["hiddenByUser"] = properties["hiddenByUser"]
    return out


# ---------------------------------------------------------------------------------------
# read (hiddenByUser — the set_props read side)
# ---------------------------------------------------------------------------------------

#: Tight read mask: only the per-dimension ``hiddenByUser`` flags + the data block origin.
_READ_FIELDS = (
    "sheets(properties(sheetId,title),"
    "data(rowMetadata.hiddenByUser,columnMetadata.hiddenByUser,startRow,startColumn))"
)


def _dim_read(services, spreadsheet_id, sheet, params) -> dict:
    """Read ``hiddenByUser`` rows/cols over ``range`` (or the whole ``sheet``).

    Pulls a tight ``spreadsheets.get`` over ``data(rowMetadata.hiddenByUser,
    columnMetadata.hiddenByUser, startRow, startColumn)`` (never grid data) and returns the
    ABSOLUTE 0-based indices of hidden rows/cols. ``params['range']`` narrows the scan; absent
    it scans the whole ``sheet``.
    """
    range_a1 = params.get("range")
    scan_range = range_a1 if range_a1 is not None else sheet
    try:
        resp = (
            services.sheets.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                ranges=[scan_range],
                fields=_READ_FIELDS,
            )
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc

    hidden_rows, hidden_cols = _collect_hidden(resp, sheet)
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "read",
        "sheet": sheet,
        "hiddenRows": hidden_rows,
        "hiddenCols": hidden_cols,
    }


def _collect_hidden(resp: dict, sheet: str) -> tuple[list[int], list[int]]:
    """Collect absolute 0-based hidden row/col indices from a metadata ``get`` response.

    Each ``data`` block carries a ``startRow``/``startColumn`` origin (absent ⇒ 0) and a
    ``rowMetadata``/``columnMetadata`` array whose entries are ``DimensionProperties`` (the
    ``hiddenByUser`` flag is present only on hidden ones). Indices are made ABSOLUTE by adding
    the block origin, de-duplicated across overlapping blocks, and returned sorted.

    Only the ``data`` blocks of the SHEET that matches ``sheet`` by title are considered (the
    ``get`` was range-scoped to one sheet, but guarding on the title keeps the collector
    robust if the response ever carries more).
    """
    hidden_rows: set[int] = set()
    hidden_cols: set[int] = set()
    for entry in resp.get("sheets", []) or []:
        props = (entry or {}).get("properties", {}) or {}
        if props.get("title") != sheet:
            continue
        for block in entry.get("data", []) or []:
            start_row = block.get("startRow", 0) or 0
            start_col = block.get("startColumn", 0) or 0
            for offset, meta in enumerate(block.get("rowMetadata", []) or []):
                if isinstance(meta, dict) and meta.get("hiddenByUser"):
                    hidden_rows.add(start_row + offset)
            for offset, meta in enumerate(block.get("columnMetadata", []) or []):
                if isinstance(meta, dict) and meta.get("hiddenByUser"):
                    hidden_cols.add(start_col + offset)
    return sorted(hidden_rows), sorted(hidden_cols)


# ---------------------------------------------------------------------------------------
# Action -> handler dispatch table
# ---------------------------------------------------------------------------------------

_HANDLERS = {
    "insert": _dim_insert,
    "delete": _dim_delete,
    "move": _dim_move,
    "append": _dim_append,
    "auto_resize": _dim_auto_resize,
    "set_props": _dim_set_props,
    "read": _dim_read,
}
