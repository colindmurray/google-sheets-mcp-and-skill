"""Row/column dimension ops + ``hiddenByUser`` read (DESIGN §X.7/§X.10/§X.13).

One dispatch fn :func:`dimensions` covers the row/column dimension verbs — a distinct,
high-frequency intent kept OFF ``manage_sheets`` (which is tab-level) on purpose (DESIGN
§X.7 decision: a separate pure module keeps both clean and keeps each integration unit
single-file). The write verbs each map to ONE ``batchUpdate`` request:

- ``insert`` -> ``insertDimension``
- ``delete`` -> ``deleteDimension``
- ``move`` -> ``moveDimension``
- ``append`` -> ``appendDimension``
- ``auto_resize`` -> ``autoResizeDimensions`` (AUTO-FIT to content — "auto-resize ON")
- ``set_props`` -> ``updateDimensionProperties`` (auto fields mask via
  ``build_fields_mask``). One ``set_props`` call sets a single span OR, via
  ``params["runs"]``, MANY spans at once (one ``batchUpdate``, one request per run; runs may
  mix ROWS and COLUMNS).

Together these form a row/column SIZING toolkit. The two size modes a caller chooses between:

- FIXED / FORCED size -- ``set_props {"pixelSize": N}`` pins the size; the row/col will NOT
  auto-grow to fit content (the ``updateDimensionProperties`` equivalent of Apps Script's
  ``setRowHeightsForced``). Use for a stable layout that ignores content height.
- AUTO-FIT size -- ``auto_resize`` sizes the row/col to its content. Use to "turn auto-resize
  back on" / fit-to-content.

The read verb is ``set_props``'s read-side counterpart:

- ``read`` -> ``spreadsheets.get`` over ``data(rowMetadata.hiddenByUser,
  columnMetadata.hiddenByUser, startRow, startColumn)`` -> ``{"hiddenRows":[idx,…],
  "hiddenCols":[idx,…]}`` (absolute 0-based indices). With ``params["sizes"]=True`` it widens
  the mask to also pull ``pixelSize`` and returns ``rowHeights``/``colWidths`` as coalesced
  ``{"start","end","pixelSize"}`` runs (absolute 0-based, half-open ``end``). API LIMITATION:
  the Sheets API exposes only ``pixelSize``/``hiddenByUser`` per dimension, so ``read`` can
  report the current pixel size but CANNOT report whether a row/col is fixed vs auto-fit.

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
    "set_props": {"dimension", "start", "end", "pixelSize", "hiddenByUser", "runs"},
    "read": {"range", "sizes"},
}

#: Valid ``Dimension`` enum values.
_DIMENSIONS = frozenset({"ROWS", "COLUMNS"})

#: Allowed keys inside a bulk ``set_props`` run (strict — same vocabulary as the single path).
_RUN_KEYS = frozenset({"dimension", "start", "end", "pixelSize", "hiddenByUser"})

#: The single-span keys that are mutually exclusive with the bulk ``runs`` form.
_SINGLE_SPAN_KEYS = frozenset({"dimension", "start", "end", "pixelSize", "hiddenByUser"})


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
    """``updateDimensionProperties``: set ``pixelSize``/``hiddenByUser`` over a span (or many).

    Two forms share this handler:

    - SINGLE span: ``{dimension,start,end,pixelSize?,hiddenByUser?}`` — one request.
    - BULK ``runs``: ``{"runs": [{dimension,start,end,pixelSize?,hiddenByUser?}, …]}`` — one
      ``batchUpdate`` carrying one ``updateDimensionProperties`` per run, so a from-scratch
      layout can set several column widths AND several row heights in a single call. ``runs``
      is mutually exclusive with the single-span keys.

    Either way the ``fields`` mask is AUTO-built from the present ``DimensionProperties`` keys
    via ``build_fields_mask`` (so an unspecified subfield is never wiped), and at least one of
    ``pixelSize``/``hiddenByUser`` is required per span — an empty payload is a no-op and rejected.
    """
    # The PRESENCE of the key (even ``runs: null``) selects the bulk path, so a malformed
    # ``runs`` surfaces a bulk-shape error and the mutual-exclusion guard still fires — it never
    # silently falls back to a single-span write.
    if "runs" in params:
        return _dim_set_props_bulk(services, spreadsheet_id, sheet, params)

    dimension = _require_dimension("set_props", params)
    start, end = _require_span("set_props", params)
    properties = _set_props_payload("set_props", params)
    sheet_id = _sheet_id_for(services, spreadsheet_id, sheet)

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


def _set_props_payload(action: str, run: dict) -> dict:
    """Build the ``DimensionProperties`` payload for one span (single or bulk run).

    Reads ``pixelSize``/``hiddenByUser`` from ``run`` with the SAME validation as the single
    path (non-negative integer pixelSize; truthy-coerced hidden flag) and rejects an empty
    payload — a span that changes neither field is a no-op.
    """
    properties: dict = {}
    if run.get("pixelSize") is not None:
        properties["pixelSize"] = _require_int(action, run, "pixelSize", minimum=0)
    if run.get("hiddenByUser") is not None:
        properties["hiddenByUser"] = bool(run["hiddenByUser"])
    if not properties:
        raise SheetsError(
            "empty_payload",
            "set_props needs at least one of 'pixelSize'/'hiddenByUser' to change",
        )
    return properties


def _dim_set_props_bulk(services, spreadsheet_id, sheet, params) -> dict:
    """Bulk ``set_props``: validate every run, emit ONE batchUpdate of N requests.

    ``runs`` is mutually exclusive with the single-span keys (passing both is a ``bad_param``).
    Each run is validated with the same dimension/span/payload rules as the single path; runs
    may freely mix ROWS and COLUMNS. ALL runs are validated BEFORE the sheet id is resolved
    (matching the single path's validate-then-resolve order, so a bad run never triggers an API
    call), and the resolved id is reused across runs.
    """
    overlap = sorted(_SINGLE_SPAN_KEYS & set(params))
    if overlap:
        raise SheetsError(
            "bad_param",
            f"set_props 'runs' is mutually exclusive with single-span keys; "
            f"also got {overlap}",
        )

    runs = params["runs"]
    if not isinstance(runs, list) or not runs:
        raise SheetsError("bad_param", "set_props 'runs' must be a non-empty list")

    # Validate every run up front (no sheet lookup yet) so invalid input fails fast and cheap.
    validated: list[tuple[str, int, int, dict, str]] = []
    for i, run in enumerate(runs):
        if not isinstance(run, dict):
            raise SheetsError("bad_param", f"set_props run #{i} must be a dict")
        unknown = set(run) - _RUN_KEYS
        if unknown:
            raise SheetsError(
                "unknown_param",
                f"set_props run #{i} has unknown keys {sorted(unknown)}; "
                f"allowed: {sorted(_RUN_KEYS)}",
            )
        dimension = _require_dimension("set_props", run)
        start, end = _require_span("set_props", run)
        properties = _set_props_payload("set_props", run)
        validated.append((dimension, start, end, properties, build_fields_mask(properties)))

    sheet_id = _sheet_id_for(services, spreadsheet_id, sheet)
    requests: list[dict] = []
    echoed: list[dict] = []
    for dimension, start, end, properties, fields in validated:
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": _dimension_range(sheet_id, dimension, start, end),
                    "properties": properties,
                    "fields": fields,
                }
            }
        )
        run_out: dict = {
            "dimension": dimension,
            "start": start,
            "end": end,
            "appliedFields": fields,
        }
        if "pixelSize" in properties:
            run_out["pixelSize"] = properties["pixelSize"]
        if "hiddenByUser" in properties:
            run_out["hiddenByUser"] = properties["hiddenByUser"]
        echoed.append(run_out)

    _batch_update(services, spreadsheet_id, requests)
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "set_props",
        "sheet": sheet,
        "runs": echoed,
        "count": len(echoed),
    }


# ---------------------------------------------------------------------------------------
# read (hiddenByUser — the set_props read side)
# ---------------------------------------------------------------------------------------

#: Tight read mask: only the per-dimension ``hiddenByUser`` flags + the data block origin.
_READ_FIELDS = (
    "sheets(properties(sheetId,title),"
    "data(rowMetadata.hiddenByUser,columnMetadata.hiddenByUser,startRow,startColumn))"
)

#: Extended read mask used when ``sizes`` is requested — adds ``pixelSize`` alongside the
#: ``hiddenByUser`` flag for both dimensions (still NO grid data).
_READ_FIELDS_WITH_SIZES = (
    "sheets(properties(sheetId,title),"
    "data(rowMetadata(hiddenByUser,pixelSize),"
    "columnMetadata(hiddenByUser,pixelSize),startRow,startColumn))"
)


def _dim_read(services, spreadsheet_id, sheet, params) -> dict:
    """Read hidden rows/cols (and optionally pixel sizes) over ``range`` (or the whole ``sheet``).

    Pulls a tight ``spreadsheets.get`` over the per-dimension metadata (never grid data) and
    returns the ABSOLUTE 0-based indices of hidden rows/cols. ``params['range']`` narrows the
    scan; absent it scans the whole ``sheet``.

    With ``params['sizes']`` truthy the mask widens to also pull ``pixelSize`` and the result
    gains ``rowHeights``/``colWidths``: lists of coalesced ``{start, end, pixelSize}`` runs
    (absolute 0-based, half-open ``end``). The Sheets API exposes only ``pixelSize``, so this
    reports the current pixel height/width but NOT whether a row/col is fixed vs auto-fit.
    """
    range_a1 = params.get("range")
    scan_range = range_a1 if range_a1 is not None else sheet
    want_sizes = bool(params.get("sizes"))
    fields = _READ_FIELDS_WITH_SIZES if want_sizes else _READ_FIELDS
    try:
        resp = (
            services.sheets.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                ranges=[scan_range],
                fields=fields,
            )
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc

    hidden_rows, hidden_cols = _collect_hidden(resp, sheet)
    out = {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "read",
        "sheet": sheet,
        "hiddenRows": hidden_rows,
        "hiddenCols": hidden_cols,
    }
    if want_sizes:
        row_runs, col_runs = _collect_sizes(resp, sheet)
        out["rowHeights"] = row_runs
        out["colWidths"] = col_runs
    return out


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


def _collect_sizes(resp: dict, sheet: str) -> tuple[list[dict], list[dict]]:
    """Collect coalesced pixel-size runs for rows and cols from a metadata ``get`` response.

    Mirrors :func:`_collect_hidden`: each ``data`` block carries a ``startRow``/``startColumn``
    origin (absent ⇒ 0) and a ``rowMetadata``/``columnMetadata`` array of ``DimensionProperties``;
    here we read each entry's ``pixelSize``. Indices are made ABSOLUTE by adding the block origin.

    To stay robust against overlapping/duplicate blocks we first build an index->size map, then
    coalesce CONSECUTIVE indices carrying the SAME pixelSize into one half-open run
    ``{"start", "end", "pixelSize"}``. A missing index or a size mismatch breaks the run, so a
    uniform 60-row block collapses to a single run while a gap or a size change splits it.
    """
    row_sizes: dict[int, int] = {}
    col_sizes: dict[int, int] = {}
    for entry in resp.get("sheets", []) or []:
        props = (entry or {}).get("properties", {}) or {}
        if props.get("title") != sheet:
            continue
        for block in entry.get("data", []) or []:
            start_row = block.get("startRow", 0) or 0
            start_col = block.get("startColumn", 0) or 0
            for offset, meta in enumerate(block.get("rowMetadata", []) or []):
                if isinstance(meta, dict) and meta.get("pixelSize") is not None:
                    row_sizes[start_row + offset] = meta["pixelSize"]
            for offset, meta in enumerate(block.get("columnMetadata", []) or []):
                if isinstance(meta, dict) and meta.get("pixelSize") is not None:
                    col_sizes[start_col + offset] = meta["pixelSize"]
    return _coalesce_runs(row_sizes), _coalesce_runs(col_sizes)


def _coalesce_runs(sizes: dict[int, int]) -> list[dict]:
    """Fold an index->pixelSize map into sorted, half-open ``{start, end, pixelSize}`` runs.

    Consecutive indices (no gap) sharing the same size extend the current run; a gap or a size
    change starts a new one. ``end`` is exclusive (a run covering indices 2..4 inclusive emits
    ``end=5``).
    """
    runs: list[dict] = []
    current: dict | None = None
    for idx in sorted(sizes):
        size = sizes[idx]
        if current is not None and idx == current["end"] and size == current["pixelSize"]:
            current["end"] = idx + 1
            continue
        current = {"start": idx, "end": idx + 1, "pixelSize": size}
        runs.append(current)
    return runs


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
