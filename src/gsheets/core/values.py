"""Value reads/writes and the jagged-array helper (DESIGN §3.3, §5.5).

Houses ``read_values`` / ``write_values`` / ``append_rows`` / ``clear`` plus :func:`pad_jagged`.
Writes default to ``USER_ENTERED`` (formulas work). ``clear`` hand-builds its
``updateCells`` fields mask directly from the requested clear flags (values/formats/
validation/notes) — it deliberately does NOT route through ``build_fields_mask``, which
would discard the empty-dict structural sentinels and silently clear nothing.

PURE core module: imports only stdlib + sibling core modules. It must NEVER import
``fastmcp``, ``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models`` (DESIGN §1 boundary).
"""

from __future__ import annotations

from googleapiclient.errors import HttpError

from .addressing import a1_to_gridrange
from .errors import SheetsError, classify_google_error
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

# public major-dimension token -> Google ``majorDimension`` (SPEC §6 P3). ``rows`` is Google's
# own default, so it is NOT sent on the wire (keeps the request minimal); ``columns`` is sent.
_MAJOR_OPTIONS: dict[str, str] = {
    "rows": "ROWS",
    "columns": "COLUMNS",
}


def _resolve_major_option(major: str) -> str | None:
    """Map the public ``major`` token to a Google ``majorDimension`` (or ``None`` to omit it).

    ``rows`` is Google's default — core omits the param so the request stays minimal and the
    pre-existing golden request shapes are unchanged. ``columns`` is sent as ``COLUMNS``. An
    unknown token raises ``SheetsError("bad_major")``.
    """
    if major not in _MAJOR_OPTIONS:
        raise SheetsError(
            "bad_major",
            f"unknown major dimension {major!r}; expected 'rows' or 'columns'",
        )
    return None if major == "rows" else _MAJOR_OPTIONS[major]


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
    ranges: list[str] | None = None,
    *,
    render: str = "plain",
    diff_only: bool = False,
    max_cells: int | None = None,
    major: str = "rows",
    data_filters: list[dict] | None = None,
) -> dict:
    """Read values for one or more A1 ranges with a render mode (DESIGN §3.3).

    ``render``: ``"plain"`` -> ``FORMATTED_VALUE``; ``"unformatted"`` -> ``UNFORMATTED_VALUE``;
    ``"formula"`` -> ``FORMULA``; ``"all"`` -> FORMULA + FORMATTED side by side. Uses
    ``values.batchGet``. For ``render="all"``, both ``values`` and ``computed`` are padded to
    a COMMON rectangle (the element-wise max of both passes' row count and per-row width) so
    they are index-aligned; non-formula cells return their literal value under FORMULA render.

    ``major`` (SPEC §6 P3): ``"rows"`` (default) | ``"columns"``. Passed straight to Google's
    ``majorDimension``; column-major output suits uniform helper columns (each emitted inner
    list is one COLUMN). ``"rows"`` is Google's own default, so core omits the param on the
    wire. The chosen mode rides back on the result as ``"major"``.

    ``data_filters`` (SPEC §6 P2 — metadata-addressed reads): an OPTIONAL alternative to ``ranges``
    for SYMBOLIC, insert-proof addressing. When supplied, core reads via ``values.batchGetByDataFilter``
    instead of ``values.batchGet``, so a range can be named by a stable selector rather than a literal
    A1 (which shifts when rows/columns are inserted). Each filter is one of:
    ``{"a1": "Sheet1!A1:B10"}`` (resolved to a ``GridRange`` inside core, mirroring the ``ranges``
    path), ``{"gridRange": {...}}`` (a raw ``GridRange`` passed through), or
    ``{"developerMetadataLookup": {...}}`` (matches a block previously tagged via ``metadata``).
    Pass EITHER ``ranges`` OR ``data_filters`` — supplying both raises ``conflicting_args``;
    supplying neither raises ``empty_ranges``.

    ``diff_only`` (ISSUES.md #12, ``render="all"`` ONLY): on a staticized region every FORMATTED
    cell equals its FORMULA-pass literal, so the parallel ``computed`` matrix is pure
    duplication (it can double an already-large payload). With ``diff_only=True`` each ``computed``
    cell that equals its ``values`` counterpart (compared str-normalized, so the typed literal
    ``185`` matches the formatted ``"185"``) is emitted as ``null``, and ``computed`` is dropped
    entirely for a range where nothing differs. The rectangle stays index-aligned — a ``null``
    hole means "computed == values here". A no-op when ``render != "all"``.

    ``max_cells`` (ISSUES.md #13): an optional guardrail. When the total cells across all ranges
    (the ``values`` grid) exceeds it, raise a structured ``result_too_large`` rather than return a
    huge payload that only fails at the caller's token cap. ``None`` (default) ⇒ unlimited. For
    genuinely large value dumps prefer ``export`` (csv/tsv → local file, no token cap).

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        ranges: One or more A1 ranges. ``None`` ⇒ use ``data_filters`` instead.
        render: ``"plain"`` | ``"unformatted"`` | ``"formula"`` | ``"all"``.
        diff_only: Sparsify ``computed`` against ``values`` (``render="all"`` only). Default off.
        max_cells: Raise ``result_too_large`` if the read exceeds this many cells. Default unlimited.
        major: ``"rows"`` (default) | ``"columns"`` — Google ``majorDimension``.
        data_filters: SYMBOLIC selectors (``a1`` / ``gridRange`` / ``developerMetadataLookup``) used
            INSTEAD of ``ranges``; reads via ``batchGetByDataFilter``. Mutually exclusive with ``ranges``.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "render": ..., "major": ..., "ranges": [...]}``.
    """
    if render not in _RENDER_OPTIONS:
        raise SheetsError(
            "bad_render",
            f"unknown render mode {render!r}; expected one of "
            "'plain', 'unformatted', 'formula', 'all'",
        )
    major_option = _resolve_major_option(major)
    if ranges and data_filters:
        raise SheetsError(
            "conflicting_args",
            "pass EITHER `ranges` OR `data_filters`, not both",
        )
    if not ranges and not data_filters:
        raise SheetsError(
            "empty_ranges",
            "read_values requires at least one range (or a data_filters selector)",
        )
    if max_cells is not None and max_cells < 1:
        raise SheetsError(
            "bad_max_cells",
            f"max_cells must be a positive integer, got {max_cells!r}",
        )

    use_filters = bool(data_filters)
    google_filters = None
    if use_filters:
        from .dataselector import build_data_filters

        google_filters = build_data_filters(services, spreadsheet_id, data_filters)

    try:
        primary = _read_values_pass(
            services,
            spreadsheet_id,
            ranges,
            google_filters,
            _RENDER_OPTIONS[render],
            major_option,
        )
        computed_resp = None
        if render == "all":
            computed_resp = _read_values_pass(
                services,
                spreadsheet_id,
                ranges,
                google_filters,
                "FORMATTED_VALUE",
                major_option,
            )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc

    primary_ranges = _extract_value_ranges(primary, use_filters)
    computed_ranges = (
        _extract_value_ranges(computed_resp, use_filters) if computed_resp else []
    )

    fallback_ranges = list(ranges) if ranges else []
    out_ranges: list[dict] = []
    total_cells = 0
    for idx, vr in enumerate(primary_ranges):
        entry: dict = {
            "range": vr.get(
                "range",
                fallback_ranges[idx] if idx < len(fallback_ranges) else None,
            ),
        }
        raw_values = vr.get("values", [])
        if render == "all":
            comp_vr = computed_ranges[idx] if idx < len(computed_ranges) else {}
            raw_computed = comp_vr.get("values", [])
            values_rect, computed_rect = _pad_to_common_rectangle(
                raw_values, raw_computed
            )
            entry["values"] = values_rect
            if diff_only:
                sparse, any_diff = _sparsify_computed(values_rect, computed_rect)
                # Omit ``computed`` entirely for a fully-static range; otherwise keep the
                # null-holed (still index-aligned) matrix.
                if any_diff:
                    entry["computed"] = sparse
            else:
                entry["computed"] = computed_rect
        else:
            entry["values"] = pad_jagged(raw_values)
        total_cells += sum(len(row) for row in entry["values"])
        out_ranges.append(entry)

    if max_cells is not None and total_cells > max_cells:
        raise SheetsError(
            "result_too_large",
            f"read spans {total_cells} cells, exceeding max_cells={max_cells}",
            hint=(
                "narrow the ranges, read only the formula columns with render='formula', or "
                "use 'export' (format=csv/tsv) for bulk values — export writes to a local file "
                "and never hits a token cap"
            ),
        )

    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "render": render,
        "major": major,
        "ranges": out_ranges,
    }


def _read_values_pass(
    services: SheetsServices,
    spreadsheet_id: str,
    ranges: list[str] | None,
    google_filters: list[dict] | None,
    render_option: str,
    major_option: str | None,
) -> dict:
    """Issue ONE values read pass — ``batchGet`` (ranges) or ``batchGetByDataFilter`` (filters).

    Centralizes the request shape shared by the FORMULA and FORMATTED passes of ``render="all"``
    so the two passes never drift. ``majorDimension`` is sent only when non-default (``columns``).
    When ``google_filters`` is set the symbolic-addressing path is used (SPEC §6 P2); otherwise the
    literal-``ranges`` path. The HttpError raised here is classified by the caller.
    """
    values_api = services.sheets.spreadsheets().values()
    if google_filters is not None:
        body: dict = {
            "dataFilters": google_filters,
            "valueRenderOption": render_option,
        }
        if major_option is not None:
            body["majorDimension"] = major_option
        return (
            values_api.batchGetByDataFilter(spreadsheetId=spreadsheet_id, body=body)
            .execute()
        )
    kwargs: dict = {
        "spreadsheetId": spreadsheet_id,
        "ranges": ranges,
        "valueRenderOption": render_option,
    }
    if major_option is not None:
        kwargs["majorDimension"] = major_option
    return values_api.batchGet(**kwargs).execute()


def _extract_value_ranges(resp: dict, use_filters: bool) -> list[dict]:
    """Pull the flat ``valueRanges`` list out of a values response.

    ``batchGet`` returns ``{"valueRanges": [ValueRange, ...]}`` directly. ``batchGetByDataFilter``
    wraps each in ``{"valueRanges": [{"dataFilters": [...], "valueRange": ValueRange}, ...]}``, so
    the inner ``valueRange`` is unwrapped here — making the downstream serialization identical for
    both addressing paths (SPEC §6 P2).
    """
    raw = resp.get("valueRanges") or []
    if not use_filters:
        return raw
    out: list[dict] = []
    for entry in raw:
        entry = entry or {}
        # ``batchGetByDataFilter`` nests the ValueRange under ``valueRange``; tolerate a flat
        # entry too (defensive — some mocked/older shapes return it directly).
        vr = entry.get("valueRange")
        out.append(vr if isinstance(vr, dict) else entry)
    return out


def _sparsify_computed(
    values_rect: list[list], computed_rect: list[list]
) -> tuple[list[list], bool]:
    """Null out ``computed`` cells equal to their ``values`` counterpart (ISSUES.md #12).

    Both inputs share one rectangle (``_pad_to_common_rectangle``), so a positional zip is
    exact. A cell where the FORMATTED value equals the FORMULA-pass literal — compared
    str-normalized so the typed ``185`` matches the formatted ``"185"`` — becomes ``None`` (a
    "same as values" hole); a differing cell keeps its computed value. Returns the sparse matrix
    and whether ANY cell differed (so the caller can drop ``computed`` for a fully-static range).
    """
    sparse: list[list] = []
    any_diff = False
    for v_row, c_row in zip(values_rect, computed_rect):
        out_row: list = []
        for v, c in zip(v_row, c_row):
            if _same_value(v, c):
                out_row.append(None)
            else:
                out_row.append(c)
                any_diff = True
        sparse.append(out_row)
    return sparse, any_diff


def _same_value(v: object, c: object) -> bool:
    """True if a FORMULA-pass literal and its FORMATTED counterpart denote the same value.

    Equal objects match directly; otherwise compare string renderings so the typed number
    ``185`` (FORMULA) equals the formatted string ``"185"`` (FORMATTED_VALUE), which is the
    common staticized-cell case (DESIGN §3.3 — FORMULA returns literals typed, FORMATTED stringy).
    """
    if v == c:
        return True
    return str(v) == str(c)


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

    return _summarize_value_update(spreadsheet_id, resp)


def _summarize_value_update(spreadsheet_id: str, resp: dict) -> dict:
    """Fold a ``batchUpdate``/``batchUpdateByDataFilter`` response into the standard write dict.

    Both value-write APIs (literal ``batchUpdate`` and the DataFilter-addressed
    ``batchUpdateByDataFilter``) return the SAME response shape: a ``responses[]`` list of
    ``UpdateValuesResponse`` (each with ``updatedRange``/``updatedCells``/``updatedRows``/
    ``updatedColumns``) plus authoritative top-level ``totalUpdated*`` totals. This shared
    summarizer keeps the two writers' return contracts identical (DESIGN "no duplicated logic").
    """
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


def write_values_by_data_filter(
    services: SheetsServices,
    spreadsheet_id: str,
    data: list[dict],
    *,
    input: str = "user_entered",
) -> dict:
    """Write/update ranges addressed by SYMBOLIC ``DataFilter`` selectors (insert-proof).

    The DataFilter counterpart of :func:`write_values` — the write-side symmetry for the
    metadata-addressed *read* path (:func:`read_values` ``data_filters``). Each write item names
    its target by a stable selector rather than a literal A1 string (which shifts when rows/columns
    are inserted), then writes via ``values.batchUpdateByDataFilter``. Each ``data`` item carries
    EXACTLY ONE selector inline — ``{"a1": "Sheet1!A1:B10"}`` | ``{"gridRange": {...}}`` |
    ``{"developerMetadataLookup": {...}}`` (matches a block previously tagged via ``metadata``) —
    plus ``values`` and an optional ``major``:

        [{"developerMetadataLookup": {"metadataKey": "block:totals"}, "values": [["x", "y"]]}]

    The selector is translated by the SHARED ``dataselector.build_data_filter`` the read path uses
    (no duplicated selector logic), and wrapped under each request item's singular ``dataFilter``
    key. Defaults to ``valueInputOption=USER_ENTERED`` (formulas work). ``major`` is ``"rows"``
    (Google's default, omitted on the wire) | ``"columns"`` per item.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        data: List of ``{<selector>, "values", "major"?}`` write items (one selector each).
        input: ``"user_entered"`` (default) | ``"raw"``.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "updatedRanges": [...], "updatedCells": ...,
        "updatedRows": ..., "updatedColumns": ...}`` — same shape as :func:`write_values`.
    """
    if not data:
        raise SheetsError(
            "empty_payload", "write_values_by_data_filter requires at least one data item"
        )
    value_input_option = _resolve_input_option(input)

    from .dataselector import build_data_filter

    api_data: list[dict] = []
    for item in data:
        # ``build_data_filter`` reads ONLY the selector key from the item, ignoring the
        # ``values``/``major`` siblings — so the whole item can be passed straight in.
        data_filter = build_data_filter(services, spreadsheet_id, item)
        entry: dict = {"dataFilter": data_filter, "values": item.get("values", [])}
        major_option = _resolve_major_option(item.get("major", "rows"))
        if major_option is not None:
            entry["majorDimension"] = major_option
        api_data.append(entry)

    body = {"valueInputOption": value_input_option, "data": api_data}
    try:
        resp = (
            services.sheets.spreadsheets()
            .values()
            .batchUpdateByDataFilter(spreadsheetId=spreadsheet_id, body=body)
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc

    return _summarize_value_update(spreadsheet_id, resp)


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


# Mapping from a clear flag to the top-level ``CellData`` field-mask token its ``updateCells``
# request must name. Masking a field with NO ``rows`` in the request clears it: ``userEnteredValue``
# erases the value, ``userEnteredFormat`` the whole format, ``dataValidation`` the per-cell rule,
# ``note`` the cell note. The mask is hand-built by joining exactly the requested tokens — it is
# deliberately NOT routed through ``build_fields_mask``, whose no-op guard discards the empty-dict
# structural sentinels (``{"userEnteredFormat": {}}`` etc.) and would silently clear nothing.
_CLEAR_FIELD_TOKENS: dict[str, str] = {
    "values": "userEnteredValue",
    "formats": "userEnteredFormat",
    "validation": "dataValidation",
    "notes": "note",
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
    ``batchUpdate`` with ``updateCells`` + a fields mask (hand-built by joining the requested
    ``CellData`` field tokens) covering only the requested subfields over the ``GridRange``.
    When ``values`` is also requested alongside structural clears, a SINGLE atomic
    ``updateCells`` ``batchUpdate`` is issued whose fields mask additionally includes
    ``userEnteredValue`` — so the value clear and the structural clear either both apply or
    neither does (no two-call window that could erase values and then fail to clear the rest).

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

    # Hand-build the ``updateCells`` fields mask once (the same token set applies to every
    # range): join exactly the requested ``CellData`` field tokens, values first so a mixed
    # clear folds ``userEnteredValue`` into the SAME atomic request. NOT routed through
    # ``build_fields_mask`` — its no-op guard would drop the empty structural sentinels.
    structural = formats or validation or notes
    field_tokens: list[str] = []
    if values:
        field_tokens.append(_CLEAR_FIELD_TOKENS["values"])
    if formats:
        field_tokens.append(_CLEAR_FIELD_TOKENS["formats"])
    if validation:
        field_tokens.append(_CLEAR_FIELD_TOKENS["validation"])
    if notes:
        field_tokens.append(_CLEAR_FIELD_TOKENS["notes"])
    fields = ",".join(field_tokens)

    try:
        if values and not structural:
            # Values-only: the cheap, targeted values API clears trailing region correctly.
            (
                services.sheets.spreadsheets()
                .values()
                .batchClear(spreadsheetId=spreadsheet_id, body={"ranges": ranges})
                .execute()
            )
        elif structural:
            # Structural-only OR mixed (values + structural). One ``updateCells`` batchUpdate.
            # For the mixed case the value clear rides in the SAME request (atomic) because
            # ``userEnteredValue`` is already folded into ``fields`` above.
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


def clear_by_data_filter(
    services: SheetsServices,
    spreadsheet_id: str,
    data_filters: list[dict],
) -> dict:
    """Clear cell VALUES from ranges addressed by SYMBOLIC ``DataFilter`` selectors (insert-proof).

    The DataFilter counterpart of :func:`clear`'s values-only path — the write-side symmetry for the
    metadata-addressed read path. Each selector names a region by a stable handle rather than a
    literal A1 (which shifts on row/column inserts), then clears via ``values.batchClearByDataFilter``.
    ``data_filters`` is the SAME public selector list the read path accepts — each item is one of
    ``{"a1": "Sheet1!A1:B10"}`` | ``{"gridRange": {...}}`` | ``{"developerMetadataLookup": {...}}`` —
    translated by the SHARED ``dataselector.build_data_filters`` (no duplicated selector logic; the
    empty-list guard lives there too). Like ``values.batchClear``, this clears VALUES only, leaving
    formats/validation/notes intact.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        data_filters: Non-empty list of selector dicts (one selector each).

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "clearedRanges": [...]}`` — the A1 ranges Google
        reports as cleared.
    """
    from .dataselector import build_data_filters

    google_filters = build_data_filters(services, spreadsheet_id, data_filters)

    try:
        resp = (
            services.sheets.spreadsheets()
            .values()
            .batchClearByDataFilter(
                spreadsheetId=spreadsheet_id,
                body={"dataFilters": google_filters},
            )
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc

    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "clearedRanges": resp.get("clearedRanges", []),
    }
