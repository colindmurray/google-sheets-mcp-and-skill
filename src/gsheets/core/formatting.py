"""Atomic cell formatting (DESIGN §3.3 ``format``, §5.1).

``format`` translates the flat ``CellFormat`` payload to a ``repeatCell.cell.userEnteredFormat``
(plus ``note`` as a cell-level field and ``padding`` as an atomic-leaf), with borders applied
via ``updateBorders``. Both requests are issued as ONE ``batchUpdate`` (all-or-nothing). The
``fields`` mask is auto-built from the payload via ``build_fields_mask``.

PURE core module: imports only stdlib + sibling core modules + ``googleapiclient``. It must
NEVER import ``fastmcp``, ``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models`` (DESIGN §1
boundary).
"""

from __future__ import annotations

from googleapiclient.errors import HttpError

from .addressing import a1_to_gridrange
from .colors import hex_to_color_style
from .errors import SheetsError
from .errors import classify_google_error
from .fieldsmask import build_fields_mask
from .service import SheetsServices

# Flat ``fmt`` keys that lift into ``userEnteredFormat.textFormat`` (the inverse of
# ``flatten_cell_format``'s ``_TEXT_FORMAT_KEYS``). ``fg`` is handled separately (it becomes
# ``textFormat.foregroundColorStyle`` via the color helper).
_TEXT_FORMAT_KEYS = (
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "fontSize",
    "fontFamily",
)

# Flat scalar ``fmt`` keys that map straight onto a ``userEnteredFormat`` field.
_SCALAR_FORMAT_KEYS: dict[str, str] = {
    "halign": "horizontalAlignment",
    "valign": "verticalAlignment",
    "wrap": "wrapStrategy",
}

# The four sides Google carries on an ``updateBorders`` request, in canonical order.
_BORDER_SIDES = ("top", "bottom", "left", "right")

# Top-level flat keys consumed by the format() translation. A key not in this set (and not a
# text-format key) is unknown and rejected so a typo never silently no-ops.
_KNOWN_KEYS = frozenset(
    {"bg", "fg", "numberFormat", "numberFormatType", "padding", "textRotation",
     "borders", "note"}
    | set(_TEXT_FORMAT_KEYS)
    | set(_SCALAR_FORMAT_KEYS)
)


def format(services: SheetsServices, spreadsheet_id: str, range: str, fmt: dict) -> dict:
    """Apply formatting to a range atomically with an auto-built fields mask (DESIGN §3.3).

    ``fmt`` accepts the flat ``CellFormat`` keys (e.g.
    ``{"bg": "#FFCDD2", "bold": True, "numberFormat": "0.00%",
    "padding": {"top": 2, "left": 3}, "borders": {"top": "SOLID #000000"}, "note": "reviewed"}``).
    Core translates flat -> Google ``repeatCell.cell`` and auto-builds the ``fields`` mask.
    ``note`` writes ``repeatCell.cell.note``; ``borders`` apply via ``updateBorders``; both
    requests go in ONE ``batchUpdate`` so the operation is all-or-nothing.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        range: A1 range to format.
        fmt: Flat ``CellFormat`` payload (see DESIGN §3.1).

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "range": ..., "appliedFields": ...}``.

    Raises:
        SheetsError: ``empty_payload`` if ``fmt`` is empty or yields no writable field;
            ``unknown_param`` for an unrecognized flat key; ``bad_format`` for a malformed
            border string; ``bad_range`` for a bad A1 range; ``google_api_error`` on API failure.
    """
    if not isinstance(fmt, dict) or not fmt:
        raise SheetsError("empty_payload", "refuse a no-op write: fmt is empty")

    unknown = [k for k in fmt if k not in _KNOWN_KEYS]
    if unknown:
        raise SheetsError(
            "unknown_param",
            f"unknown format key(s) {sorted(unknown)}; expected flat CellFormat keys",
            hint=f"valid keys: {sorted(_KNOWN_KEYS)}",
        )

    grid_range = a1_to_gridrange(services, spreadsheet_id, range)

    # --- build the repeatCell payload (userEnteredFormat + note) ----------------------------
    cell_payload = _build_cell_payload(fmt)

    # --- build the updateBorders request (separate request kind) ----------------------------
    borders_request = _build_borders_request(grid_range, fmt.get("borders"))

    if not cell_payload and borders_request is None:
        # No userEnteredFormat/note AND no borders -> nothing to write.
        raise SheetsError("empty_payload", "refuse a no-op write: fmt yields no writable field")

    requests: list[dict] = []
    applied_fields: str | None = None

    if cell_payload:
        applied_fields = build_fields_mask(cell_payload)
        requests.append(
            {
                "repeatCell": {
                    "range": grid_range,
                    "cell": cell_payload,
                    "fields": applied_fields,
                }
            }
        )

    if borders_request is not None:
        requests.append({"updateBorders": borders_request})

    # ONE batchUpdate carrying BOTH the repeatCell and the updateBorders (all-or-nothing): a
    # partial failure can never land the fill without the borders (DESIGN §5.1(4)).
    try:
        (
            services.sheets.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests},
            )
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc

    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "range": range,
        "appliedFields": _applied_fields_summary(applied_fields, borders_request is not None),
    }


def _build_cell_payload(fmt: dict) -> dict:
    """Translate the flat ``fmt`` into a ``repeatCell.cell`` payload.

    Produces ``{"userEnteredFormat": {...}, "note": ...}`` where ``userEnteredFormat`` is the
    inverse of :func:`flatten_cell_format` (minus borders, which live in ``updateBorders``).
    ``note`` is a cell-level sibling. Returns an empty dict when nothing maps (e.g. ``fmt`` had
    only ``borders``).
    """
    user_entered: dict = {}

    # --- background color -> backgroundColorStyle -------------------------------------------
    if "bg" in fmt and fmt["bg"] is not None:
        user_entered["backgroundColorStyle"] = hex_to_color_style(fmt["bg"])

    # --- textFormat (foreground color + lifted scalar styles) -------------------------------
    text_format: dict = {}
    if "fg" in fmt and fmt["fg"] is not None:
        text_format["foregroundColorStyle"] = hex_to_color_style(fmt["fg"])
    for key in _TEXT_FORMAT_KEYS:
        if key in fmt and fmt[key] is not None:
            text_format[key] = fmt[key]
    if text_format:
        user_entered["textFormat"] = text_format

    # --- numberFormat (atomic leaf: {type, pattern}) ----------------------------------------
    number_format = _build_number_format(fmt)
    if number_format is not None:
        user_entered["numberFormat"] = number_format

    # --- alignment / wrap scalars -----------------------------------------------------------
    for flat_key, google_key in _SCALAR_FORMAT_KEYS.items():
        if flat_key in fmt and fmt[flat_key] is not None:
            user_entered[google_key] = fmt[flat_key]

    # --- padding / textRotation (atomic leaves, preserved verbatim) -------------------------
    if "padding" in fmt and fmt["padding"] is not None:
        padding = fmt["padding"]
        if not isinstance(padding, dict) or not padding:
            raise SheetsError("bad_format", "padding must be a non-empty {top,right,bottom,left} dict")
        user_entered["padding"] = dict(padding)
    if "textRotation" in fmt and fmt["textRotation"] is not None:
        rotation = fmt["textRotation"]
        if not isinstance(rotation, dict) or not rotation:
            raise SheetsError("bad_format", "textRotation must be a non-empty {angle}|{vertical} dict")
        user_entered["textRotation"] = dict(rotation)

    cell: dict = {}
    if user_entered:
        cell["userEnteredFormat"] = user_entered
    # ``note`` is a cell-level field alongside ``userEnteredFormat``. An empty string is a
    # legitimate note write (it clears the note), so only ``None``/absent is skipped.
    if "note" in fmt and fmt["note"] is not None:
        cell["note"] = fmt["note"]

    return cell


def _build_number_format(fmt: dict) -> dict | None:
    """Build the Google ``numberFormat`` (``{type, pattern}``) from flat keys, else ``None``.

    ``numberFormat`` (flat) carries the pattern; ``numberFormatType`` carries the type. Either
    may be present. When only a pattern is given, the type defaults to ``NUMBER``; when only a
    type is given (e.g. ``TEXT``), no pattern is required.
    """
    pattern = fmt.get("numberFormat")
    nf_type = fmt.get("numberFormatType")
    if pattern is None and nf_type is None:
        return None
    out: dict = {}
    out["type"] = nf_type if nf_type is not None else "NUMBER"
    if pattern is not None:
        out["pattern"] = pattern
    return out


def _build_borders_request(grid_range: dict, borders: object) -> dict | None:
    """Build the ``updateBorders`` request body from flat ``borders``, else ``None``.

    ``borders`` is a ``{side: "<style> <hex>"}`` dict (the same flat form
    :func:`flatten_cell_format` reads back — full CRUD symmetry). Each side becomes a Google
    ``Border`` (``{"style": ..., "colorStyle": ColorStyle}``). Sides not present are omitted so
    ``updateBorders`` only touches the requested edges.
    """
    if borders is None:
        return None
    if not isinstance(borders, dict) or not borders:
        raise SheetsError(
            "bad_format",
            "borders must be a non-empty {side: '<style> <hex>'} dict",
        )

    unknown = [s for s in borders if s not in _BORDER_SIDES]
    if unknown:
        raise SheetsError(
            "bad_format",
            f"unknown border side(s) {sorted(unknown)}; expected {list(_BORDER_SIDES)}",
        )

    request: dict = {"range": grid_range}
    for side in _BORDER_SIDES:
        if side in borders and borders[side] is not None:
            request[side] = _parse_border(borders[side], side)

    # Only the ``range`` key means no actual edge was set.
    if len(request) == 1:
        return None
    return request


def _parse_border(spec: object, side: str) -> dict:
    """Parse a flat border string ``"<style> <hex>"`` into a Google ``Border`` dict.

    Examples:
        ``"SOLID #000000"`` -> ``{"style": "SOLID", "colorStyle": {"rgbColor": {...}}}``
        ``"SOLID"`` -> ``{"style": "SOLID"}`` (color defaults Google-side to black)
        ``"SOLID theme:TEXT"`` -> ``{"style": "SOLID", "colorStyle": {"themeColor": "TEXT"}}``
    """
    if not isinstance(spec, str) or not spec.strip():
        raise SheetsError("bad_format", f"border for side {side!r} must be a non-empty string")
    parts = spec.split()
    style = parts[0]
    border: dict = {"style": style}
    if len(parts) == 1:
        return border
    if len(parts) != 2:
        raise SheetsError(
            "bad_format",
            f"border for side {side!r} must be '<style>' or '<style> <hex>', got {spec!r}",
        )
    try:
        border["colorStyle"] = hex_to_color_style(parts[1])
    except ValueError as exc:
        raise SheetsError("bad_format", f"bad border color for side {side!r}: {exc}") from exc
    return border


def _applied_fields_summary(applied_fields: str | None, has_borders: bool) -> str:
    """Compose the returned ``appliedFields`` summary string.

    The ``repeatCell`` mask (``applied_fields``) plus a trailing ``borders`` token when an
    ``updateBorders`` request was issued (its mask is implicit per-side, not a dotted field
    mask, so it is summarized as the literal ``borders``).
    """
    parts: list[str] = []
    if applied_fields:
        parts.append(applied_fields)
    if has_borders:
        parts.append("borders")
    return ",".join(parts)
