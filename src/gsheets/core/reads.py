"""The rich read surface: overview / inspect / read_conditional_formats (DESIGN §3.3).

This is where read-side richness lives. ``overview`` is the cheap orientation call (no grid
data; counts ``len()``-ed in core). ``inspect`` is the flagship per-cell read (values +
formulas + both formats + merges + validation, optional compact rectangular RLE).
``read_conditional_formats`` is the PRIORITY read (CF rules serialized to body-only lines).

PURE core module: imports only stdlib + sibling core modules. It must NEVER import
``fastmcp``, ``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models`` (DESIGN §1 boundary).
"""

from __future__ import annotations

from googleapiclient.errors import HttpError

from . import condformat
from .addressing import gridrange_to_a1, parse_a1
from .errors import SheetsError, classify_google_error
from .flatten import flatten_cell_format
from .rules import validation_to_rule
from .service import SheetsServices
from .values import pad_jagged

# ---------------------------------------------------------------------------
# overview — cheap orientation snapshot (DESIGN §3.3)
# ---------------------------------------------------------------------------

# The NARROW mask: only the cheapest length-yielding subfields of the protectedRanges /
# conditionalFormats arrays (DESIGN §3.3). MUST NOT widen to whole rule/protected bodies.
_OVERVIEW_FIELDS = (
    "properties.title,"
    "sheets.properties("
    "sheetId,title,index,sheetType,gridProperties,tabColorStyle"
    "),"
    "sheets.protectedRanges.protectedRangeId,"
    "sheets.conditionalFormats.ranges,"
    "namedRanges(name,namedRangeId,range)"
)


def overview(services: SheetsServices, spreadsheet_id: str) -> dict:
    """Cheap orientation snapshot — NO grid data (DESIGN §3.3).

    Reads a narrow mask (``sheets.protectedRanges.protectedRangeId`` +
    ``sheets.conditionalFormats.ranges`` — the cheapest length-yielding subfields) and
    ``len()``s the arrays in core to produce ``protectedRangeCount`` /
    ``conditionalFormatCount``. The mask MUST NOT be widened to whole rule/protected bodies.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "title": ..., "sheets": [...],
        "namedRanges": [...]}`` (see DESIGN §3.3 for the per-sheet shape).
    """
    try:
        resp = (
            services.sheets.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields=_OVERVIEW_FIELDS)
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc

    title = (resp.get("properties") or {}).get("title")

    sheets_out: list[dict] = []
    for entry in resp.get("sheets", []) or []:
        sheets_out.append(_overview_sheet(entry or {}))

    named_ranges_out: list[dict] = []
    for nr in resp.get("namedRanges", []) or []:
        named_ranges_out.append(
            _overview_named_range(services, spreadsheet_id, nr or {})
        )

    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "title": title,
        "sheets": sheets_out,
        "namedRanges": named_ranges_out,
    }


def _overview_sheet(entry: dict) -> dict:
    """Build one per-sheet overview row, ``len()``-ing the count-yielding arrays."""
    props = entry.get("properties") or {}
    grid = props.get("gridProperties") or {}

    out: dict = {
        "sheetId": props.get("sheetId"),
        "title": props.get("title"),
        "index": props.get("index"),
        "type": props.get("sheetType"),
        "rows": grid.get("rowCount"),
        "cols": grid.get("columnCount"),
        "frozenRows": grid.get("frozenRowCount", 0),
        "frozenCols": grid.get("frozenColumnCount", 0),
    }

    tab_color = _tab_color_hex(props.get("tabColorStyle"))
    if tab_color is not None:
        out["tabColor"] = tab_color

    out["protectedRangeCount"] = len(entry.get("protectedRanges") or [])
    out["conditionalFormatCount"] = len(entry.get("conditionalFormats") or [])
    return out


def _tab_color_hex(tab_color_style: object) -> str | None:
    """Flatten a sheet's ``tabColorStyle`` to a hex/theme string, else ``None``."""
    if not isinstance(tab_color_style, dict) or not tab_color_style:
        return None
    from . import colors

    try:
        return colors.color_style_to_hex(tab_color_style)
    except ValueError:
        return None


def _overview_named_range(
    services: SheetsServices, spreadsheet_id: str, nr: dict
) -> dict:
    """Build one named-range overview row, resolving the ``GridRange`` to an A1 string."""
    out: dict = {"name": nr.get("name"), "namedRangeId": nr.get("namedRangeId")}
    grid = nr.get("range")
    if isinstance(grid, dict) and "sheetId" in grid:
        try:
            out["range"] = gridrange_to_a1(services, spreadsheet_id, grid)
        except SheetsError:
            # A dangling sheetId (rare) — surface the named range without an A1 range
            # rather than failing the whole overview.
            out["range"] = None
    else:
        out["range"] = None
    return out


# ---------------------------------------------------------------------------
# inspect — flagship rich per-cell read (DESIGN §3.3)
# ---------------------------------------------------------------------------


def inspect(
    services: SheetsServices,
    spreadsheet_id: str,
    range: str,
    *,
    compact: bool = False,
    include_effective_format: bool = True,
    include_user_entered_format: bool = True,
    include_formulas: bool = True,
    include_validation: bool = True,
) -> dict:
    """Flagship rich read: values + formulas + both formats + merges + validation (DESIGN §3.3).

    Uses a tight ``fields`` mask (never ``includeGridData``), trimmed by the ``include_*``
    flags. Non-compact returns a per-cell row-major padded rectangle under ``cells``;
    ``compact=True`` collapses identical cells into rectangular ``a1Range`` runs (carrying
    ``note`` and ``validationRule`` so compact reads do not lose them). Each cell surfaces the
    structured ``validationRule`` that round-trips into ``set_validation``.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        range: A1 range to inspect.
        compact: Collapse identical cells into rectangular runs.
        include_effective_format: Include ``effectiveFormat`` per cell.
        include_user_entered_format: Include ``userEnteredFormat`` per cell.
        include_formulas: Include the cell formula when present.
        include_validation: Include validation (terse one-liner + structured rule).

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "sheet": ..., "range": ..., "rows": ...,
        "cols": ..., "cells"|"runs": [...], "merges": [...], "compact": ...}``.
    """
    # Validate the A1 range up front (raises bad_range on garbage) so we never issue a
    # request for an unparseable range.
    parse_a1(range)

    fields = _inspect_fields(
        include_effective_format=include_effective_format,
        include_user_entered_format=include_user_entered_format,
        include_formulas=include_formulas,
        include_validation=include_validation,
    )

    try:
        resp = (
            services.sheets.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, ranges=[range], fields=fields)
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc

    sheets = resp.get("sheets") or []
    if not sheets:
        raise SheetsError(
            "no_data",
            f"no sheet data returned for range {range!r}",
            hint="check the spreadsheet id and that the range names an existing sheet",
        )
    sheet_obj = sheets[0] or {}
    sheet_props = sheet_obj.get("properties") or {}
    sheet_title = sheet_props.get("title")

    data_blocks = sheet_obj.get("data") or []
    block = data_blocks[0] if data_blocks else {}
    start_row = block.get("startRow", 0) or 0
    start_col = block.get("startColumn", 0) or 0
    row_data = block.get("rowData") or []

    # Build a rectangular grid of per-cell structured dicts (None for absent cells), then
    # pad jagged rows to a full rectangle (DESIGN §3.3 — "jagged filled").
    grid = _build_cell_grid(
        row_data,
        start_row=start_row,
        start_col=start_col,
        include_effective_format=include_effective_format,
        include_user_entered_format=include_user_entered_format,
        include_formulas=include_formulas,
        include_validation=include_validation,
    )
    n_rows = len(grid)
    n_cols = max((len(r) for r in grid), default=0)
    grid = _pad_grid(grid, n_cols)

    merges = [
        gridrange_to_a1(services, spreadsheet_id, m)
        for m in (sheet_obj.get("merges") or [])
    ]

    out: dict = {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "sheet": sheet_title,
        "range": range,
        "rows": n_rows,
        "cols": n_cols,
        "merges": merges,
        "compact": compact,
    }

    if compact:
        out["runs"] = _build_runs(grid, start_row=start_row, start_col=start_col)
    else:
        out["cells"] = _flatten_grid_to_cells(grid, start_row=start_row, start_col=start_col)

    return out


def _inspect_fields(
    *,
    include_effective_format: bool,
    include_user_entered_format: bool,
    include_formulas: bool,
    include_validation: bool,
) -> str:
    """Build the tight per-cell ``fields`` mask, trimmed by the include_* flags (DESIGN §3.3).

    ``userEnteredValue`` (the formula source) is requested only when formulas are wanted;
    ``effectiveValue``/``formattedValue`` always ride along (they carry the value), so a
    cell's value is never lost.
    """
    cell_fields = ["effectiveValue", "formattedValue"]
    if include_formulas:
        cell_fields.append("userEnteredValue")
    if include_user_entered_format:
        cell_fields.append("userEnteredFormat")
    if include_effective_format:
        cell_fields.append("effectiveFormat")
    if include_validation:
        cell_fields.append("dataValidation")
    # ``note`` is cheap and always useful (also surfaced in compact runs).
    cell_fields.append("note")

    values_mask = f"values({','.join(cell_fields)})"
    data_mask = f"data(rowData({values_mask}),startRow,startColumn)"
    return f"sheets(properties(sheetId,title),merges,{data_mask})"


def _build_cell_grid(
    row_data: list[dict],
    *,
    start_row: int,
    start_col: int,
    include_effective_format: bool,
    include_user_entered_format: bool,
    include_formulas: bool,
    include_validation: bool,
) -> list[list[dict | None]]:
    """Build a row-major grid of per-cell structured dicts (``None`` for empty cells).

    Each non-empty cell carries the structured fields DESIGN §3.1 ``Cell`` defines, MINUS the
    ``a1`` key (added later, where the absolute row/col is known). An entirely-empty cell maps
    to ``None`` so the rectangle-padder and the compact run-finder can treat "absent" cells
    uniformly.
    """
    grid: list[list[dict | None]] = []
    for r_row in row_data:
        cells = (r_row or {}).get("values") or []
        row_out: list[dict | None] = []
        for cell in cells:
            row_out.append(
                _build_cell(
                    cell or {},
                    include_effective_format=include_effective_format,
                    include_user_entered_format=include_user_entered_format,
                    include_formulas=include_formulas,
                    include_validation=include_validation,
                )
            )
        grid.append(row_out)
    return grid


def _build_cell(
    cell: dict,
    *,
    include_effective_format: bool,
    include_user_entered_format: bool,
    include_formulas: bool,
    include_validation: bool,
) -> dict | None:
    """Build one structured cell dict (no ``a1`` yet), or ``None`` if the cell is empty.

    A cell is "empty" when it carries no value, no formula, no formatting, no note, and no
    validation under the requested mask — those collapse to ``None`` so compact runs drop them
    and non-compact reads pad them as blanks.
    """
    out: dict = {}

    value = _cell_value(cell)
    if value is not None:
        out["value"] = value

    if include_formulas:
        formula = _cell_formula(cell)
        if formula is not None:
            out["formula"] = formula

    if include_user_entered_format:
        uef = flatten_cell_format(cell.get("userEnteredFormat"))
        if uef:
            out["userEnteredFormat"] = uef

    if include_effective_format:
        eff = flatten_cell_format(cell.get("effectiveFormat"))
        if eff:
            out["effectiveFormat"] = eff

    note = cell.get("note")
    if note is not None and note != "":
        out["note"] = note

    if include_validation:
        google_validation = cell.get("dataValidation")
        if isinstance(google_validation, dict) and google_validation:
            rule = validation_to_rule(google_validation)
            out["validation"] = _validation_one_liner(rule)
            out["validationRule"] = rule

    return out or None


def _cell_value(cell: dict) -> object | None:
    """Resolve a cell's display value.

    Prefers ``formattedValue`` (what the user sees); falls back to the typed
    ``effectiveValue`` (``stringValue``/``numberValue``/``boolValue``/``errorValue``) when
    Google omits the formatted string. Returns ``None`` when the cell has no value at all.
    """
    formatted = cell.get("formattedValue")
    if formatted is not None:
        return formatted

    effective = cell.get("effectiveValue")
    if isinstance(effective, dict):
        for key in ("stringValue", "numberValue", "boolValue"):
            if key in effective:
                return effective[key]
        error = effective.get("errorValue")
        if isinstance(error, dict):
            return error.get("type") or error.get("message")
    return None


def _cell_formula(cell: dict) -> str | None:
    """Return the cell's formula (a ``userEnteredValue.formulaValue``) or ``None``.

    A formula is present only when the user-entered value is an actual formula string (begins
    with ``=``); a literal user-entered string/number is NOT a formula and yields ``None``.
    """
    uev = cell.get("userEnteredValue")
    if isinstance(uev, dict):
        formula = uev.get("formulaValue")
        if isinstance(formula, str) and formula.startswith("="):
            return formula
    return None


def _validation_one_liner(rule: dict) -> str:
    """Render a structured ``ValidationRule`` to the terse, token-cheap one-liner.

    ``{"type": "ONE_OF_LIST", "values": ["Yes", "No"]}`` -> ``"ONE_OF_LIST(Yes,No)"``;
    ``{"type": "BOOLEAN"}`` -> ``"BOOLEAN"``; a range-sourced rule ->
    ``"ONE_OF_RANGE(Cliff!Z1:Z10)"``.
    """
    if not isinstance(rule, dict):
        return ""
    vtype = rule.get("type")
    if not vtype:
        return ""
    source = rule.get("source")
    if source:
        return f"{vtype}({source})"
    values = rule.get("values")
    if values:
        return f"{vtype}({','.join(str(v) for v in values)})"
    return str(vtype)


def _pad_grid(
    grid: list[list[dict | None]], width: int
) -> list[list[dict | None]]:
    """Pad every grid row to ``width`` with ``None`` (the "jagged filled" rectangle)."""
    out: list[list[dict | None]] = []
    for row in grid:
        row = list(row)
        if len(row) < width:
            row = row + [None] * (width - len(row))
        out.append(row)
    return out


def _flatten_grid_to_cells(
    grid: list[list[dict | None]], *, start_row: int, start_col: int
) -> list[dict]:
    """Flatten the padded grid to a row-major list of ``Cell`` dicts (DESIGN §3.1).

    Each emitted cell gains its absolute ``a1`` address (computed from the data block's
    ``startRow``/``startColumn`` offset). Empty cells (``None``) are emitted as a bare
    ``{"a1": ...}`` so the rectangle is fully padded (jagged filled) and consumers can index by
    position.
    """
    cells: list[dict] = []
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            a1 = _cell_a1(start_row + r, start_col + c)
            if cell is None:
                cells.append({"a1": a1})
            else:
                cells.append({"a1": a1, **cell})
    return cells


# ---------------------------------------------------------------------------
# Compact rectangular RLE (DESIGN §3.3 "Run shape and RLE direction")
# ---------------------------------------------------------------------------


def _run_key(cell: dict | None) -> tuple:
    """A hashable identity key for a cell so two equal cells merge into one run.

    Two cells merge iff their ``value``, ``formula``, ``format``, ``note``, AND
    ``validationRule`` are all identical (DESIGN §3.3). ``None`` (empty) cells never merge
    with a non-empty cell and are dropped entirely from runs.
    """
    if cell is None:
        return ("__empty__",)
    return (
        cell.get("value"),
        cell.get("formula"),
        _stable_repr(_run_format(cell)),
        cell.get("note"),
        _stable_repr(cell.get("validationRule")),
    )


def _run_format(cell: dict) -> dict | None:
    """The single ``format`` a run carries (DESIGN §3.3 run shape uses one ``format`` key).

    Prefers ``effectiveFormat`` (what renders, incl. conditional results) and falls back to
    ``userEnteredFormat``; this is the format the compact run surfaces as ``format``.
    """
    if "effectiveFormat" in cell:
        return cell["effectiveFormat"]
    if "userEnteredFormat" in cell:
        return cell["userEnteredFormat"]
    return None


def _stable_repr(obj: object) -> str:
    """A deterministic string identity for a (possibly nested) JSON-ish object."""
    import json

    try:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    except TypeError:
        return repr(obj)


def _build_runs(
    grid: list[list[dict | None]], *, start_row: int, start_col: int
) -> list[dict]:
    """Collapse identical cells into maximal rectangular ``a1Range`` runs (DESIGN §3.3).

    Greedy rectangle growth: scan row-major for the first un-consumed non-empty cell; extend
    RIGHT while the row matches, then extend DOWN while the full row-band matches; mark the
    rectangle consumed; emit one run. Single non-repeating cells degenerate to a 1x1 range
    (e.g. ``"D7:D7"``). Empty cells are skipped. The run carries ``note`` and
    ``validationRule`` whenever present so compact reads never drop them.
    """
    n_rows = len(grid)
    n_cols = max((len(r) for r in grid), default=0)
    consumed = [[False] * n_cols for _ in range(n_rows)]
    runs: list[dict] = []

    for r in range(n_rows):
        for c in range(n_cols):
            if consumed[r][c]:
                continue
            cell = grid[r][c]
            if cell is None:
                consumed[r][c] = True
                continue

            key = _run_key(cell)

            # Extend RIGHT along this row while the cell key matches and is un-consumed.
            c_end = c
            while (
                c_end + 1 < n_cols
                and not consumed[r][c_end + 1]
                and _run_key(grid[r][c_end + 1]) == key
            ):
                c_end += 1

            # Extend DOWN while the ENTIRE row-band [c..c_end] matches and is un-consumed.
            r_end = r
            while r_end + 1 < n_rows and _band_matches(
                grid, consumed, r_end + 1, c, c_end, key
            ):
                r_end += 1

            for rr in range(r, r_end + 1):
                for cc in range(c, c_end + 1):
                    consumed[rr][cc] = True

            runs.append(
                _make_run(
                    cell,
                    start_row + r,
                    start_col + c,
                    start_row + r_end,
                    start_col + c_end,
                )
            )

    return runs


def _band_matches(
    grid: list[list[dict | None]],
    consumed: list[list[bool]],
    row: int,
    c_lo: int,
    c_hi: int,
    key: tuple,
) -> bool:
    """True iff every cell in ``grid[row][c_lo..c_hi]`` is un-consumed and matches ``key``."""
    for cc in range(c_lo, c_hi + 1):
        if consumed[row][cc] or _run_key(grid[row][cc]) != key:
            return False
    return True


def _make_run(
    cell: dict, r_lo: int, c_lo: int, r_hi: int, c_hi: int
) -> dict:
    """Build one run dict for a rectangle whose top-left cell is ``cell`` (DESIGN §3.3).

    The run carries ``a1Range`` (a bare ``"A1:B2"`` with no sheet prefix), ``value``,
    ``formula`` (``None`` when the cell has none), ``format`` (the §3.3 single format), and —
    only when set — ``note`` and ``validationRule``.
    """
    # A run is ALWAYS rendered as a "lo:hi" rectangle, even when it is a single cell
    # (DESIGN §3.3: a 1x1 run degenerates to e.g. "D7:D7", not "D7").
    a1_lo = _cell_a1(r_lo, c_lo)
    a1_hi = _cell_a1(r_hi, c_hi)
    a1_range = f"{a1_lo}:{a1_hi}"

    run: dict = {
        "a1Range": a1_range,
        "value": cell.get("value"),
        "formula": cell.get("formula"),
    }
    fmt = _run_format(cell)
    run["format"] = fmt if fmt is not None else {}

    note = cell.get("note")
    if note is not None and note != "":
        run["note"] = note
    validation_rule = cell.get("validationRule")
    if validation_rule:
        run["validationRule"] = validation_rule
    return run


def _cell_a1(row0: int, col0: int) -> str:
    """Render a 0-based (row, col) to a bare A1 cell ref (``(0,0)`` -> ``"A1"``)."""
    return f"{_col_letters(col0)}{row0 + 1}"


def _col_letters(col0: int) -> str:
    """Convert a 0-based column index to letters (``0`` -> ``"A"``, ``26`` -> ``"AA"``)."""
    letters: list[str] = []
    n = col0 + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(ord("A") + rem))
    return "".join(reversed(letters))


# ---------------------------------------------------------------------------
# read_conditional_formats — PRIORITY read (DESIGN §3.3)
# ---------------------------------------------------------------------------

_CF_FIELDS = "sheets(properties(sheetId,title),conditionalFormats)"


def read_conditional_formats(
    services: SheetsServices,
    spreadsheet_id: str,
    sheet: str | None = None,
) -> dict:
    """PRIORITY read: per-sheet conditional-format rules serialized to readable lines (DESIGN §3.3).

    Reads ``sheets(properties(sheetId,title),conditionalFormats)``, optionally filtered to one
    sheet. Each rule is serialized to a body-only ``line`` (the human/AI-facing rendering) plus
    structured fields (``ranges``/``kind``/``condition``/``format``) for round-trip; the
    positional ``index`` (0 = highest priority) is the only addressing source of truth. Returns
    the multi-sheet envelope shared with ``structure(action="read")``.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        sheet: Restrict to one tab; ``None`` ⇒ every sheet.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "sheets": [{"sheet", "sheetId", "rules": [...]}]}``.
    """
    try:
        resp = (
            services.sheets.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields=_CF_FIELDS)
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc

    all_sheets = resp.get("sheets") or []

    if sheet is not None and not any(
        ((s or {}).get("properties") or {}).get("title") == sheet for s in all_sheets
    ):
        available = ", ".join(
            repr(((s or {}).get("properties") or {}).get("title")) for s in all_sheets
        ) or "(none)"
        raise SheetsError(
            "sheet_not_found",
            f"sheet {sheet!r} not found in spreadsheet",
            hint=f"available sheets: {available}",
        )

    sheets_out: list[dict] = []
    for sheet_obj in all_sheets:
        sheet_obj = sheet_obj or {}
        props = sheet_obj.get("properties") or {}
        title = props.get("title")
        if sheet is not None and title != sheet:
            continue

        rules_out: list[dict] = []
        for index, raw_rule in enumerate(sheet_obj.get("conditionalFormats") or []):
            rules_out.append(
                _serialize_cf_rule(
                    services, spreadsheet_id, raw_rule or {}, index
                )
            )

        sheets_out.append(
            {"sheet": title, "sheetId": props.get("sheetId"), "rules": rules_out}
        )

    return {"ok": True, "spreadsheetId": spreadsheet_id, "sheets": sheets_out}


def _serialize_cf_rule(
    services: SheetsServices,
    spreadsheet_id: str,
    raw_rule: dict,
    index: int,
) -> dict:
    """Serialize one Google ``ConditionalFormatRule`` to the read-side rule dict (DESIGN §3.3).

    Resolves each Google ``GridRange`` to an A1 string FIRST (condformat is serviceless and
    takes A1 ranges, DESIGN §4 boundary), then serializes to the body-only ``line`` and parses
    it back to recover the structured ``ranges``/``kind``/``condition``-or-``stops``/``format``
    fields — keeping ``line`` and the structured fields trivially consistent (one source).
    """
    a1_ranges = [
        gridrange_to_a1(services, spreadsheet_id, gr)
        for gr in (raw_rule.get("ranges") or [])
    ]

    # Build a copy with A1-string ranges for the serviceless serializer.
    rule_a1 = dict(raw_rule)
    rule_a1["ranges"] = a1_ranges

    line = condformat.serialize_rule(rule_a1)
    parsed = condformat.parse_rule_line(line)

    out: dict = {
        "index": index,
        "line": line,
        "ranges": parsed["ranges"],
        "kind": parsed["kind"],
    }
    if parsed["kind"] == "gradient":
        out["stops"] = parsed.get("stops", [])
    else:
        out["condition"] = parsed.get("condition", {})
    out["format"] = parsed.get("format", {})
    return out
