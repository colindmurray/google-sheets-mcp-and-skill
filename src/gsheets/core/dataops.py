"""Data verbs over one-request ``batchUpdate`` data ops (DESIGN §X.2/§X.11/§X.14/§X.15).

One dispatch fn :func:`data_ops` covers the single-request ``batchUpdate`` data verbs that are
natural AI intents but should not be forced into the raw ``batch`` escape hatch:

- ``find_replace`` -> ``findReplace``
- ``delete_duplicates`` / ``trim_whitespace`` -> ``deleteDuplicates`` / ``trimWhitespace``
- ``sort_range`` / ``text_to_columns`` / ``auto_fill`` -> ``sortRange`` / ``textToColumns`` /
  ``autoFill``
- ``copy_paste`` / ``cut_paste`` -> ``copyPaste`` / ``cutPaste``

Each action consumes ONLY its documented ``params`` keys; an unknown key raises
``SheetsError("unknown_param")`` (the typed surface stays strict — ``params`` is NOT a raw escape
hatch, exactly like ``structure``/``manage_sheets`` in DESIGN §3.3). The returns surface the
action-specific reply summary from ``replies[]`` (e.g. ``find_replace`` ->
``occurrencesChanged``/``valuesChanged``/``formulasChanged``; ``delete_duplicates`` ->
``duplicatesRemoved``; ``trim_whitespace`` -> ``cellsChangedCount``) so a caller sees what changed
without a re-read.

This is a NEW pure-core module: it imports ONLY stdlib + sibling core modules
(``addressing``/``errors``/``service``/``fieldsmask``) + ``googleapiclient`` errors. It must
NEVER import ``fastmcp``, ``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models`` (DESIGN §1
boundary). All ranges are A1 strings resolved to ``GridRange`` at the edges via the addressing
layer; callers never pass a ``sheetId``.
"""

from __future__ import annotations

from googleapiclient.errors import HttpError

from .addressing import a1_to_gridrange
from .errors import SheetsError, classify_google_error
from .fieldsmask import build_fields_mask  # noqa: F401  (reserved for symmetry; unused today)
from .service import SheetsServices

# ---------------------------------------------------------------------------------------
# Action surface + per-action allowed params (LOCKED — DESIGN §X.2 data_ops table).
# ---------------------------------------------------------------------------------------

#: Every supported ``data_ops`` action.
_DATA_OPS_ACTIONS = frozenset(
    {
        "find_replace",
        "delete_duplicates",
        "trim_whitespace",
        "sort_range",
        "text_to_columns",
        "auto_fill",
        "copy_paste",
        "cut_paste",
    }
)

#: Per-action allowed ``params`` keys (LOCKED). An unknown key -> ``unknown_param``.
_DATA_OPS_PARAMS: dict[str, set[str]] = {
    "find_replace": {
        "find",
        "replacement",
        "searchByRegex",
        "matchCase",
        "matchEntireCell",
        "includeFormulas",
        "range",
        "sheet",
        "allSheets",
    },
    "delete_duplicates": {"range", "comparisonColumns"},
    "trim_whitespace": {"range"},
    "sort_range": {"range", "specs"},
    "text_to_columns": {"range", "delimiter", "delimiterType"},
    "auto_fill": {"range", "useAlternateSeries", "source", "destination"},
    "copy_paste": {"source", "destination", "pasteType", "pasteOrientation"},
    "cut_paste": {"source", "destination", "pasteType"},
}

#: Allowed ``sortRange`` / ``sortSpec`` order values.
_SORT_ORDERS = frozenset({"ASCENDING", "DESCENDING"})

#: Allowed ``textToColumns`` delimiter types.
_DELIMITER_TYPES = frozenset(
    {"COMMA", "SEMICOLON", "PERIOD", "SPACE", "CUSTOM", "AUTODETECT"}
)

#: Allowed ``copyPaste`` / ``cutPaste`` paste types.
_PASTE_TYPES = frozenset(
    {
        "PASTE_NORMAL",
        "PASTE_VALUES",
        "PASTE_FORMAT",
        "PASTE_NO_BORDERS",
        "PASTE_FORMULA",
        "PASTE_DATA_VALIDATION",
        "PASTE_CONDITIONAL_FORMATTING",
    }
)

#: Allowed ``copyPaste`` paste orientations.
_PASTE_ORIENTATIONS = frozenset({"NORMAL", "TRANSPOSE"})


# ---------------------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------------------


def data_ops(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    action: str,
    params: dict | None = None,
) -> dict:
    """Run one ``batchUpdate`` data verb and surface its reply summary (DESIGN §X.2).

    Dispatches over the one-request ``batchUpdate`` data ops: ``find_replace``,
    ``delete_duplicates``/``trim_whitespace``, ``sort_range``/``text_to_columns``/
    ``auto_fill``, and ``copy_paste``/``cut_paste``. Each action consumes only its
    documented ``params`` keys; an unknown key raises ``SheetsError("unknown_param")``. All
    ranges are A1 strings, resolved to ``GridRange`` internally via the addressing layer.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        action: One of the ``data_ops`` actions (see DESIGN §X.2 table).
        params: Per-action parameter dict (see the LOCKED key table).

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "action": ..., ...action-specific summary...}``.
        ``find_replace`` surfaces ``occurrencesChanged``/``valuesChanged``/``formulasChanged``/
        ``rowsChanged``/``sheetsChanged``; ``delete_duplicates`` surfaces ``duplicatesRemoved``;
        ``trim_whitespace`` surfaces ``cellsChangedCount``; the geometry verbs echo their
        resolved source/destination/range.

    Raises:
        SheetsError: ``unknown_action``/``unknown_param``/``missing_param``/``bad_range`` on bad
            input; ``google_api_error`` on an API failure.
    """
    if action not in _DATA_OPS_ACTIONS:
        raise SheetsError(
            "unknown_action",
            f"unknown data_ops action {action!r}; expected one of "
            f"{sorted(_DATA_OPS_ACTIONS)}",
        )
    params = _require_params(action, params, _DATA_OPS_PARAMS[action])
    handler = _HANDLERS[action]
    return handler(services, spreadsheet_id, params)


# ---------------------------------------------------------------------------------------
# Shared helpers (kept local so the module stays dependency-light, DESIGN §X.2)
# ---------------------------------------------------------------------------------------


def _require_params(action: str, params: dict | None, allowed: set[str]) -> dict:
    """Return a validated ``params`` dict, rejecting unknown keys (DESIGN §3.3 pattern).

    An unknown key for the given ``action`` raises ``SheetsError("unknown_param")`` so the typed
    surface stays strict (``params`` is NOT a raw escape hatch). ``None`` is treated as an empty
    dict.
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


def _require_param(action: str, params: dict, key: str) -> object:
    """Return ``params[key]`` or raise ``missing_param`` when absent/blank."""
    value = params.get(key)
    if value is None or (isinstance(value, str) and value == ""):
        raise SheetsError(
            "missing_param", f"action {action!r} requires params={{{key!r}: ...}}"
        )
    return value


def _grid(services: SheetsServices, spreadsheet_id: str, a1: str) -> dict:
    """Resolve an A1 range to a ``GridRange`` (delegates to the addressing layer)."""
    return a1_to_gridrange(services, spreadsheet_id, a1)


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


def _first_reply(resp: dict, reply_key: str) -> dict:
    """Return ``replies[0][reply_key]`` as a dict, or ``{}`` when absent.

    The data verbs issue exactly ONE request, so the matching reply (when any) is at index 0.
    ``sortRange``/``textToColumns``/``autoFill``/``copyPaste``/``cutPaste`` return an EMPTY reply
    (``{}``); only ``findReplace``/``deleteDuplicates``/``trimWhitespace`` carry a summary.
    """
    replies = resp.get("replies") or []
    if not replies:
        return {}
    first = replies[0]
    if not isinstance(first, dict):
        return {}
    child = first.get(reply_key)
    return child if isinstance(child, dict) else {}


# ---------------------------------------------------------------------------------------
# find_replace
# ---------------------------------------------------------------------------------------

#: ``FindReplaceResponse`` count fields surfaced into the return summary (in canonical order).
_FIND_REPLACE_COUNTS: tuple[str, ...] = (
    "valuesChanged",
    "formulasChanged",
    "rowsChanged",
    "sheetsChanged",
    "occurrencesChanged",
)


def _data_find_replace(services, spreadsheet_id, params) -> dict:
    """``findReplace`` over exactly ONE scope of ``range`` / ``sheet`` / ``allSheets``."""
    find = params.get("find")
    if find is None:
        raise SheetsError("missing_param", "find_replace requires params={'find': <str>}")
    replacement = params.get("replacement")
    if replacement is None:
        raise SheetsError(
            "missing_param", "find_replace requires params={'replacement': <str>}"
        )

    req_body: dict = {"find": str(find), "replacement": str(replacement)}
    for flag in ("searchByRegex", "matchCase", "matchEntireCell", "includeFormulas"):
        if params.get(flag) is not None:
            req_body[flag] = bool(params[flag])

    # Exactly one scope: range | sheet | allSheets.
    range_a1 = params.get("range")
    sheet = params.get("sheet")
    all_sheets = params.get("allSheets")
    scopes = [
        ("range", range_a1 is not None),
        ("sheet", sheet is not None),
        ("allSheets", bool(all_sheets)),
    ]
    chosen = [name for name, present in scopes if present]
    if len(chosen) != 1:
        raise SheetsError(
            "conflicting_args",
            "find_replace needs EXACTLY one scope of {'range', 'sheet', 'allSheets'}, "
            f"got {chosen or '(none)'}",
        )

    scope_out: dict = {}
    if range_a1 is not None:
        req_body["range"] = _grid(services, spreadsheet_id, range_a1)
        scope_out["range"] = range_a1
    elif sheet is not None:
        sheet_id = _grid(services, spreadsheet_id, sheet)["sheetId"]
        req_body["sheetId"] = sheet_id
        scope_out["sheet"] = sheet
    else:
        req_body["allSheets"] = True
        scope_out["allSheets"] = True

    resp = _batch_update(
        services, spreadsheet_id, [{"findReplace": req_body}]
    )
    reply = _first_reply(resp, "findReplace")

    out: dict = {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "find_replace",
        **scope_out,
    }
    for key in _FIND_REPLACE_COUNTS:
        out[key] = int(reply.get(key, 0) or 0)
    return out


# ---------------------------------------------------------------------------------------
# delete_duplicates
# ---------------------------------------------------------------------------------------


def _data_delete_duplicates(services, spreadsheet_id, params) -> dict:
    """``deleteDuplicates`` over ``range`` with optional ``comparisonColumns`` (col letters)."""
    range_a1 = _require_param("delete_duplicates", params, "range")
    grid_range = _grid(services, spreadsheet_id, range_a1)
    req_body: dict = {"range": grid_range}

    comparison_columns = params.get("comparisonColumns")
    if comparison_columns is not None:
        req_body["comparisonColumns"] = _comparison_columns(
            grid_range, comparison_columns
        )

    resp = _batch_update(
        services, spreadsheet_id, [{"deleteDuplicates": req_body}]
    )
    reply = _first_reply(resp, "deleteDuplicates")
    # Google's DeleteDuplicatesResponse field is ``duplicatesRemovedCount`` (NOT
    # ``duplicatesRemoved``). We expose it under our stable ``duplicatesRemoved`` output key;
    # accept the legacy/alt name too for forward-compatibility, but the live field is the *Count*
    # form (verified against the real API — reading the wrong key always yielded 0).
    removed = reply.get("duplicatesRemovedCount")
    if removed is None:
        removed = reply.get("duplicatesRemoved", 0)
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "delete_duplicates",
        "range": range_a1,
        "duplicatesRemoved": int(removed or 0),
    }


def _comparison_columns(grid_range: dict, cols: object) -> list[dict]:
    """Build ``comparisonColumns`` (a list of single-column ``DimensionRange``s).

    Each entry is a column LETTER (``"A"``, ``"C"``) or a 0-based integer offset. Letters are
    interpreted as ABSOLUTE columns (their absolute 0-based index), matching how the API expects
    a ``DimensionRange``; an integer is used verbatim as an absolute column index. The resulting
    ``DimensionRange`` covers exactly that one column over the duplicate ``range``'s sheet.
    """
    if not isinstance(cols, (list, tuple)):
        raise SheetsError(
            "bad_param", "comparisonColumns must be a list of column letters or indices"
        )
    sheet_id = grid_range.get("sheetId")
    out: list[dict] = []
    for col in cols:
        index = _column_to_index(col)
        out.append(
            {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": index,
                "endIndex": index + 1,
            }
        )
    return out


def _column_to_index(col: object) -> int:
    """Convert a column LETTER (``"A"`` -> 0, ``"AA"`` -> 26) or int offset to a 0-based index."""
    if isinstance(col, bool):
        raise SheetsError("bad_param", "a comparison column must not be a boolean")
    if isinstance(col, int):
        if col < 0:
            raise SheetsError("bad_param", f"column index must be >= 0, got {col}")
        return col
    if isinstance(col, str) and col.strip():
        text = col.strip().upper()
        if not text.isalpha():
            raise SheetsError(
                "bad_param", f"column letter must be A-Z only, got {col!r}"
            )
        idx = 0
        for ch in text:
            idx = idx * 26 + (ord(ch) - ord("A") + 1)
        return idx - 1
    raise SheetsError(
        "bad_param", f"a comparison column must be a letter or int, got {col!r}"
    )


# ---------------------------------------------------------------------------------------
# trim_whitespace
# ---------------------------------------------------------------------------------------


def _data_trim_whitespace(services, spreadsheet_id, params) -> dict:
    """``trimWhitespace`` over ``range``."""
    range_a1 = _require_param("trim_whitespace", params, "range")
    grid_range = _grid(services, spreadsheet_id, range_a1)
    resp = _batch_update(
        services,
        spreadsheet_id,
        [{"trimWhitespace": {"range": grid_range}}],
    )
    reply = _first_reply(resp, "trimWhitespace")
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "trim_whitespace",
        "range": range_a1,
        "cellsChangedCount": int(reply.get("cellsChangedCount", 0) or 0),
    }


# ---------------------------------------------------------------------------------------
# sort_range
# ---------------------------------------------------------------------------------------


def _data_sort_range(services, spreadsheet_id, params) -> dict:
    """``sortRange`` over ``range`` with one or more ``sortSpec``s (col letter + order)."""
    range_a1 = _require_param("sort_range", params, "range")
    specs = params.get("specs")
    if not isinstance(specs, (list, tuple)) or not specs:
        raise SheetsError(
            "missing_param",
            "sort_range requires params={'specs': [{'col': 'B', 'order': "
            "'ASCENDING'|'DESCENDING'}, ...]}",
        )
    grid_range = _grid(services, spreadsheet_id, range_a1)
    start_col = grid_range.get("startColumnIndex", 0) or 0

    sort_specs: list[dict] = []
    for spec in specs:
        if not isinstance(spec, dict):
            raise SheetsError("bad_param", "each sort spec must be a dict")
        col = spec.get("col")
        if col is None:
            raise SheetsError("missing_param", "a sort spec requires 'col'")
        order = spec.get("order", "ASCENDING")
        if order not in _SORT_ORDERS:
            raise SheetsError(
                "bad_param",
                f"sort order must be one of {sorted(_SORT_ORDERS)}, got {order!r}",
            )
        # ``dimensionIndex`` is RELATIVE to the sorted range's first column.
        dimension_index = _column_to_index(col) - start_col
        if dimension_index < 0:
            raise SheetsError(
                "bad_param",
                f"sort column {col!r} falls outside the sorted range {range_a1!r}",
            )
        sort_specs.append(
            {"dimensionIndex": dimension_index, "sortOrder": order}
        )

    _batch_update(
        services,
        spreadsheet_id,
        [{"sortRange": {"range": grid_range, "sortSpecs": sort_specs}}],
    )
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "sort_range",
        "range": range_a1,
        "specs": [
            {"col": _normalize_col(spec.get("col")), "order": spec.get("order", "ASCENDING")}
            for spec in specs
        ],
    }


def _normalize_col(col: object) -> object:
    """Echo a sort spec's column back as-given (letter string or int) for the return summary."""
    return col


# ---------------------------------------------------------------------------------------
# text_to_columns
# ---------------------------------------------------------------------------------------


def _data_text_to_columns(services, spreadsheet_id, params) -> dict:
    """``textToColumns`` over a single-column ``range`` with a delimiter / delimiter type."""
    range_a1 = _require_param("text_to_columns", params, "range")
    grid_range = _grid(services, spreadsheet_id, range_a1)
    req_body: dict = {"source": grid_range}

    delimiter_type = params.get("delimiterType")
    delimiter = params.get("delimiter")
    if delimiter_type is not None:
        if delimiter_type not in _DELIMITER_TYPES:
            raise SheetsError(
                "bad_param",
                f"delimiterType must be one of {sorted(_DELIMITER_TYPES)}, "
                f"got {delimiter_type!r}",
            )
        req_body["delimiterType"] = delimiter_type
    elif delimiter is not None:
        # A bare delimiter implies a CUSTOM type per the API.
        req_body["delimiterType"] = "CUSTOM"
    if delimiter is not None:
        req_body["delimiter"] = str(delimiter)

    _batch_update(
        services, spreadsheet_id, [{"textToColumns": req_body}]
    )
    out: dict = {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "text_to_columns",
        "range": range_a1,
    }
    if "delimiterType" in req_body:
        out["delimiterType"] = req_body["delimiterType"]
    if delimiter is not None:
        out["delimiter"] = str(delimiter)
    return out


# ---------------------------------------------------------------------------------------
# auto_fill
# ---------------------------------------------------------------------------------------


def _data_auto_fill(services, spreadsheet_id, params) -> dict:
    """``autoFill`` in one of two forms: a single ``range`` OR a ``source``->``destination`` pair."""
    range_a1 = params.get("range")
    source = params.get("source")
    destination = params.get("destination")
    use_alternate = params.get("useAlternateSeries")

    has_range = range_a1 is not None
    has_pair = source is not None or destination is not None
    if has_range and has_pair:
        raise SheetsError(
            "conflicting_args",
            "auto_fill takes EITHER {'range'} OR {'source','destination'}, not both",
        )
    if not has_range and not has_pair:
        raise SheetsError(
            "missing_param",
            "auto_fill requires {'range'} OR {'source','destination'}",
        )

    req_body: dict = {}
    if use_alternate is not None:
        req_body["useAlternateSeries"] = bool(use_alternate)

    out: dict = {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "auto_fill",
    }
    if has_range:
        req_body["range"] = _grid(services, spreadsheet_id, range_a1)
        out["range"] = range_a1
    else:
        if source is None or destination is None:
            raise SheetsError(
                "missing_param",
                "auto_fill source/destination form requires BOTH 'source' and 'destination'",
            )
        req_body["sourceAndDestination"] = {
            "source": _grid(services, spreadsheet_id, source),
            "dimension": _fill_dimension(
                _grid(services, spreadsheet_id, source),
                _grid(services, spreadsheet_id, destination),
            ),
            "fillLength": _fill_length(
                _grid(services, spreadsheet_id, source),
                _grid(services, spreadsheet_id, destination),
            ),
        }
        out["source"] = source
        out["destination"] = destination
    if use_alternate is not None:
        out["useAlternateSeries"] = bool(use_alternate)

    _batch_update(services, spreadsheet_id, [{"autoFill": req_body}])
    return out


def _fill_dimension(source_gr: dict, dest_gr: dict) -> str:
    """Infer the fill ``dimension`` (ROWS/COLUMNS) from how source and destination differ.

    ``SourceAndDestination`` extends ``source`` along one dimension. If the destination grows the
    column span beyond the source it is a COLUMNS fill; otherwise (the common vertical case) it is
    a ROWS fill.
    """
    src_cols = _span(source_gr, "startColumnIndex", "endColumnIndex")
    dst_cols = _span(dest_gr, "startColumnIndex", "endColumnIndex")
    if dst_cols is not None and src_cols is not None and dst_cols > src_cols:
        return "COLUMNS"
    return "ROWS"


def _fill_length(source_gr: dict, dest_gr: dict) -> int:
    """Compute ``fillLength`` = how many rows/cols beyond the source the destination extends.

    Positive fills DOWN/RIGHT; the API also accepts a negative length for UP/LEFT fills. We
    measure along the inferred dimension; a non-determinable span degrades to 0 (no-op safe).
    """
    dimension = _fill_dimension(source_gr, dest_gr)
    if dimension == "COLUMNS":
        src = _span(source_gr, "startColumnIndex", "endColumnIndex")
        dst = _span(dest_gr, "startColumnIndex", "endColumnIndex")
    else:
        src = _span(source_gr, "startRowIndex", "endRowIndex")
        dst = _span(dest_gr, "startRowIndex", "endRowIndex")
    if src is None or dst is None:
        return 0
    return max(dst - src, 0)


def _span(gr: dict, start_key: str, end_key: str) -> int | None:
    """Return the half-open span ``end - start`` for a GridRange axis, or ``None`` if unbounded."""
    start = gr.get(start_key)
    end = gr.get(end_key)
    if start is None or end is None:
        return None
    return int(end) - int(start)


# ---------------------------------------------------------------------------------------
# copy_paste
# ---------------------------------------------------------------------------------------


def _data_copy_paste(services, spreadsheet_id, params) -> dict:
    """``copyPaste`` from ``source`` to ``destination`` with a paste type/orientation."""
    source = _require_param("copy_paste", params, "source")
    destination = _require_param("copy_paste", params, "destination")
    paste_type = _validate_paste_type(params.get("pasteType"))
    orientation = params.get("pasteOrientation")
    if orientation is not None and orientation not in _PASTE_ORIENTATIONS:
        raise SheetsError(
            "bad_param",
            f"pasteOrientation must be one of {sorted(_PASTE_ORIENTATIONS)}, "
            f"got {orientation!r}",
        )

    req_body: dict = {
        "source": _grid(services, spreadsheet_id, source),
        "destination": _grid(services, spreadsheet_id, destination),
    }
    if paste_type is not None:
        req_body["pasteType"] = paste_type
    if orientation is not None:
        req_body["pasteOrientation"] = orientation

    _batch_update(services, spreadsheet_id, [{"copyPaste": req_body}])
    out: dict = {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "copy_paste",
        "source": source,
        "destination": destination,
    }
    if paste_type is not None:
        out["pasteType"] = paste_type
    if orientation is not None:
        out["pasteOrientation"] = orientation
    return out


# ---------------------------------------------------------------------------------------
# cut_paste
# ---------------------------------------------------------------------------------------


def _data_cut_paste(services, spreadsheet_id, params) -> dict:
    """``cutPaste`` from ``source`` to a top-left ``destination`` coordinate.

    ``cutPaste`` destination is a ``GridCoordinate`` (a top-left anchor), not a full range — we
    take the resolved destination GridRange's top-left ``(startRowIndex, startColumnIndex)``.
    """
    source = _require_param("cut_paste", params, "source")
    destination = _require_param("cut_paste", params, "destination")
    paste_type = _validate_paste_type(params.get("pasteType"))

    dest_gr = _grid(services, spreadsheet_id, destination)
    req_body: dict = {
        "source": _grid(services, spreadsheet_id, source),
        "destination": {
            "sheetId": dest_gr.get("sheetId"),
            "rowIndex": dest_gr.get("startRowIndex", 0) or 0,
            "columnIndex": dest_gr.get("startColumnIndex", 0) or 0,
        },
    }
    if paste_type is not None:
        req_body["pasteType"] = paste_type

    _batch_update(services, spreadsheet_id, [{"cutPaste": req_body}])
    out: dict = {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "cut_paste",
        "source": source,
        "destination": destination,
    }
    if paste_type is not None:
        out["pasteType"] = paste_type
    return out


def _validate_paste_type(paste_type: object) -> str | None:
    """Validate an optional ``pasteType``; ``None`` passes through (API default PASTE_NORMAL)."""
    if paste_type is None:
        return None
    if paste_type not in _PASTE_TYPES:
        raise SheetsError(
            "bad_param",
            f"pasteType must be one of {sorted(_PASTE_TYPES)}, got {paste_type!r}",
        )
    return paste_type


# ---------------------------------------------------------------------------------------
# Action -> handler dispatch table
# ---------------------------------------------------------------------------------------

_HANDLERS = {
    "find_replace": _data_find_replace,
    "delete_duplicates": _data_delete_duplicates,
    "trim_whitespace": _data_trim_whitespace,
    "sort_range": _data_sort_range,
    "text_to_columns": _data_text_to_columns,
    "auto_fill": _data_auto_fill,
    "copy_paste": _data_copy_paste,
    "cut_paste": _data_cut_paste,
}
