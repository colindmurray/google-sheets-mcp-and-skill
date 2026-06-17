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
from .addressing import (
    a1_to_gridrange,
    gridrange_to_a1,
    parse_a1,
    sheet_index_cache,
    sheet_titles,
)
from .errors import SheetsError, classify_google_error
from .flatten import flatten_cell_format
from .pivot import serialize_pivot
from .richtext import serialize_text_runs
from .rules import validation_to_rule
from .service import SheetsServices
from .values import pad_jagged

# ---------------------------------------------------------------------------
# overview — cheap orientation snapshot (DESIGN §3.3)
# ---------------------------------------------------------------------------

# The NARROW mask: only the cheapest length-yielding subfields of the protectedRanges /
# conditionalFormats arrays (DESIGN §3.3). MUST NOT widen to whole rule/protected bodies.
_OVERVIEW_FIELDS = (
    "properties(title,locale,timeZone),"
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

    props = resp.get("properties") or {}
    title = props.get("title")

    sheets_out: list[dict] = []
    for entry in resp.get("sheets", []) or []:
        sheets_out.append(_overview_sheet(entry or {}))

    named_ranges_out: list[dict] = []
    for nr in resp.get("namedRanges", []) or []:
        named_ranges_out.append(
            _overview_named_range(services, spreadsheet_id, nr or {})
        )

    out: dict = {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "title": title,
        "sheets": sheets_out,
        "namedRanges": named_ranges_out,
    }

    # Spreadsheet-level locale / timeZone (§X.12) — omit when absent (token efficiency).
    locale = props.get("locale")
    if locale:
        out["locale"] = locale
    time_zone = props.get("timeZone")
    if time_zone:
        out["timeZone"] = time_zone

    return out


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
    include_rich_text: bool = False,
    include_pivot: bool = False,
) -> dict:
    """Flagship rich read: values + formulas + both formats + merges + validation (DESIGN §3.3).

    Uses a tight ``fields`` mask (never ``includeGridData``), trimmed by the ``include_*``
    flags. Non-compact returns a per-cell row-major padded rectangle under ``cells``;
    ``compact=True`` collapses identical cells into rectangular ``a1Range`` runs (carrying
    ``note`` and ``validationRule`` so compact reads do not lose them). Each cell surfaces the
    structured ``validationRule`` that round-trips into ``set_validation``.

    Two OPT-IN read enrichments (off by default → base mask + base behavior unchanged, zero
    token cost):
    - ``include_rich_text`` (DESIGN §X.1): adds ``textFormatRuns`` + ``hyperlink``
      to the per-cell mask; a cell gains ``"runs": [TextRun, …]`` (per styled char-range
      segment) and/or ``"hyperlink": "https://…"`` ONLY when it carries them.
    - ``include_pivot`` (DESIGN §X.6): adds ``pivotTable`` to the per-cell mask; the
      pivot's anchor (top-left) cell gains ``"pivot": {…}`` ONLY when present.
    In compact mode, two cells differing in ``runs``/``hyperlink``/``pivot`` never merge into one
    run (``_run_key`` includes a stable repr of each).

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        range: A1 range to inspect.
        compact: Collapse identical cells into rectangular runs.
        include_effective_format: Include ``effectiveFormat`` per cell.
        include_user_entered_format: Include ``userEnteredFormat`` per cell.
        include_formulas: Include the cell formula when present.
        include_validation: Include validation (terse one-liner + structured rule).
        include_rich_text: Add ``textFormatRuns`` + ``hyperlink`` to the mask; attach per-cell
            ``runs``/``hyperlink`` only when present (§X.1). Default ``False``.
        include_pivot: Add ``pivotTable`` to the mask; attach ``pivot`` to its anchor cell only
            when present (§X.6). Default ``False``.

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
        include_rich_text=include_rich_text,
        include_pivot=include_pivot,
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

    cell_view = _serialize_cells(
        block,
        compact=compact,
        include_effective_format=include_effective_format,
        include_user_entered_format=include_user_entered_format,
        include_formulas=include_formulas,
        include_validation=include_validation,
        include_rich_text=include_rich_text,
        include_pivot=include_pivot,
        services=services,
        spreadsheet_id=spreadsheet_id,
    )

    merges = [
        gridrange_to_a1(services, spreadsheet_id, m)
        for m in (sheet_obj.get("merges") or [])
    ]

    out: dict = {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "sheet": sheet_title,
        "range": range,
        "rows": cell_view["rows"],
        "cols": cell_view["cols"],
        "merges": merges,
        "compact": compact,
    }

    if compact:
        out["runs"] = cell_view["runs"]
    else:
        out["cells"] = cell_view["cells"]

    return out


def _serialize_cells(
    block: dict,
    *,
    compact: bool,
    include_effective_format: bool,
    include_user_entered_format: bool,
    include_formulas: bool,
    include_validation: bool,
    include_rich_text: bool = False,
    include_pivot: bool = False,
    services: SheetsServices | None = None,
    spreadsheet_id: str | None = None,
) -> dict:
    """Serialize a PRE-FETCHED grid data block into the cells/runs view (SPEC §3.3 fetch/serialize).

    The whole serialize half of ``inspect``, callable on an already-fetched
    ``GridData`` block (``{startRow, startColumn, rowData}``) — so ``describe`` can reuse the exact
    flatten/colors/compact logic on a slice of its single ``spreadsheets.get`` response instead of
    re-implementing it (DESIGN "no duplicated logic"). Builds the padded per-cell grid, then emits
    EITHER ``cells`` (row-major padded rectangle) or ``runs`` (rectangular RLE) per ``compact``.

    Args:
        block: A Google ``GridData`` block (``startRow``/``startColumn``/``rowData``); an empty
            dict yields an empty (0x0) view.
        compact: Collapse identical cells into rectangular runs (else emit a padded cell list).
        include_*: The same per-cell include flags ``inspect`` exposes (they select which
            subfields each cell carries, matching the fetched mask).
        services / spreadsheet_id: Threaded only for the pivot serializer's source-range
            resolution (used solely when ``include_pivot`` is on).

    Returns:
        ``{"rows": int, "cols": int, "cells": [...]}`` (non-compact) or
        ``{"rows": int, "cols": int, "runs": [...]}`` (compact).
    """
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
        include_rich_text=include_rich_text,
        include_pivot=include_pivot,
        services=services,
        spreadsheet_id=spreadsheet_id,
    )
    n_rows = len(grid)
    n_cols = max((len(r) for r in grid), default=0)
    grid = _pad_grid(grid, n_cols)

    out: dict = {"rows": n_rows, "cols": n_cols}
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
    include_rich_text: bool = False,
    include_pivot: bool = False,
) -> str:
    """Build the tight per-cell ``fields`` mask, trimmed by the include_* flags (DESIGN §3.3).

    ``userEnteredValue`` (the formula source) is requested only when formulas are wanted;
    ``effectiveValue``/``formattedValue`` always ride along (they carry the value), so a
    cell's value is never lost.

    The OPT-IN enrichments (DESIGN §X.1 / §X.6) widen the per-cell mask ONLY when their flag is
    set, so the base mask is unchanged (zero token cost) by default:
    - ``include_rich_text`` → adds ``textFormatRuns`` (per-run styled segments) + ``hyperlink``
      (cell-level link);
    - ``include_pivot`` → adds ``pivotTable`` (the anchor cell's pivot definition).
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
    if include_rich_text:
        cell_fields.append("textFormatRuns")
        cell_fields.append("hyperlink")
    if include_pivot:
        cell_fields.append("pivotTable")

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
    include_rich_text: bool = False,
    include_pivot: bool = False,
    services: SheetsServices | None = None,
    spreadsheet_id: str | None = None,
) -> list[list[dict | None]]:
    """Build a row-major grid of per-cell structured dicts (``None`` for empty cells).

    Each non-empty cell carries the structured fields DESIGN §3.1 ``Cell`` defines, MINUS the
    ``a1`` key (added later, where the absolute row/col is known). An entirely-empty cell maps
    to ``None`` so the rectangle-padder and the compact run-finder can treat "absent" cells
    uniformly. ``services``/``spreadsheet_id`` are threaded only for the pivot serializer's
    ``source`` GridRange → A1 resolution (used solely when ``include_pivot`` is on).
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
                    include_rich_text=include_rich_text,
                    include_pivot=include_pivot,
                    services=services,
                    spreadsheet_id=spreadsheet_id,
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
    include_rich_text: bool = False,
    include_pivot: bool = False,
    services: SheetsServices | None = None,
    spreadsheet_id: str | None = None,
) -> dict | None:
    """Build one structured cell dict (no ``a1`` yet), or ``None`` if the cell is empty.

    A cell is "empty" when it carries no value, no formula, no formatting, no note, no
    validation, no rich-text runs, no hyperlink, and no pivot under the requested mask — those
    collapse to ``None`` so compact runs drop them and non-compact reads pad them as blanks. The
    rich-text/hyperlink/pivot enrichments (§X.1/§X.6) are attached ONLY when present on the cell.
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

    if include_rich_text:
        # textFormatRuns → per-run styled segments (attach ONLY when the cell has them, §X.1).
        runs = serialize_text_runs(cell.get("textFormatRuns"), _cell_text(cell))
        if runs:
            out["runs"] = runs
        # hyperlink is a READ-ONLY Google cell field; attach FLAT only when set.
        hyperlink = cell.get("hyperlink")
        if isinstance(hyperlink, str) and hyperlink:
            out["hyperlink"] = hyperlink

    if include_pivot:
        # Only the pivot's anchor (top-left) cell carries the definition (§X.6).
        pivot = cell.get("pivotTable")
        if isinstance(pivot, dict) and pivot:
            out["pivot"] = serialize_pivot(pivot, services, spreadsheet_id)

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


def _cell_text(cell: dict) -> str | None:
    """The cell's plain display TEXT for slicing ``textFormatRuns`` substrings (§X.0a).

    ``textFormatRuns`` index into the cell's display string, so the slicer needs the raw text
    (not a typed number/bool). Prefers ``formattedValue`` (what the user sees); falls back to
    ``effectiveValue.stringValue``. Returns ``None`` when the cell has no string text (the
    serializer treats ``None`` as the empty string, yielding empty run substrings).
    """
    formatted = cell.get("formattedValue")
    if isinstance(formatted, str):
        return formatted

    effective = cell.get("effectiveValue")
    if isinstance(effective, dict):
        string_value = effective.get("stringValue")
        if isinstance(string_value, str):
            return string_value
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

    Two cells merge iff their ``value``, ``formula``, ``format``, ``note``, ``validationRule``,
    AND the rich-text ``runs`` / ``hyperlink`` / ``pivot`` enrichments are all identical (DESIGN
    §3.3, §X.1). ``None`` (empty) cells never merge with a non-empty cell and are dropped
    entirely from runs. The runs/hyperlink/pivot keys are absent on cells read without those
    flags, so the key degenerates to the base identity (no behavior change by default).
    """
    if cell is None:
        return ("__empty__",)
    return (
        cell.get("value"),
        cell.get("formula"),
        _stable_repr(_run_format(cell)),
        cell.get("note"),
        _stable_repr(cell.get("validationRule")),
        _stable_repr(cell.get("runs")),
        cell.get("hyperlink"),
        _stable_repr(cell.get("pivot")),
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
    only when set — ``note``, ``validationRule``, and the §X.1/§X.6 enrichments ``runs`` /
    ``hyperlink`` / ``pivot``.
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
    # Rich-text / hyperlink / pivot enrichments (§X.1/§X.6) ride the run only when present, so
    # compact reads never silently lose them (cells differing here never merged in the first
    # place, per ``_run_key``).
    runs = cell.get("runs")
    if runs:
        run["runs"] = runs
    hyperlink = cell.get("hyperlink")
    if hyperlink:
        run["hyperlink"] = hyperlink
    pivot = cell.get("pivot")
    if pivot:
        run["pivot"] = pivot
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
    *,
    range: str | None = None,
) -> dict:
    """PRIORITY read: per-sheet conditional-format rules serialized to readable lines (DESIGN §3.3).

    Reads ``sheets(properties(sheetId,title),conditionalFormats)``, optionally filtered to one
    sheet. Each rule is serialized to a body-only ``line`` (the human/AI-facing rendering) plus
    structured fields (``ranges``/``kind``/``condition``/``format``) for round-trip; the
    positional ``index`` (0 = highest priority) is the only addressing source of truth. Returns
    the multi-sheet envelope shared with ``structure(action="read")``.

    ``range`` (SPEC §6 P3 — range-scoped read): an OPTIONAL A1 range. When given, the read is
    scoped to THAT range's sheet and each rule is kept only when its ranges actually INTERSECT the
    range — reusing the SAME ``addressing.gridranges_intersect`` filter ``describe`` uses (SPEC §3.3),
    so "which rules touch this region" is one shared codepath. A surviving rule keeps its ORIGINAL
    positional ``index`` (priority is array position — never renumbered by filtering), so it still
    addresses correctly via ``set_conditional_format``. ``range`` carries its own sheet, so passing
    both ``range`` and ``sheet`` raises ``conflicting_args``.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        sheet: Restrict to one tab; ``None`` ⇒ every sheet. Mutually exclusive with ``range``.
        range: Restrict to the rules INTERSECTING this A1 range (on its own sheet).

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "sheets": [{"sheet", "sheetId", "rules": [...]}]}``.
    """
    if sheet is not None and range is not None:
        raise SheetsError(
            "conflicting_args",
            "pass EITHER `sheet` OR `range` (a range carries its own sheet), not both",
        )

    # A range scopes the read to its sheet AND filters rules to those intersecting it. Resolve it to
    # a GridRange up front (validates the A1 and yields the sheetId + bounds for the intersect test).
    intersecting: dict | None = None
    scope_sheet_id: object = None
    if range is not None:
        intersecting = a1_to_gridrange(services, spreadsheet_id, range)
        scope_sheet_id = intersecting.get("sheetId")

    # Scope the get to the requested sheet/range. WITHOUT this, a single-sheet CF read makes the
    # API load EVERY tab's conditional-format model — on a large workbook that is a multi-minute
    # call (one real sheet: 54 rules took 5m21s unscoped vs 0.74s scoped — ISSUES.md #26). Reading
    # ALL sheets (sheet and range both None) still needs the unscoped fetch. ``ranges`` filters only
    # the per-sheet ``sheets[]``; top-level fields are returned regardless.
    if range is not None:
        get_kwargs: dict = {"ranges": [range]}  # range's sheet already validated by a1_to_gridrange
    elif sheet is not None:
        get_kwargs = {"ranges": [sheet]}
    else:
        get_kwargs = {}

    try:
        resp = (
            services.sheets.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields=_CF_FIELDS, **get_kwargs)
            .execute()
        )
    except HttpError as exc:
        # A range-scoped read of a non-existent sheet 400s on the range parse; preserve the friendly
        # sheet_not_found (with the available list) the unscoped path produced.
        if sheet is not None:
            try:
                titles = sheet_titles(services, spreadsheet_id)
            except SheetsError:
                titles = None
            if titles is not None and sheet not in titles:
                available = ", ".join(repr(t) for t in titles) or "(none)"
                raise SheetsError(
                    "sheet_not_found",
                    f"sheet {sheet!r} not found in spreadsheet",
                    hint=f"available sheets: {available}",
                ) from exc
        raise classify_google_error(exc, account_email=services.account_email) from exc

    all_sheets = resp.get("sheets") or []

    # Defensive net (a scoped get returns only the matching sheet, so this rarely fires now; it
    # still covers the mocked-test path, where the canned response ignores ``ranges``).
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
    # Each rule's gridrange_to_a1 resolves sheetId -> title via _sheet_index; cache it for the whole
    # serialization so a 50+-rule sheet does ONE sheet-index get, not one per rule (ISSUES.md #26).
    with sheet_index_cache():
        for sheet_obj in all_sheets:
            sheet_obj = sheet_obj or {}
            props = sheet_obj.get("properties") or {}
            title = props.get("title")
            if sheet is not None and title != sheet:
                continue
            # A range scopes to only its own sheet (matched by sheetId, not title).
            if intersecting is not None and props.get("sheetId") != scope_sheet_id:
                continue

            rules_out = _serialize_cf_rules(
                services,
                spreadsheet_id,
                sheet_obj.get("conditionalFormats") or [],
                intersecting=intersecting,
            )

            sheets_out.append(
                {"sheet": title, "sheetId": props.get("sheetId"), "rules": rules_out}
            )

    return {"ok": True, "spreadsheetId": spreadsheet_id, "sheets": sheets_out}


def _serialize_cf_rules(
    services: SheetsServices,
    spreadsheet_id: str,
    raw_rules: list,
    *,
    intersecting: dict | None = None,
) -> list[dict]:
    """Serialize a sheet's ``conditionalFormats`` array to the read-side rule list (SPEC §3.3).

    The list-level counterpart of :func:`_serialize_cf_rule`, callable on a pre-fetched per-sheet
    ``conditionalFormats`` array — so ``describe`` reuses the SAME line-grammar serializer instead
    of re-implementing it. Each rule keeps its ORIGINAL positional ``index`` (priority — 0 highest)
    regardless of any filtering, so a rule surfaced by ``describe`` still addresses correctly via
    ``set_conditional_format``.

    When ``intersecting`` (a ``GridRange``) is supplied, only rules whose ranges actually overlap it
    are emitted (via ``addressing.gridranges_intersect``) — the SPEC §3.3 "rules intersecting this
    range only" filter that delivers range-scoped CF for free. ``None`` (the default) emits every
    rule (the standalone ``read_conditional_formats`` behavior, unchanged).

    Args:
        services: The authed handle (resolves each rule's ``GridRange`` -> A1).
        spreadsheet_id: Target spreadsheet id.
        raw_rules: The per-sheet ``conditionalFormats`` array (Google rule dicts).
        intersecting: A ``GridRange`` to filter against; ``None`` keeps every rule.

    Returns:
        A list of serialized rule dicts (``index``/``line``/``ranges``/``kind``/…), each carrying
        its original array index.
    """
    out: list[dict] = []
    for index, raw_rule in enumerate(raw_rules):
        raw_rule = raw_rule or {}
        if intersecting is not None and not _rule_intersects(raw_rule, intersecting):
            continue
        out.append(_serialize_cf_rule(services, spreadsheet_id, raw_rule, index))
    return out


def _rule_intersects(raw_rule: dict, target: dict) -> bool:
    """True iff ANY of a Google CF rule's (GridRange) ranges overlaps ``target`` (SPEC §3.3).

    The rule's ranges are still Google ``GridRange`` dicts at this point (serialization to A1
    happens inside ``_serialize_cf_rule``), so the overlap test runs directly on them via
    ``addressing.gridranges_intersect`` — no A1 round-trip needed for the filter.
    """
    from .addressing import gridranges_intersect

    for gr in raw_rule.get("ranges") or []:
        if isinstance(gr, dict) and gridranges_intersect(gr, target):
            return True
    return False


# ---------------------------------------------------------------------------
# describe — unified one-call region read (SPEC §3)
# ---------------------------------------------------------------------------

# The tight UNION mask: the per-cell facets ``inspect`` requests (full per-cell, since describe is
# the "understand a region" verb) + the structural facets ``structure``/``read_conditional_formats``
# request — merges, conditionalFormats (whole rule bodies), tables, bandedRanges, protectedRanges.
# ONE get carries all of them. ``includeGridData`` is a get PARAMETER (set True separately), never a
# field in this mask. The per-cell ``values(...)`` subfields mirror ``_inspect_fields`` at its full
# (every-flag-on) form.
_DESCRIBE_CELL_FIELDS = (
    "effectiveValue,formattedValue,userEnteredValue,userEnteredFormat,"
    "effectiveFormat,dataValidation,note"
)
_DESCRIBE_FIELDS = (
    "sheets(properties(sheetId,title),"
    f"data(rowData(values({_DESCRIBE_CELL_FIELDS})),startRow,startColumn),"
    "merges,conditionalFormats,"
    "tables,bandedRanges,"
    "protectedRanges(protectedRangeId,range,description,editors,warningOnly))"
)


def describe(
    services: SheetsServices,
    spreadsheet_id: str,
    ranges: list[str] | None = None,
    *,
    max_cells: int | None = None,
    data_filters: list[dict] | None = None,
) -> dict:
    """One-call merged region view: cells + structure + CF for one or more ranges (SPEC §3).

    Characterizing one region the old way cost 3-4 reads (``inspect`` + ``structure`` +
    ``read_conditional_formats`` [+ ``read_values``]). ``describe`` issues ONE
    ``spreadsheets.get(ranges=[...], includeGridData=True, fields=<tight union mask>)`` — multi-range
    AND multi-sheet in a single request — then, per requested range, returns a merged view built by
    reusing the EXISTING serializers (``_serialize_cells``, ``_serialize_cf_rules`` + the addressing
    intersect filter, ``structure._serialize_{tables,banding,protected}``) on slices of that one
    response. There is NO cache: the quota win is the single multi-range get, not stored state (a
    stale read after a write is worse for an agent than a human).

    Per requested range the result carries ``{range, sheet, cells, merges, conditionalFormats,
    tables, bandedRanges, protectedRanges, validationSummary}``. ``conditionalFormats`` is filtered
    to ONLY the rules whose ranges intersect that requested range (range-scoped CF for free),
    each keeping its original priority ``index``. The structural facets (merges/tables/banding/
    protected) are sheet-scoped — every region on a sheet sees that sheet's full structural set.

    ``data_filters`` (SPEC §6 P2 — metadata-addressed reads): an OPTIONAL alternative to ``ranges``
    for SYMBOLIC, insert-proof addressing. When supplied, core issues ONE
    ``spreadsheets.getByDataFilter(dataFilters=[...], includeGridData=True)`` and derives each
    region's effective ``GridRange`` from the RETURNED grid block (start offset + dimensions) — so a
    ``developerMetadataLookup`` selector, whose range is unknown until the response, still gets a
    correctly range-scoped CF filter. Each selector is one of ``{"a1": ...}`` / ``{"gridRange": ...}``
    / ``{"developerMetadataLookup": ...}``. Pass EITHER ``ranges`` OR ``data_filters`` (not both).

    ``max_cells`` (like ``read_values``): when the total cells across all regions exceed it, raise
    ``result_too_large`` rather than return a payload that only fails at the caller's token cap;
    ``None`` (default) is unlimited.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        ranges: One or more A1 ranges (multi-sheet allowed). ``None`` ⇒ use ``data_filters``.
        max_cells: Raise ``result_too_large`` past this many cells. Default unlimited.
        data_filters: SYMBOLIC selectors used INSTEAD of ``ranges`` (read via getByDataFilter).
            Mutually exclusive with ``ranges``.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "regions": [{range, sheet, cells, merges,
        conditionalFormats, tables, bandedRanges, protectedRanges, validationSummary}, ...]}``.
    """
    if ranges and data_filters:
        raise SheetsError(
            "conflicting_args", "pass EITHER `ranges` OR `data_filters`, not both"
        )
    if not ranges and not data_filters:
        raise SheetsError(
            "empty_ranges",
            "describe requires at least one range (or a data_filters selector)",
        )
    if max_cells is not None and max_cells < 1:
        raise SheetsError(
            "bad_max_cells", f"max_cells must be a positive integer, got {max_cells!r}"
        )

    # Resolve each requested range to a GridRange up front (validates A1 BEFORE any data get, and
    # gives the (sheetId, start row/col) key that maps response blocks back to ranges). The
    # data-filter path has no A1 ranges to pre-validate (the selectors resolve server-side).
    resolved = (
        [a1_to_gridrange(services, spreadsheet_id, r) for r in ranges]
        if not data_filters
        else []
    )

    if data_filters:
        resp = _describe_get_by_data_filter(services, spreadsheet_id, data_filters)
    else:
        resp = _describe_get_by_ranges(services, spreadsheet_id, ranges)

    # Index the response sheets by sheetId; within each sheet, walk its data blocks in order so a
    # repeated (sheetId, startRow, startColumn) key consumes successive blocks (two requests for the
    # same anchor each get their own block).
    sheets_by_id: dict[object, dict] = {}
    block_cursor: dict[object, int] = {}
    for sheet_obj in resp.get("sheets") or []:
        sheet_obj = sheet_obj or {}
        sid = ((sheet_obj.get("properties") or {}).get("sheetId"))
        sheets_by_id[sid] = sheet_obj
        block_cursor[sid] = 0

    regions: list[dict] = []
    total_cells = 0

    if data_filters:
        # With data filters the literal A1 / sheetId per region is not known up front (a
        # developerMetadataLookup resolves only server-side), so walk the response sheets in order,
        # deriving each region's effective GridRange (and A1 label) from the returned block itself.
        for sheet_obj in resp.get("sheets") or []:
            sheet_obj = sheet_obj or {}
            sid = ((sheet_obj.get("properties") or {}).get("sheetId"))
            for block in sheet_obj.get("data") or []:
                block = block or {}
                gr = _gridrange_from_block(sid, block)
                a1 = gridrange_to_a1(services, spreadsheet_id, gr)
                region = _build_region(
                    services, spreadsheet_id, a1, gr, sheet_obj, block
                )
                total_cells += _region_cell_count(region)
                regions.append(region)
    else:
        for a1, gr in zip(ranges, resolved):
            sid = gr.get("sheetId")
            sheet_obj = sheets_by_id.get(sid) or {}
            block = _match_block(sheet_obj, gr, block_cursor, sid)
            region = _build_region(services, spreadsheet_id, a1, gr, sheet_obj, block)
            total_cells += _region_cell_count(region)
            regions.append(region)

    if max_cells is not None and total_cells > max_cells:
        raise SheetsError(
            "result_too_large",
            f"describe spans {total_cells} cells, exceeding max_cells={max_cells}",
            hint=(
                "narrow the ranges, or use 'inspect'/'read_values' (render='formula') on a tighter "
                "region — describe pulls full per-cell grid data for every range"
            ),
        )

    return {"ok": True, "spreadsheetId": spreadsheet_id, "regions": regions}


def _describe_get_by_ranges(
    services: SheetsServices, spreadsheet_id: str, ranges: list[str]
) -> dict:
    """Issue the one ``spreadsheets.get(ranges=[...], includeGridData=True)`` for ``describe``."""
    try:
        return (
            services.sheets.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                ranges=list(ranges),
                includeGridData=True,
                fields=_DESCRIBE_FIELDS,
            )
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc


def _describe_get_by_data_filter(
    services: SheetsServices, spreadsheet_id: str, data_filters: list[dict]
) -> dict:
    """Issue ONE ``spreadsheets.getByDataFilter`` for ``describe`` (SPEC §6 P2 symbolic addressing).

    Translates the public selectors via the shared :mod:`gsheets.core.dataselector` (so the A1 /
    gridRange / developerMetadataLookup mapping is the SAME one ``read_values`` / ``read_many`` use)
    and sends the same tight union ``fields`` mask + ``includeGridData=True`` as the ranges path,
    so the downstream block serialization is identical for both addressing modes.
    """
    from .dataselector import build_data_filters

    google_filters = build_data_filters(services, spreadsheet_id, data_filters)
    body = {"dataFilters": google_filters, "includeGridData": True}
    try:
        return (
            services.sheets.spreadsheets()
            .getByDataFilter(
                spreadsheetId=spreadsheet_id,
                body=body,
                fields=_DESCRIBE_FIELDS,
            )
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc


def _gridrange_from_block(sid: object, block: dict) -> dict:
    """Derive a region's effective ``GridRange`` from a returned grid block (SPEC §6 P2).

    With ``data_filters`` the literal requested range is unknown up front (a developerMetadataLookup
    resolves only server-side), so the region's bounds come from the response block itself: its
    ``startRow``/``startColumn`` offset plus the actual ``rowData`` row count and max row width. This
    GridRange anchors the region's A1 label AND scopes its conditional-format intersect filter.
    """
    start_row = block.get("startRow", 0) or 0
    start_col = block.get("startColumn", 0) or 0
    row_data = block.get("rowData") or []
    n_rows = len(row_data)
    n_cols = max(
        (len((r or {}).get("values") or []) for r in row_data), default=0
    )
    gr: dict = {"sheetId": sid}
    if n_rows:
        gr["startRowIndex"] = start_row
        gr["endRowIndex"] = start_row + n_rows
    if n_cols:
        gr["startColumnIndex"] = start_col
        gr["endColumnIndex"] = start_col + n_cols
    return gr


def _match_block(
    sheet_obj: dict, gr: dict, block_cursor: dict, sid: object
) -> dict:
    """Return the response grid block for a requested range, mapping by (start row/col) offset.

    With ``includeGridData=True`` and multiple ranges, each sheet's ``data[]`` holds one block per
    requested range on that sheet (in request order). A block carries ``startRow``/``startColumn``;
    we match a requested range's resolved ``GridRange`` start to a not-yet-consumed block with the
    same offset (a 0-based start; an unbounded/whole-sheet range starts at 0,0). Falls back to the
    next block in order if no offset match is found (defensive — Google preserves request order).
    """
    blocks = sheet_obj.get("data") or []
    want_row = gr.get("startRowIndex", 0) or 0
    want_col = gr.get("startColumnIndex", 0) or 0
    # First, an exact (startRow, startColumn) match among un-consumed blocks (preserves order via
    # the cursor for repeated identical anchors).
    start = block_cursor.get(sid, 0)
    for i in range(start, len(blocks)):
        b = blocks[i] or {}
        if (b.get("startRow", 0) or 0) == want_row and (
            (b.get("startColumn", 0) or 0) == want_col
        ):
            block_cursor[sid] = i + 1
            return b
    # Fallback: consume the next block in order.
    if start < len(blocks):
        block_cursor[sid] = start + 1
        return blocks[start] or {}
    return {}


def _build_region(
    services: SheetsServices,
    spreadsheet_id: str,
    a1: str,
    gr: dict,
    sheet_obj: dict,
    block: dict,
) -> dict:
    """Build one region's merged view from the single ``describe`` response (SPEC §3.2).

    Reuses the SAME serializers the standalone reads use — ``_serialize_cells`` (inspect's cell
    flatten, full per-cell mask), ``_serialize_cf_rules(..., intersecting=gr)`` (the CF line grammar,
    filtered to rules touching this range), and ``structure._serialize_{tables,banding,protected}``
    — on slices of the one fetched sheet. ``merges`` are the sheet's merges resolved to A1; the
    structural facets are sheet-scoped (every region on a sheet sees that sheet's set).
    """
    # Reach the ``structure`` MODULE explicitly: ``core/__init__`` re-exports a ``structure``
    # FUNCTION, so a bare ``from . import structure`` would resolve to the function, not the module
    # (the same shadow ``paths``/``export`` work around for ``format``). ``import_module`` returns
    # the real module object carrying ``_serialize_{tables,banding,protected}``.
    import importlib

    _structure = importlib.import_module("gsheets.core.structure")

    sheet_title = (sheet_obj.get("properties") or {}).get("title")

    cell_view = _serialize_cells(
        block,
        compact=False,
        include_effective_format=True,
        include_user_entered_format=True,
        include_formulas=True,
        include_validation=True,
        services=services,
        spreadsheet_id=spreadsheet_id,
    )
    cells = cell_view["cells"]

    merges = [
        gridrange_to_a1(services, spreadsheet_id, m)
        for m in (sheet_obj.get("merges") or [])
    ]

    conditional_formats = _serialize_cf_rules(
        services,
        spreadsheet_id,
        sheet_obj.get("conditionalFormats") or [],
        intersecting=gr,
    )

    return {
        "range": a1,
        "sheet": sheet_title,
        "cells": cells,
        "merges": merges,
        "conditionalFormats": conditional_formats,
        "tables": _structure._serialize_tables(services, spreadsheet_id, sheet_obj),
        "bandedRanges": _structure._serialize_banding(services, spreadsheet_id, sheet_obj),
        "protectedRanges": _structure._serialize_protected(
            services, spreadsheet_id, sheet_obj
        ),
        "validationSummary": _validation_summary(cells),
    }


def _validation_summary(cells: list[dict]) -> dict:
    """Summarize the data-validation across a region's cells (SPEC §3.2 ``validationSummary``).

    Token-cheap rollup rather than per-cell repetition: how many cells carry a validation rule, and
    the DISTINCT terse one-liners present (in first-seen order). The per-cell ``validation`` /
    ``validationRule`` keys are still on the ``cells`` themselves for full fidelity.
    """
    count = 0
    distinct: list[str] = []
    for cell in cells:
        line = cell.get("validation")
        if line:
            count += 1
            if line not in distinct:
                distinct.append(line)
    return {"cells": count, "rules": distinct}


def _region_cell_count(region: dict) -> int:
    """Count a region's serialized cells (for the ``max_cells`` guard)."""
    return len(region.get("cells") or [])


def _serialize_cf_rule(
    services: SheetsServices,
    spreadsheet_id: str,
    raw_rule: dict,
    index: int,
) -> dict:
    """Serialize one Google ``ConditionalFormatRule`` to the read-side rule dict (DESIGN §3.3).

    Resolves each Google ``GridRange`` to an A1 string FIRST (condformat is serviceless and
    takes A1 ranges, DESIGN §4 boundary), then derives BOTH the body-only ``line`` and the
    structured ``ranges``/``kind``/``condition``-or-``stops``/``format`` fields DIRECTLY from the
    same A1-resolved rule. The structured fields are NOT re-parsed from the line: re-parsing
    comma-splits a single ``CUSTOM_FORMULA`` value whose formula contains commas, so
    ``serialize_rule_structured`` reads the condition's ``values[]`` straight off the rule
    instead (ISSUES.md #2). ``line`` and the structured fields still share one source — the rule.
    """
    a1_ranges = [
        gridrange_to_a1(services, spreadsheet_id, gr)
        for gr in (raw_rule.get("ranges") or [])
    ]

    # Build a copy with A1-string ranges for the serviceless serializer.
    rule_a1 = dict(raw_rule)
    rule_a1["ranges"] = a1_ranges

    line = condformat.serialize_rule(rule_a1)
    structured = condformat.serialize_rule_structured(rule_a1)

    out: dict = {
        "index": index,
        "line": line,
        "ranges": structured["ranges"],
        "kind": structured["kind"],
    }
    if structured["kind"] == "gradient":
        out["stops"] = structured.get("stops", [])
    else:
        out["condition"] = structured.get("condition", {})
    out["format"] = structured.get("format", {})
    return out
