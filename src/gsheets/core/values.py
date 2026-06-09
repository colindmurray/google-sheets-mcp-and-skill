"""Value reads/writes and the jagged-array helper (DESIGN §3.3, §5.5).

Houses ``read_values`` / ``write_values`` / ``append_rows`` / ``clear`` plus :func:`pad_jagged`.
Writes default to ``USER_ENTERED`` (formulas work). ``clear`` routes any
formats/validation/notes clearing through ``build_fields_mask``.

PURE core module: imports only stdlib + sibling core modules. It must NEVER import
``fastmcp``, ``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models`` (DESIGN §1 boundary).
"""

from __future__ import annotations

from googleapiclient.errors import HttpError

from .addressing import a1_to_gridrange
from .errors import SheetsError, classify_google_error
from .fieldsmask import build_fields_mask
from .service import SheetsServices

# render mode -> Google ``valueRenderOption``.
_RENDER_OPTIONS: dict[str, str] = {
    "plain": "FORMATTED_VALUE",
    "unformatted": "UNFORMATTED_VALUE",
    "formula": "FORMULA",
    "all": "FORMULA",  # primary pass; "all" also issues a FORMATTED_VALUE pass for ``computed``
}

# input mode -> Google ``valueInputOption``.
_INPUT_OPTIONS: dict[str, str] = {
    "user_entered": "USER_ENTERED",
    "raw": "RAW",
}


def pad_jagged(values: list[list], width: int | None = None) -> list[list]:
    """Pad every row of a jagged values array to a uniform width with ``""``.

    Values reads omit empty trailing cells; this fills each row to the max width (or an
    explicit ``width``) so downstream consumers see a rectangle (DESIGN §5.5).

    Args:
        values: A possibly-jagged list of rows.
        width: Explicit target width; defaults to the max row length.

    Returns:
        A rectangular list of rows, each of equal length. The input is not mutated.
    """
    if not values:
        return []
    if width is None:
        width = max((len(row) for row in values), default=0)
    padded: list[list] = []
    for row in values:
        row = list(row)
        if len(row) < width:
            row = row + [""] * (width - len(row))
        padded.append(row)
    return padded


def _resolve_input_option(input: str) -> str:
    """Map the public ``input`` token to a Google ``valueInputOption`` or raise."""
    try:
        return _INPUT_OPTIONS[input]
    except KeyError:
        raise SheetsError(
            "bad_input",
            f"unknown input mode {input!r}; expected 'user_entered' or 'raw'",
        ) from None


def read_values(
    services: SheetsServices,
    spreadsheet_id: str,
    ranges: list[str],
    *,
    render: str = "plain",
) -> dict:
    """Read values for one or more A1 ranges with a render mode (DESIGN §3.3).

    ``render``: ``"plain"`` -> ``FORMATTED_VALUE``; ``"unformatted"`` -> ``UNFORMATTED_VALUE``;
    ``"formula"`` -> ``FORMULA``; ``"all"`` -> FORMULA + FORMATTED side by side. Uses
    ``values.batchGet``. For ``render="all"``, both ``values`` and ``computed`` are padded to
    a COMMON rectangle (the element-wise max of both passes' row count and per-row width) so
    they are index-aligned; non-formula cells return their literal value under FORMULA render
    (DESIGN §3.3 LOCKED note).

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        ranges: One or more A1 ranges.
        render: ``"plain"`` | ``"unformatted"`` | ``"formula"`` | ``"all"``.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "render": ..., "ranges": [...]}``.
    """
    if render not in _RENDER_OPTIONS:
        raise SheetsError(
            "bad_render",
            f"unknown render mode {render!r}; expected one of "
            "'plain', 'unformatted', 'formula', 'all'",
        )
    if not ranges:
        raise SheetsError("empty_ranges", "read_values requires at least one range")

    values_api = services.sheets.spreadsheets().values()
    try:
        primary = (
            values_api.batchGet(
                spreadsheetId=spreadsheet_id,
                ranges=ranges,
                valueRenderOption=_RENDER_OPTIONS[render],
            )
            .execute()
        )
        computed_resp = None
        if render == "all":
            computed_resp = (
                values_api.batchGet(
                    spreadsheetId=spreadsheet_id,
                    ranges=ranges,
                    valueRenderOption="FORMATTED_VALUE",
                )
                .execute()
            )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc

    primary_ranges = primary.get("valueRanges") or []
    computed_ranges = (computed_resp.get("valueRanges") or []) if computed_resp else []

    out_ranges: list[dict] = []
    for idx, vr in enumerate(primary_ranges):
        entry: dict = {
            "range": vr.get("range", ranges[idx] if idx < len(ranges) else None),
        }
        raw_values = vr.get("values", [])
        if render == "all":
            comp_vr = computed_ranges[idx] if idx < len(computed_ranges) else {}
            raw_computed = comp_vr.get("values", [])
            values_rect, computed_rect = _pad_to_common_rectangle(
                raw_values, raw_computed
            )
            entry["values"] = values_rect
            entry["computed"] = computed_rect
        else:
            entry["values"] = pad_jagged(raw_values)
        out_ranges.append(entry)

    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "render": render,
        "ranges": out_ranges,
    }


def _pad_to_common_rectangle(
    a: list[list], b: list[list]
) -> tuple[list[list], list[list]]:
    """Pad two value arrays to a single common rectangle so they are index-aligned.

    The common rectangle is the element-wise max of both arrays' row count and per-row
    width. Both arrays are padded to ``(max_rows, max_cols)`` with ``""`` so ``a[r][c]`` and
    ``b[r][c]`` address the same cell (DESIGN §3.3 ``render="all"`` LOCKED note). Padding each
    independently could misalign rows/cols, so they MUST share one width.
    """
    max_rows = max(len(a), len(b))
    max_cols = 0
    for row in a:
        max_cols = max(max_cols, len(row))
    for row in b:
        max_cols = max(max_cols, len(row))

    def _pad(rows: list[list]) -> list[list]:
        out = [list(r) + [""] * (max_cols - len(r)) for r in rows]
        out.extend([[""] * max_cols for _ in range(max_rows - len(rows))])
        return out

    return _pad(a), _pad(b)


def write_values(
    services: SheetsServices,
    spreadsheet_id: str,
    data: list[dict],
    *,
    input: str = "user_entered",
) -> dict:
    """Write/update one or more ranges via ``values.batchUpdate`` (DESIGN §3.3).

    Defaults to ``valueInputOption=USER_ENTERED`` (formulas work). ``data`` items are
    ``{"range": "Cliff!A1", "values": [["=SUM(B:B)"]]}``.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        data: List of ``{"range", "values"}`` write items.
        input: ``"user_entered"`` (default) | ``"raw"``.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "updatedRanges": [...], "updatedCells": ...,
        "updatedRows": ..., "updatedColumns": ...}``.
    """
    if not data:
        raise SheetsError("empty_payload", "write_values requires at least one data item")
    value_input_option = _resolve_input_option(input)

    body = {
        "valueInputOption": value_input_option,
        "data": [
            {"range": item["range"], "values": item.get("values", [])}
            for item in data
        ],
    }
    try:
        resp = (
            services.sheets.spreadsheets()
            .values()
            .batchUpdate(spreadsheetId=spreadsheet_id, body=body)
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc

    updated_ranges: list[str] = []
    updated_cells = 0
    updated_rows = 0
    updated_columns = 0
    for resp_item in resp.get("responses", []):
        rng = resp_item.get("updatedRange")
        if rng is not None:
            updated_ranges.append(rng)
        updated_cells += resp_item.get("updatedCells", 0)
        updated_rows += resp_item.get("updatedRows", 0)
        updated_columns += resp_item.get("updatedColumns", 0)

    # Top-level totals are authoritative when present (batchUpdate echoes them).
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "updatedRanges": updated_ranges,
        "updatedCells": resp.get("totalUpdatedCells", updated_cells),
        "updatedRows": resp.get("totalUpdatedRows", updated_rows),
        "updatedColumns": resp.get("totalUpdatedColumns", updated_columns),
    }


def append_rows(
    services: SheetsServices,
    spreadsheet_id: str,
    range: str,
    values: list[list],
    *,
    input: str = "user_entered",
) -> dict:
    """Append rows after the last row of a table (``INSERT_ROWS``, no overwrite) (DESIGN §3.3).

    Uses ``values.append(valueInputOption=..., insertDataOption="INSERT_ROWS")``.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        range: An A1 range identifying the table.
        values: Rows to append.
        input: ``"user_entered"`` (default) | ``"raw"``.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "updates": {...}, "tableRange": ...}``.
    """
    if not values:
        raise SheetsError("empty_payload", "append_rows requires at least one row")
    value_input_option = _resolve_input_option(input)

    try:
        resp = (
            services.sheets.spreadsheets()
            .values()
            .append(
                spreadsheetId=spreadsheet_id,
                range=range,
                valueInputOption=value_input_option,
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            )
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc

    raw_updates = resp.get("updates", {}) or {}
    updates = {
        "updatedRange": raw_updates.get("updatedRange"),
        "updatedRows": raw_updates.get("updatedRows", 0),
        "updatedCells": raw_updates.get("updatedCells", 0),
    }
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "updates": updates,
        "tableRange": resp.get("tableRange"),
    }


# Mapping from a clear flag to the ``Cell``/``CellData`` payload subfields its
# ``updateCells`` request must touch. ``formats`` clears the whole format; ``validation``
# clears the per-cell data validation; ``notes`` clears the cell note.
_CLEAR_FIELDS: dict[str, dict] = {
    "formats": {"userEnteredFormat": {}},
    "validation": {"dataValidation": {}},
    "notes": {"note": ""},
}


def clear(
    services: SheetsServices,
    spreadsheet_id: str,
    ranges: list[str],
    *,
    values: bool = True,
    formats: bool = False,
    validation: bool = False,
    notes: bool = False,
) -> dict:
    """Clear values (and optionally formats/validation/notes) from ranges (DESIGN §3.3).

    ``values``-only -> ``values.batchClear``. Any of formats/validation/notes ->
    ``batchUpdate`` with ``updateCells`` + an auto fields mask (via ``build_fields_mask``)
    covering only the requested subfields over the ``GridRange``. When ``values`` is also
    requested alongside structural clears, BOTH a ``batchClear`` (values) and a ``batchUpdate``
    (structural) are issued.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        ranges: A1 ranges to clear.
        values: Clear cell values (default ``True``).
        formats: Clear cell formatting.
        validation: Clear data validation.
        notes: Clear cell notes.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "clearedRanges": [...], "cleared": {...}}``.
    """
    if not ranges:
        raise SheetsError("empty_ranges", "clear requires at least one range")
    if not (values or formats or validation or notes):
        raise SheetsError(
            "empty_payload",
            "clear must clear at least one of values/formats/validation/notes",
        )

    # Assemble the structural-clear payload (formats/validation/notes) once; the same
    # subfield set applies to every range. ``build_fields_mask`` turns it into the mask.
    structural_payload: dict = {}
    if formats:
        structural_payload.update(_CLEAR_FIELDS["formats"])
    if validation:
        structural_payload.update(_CLEAR_FIELDS["validation"])
    if notes:
        structural_payload.update(_CLEAR_FIELDS["notes"])

    try:
        if values:
            (
                services.sheets.spreadsheets()
                .values()
                .batchClear(spreadsheetId=spreadsheet_id, body={"ranges": ranges})
                .execute()
            )

        if structural_payload:
            fields = build_fields_mask(structural_payload)
            requests = []
            for a1 in ranges:
                grid_range = a1_to_gridrange(services, spreadsheet_id, a1)
                requests.append(
                    {
                        "updateCells": {
                            "range": grid_range,
                            "fields": fields,
                        }
                    }
                )
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
        "clearedRanges": list(ranges),
        "cleared": {
            "values": values,
            "formats": formats,
            "validation": validation,
            "notes": notes,
        },
    }
