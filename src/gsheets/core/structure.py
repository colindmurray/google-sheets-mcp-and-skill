"""Structural ops, tab management, developer metadata + reply-id capture (DESIGN §3.3, §5.4).

Houses ``structure`` / ``manage_sheets`` / ``metadata`` plus :func:`capture_new_ids`.
``structure(action="read")`` returns a shape-stable multi-sheet envelope (``sheets`` always a
list; spreadsheet-scoped ``namedRanges`` at top level) shared with ``read_conditional_formats``.

PURE core module: imports only stdlib + sibling core modules + ``googleapiclient`` errors. It
must NEVER import ``fastmcp``, ``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models``
(DESIGN §1 boundary).
"""

from __future__ import annotations

from googleapiclient.errors import HttpError

from .addressing import a1_to_gridrange, gridrange_to_a1
from .colors import color_style_to_hex, hex_to_color_style
from .errors import SheetsError, classify_google_error
from .fieldsmask import build_fields_mask
from .service import SheetsServices

# ---------------------------------------------------------------------------------------
# Reply-id capture (DESIGN §5.4)
# ---------------------------------------------------------------------------------------

# Map a ``batchUpdate`` reply key -> (output bucket, the field that carries the new id).
# ``addSheet``/``duplicateSheet`` nest the id under ``properties.sheetId``; the rest carry the
# id directly under the named child object.
_REPLY_ID_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("addSheet", "sheetIds", ("properties", "sheetId")),
    ("duplicateSheet", "sheetIds", ("properties", "sheetId")),
    ("addChart", "chartIds", ("chart", "chartId")),
    ("addNamedRange", "namedRangeIds", ("namedRange", "namedRangeId")),
    ("addProtectedRange", "protectedRangeIds", ("protectedRange", "protectedRangeId")),
    ("createDeveloperMetadata", "metadataIds", ("developerMetadata", "metadataId")),
)


def capture_new_ids(replies: list[dict]) -> dict:
    """Surface new ids returned only in ``batchUpdate`` ``replies[]`` (DESIGN §5.4).

    ``addSheet``/``duplicateSheet``/``addChart``/``addNamedRange``/``addProtectedRange``/
    ``createDeveloperMetadata`` return new ids in ``replies[]``; this matches reply to request
    by order and extracts ``sheetId``/``chartId``/``namedRangeId``/``protectedRangeId``/
    ``metadataId`` so create+populate is one batch.

    Args:
        replies: The ``replies`` list from a ``batchUpdate`` response.

    Returns:
        A dict of captured id lists, always carrying every bucket key (empty lists when the
        corresponding reply kind is absent), e.g.
        ``{"sheetIds": [7], "chartIds": [], "namedRangeIds": [], "protectedRangeIds": [],
        "metadataIds": []}``.
    """
    out: dict[str, list] = {
        "sheetIds": [],
        "chartIds": [],
        "namedRangeIds": [],
        "protectedRangeIds": [],
        "metadataIds": [],
    }
    for reply in replies or []:
        if not isinstance(reply, dict):
            continue
        for reply_key, bucket, path in _REPLY_ID_SPECS:
            child = reply.get(reply_key)
            if not isinstance(child, dict):
                continue
            value = child
            for step in path:
                if not isinstance(value, dict):
                    value = None
                    break
                value = value.get(step)
            if value is not None:
                out[bucket].append(value)
    return out


# ---------------------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------------------


def _require_params(action: str, params: dict | None, allowed: set[str]) -> dict:
    """Return a validated ``params`` dict, rejecting unknown keys (DESIGN §3.3).

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
    """Return ``sheet`` or raise ``missing_sheet`` for a mutating action that needs a tab."""
    if not sheet:
        raise SheetsError(
            "missing_sheet",
            f"action {action!r} targets one sheet — pass sheet=<tab name>",
        )
    return sheet


def _require_range(action: str, range: str | None) -> str:
    """Return ``range`` or raise ``bad_range`` for a range-scoped action."""
    if not range:
        raise SheetsError(
            "bad_range", f"action {action!r} requires a range (A1)"
        )
    return range


def _sheet_id_for(services: SheetsServices, spreadsheet_id: str, sheet: str) -> int:
    """Resolve a sheet NAME to its ``sheetId`` (reusing the addressing layer's cache)."""
    gr = a1_to_gridrange(services, spreadsheet_id, sheet)
    return gr["sheetId"]


def _batch_update(services: SheetsServices, spreadsheet_id: str, requests: list[dict]) -> dict:
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
# structure
# ---------------------------------------------------------------------------------------

#: Mutating structure actions (everything except ``read``).
_STRUCTURE_ACTIONS = frozenset(
    {
        "read",
        "merge",
        "unmerge",
        "add_named",
        "delete_named",
        "protect",
        "unprotect",
        "freeze",
        "tab_color",
        "group",
        "ungroup",
    }
)

#: Per-action allowed ``params`` keys (LOCKED — DESIGN §3.3 structure table).
_STRUCTURE_PARAMS: dict[str, set[str]] = {
    "read": set(),
    "merge": {"mergeType"},
    "unmerge": set(),
    "add_named": {"name"},
    "delete_named": {"name", "namedRangeId"},
    "protect": {"description", "editors", "warningOnly"},
    "unprotect": {"protectedRangeId"},
    "freeze": {"rows", "cols"},
    "tab_color": {"color"},
    "group": {"dimension", "start", "end"},
    "ungroup": {"dimension", "start", "end"},
}

_MERGE_TYPES = frozenset({"MERGE_ALL", "MERGE_COLUMNS", "MERGE_ROWS"})
_DIMENSIONS = frozenset({"ROWS", "COLUMNS"})


def structure(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    action: str,
    sheet: str | None = None,
    range: str | None = None,
    params: dict | None = None,
) -> dict:
    """Read or modify spreadsheet structure through one interface (DESIGN §3.3).

    ``action="read"`` returns the shape-stable envelope (``sheets`` always a list;
    spreadsheet-scoped ``namedRanges`` at top level; ``sheet`` optional ⇒ every tab). Mutating
    actions (``merge``/``unmerge``/``add_named``/``delete_named``/``protect``/``unprotect``/
    ``freeze``/``tab_color``/``group``/``ungroup``) require ``sheet`` and use the matching
    ``batchUpdate`` request with an auto fields mask where applicable. Each action consumes only
    its documented ``params`` keys; unknown keys raise ``SheetsError("unknown_param")``.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        action: One of the structural actions (see DESIGN §3.3 ``structure`` table).
        sheet: Target tab name (optional for read; required for mutate).
        range: A1 range for range-scoped actions (merge/unmerge/add_named/protect).
        params: Per-action parameter dict (see the LOCKED key table).

    Returns:
        For read: the multi-sheet envelope. For mutate:
        ``{"ok": True, "spreadsheetId": ..., ...ids/ranges affected...}``.
    """
    if action not in _STRUCTURE_ACTIONS:
        raise SheetsError(
            "unknown_action",
            f"unknown structure action {action!r}; expected one of "
            f"{sorted(_STRUCTURE_ACTIONS)}",
        )
    params = _require_params(action, params, _STRUCTURE_PARAMS[action])

    if action == "read":
        return _structure_read(services, spreadsheet_id, sheet)

    handler = _STRUCTURE_HANDLERS[action]
    return handler(services, spreadsheet_id, sheet, range, params)


def _structure_read(
    services: SheetsServices, spreadsheet_id: str, sheet: str | None
) -> dict:
    """Read the full structural picture into the shape-stable multi-sheet envelope."""
    fields = (
        "namedRanges(name,namedRangeId,range),"
        "sheets(properties(sheetId,title,gridProperties(frozenRowCount,frozenColumnCount),"
        "tabColorStyle),merges,protectedRanges(protectedRangeId,range,description,editors,"
        "warningOnly),rowGroups(range,depth,collapsed),columnGroups(range,depth,collapsed))"
    )
    try:
        resp = (
            services.sheets.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields=fields)
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc

    all_sheets = resp.get("sheets", []) or []

    # Top-level spreadsheet-scoped named ranges.
    named_ranges: list[dict] = []
    for nr in resp.get("namedRanges", []) or []:
        entry: dict = {
            "name": nr.get("name"),
            "namedRangeId": nr.get("namedRangeId"),
        }
        gr = nr.get("range")
        if isinstance(gr, dict):
            entry["range"] = _safe_gridrange_to_a1(services, spreadsheet_id, gr)
        named_ranges.append(entry)

    sheets_out: list[dict] = []
    for entry in all_sheets:
        props = (entry or {}).get("properties", {}) or {}
        title = props.get("title")
        if sheet is not None and title != sheet:
            continue
        sheets_out.append(
            _serialize_sheet_structure(services, spreadsheet_id, entry, props)
        )

    if sheet is not None and not sheets_out:
        # Surface a clear error if a named sheet was requested but not present.
        available = ", ".join(
            repr(((s or {}).get("properties", {}) or {}).get("title"))
            for s in all_sheets
        )
        raise SheetsError(
            "sheet_not_found",
            f"sheet {sheet!r} not found in spreadsheet",
            hint=f"available sheets: {available or '(none)'}",
        )

    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "namedRanges": named_ranges,
        "sheets": sheets_out,
    }


def _serialize_sheet_structure(
    services: SheetsServices, spreadsheet_id: str, entry: dict, props: dict
) -> dict:
    """Serialize one sheet's structural data for the read envelope."""
    grid_props = props.get("gridProperties", {}) or {}
    out: dict = {
        "sheet": props.get("title"),
        "sheetId": props.get("sheetId"),
        "merges": [
            _safe_gridrange_to_a1(services, spreadsheet_id, m)
            for m in (entry.get("merges", []) or [])
        ],
        "frozenRows": grid_props.get("frozenRowCount", 0),
        "frozenCols": grid_props.get("frozenColumnCount", 0),
    }

    tab_color_style = props.get("tabColorStyle")
    if isinstance(tab_color_style, dict):
        try:
            out["tabColor"] = color_style_to_hex(tab_color_style)
        except ValueError:
            pass

    protected: list[dict] = []
    for pr in entry.get("protectedRanges", []) or []:
        p_entry: dict = {"protectedRangeId": pr.get("protectedRangeId")}
        gr = pr.get("range")
        if isinstance(gr, dict):
            p_entry["range"] = _safe_gridrange_to_a1(services, spreadsheet_id, gr)
        if pr.get("description") is not None:
            p_entry["description"] = pr.get("description")
        if pr.get("editors") is not None:
            users = (pr.get("editors") or {}).get("users")
            p_entry["editors"] = list(users) if users else []
        p_entry["warningOnly"] = bool(pr.get("warningOnly", False))
        protected.append(p_entry)
    out["protectedRanges"] = protected

    groups: list[dict] = []
    for dim, key in (("ROWS", "rowGroups"), ("COLUMNS", "columnGroups")):
        for g in entry.get(key, []) or []:
            gr = g.get("range", {}) or {}
            groups.append(
                {
                    "dimension": dim,
                    "start": gr.get("startIndex"),
                    "end": gr.get("endIndex"),
                    "depth": g.get("depth"),
                    "collapsed": bool(g.get("collapsed", False)),
                }
            )
    out["dimensionGroups"] = groups
    return out


def _safe_gridrange_to_a1(
    services: SheetsServices, spreadsheet_id: str, gr: dict
) -> str | None:
    """Convert a GridRange to A1, degrading to ``None`` if the sheetId can't be resolved."""
    try:
        return gridrange_to_a1(services, spreadsheet_id, gr)
    except SheetsError:
        return None


# --- structure mutators ---------------------------------------------------------------


def _structure_merge(services, spreadsheet_id, sheet, range, params) -> dict:
    range = _require_range("merge", range)
    merge_type = params.get("mergeType", "MERGE_ALL")
    if merge_type not in _MERGE_TYPES:
        raise SheetsError(
            "unknown_param",
            f"unknown mergeType {merge_type!r}; expected one of {sorted(_MERGE_TYPES)}",
        )
    grid_range = a1_to_gridrange(services, spreadsheet_id, range)
    _batch_update(
        services,
        spreadsheet_id,
        [{"mergeCells": {"range": grid_range, "mergeType": merge_type}}],
    )
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "merge",
        "range": range,
        "mergeType": merge_type,
    }


def _structure_unmerge(services, spreadsheet_id, sheet, range, params) -> dict:
    range = _require_range("unmerge", range)
    grid_range = a1_to_gridrange(services, spreadsheet_id, range)
    _batch_update(
        services,
        spreadsheet_id,
        [{"unmergeCells": {"range": grid_range}}],
    )
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "unmerge",
        "range": range,
    }


def _structure_add_named(services, spreadsheet_id, sheet, range, params) -> dict:
    range = _require_range("add_named", range)
    name = params.get("name")
    if not name:
        raise SheetsError(
            "missing_param", "add_named requires params={'name': <str>}"
        )
    grid_range = a1_to_gridrange(services, spreadsheet_id, range)
    resp = _batch_update(
        services,
        spreadsheet_id,
        [{"addNamedRange": {"namedRange": {"name": name, "range": grid_range}}}],
    )
    new_ids = capture_new_ids(resp.get("replies", []))
    named_range_id = new_ids["namedRangeIds"][0] if new_ids["namedRangeIds"] else None
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "add_named",
        "name": name,
        "range": range,
        "namedRangeId": named_range_id,
    }


def _structure_delete_named(services, spreadsheet_id, sheet, range, params) -> dict:
    named_range_id = params.get("namedRangeId")
    name = params.get("name")
    if not named_range_id and not name:
        raise SheetsError(
            "missing_param",
            "delete_named requires params={'namedRangeId': ...} OR {'name': ...}",
        )
    if not named_range_id:
        named_range_id = _resolve_named_range_id(services, spreadsheet_id, name)
    _batch_update(
        services,
        spreadsheet_id,
        [{"deleteNamedRange": {"namedRangeId": named_range_id}}],
    )
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "delete_named",
        "namedRangeId": named_range_id,
    }


def _resolve_named_range_id(
    services: SheetsServices, spreadsheet_id: str, name: str
) -> str:
    """Resolve a named-range NAME to its ``namedRangeId`` via a narrow get."""
    try:
        resp = (
            services.sheets.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                fields="namedRanges(name,namedRangeId)",
            )
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc
    for nr in resp.get("namedRanges", []) or []:
        if nr.get("name") == name:
            nr_id = nr.get("namedRangeId")
            if nr_id is not None:
                return nr_id
    raise SheetsError(
        "named_range_not_found",
        f"named range {name!r} not found in spreadsheet",
    )


def _structure_protect(services, spreadsheet_id, sheet, range, params) -> dict:
    range = _require_range("protect", range)
    grid_range = a1_to_gridrange(services, spreadsheet_id, range)
    protected: dict = {"range": grid_range}
    if params.get("description") is not None:
        protected["description"] = params["description"]
    if params.get("warningOnly") is not None:
        protected["warningOnly"] = bool(params["warningOnly"])
    if params.get("editors") is not None:
        protected["editors"] = {"users": list(params["editors"])}
    resp = _batch_update(
        services,
        spreadsheet_id,
        [{"addProtectedRange": {"protectedRange": protected}}],
    )
    new_ids = capture_new_ids(resp.get("replies", []))
    pr_id = new_ids["protectedRangeIds"][0] if new_ids["protectedRangeIds"] else None
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "protect",
        "range": range,
        "protectedRangeId": pr_id,
    }


def _structure_unprotect(services, spreadsheet_id, sheet, range, params) -> dict:
    pr_id = params.get("protectedRangeId")
    if pr_id is None:
        raise SheetsError(
            "missing_param", "unprotect requires params={'protectedRangeId': <int>}"
        )
    _batch_update(
        services,
        spreadsheet_id,
        [{"deleteProtectedRange": {"protectedRangeId": pr_id}}],
    )
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "unprotect",
        "protectedRangeId": pr_id,
    }


def _structure_freeze(services, spreadsheet_id, sheet, range, params) -> dict:
    sheet = _require_sheet("freeze", sheet)
    rows = params.get("rows")
    cols = params.get("cols")
    if rows is None and cols is None:
        raise SheetsError(
            "missing_param", "freeze requires params={'rows': int} and/or {'cols': int}"
        )
    sheet_id = _sheet_id_for(services, spreadsheet_id, sheet)
    grid_props: dict = {}
    if rows is not None:
        grid_props["frozenRowCount"] = int(rows)
    if cols is not None:
        grid_props["frozenColumnCount"] = int(cols)
    properties = {"sheetId": sheet_id, "gridProperties": grid_props}
    fields = build_fields_mask({"gridProperties": grid_props})
    _batch_update(
        services,
        spreadsheet_id,
        [{"updateSheetProperties": {"properties": properties, "fields": fields}}],
    )
    out: dict = {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "freeze",
        "sheet": sheet,
    }
    if rows is not None:
        out["frozenRows"] = int(rows)
    if cols is not None:
        out["frozenCols"] = int(cols)
    return out


def _structure_tab_color(services, spreadsheet_id, sheet, range, params) -> dict:
    sheet = _require_sheet("tab_color", sheet)
    color = params.get("color")
    if not color:
        raise SheetsError(
            "missing_param",
            "tab_color requires params={'color': '#RRGGBB' | 'theme:NAME'}",
        )
    try:
        color_style = hex_to_color_style(color)
    except ValueError as exc:
        raise SheetsError("bad_color", str(exc)) from exc
    sheet_id = _sheet_id_for(services, spreadsheet_id, sheet)
    properties = {"sheetId": sheet_id, "tabColorStyle": color_style}
    fields = build_fields_mask({"tabColorStyle": color_style})
    _batch_update(
        services,
        spreadsheet_id,
        [{"updateSheetProperties": {"properties": properties, "fields": fields}}],
    )
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "tab_color",
        "sheet": sheet,
        "tabColor": color_style_to_hex(color_style),
    }


def _structure_group(services, spreadsheet_id, sheet, range, params) -> dict:
    return _structure_group_op(
        "group", "addDimensionGroup", services, spreadsheet_id, sheet, params
    )


def _structure_ungroup(services, spreadsheet_id, sheet, range, params) -> dict:
    return _structure_group_op(
        "ungroup", "deleteDimensionGroup", services, spreadsheet_id, sheet, params
    )


def _structure_group_op(
    action, request_key, services, spreadsheet_id, sheet, params
) -> dict:
    sheet = _require_sheet(action, sheet)
    dimension = params.get("dimension")
    start = params.get("start")
    end = params.get("end")
    if dimension not in _DIMENSIONS:
        raise SheetsError(
            "missing_param",
            f"{action} requires params={{'dimension': 'ROWS'|'COLUMNS', "
            "'start': int, 'end': int}}",
        )
    if start is None or end is None:
        raise SheetsError(
            "missing_param",
            f"{action} requires both 'start' and 'end' (0-based half-open) in params",
        )
    sheet_id = _sheet_id_for(services, spreadsheet_id, sheet)
    dimension_range = {
        "sheetId": sheet_id,
        "dimension": dimension,
        "startIndex": int(start),
        "endIndex": int(end),
    }
    _batch_update(
        services,
        spreadsheet_id,
        [{request_key: {"range": dimension_range}}],
    )
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": action,
        "sheet": sheet,
        "dimension": dimension,
        "start": int(start),
        "end": int(end),
    }


_STRUCTURE_HANDLERS = {
    "merge": _structure_merge,
    "unmerge": _structure_unmerge,
    "add_named": _structure_add_named,
    "delete_named": _structure_delete_named,
    "protect": _structure_protect,
    "unprotect": _structure_unprotect,
    "freeze": _structure_freeze,
    "tab_color": _structure_tab_color,
    "group": _structure_group,
    "ungroup": _structure_ungroup,
}


# ---------------------------------------------------------------------------------------
# manage_sheets
# ---------------------------------------------------------------------------------------

_MANAGE_ACTIONS = frozenset({"add", "delete", "duplicate", "rename", "reorder"})

#: Per-action allowed ``params`` keys (LOCKED — DESIGN §3.3 manage_sheets).
_MANAGE_PARAMS: dict[str, set[str]] = {
    "add": {"title", "index", "rows", "cols"},
    "delete": set(),
    "duplicate": {"newName", "newIndex"},
    "rename": {"newName"},
    "reorder": {"newIndex"},
}


def manage_sheets(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    action: str,
    sheet: str | None = None,
    params: dict | None = None,
) -> dict:
    """Add/delete/duplicate/rename/reorder tabs; capture new ``sheetId``s (DESIGN §3.3).

    Per-action ``params`` keys (unknown key -> ``SheetsError("unknown_param")``): ``add`` ->
    ``{"title", "index", "rows", "cols"}`` (all optional); ``delete`` -> none (target via
    ``sheet``); ``duplicate`` -> ``{"newName", "newIndex"}``; ``rename`` -> ``{"newName"}``
    (required); ``reorder`` -> ``{"newIndex"}`` (required).

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        action: ``"add"`` | ``"delete"`` | ``"duplicate"`` | ``"rename"`` | ``"reorder"``.
        sheet: Target tab name for delete/duplicate/rename/reorder.
        params: Per-action parameter dict.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "action": ...,
        "sheet": {"sheetId", "title", "index"}}``.
    """
    if action not in _MANAGE_ACTIONS:
        raise SheetsError(
            "unknown_action",
            f"unknown manage_sheets action {action!r}; expected one of "
            f"{sorted(_MANAGE_ACTIONS)}",
        )
    params = _require_params(action, params, _MANAGE_PARAMS[action])
    handler = _MANAGE_HANDLERS[action]
    return handler(services, spreadsheet_id, sheet, params)


def _manage_add(services, spreadsheet_id, sheet, params) -> dict:
    properties: dict = {}
    if params.get("title") is not None:
        properties["title"] = params["title"]
    if params.get("index") is not None:
        properties["index"] = int(params["index"])
    grid_props: dict = {}
    if params.get("rows") is not None:
        grid_props["rowCount"] = int(params["rows"])
    if params.get("cols") is not None:
        grid_props["columnCount"] = int(params["cols"])
    if grid_props:
        properties["gridProperties"] = grid_props
    resp = _batch_update(
        services,
        spreadsheet_id,
        [{"addSheet": {"properties": properties}}],
    )
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "add",
        "sheet": _added_sheet_props(resp),
    }


def _manage_delete(services, spreadsheet_id, sheet, params) -> dict:
    sheet = _require_sheet("delete", sheet)
    sheet_id = _sheet_id_for(services, spreadsheet_id, sheet)
    _batch_update(
        services,
        spreadsheet_id,
        [{"deleteSheet": {"sheetId": sheet_id}}],
    )
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "delete",
        "sheet": {"sheetId": sheet_id, "title": sheet},
    }


def _manage_duplicate(services, spreadsheet_id, sheet, params) -> dict:
    sheet = _require_sheet("duplicate", sheet)
    sheet_id = _sheet_id_for(services, spreadsheet_id, sheet)
    req: dict = {"sourceSheetId": sheet_id}
    if params.get("newName") is not None:
        req["newSheetName"] = params["newName"]
    if params.get("newIndex") is not None:
        req["insertSheetIndex"] = int(params["newIndex"])
    resp = _batch_update(
        services,
        spreadsheet_id,
        [{"duplicateSheet": req}],
    )
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "duplicate",
        "sheet": _added_sheet_props(resp),
    }


def _manage_rename(services, spreadsheet_id, sheet, params) -> dict:
    sheet = _require_sheet("rename", sheet)
    new_name = params.get("newName")
    if not new_name:
        raise SheetsError(
            "missing_param", "rename requires params={'newName': <str>}"
        )
    sheet_id = _sheet_id_for(services, spreadsheet_id, sheet)
    properties = {"sheetId": sheet_id, "title": new_name}
    fields = build_fields_mask({"title": new_name})
    _batch_update(
        services,
        spreadsheet_id,
        [{"updateSheetProperties": {"properties": properties, "fields": fields}}],
    )
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "rename",
        "sheet": {"sheetId": sheet_id, "title": new_name},
    }


def _manage_reorder(services, spreadsheet_id, sheet, params) -> dict:
    sheet = _require_sheet("reorder", sheet)
    new_index = params.get("newIndex")
    if new_index is None:
        raise SheetsError(
            "missing_param", "reorder requires params={'newIndex': <int>}"
        )
    sheet_id = _sheet_id_for(services, spreadsheet_id, sheet)
    properties = {"sheetId": sheet_id, "index": int(new_index)}
    fields = build_fields_mask({"index": int(new_index)})
    _batch_update(
        services,
        spreadsheet_id,
        [{"updateSheetProperties": {"properties": properties, "fields": fields}}],
    )
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "reorder",
        "sheet": {"sheetId": sheet_id, "title": sheet, "index": int(new_index)},
    }


def _added_sheet_props(resp: dict) -> dict:
    """Extract ``{sheetId, title, index}`` from an addSheet/duplicateSheet reply."""
    for reply in resp.get("replies", []) or []:
        if not isinstance(reply, dict):
            continue
        child = reply.get("addSheet") or reply.get("duplicateSheet")
        if isinstance(child, dict):
            props = child.get("properties", {}) or {}
            return {
                "sheetId": props.get("sheetId"),
                "title": props.get("title"),
                "index": props.get("index"),
            }
    return {"sheetId": None, "title": None, "index": None}


_MANAGE_HANDLERS = {
    "add": _manage_add,
    "delete": _manage_delete,
    "duplicate": _manage_duplicate,
    "rename": _manage_rename,
    "reorder": _manage_reorder,
}


# ---------------------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------------------

_METADATA_ACTIONS = frozenset({"read", "create", "update", "delete"})

#: Allowed ``location`` keys across the three anchor forms (LOCKED — DESIGN §3.3 metadata).
_LOCATION_KEYS = frozenset({"sheet", "dimension", "start", "end"})


def metadata(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    action: str,
    key: str | None = None,
    value: str | None = None,
    location: dict | None = None,
    visibility: str = "DOCUMENT",
    metadata_id: int | None = None,
) -> dict:
    """Read/write developer metadata for durable row/column/sheet anchors (DESIGN §3.3).

    ``read`` -> ``developerMetadata.search`` (or all for a key). ``create`` ->
    ``createDeveloperMetadata`` with a dimension-range / whole-sheet / spreadsheet anchor;
    captures the assigned ``metadataId``. ``location`` is one of a dimension anchor
    ``{"sheet", "dimension", "start", "end"}``, a whole-sheet anchor ``{"sheet"}``, or a
    spreadsheet anchor ``{}`` (unknown key -> ``SheetsError("unknown_param")``).

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        action: ``"read"`` | ``"create"`` | ``"update"`` | ``"delete"``.
        key: Metadata key.
        value: Metadata value.
        location: Anchor location dict (see above).
        visibility: ``"DOCUMENT"`` (default) or ``"PROJECT"``.
        metadata_id: Existing metadata id for update/delete.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "action": ..., "metadata": [...]}``.
    """
    if action not in _METADATA_ACTIONS:
        raise SheetsError(
            "unknown_action",
            f"unknown metadata action {action!r}; expected one of "
            f"{sorted(_METADATA_ACTIONS)}",
        )

    if action == "read":
        return _metadata_read(services, spreadsheet_id, key, metadata_id)
    if action == "create":
        return _metadata_create(
            services, spreadsheet_id, key, value, location, visibility
        )
    if action == "update":
        return _metadata_update(services, spreadsheet_id, key, value, metadata_id)
    return _metadata_delete(services, spreadsheet_id, metadata_id)


def _validate_location(location: dict | None) -> dict:
    """Validate ``location`` keys and return the dict (``None`` => spreadsheet anchor ``{}``)."""
    location = location or {}
    if not isinstance(location, dict):
        raise SheetsError("unknown_param", "location must be a dict")
    unknown = set(location) - _LOCATION_KEYS
    if unknown:
        raise SheetsError(
            "unknown_param",
            f"unknown location keys: {sorted(unknown)}; allowed: {sorted(_LOCATION_KEYS)}",
        )
    return location


def _location_to_metadata_location(
    services: SheetsServices, spreadsheet_id: str, location: dict
) -> dict:
    """Build a Google ``DeveloperMetadataLocation`` from the public ``location`` dict."""
    if not location:
        # Spreadsheet anchor.
        return {"spreadsheet": True}
    sheet = location.get("sheet")
    dimension = location.get("dimension")
    start = location.get("start")
    end = location.get("end")
    if dimension is not None or start is not None or end is not None:
        if not sheet or dimension is None or start is None or end is None:
            raise SheetsError(
                "missing_param",
                "a dimension anchor needs sheet+dimension+start+end together",
            )
        if dimension not in _DIMENSIONS:
            raise SheetsError(
                "unknown_param",
                f"dimension must be 'ROWS' or 'COLUMNS', got {dimension!r}",
            )
        sheet_id = _sheet_id_for(services, spreadsheet_id, sheet)
        return {
            "dimensionRange": {
                "sheetId": sheet_id,
                "dimension": dimension,
                "startIndex": int(start),
                "endIndex": int(end),
            }
        }
    # Whole-sheet anchor.
    if not sheet:
        raise SheetsError("missing_param", "a whole-sheet anchor needs 'sheet'")
    sheet_id = _sheet_id_for(services, spreadsheet_id, sheet)
    return {"sheetId": sheet_id}


def _metadata_location_to_public(
    services: SheetsServices, spreadsheet_id: str, loc: dict
) -> dict:
    """Convert a Google ``DeveloperMetadataLocation`` back to the public ``location`` dict."""
    if not isinstance(loc, dict):
        return {}
    dim_range = loc.get("dimensionRange")
    if isinstance(dim_range, dict):
        out: dict = {
            "dimension": dim_range.get("dimension"),
            "start": dim_range.get("startIndex"),
            "end": dim_range.get("endIndex"),
        }
        sheet_id = dim_range.get("sheetId")
        title = _safe_sheet_title(services, spreadsheet_id, sheet_id)
        out = {"sheet": title, **out}
        return out
    sheet_id = loc.get("sheetId")
    if sheet_id is not None:
        return {"sheet": _safe_sheet_title(services, spreadsheet_id, sheet_id)}
    return {}


def _safe_sheet_title(services, spreadsheet_id, sheet_id) -> str | None:
    """Resolve a ``sheetId`` -> title, degrading to ``None`` on failure."""
    if sheet_id is None:
        return None
    try:
        return gridrange_to_a1(services, spreadsheet_id, {"sheetId": sheet_id})
    except SheetsError:
        return None


def _serialize_metadata_entry(
    services: SheetsServices, spreadsheet_id: str, dm: dict
) -> dict:
    """Serialize one Google ``DeveloperMetadata`` to the public metadata dict."""
    entry: dict = {
        "metadataId": dm.get("metadataId"),
        "key": dm.get("metadataKey"),
        "value": dm.get("metadataValue"),
        "visibility": dm.get("visibility"),
    }
    loc = dm.get("location")
    if isinstance(loc, dict):
        entry["location"] = _metadata_location_to_public(
            services, spreadsheet_id, loc
        )
    return entry


def _metadata_read(
    services: SheetsServices,
    spreadsheet_id: str,
    key: str | None,
    metadata_id: int | None,
) -> dict:
    """Read developer metadata — by id, by key, or all (DESIGN §3.3)."""
    try:
        if metadata_id is not None:
            resp = (
                services.sheets.spreadsheets()
                .developerMetadata()
                .get(spreadsheetId=spreadsheet_id, metadataId=metadata_id)
                .execute()
            )
            entries = [resp] if resp else []
        else:
            data_filter: dict = {}
            if key is not None:
                data_filter = {"developerMetadataLookup": {"metadataKey": key}}
            else:
                # Match all metadata in the document.
                data_filter = {
                    "developerMetadataLookup": {
                        "locationType": "SPREADSHEET",
                        "visibility": "DOCUMENT",
                    }
                }
            search_resp = (
                services.sheets.spreadsheets()
                .developerMetadata()
                .search(
                    spreadsheetId=spreadsheet_id,
                    body={"dataFilters": [data_filter]},
                )
                .execute()
            )
            entries = [
                m.get("developerMetadata", {})
                for m in (search_resp.get("matchedDeveloperMetadata", []) or [])
            ]
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc

    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "read",
        "metadata": [
            _serialize_metadata_entry(services, spreadsheet_id, dm) for dm in entries
        ],
    }


def _metadata_create(
    services: SheetsServices,
    spreadsheet_id: str,
    key: str | None,
    value: str | None,
    location: dict | None,
    visibility: str,
) -> dict:
    """Create developer metadata at the given anchor; capture the assigned id."""
    if not key:
        raise SheetsError("missing_param", "metadata create requires a key")
    location = _validate_location(location)
    metadata_location = _location_to_metadata_location(
        services, spreadsheet_id, location
    )
    dm: dict = {
        "metadataKey": key,
        "metadataValue": value if value is not None else "",
        "location": metadata_location,
        "visibility": visibility,
    }
    resp = _batch_update(
        services,
        spreadsheet_id,
        [{"createDeveloperMetadata": {"developerMetadata": dm}}],
    )
    metadata_id = None
    for reply in resp.get("replies", []) or []:
        child = (reply or {}).get("createDeveloperMetadata")
        if isinstance(child, dict):
            created = child.get("developerMetadata", {}) or {}
            metadata_id = created.get("metadataId")
            break
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "create",
        "metadata": [
            {
                "metadataId": metadata_id,
                "key": key,
                "value": value if value is not None else "",
                "visibility": visibility,
                "location": location,
            }
        ],
    }


def _metadata_update(
    services: SheetsServices,
    spreadsheet_id: str,
    key: str | None,
    value: str | None,
    metadata_id: int | None,
) -> dict:
    """Update an existing developer-metadata entry's key/value by id."""
    if metadata_id is None:
        raise SheetsError(
            "missing_param", "metadata update requires metadata_id"
        )
    dm: dict = {}
    if key is not None:
        dm["metadataKey"] = key
    if value is not None:
        dm["metadataValue"] = value
    if not dm:
        raise SheetsError(
            "empty_payload", "metadata update needs at least a key or value to change"
        )
    fields = build_fields_mask(dm)
    request = {
        "updateDeveloperMetadata": {
            "dataFilters": [{"developerMetadataLookup": {"metadataId": metadata_id}}],
            "developerMetadata": dm,
            "fields": fields,
        }
    }
    _batch_update(services, spreadsheet_id, [request])
    out_entry: dict = {"metadataId": metadata_id}
    if key is not None:
        out_entry["key"] = key
    if value is not None:
        out_entry["value"] = value
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "update",
        "metadata": [out_entry],
    }


def _metadata_delete(
    services: SheetsServices, spreadsheet_id: str, metadata_id: int | None
) -> dict:
    """Delete a developer-metadata entry by id."""
    if metadata_id is None:
        raise SheetsError(
            "missing_param", "metadata delete requires metadata_id"
        )
    request = {
        "deleteDeveloperMetadata": {
            "dataFilter": {"developerMetadataLookup": {"metadataId": metadata_id}}
        }
    }
    _batch_update(services, spreadsheet_id, [request])
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "delete",
        "metadata": [{"metadataId": metadata_id}],
    }
