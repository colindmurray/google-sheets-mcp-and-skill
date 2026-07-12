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
from .serializers.chips import serialize_chip_runs
from .service import SheetsServices
from .values import pad_jagged

# ---------------------------------------------------------------------------
# overview — cheap orientation snapshot (DESIGN §3.3)
# ---------------------------------------------------------------------------

# The NARROW mask: only the cheapest length-yielding subfields of the protectedRanges /
# conditionalFormats arrays (DESIGN §3.3). MUST NOT widen to whole rule/protected bodies.
_OVERVIEW_FIELDS = (
    "properties(title,locale,timeZone,"
    "spreadsheetTheme(primaryFontFamily,themeColors(colorType,color))),"
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

    # Theme-color resolver (T2.1): flatten each themeColors[] pair to a compact NAME->hex map so an
    # agent can resolve any ``theme:NAME`` it reads elsewhere to the sheet's actual concrete color.
    # Omitted gracefully when the workbook carries no theme.
    theme_map = _build_theme_map(props.get("spreadsheetTheme"))
    if theme_map:
        out["theme"] = theme_map
    theme_font = _theme_font_family(props.get("spreadsheetTheme"))
    if theme_font:
        out["themeFontFamily"] = theme_font

    return out


def _build_theme_map(spreadsheet_theme: object) -> dict | None:
    """Flatten a Google ``SpreadsheetTheme`` to a compact ``{colorType: "#RRGGBB"}`` map (T2.1).

    Each ``ThemeColorPair`` maps a ``colorType`` (``TEXT``/``BACKGROUND``/``ACCENT1..6``/``LINK``)
    to a concrete ``color`` (a ``ColorStyle``); the pair's color is flattened through the existing
    :func:`gsheets.core.colors.color_style_to_hex` so an agent can resolve any ``theme:NAME`` it
    reads anywhere to the workbook's real color. The unspecified sentinel and any pair whose color
    can't be flattened are skipped. Returns ``None`` when there is no usable theme (caller omits the
    ``theme`` key entirely — token efficiency).
    """
    if not isinstance(spreadsheet_theme, dict) or not spreadsheet_theme:
        return None
    from . import colors

    out: dict = {}
    for pair in spreadsheet_theme.get("themeColors") or []:
        if not isinstance(pair, dict):
            continue
        color_type = pair.get("colorType")
        if not color_type or color_type == "THEME_COLOR_TYPE_UNSPECIFIED":
            continue
        color = pair.get("color")
        if not isinstance(color, dict) or not color:
            continue
        try:
            out[color_type] = colors.color_style_to_hex(color)
        except ValueError:
            # A malformed pair (neither theme nor rgb) is skipped rather than failing overview.
            continue
    return out or None


def _theme_font_family(spreadsheet_theme: object) -> str | None:
    """Return the theme's ``primaryFontFamily`` (surfaced as ``themeFontFamily``), or ``None``."""
    if not isinstance(spreadsheet_theme, dict):
        return None
    font = spreadsheet_theme.get("primaryFontFamily")
    if isinstance(font, str) and font:
        return font
    return None


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
    chips: bool = False,
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
    - ``chips`` (T2.2): adds ``chipRuns`` to the per-cell mask; a cell gains ``"chips": [ChipRun,
      …]`` (each smart chip serialized to ``{start, kind, …}``) ONLY when it carries chip runs.
    In compact mode, two cells differing in ``runs``/``hyperlink``/``pivot``/``chips`` never merge
    into one run (``_run_key`` includes a stable repr of each).

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
        chips: Add ``chipRuns`` to the mask; attach ``chips`` (serialized smart chips) to a cell
            only when it carries chip runs (T2.2). Default ``False``.

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
        include_chips=chips,
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
        include_chips=chips,
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
    include_chips: bool = False,
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
        include_chips=include_chips,
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
    include_chips: bool = False,
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
    if include_chips:
        # Tight sub-mask matching exactly what the chip serializer reads (T2.2) — never the whole
        # chipRuns blob.
        cell_fields.append(
            "chipRuns(startIndex,chip(personProperties(email,displayFormat),"
            "richLinkProperties(uri,mimeType)))"
        )

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
    include_chips: bool = False,
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
                    include_chips=include_chips,
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
    include_chips: bool = False,
    services: SheetsServices | None = None,
    spreadsheet_id: str | None = None,
) -> dict | None:
    """Build one structured cell dict (no ``a1`` yet), or ``None`` if the cell is empty.

    A cell is "empty" when it carries no value, no formula, no formatting, no note, no
    validation, no rich-text runs, no hyperlink, no pivot, and no smart chips under the requested
    mask — those collapse to ``None`` so compact runs drop them and non-compact reads pad them as
    blanks. The rich-text/hyperlink/pivot/chips enrichments (§X.1/§X.6/T2.2) are attached ONLY when
    present on the cell.
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

    if include_chips:
        # Smart chips (T2.2) — attach ONLY when the cell actually has chipRuns.
        chips = serialize_chip_runs(cell.get("chipRuns"))
        if chips:
            out["chips"] = chips

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
    AND the rich-text ``runs`` / ``hyperlink`` / ``pivot`` / ``chips`` enrichments are all identical
    (DESIGN §3.3, §X.1, T2.2). ``None`` (empty) cells never merge with a non-empty cell and are
    dropped entirely from runs. The runs/hyperlink/pivot/chips keys are absent on cells read without
    those flags, so the key degenerates to the base identity (no behavior change by default).
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
        _stable_repr(cell.get("chips")),
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
    only when set — ``note``, ``validationRule``, and the §X.1/§X.6/T2.2 enrichments ``runs`` /
    ``hyperlink`` / ``pivot`` / ``chips``.
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
    chips = cell.get("chips")
    if chips:
        run["chips"] = chips
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
    range — via the shared ``addressing.gridranges_intersect`` geometric filter (SPEC §3.3), so
    "which rules touch this region" is one shared codepath. A surviving rule keeps its ORIGINAL
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

    # Resolve the scope, fetch, and serialize under ONE sheet-index cache: a1_to_gridrange (the
    # range), the whole-sheet scope title, and every rule's gridrange_to_a1 all share a single
    # sheet-index get, so a 50+-rule sheet does ONE lookup, not one per rule (ISSUES.md #26). The
    # scope is re-entrant — a free no-op when the adapter already opened one around dispatch.
    with sheet_index_cache():
        # A range scopes the read to its sheet AND filters rules to those intersecting it. Resolve
        # it to a GridRange up front (validates the A1 and yields the sheetId + bounds for the test).
        intersecting: dict | None = None
        scope_sheet_id: object = None
        if range is not None:
            intersecting = a1_to_gridrange(services, spreadsheet_id, range)
            scope_sheet_id = intersecting.get("sheetId")

        # Scope the get to the requested sheet/range. WITHOUT this, a single-sheet CF read makes the
        # API load EVERY tab's conditional-format model — on a large workbook that is a multi-minute
        # call (one real sheet: 54 rules took 5m21s unscoped vs 0.74s scoped — ISSUES.md #26).
        # Reading ALL sheets (sheet and range both None) still needs the unscoped fetch. ``ranges``
        # filters only the per-sheet ``sheets[]``; top-level fields are returned regardless.
        #
        # A ``range`` filter scopes to the range's whole SHEET, NOT the narrow range: a narrow-range
        # get makes Google return a PRE-FILTERED, RE-INDEXED ``conditionalFormats`` array (only the
        # rules intersecting the range, renumbered from 0), so a surviving rule's positional index
        # would be a subset position — not its true priority — and editing by it hits the WRONG rule
        # (ISSUES.md #38, confirmed live). Scoping to the whole sheet returns the FULL array (true
        # global indices); the client-side ``intersecting`` filter below narrows it while preserving
        # each rule's original index.
        if range is not None:
            scope = gridrange_to_a1(services, spreadsheet_id, {"sheetId": scope_sheet_id})
            get_kwargs: dict = {"ranges": [scope]}
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
            # A range-scoped read of a non-existent sheet 400s on the range parse; preserve the
            # friendly sheet_not_found (with the available list) the unscoped path produced.
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
    ``conditionalFormats`` array. Each rule keeps its ORIGINAL positional ``index`` (priority — 0
    highest) regardless of any filtering, so a rule surfaced by a range-scoped read still addresses
    correctly via ``set_conditional_format``.

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
# describe — compact per-column PROFILER (T4.1, audit #15) — BREAKING v0.6
# ---------------------------------------------------------------------------
#
# describe is the CHEAP "shape/profile" verb, the complement of ``inspect`` (the full per-cell read).
# It answers "what does this region look like?" — per-column inferred type, fill density, and formula
# ratio — WITHOUT returning any per-cell array. The mask is deliberately tight: enough to type each
# cell (``effectiveValue`` + ``effectiveFormat.numberFormat.type`` so a serial-number date is not
# mistaken for a plain number), detect a formula (``userEnteredValue.formulaValue``), and label a
# column (``formattedValue`` for a readable ``distinctSample``), plus the sheet's ``merges``. The
# per-cell grid is aggregated to column stats and then DISCARDED — nothing per-cell is returned.
#
# BREAKING (from v0.5 and earlier): describe no longer returns ``regions``/``cells``. Callers that
# need per-cell values/formulas/formats move to ``inspect`` (or ``read_values`` for a plain grid).
# The tight fields mask returns grid data WITHOUT ``includeGridData=True`` (a field mask that names
# grid-data subfields makes Google return them regardless — same as ``inspect``).
_DESCRIBE_CELL_FIELDS = (
    "effectiveValue,formattedValue,"
    "effectiveFormat.numberFormat.type,userEnteredValue.formulaValue"
)
_DESCRIBE_FIELDS = (
    "sheets(properties(sheetId,title),"
    f"data(rowData(values({_DESCRIBE_CELL_FIELDS})),startRow,startColumn),"
    "merges)"
)

# How many distinct display values a column's ``distinctSample`` carries (a bounded profile
# statistic — NOT a per-cell dump).
_DISTINCT_SAMPLE_LIMIT = 5

# effectiveFormat.numberFormat.type values that mean a numeric cell is really a DATE/TIME.
_DATE_NUMBER_FORMATS = frozenset({"DATE", "TIME", "DATE_TIME"})


def describe(
    services: SheetsServices,
    spreadsheet_id: str,
    range: str,
    *,
    max_cells: int | None = None,
    header: bool = True,
) -> dict:
    """Cheap region PROFILE — per-column type/density/formula-ratio, NO per-cell data (T4.1).

    ``describe`` is the "what does this region look like?" verb, the complement of ``inspect`` (the
    full per-cell read). It issues ONE tight-mask ``spreadsheets.get`` — just enough to type each
    cell (``effectiveValue`` + the number-format type, so a serial-number date is not mistaken for a
    plain number), detect a formula (``userEnteredValue.formulaValue``), and sample a readable label
    (``formattedValue``) — then AGGREGATES the grid to per-column statistics and DISCARDS every
    per-cell value. The result is a compact profile, not a grid dump.

    Per column the profile carries ``{col, header?, type, density, formulaRatio, distinctSample?}``:

    * ``col`` — the A1 column letter (absolute, e.g. ``"B"``).
    * ``header`` — the column's header text (only in ``header`` mode, when the first row's cell has a
      non-empty display string).
    * ``type`` — inferred from the column's non-empty DATA cells: ``number`` | ``date`` | ``text`` |
      ``bool`` | ``formula-heavy`` (>= half the non-empty cells are formulas) | ``empty``.
    * ``density`` — non-empty data cells / data-row count (0.0–1.0, 3 decimals).
    * ``formulaRatio`` — formula cells / non-empty data cells (0.0–1.0, 3 decimals).
    * ``distinctSample`` — up to five distinct display values (first-seen order), omitted when empty.

    ``header`` (default ``True``): treat the region's FIRST returned row as a header row — each
    column's header text is taken from it and it is EXCLUDED from the type/density/formula stats.
    Pass ``header=False`` for headerless data (row 0 is profiled like any other).

    ``max_cells``: refuse an over-large region UP FRONT. When the requested range is fully bounded
    (e.g. ``A1:C100``), core estimates ``rows*cols`` from the resolved range and raises
    ``result_too_large`` BEFORE issuing the data get (no wasted fetch). An unbounded range (a whole
    column/sheet, whose extent is unknown until fetched) is still guarded against the actual returned
    cell count after the fetch. ``None`` (default) is unlimited.

    For the full per-cell values/formulas/formats of a region use ``inspect``; for a plain
    rectangular value grid use ``read_values``.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        range: The A1 range to profile (a single range; sheet-qualified or not).
        max_cells: Raise ``result_too_large`` past this many cells (pre-fetch when bounded). Default
            unlimited.
        header: Treat the first returned row as a header row (default ``True``).

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "sheet": ..., "range": ..., "rows": ..., "cols": ...,
        "columns": [{col, header?, type, density, formulaRatio, distinctSample?}, ...], "merges"?}``.
        ``merges`` is present only when the profiled range's sheet has merges.
    """
    # Validate the A1 up front (raises bad_range on garbage) BEFORE any network call.
    parse_a1(range)
    if max_cells is not None and max_cells < 1:
        raise SheetsError(
            "bad_max_cells", f"max_cells must be a positive integer, got {max_cells!r}"
        )

    # One sheet-index cache for the whole op: the range resolution AND every merge's
    # gridrange_to_a1 share a single sheet-index get (mirrors read_conditional_formats / structure).
    with sheet_index_cache():
        gr = a1_to_gridrange(services, spreadsheet_id, range)

        # PRE-FETCH max_cells guard: when the resolved range is fully bounded, estimate rows*cols and
        # refuse up front — never fetch a grid only to trim it (T4.1). An unbounded range (open
        # row/col) can't be sized without the sheet dimensions, so it is guarded post-fetch below.
        estimate = _bounded_cell_estimate(gr)
        if max_cells is not None and estimate is not None and estimate > max_cells:
            raise SheetsError(
                "result_too_large",
                f"range {range!r} spans ~{estimate} cells, exceeding max_cells={max_cells}",
                hint="narrow the range, or drop max_cells to profile it anyway",
            )

        try:
            resp = (
                services.sheets.spreadsheets()
                .get(spreadsheetId=spreadsheet_id, ranges=[range], fields=_DESCRIBE_FIELDS)
                .execute()
            )
        except HttpError as exc:
            raise classify_google_error(
                exc, account_email=services.account_email
            ) from exc

        sheets = resp.get("sheets") or []
        if not sheets:
            raise SheetsError(
                "no_data",
                f"no sheet data returned for range {range!r}",
                hint="check the spreadsheet id and that the range names an existing sheet",
            )
        sheet_obj = sheets[0] or {}
        sheet_title = (sheet_obj.get("properties") or {}).get("title")

        data_blocks = sheet_obj.get("data") or []
        block = data_blocks[0] if data_blocks else {}

        n_rows, n_cols, columns = _profile_block(block or {}, header=header)

        # POST-FETCH max_cells guard for unbounded ranges (the pre-fetch estimate was None).
        if max_cells is not None and estimate is None:
            actual = n_rows * n_cols
            if actual > max_cells:
                raise SheetsError(
                    "result_too_large",
                    f"range {range!r} spans {actual} cells, exceeding max_cells={max_cells}",
                    hint="narrow the range, or drop max_cells to profile it anyway",
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
        "rows": n_rows,
        "cols": n_cols,
        "columns": columns,
    }
    if merges:
        out["merges"] = merges
    return out


def _bounded_cell_estimate(gr: dict) -> int | None:
    """Estimate a resolved ``GridRange``'s rows*cols, or ``None`` if unbounded on either axis.

    A bounded range (``endRowIndex`` and ``endColumnIndex`` both present) yields an exact cell count
    for the pre-fetch ``max_cells`` guard. A range left open in either direction (a whole column, a
    whole row, a whole sheet) can't be sized without the sheet's dimensions, so ``None`` signals the
    caller to fall back to the post-fetch (actual-cell) guard.
    """
    end_row = gr.get("endRowIndex")
    end_col = gr.get("endColumnIndex")
    if end_row is None or end_col is None:
        return None
    rows = end_row - (gr.get("startRowIndex") or 0)
    cols = end_col - (gr.get("startColumnIndex") or 0)
    if rows <= 0 or cols <= 0:
        return 0
    return rows * cols


def _profile_block(block: dict, *, header: bool) -> tuple[int, int, list[dict]]:
    """Aggregate a PRE-FETCHED grid block into ``(rows, cols, columns)`` per-column stats (T4.1).

    Builds the rectangular grid of raw Google cell dicts (padding jagged rows), optionally peels the
    first row off as a header row, then for each column computes the inferred type, fill density,
    formula ratio, and a bounded distinct-value sample from the DATA rows — discarding every per-cell
    value. ``rows``/``cols`` describe the returned data extent (trailing-empty-trimmed, like a values
    read); ``columns`` has one entry per column in that extent.
    """
    start_col = block.get("startColumn", 0) or 0
    row_data = block.get("rowData") or []

    grid: list[list[dict]] = []
    for r_row in row_data:
        grid.append([c or {} for c in ((r_row or {}).get("values") or [])])
    n_rows = len(grid)
    n_cols = max((len(r) for r in grid), default=0)

    header_row: list[dict] = []
    if header and n_rows >= 1:
        header_row = grid[0]
        data_rows = grid[1:]
    else:
        data_rows = grid
    data_row_count = len(data_rows)

    columns: list[dict] = []
    for c in range(n_cols):
        col_letter = _col_letters(start_col + c)
        col_out: dict = {"col": col_letter}

        if header and c < len(header_row):
            header_text = _cell_display(header_row[c])
            if header_text:
                col_out["header"] = header_text

        non_empty = 0
        formula_cnt = 0
        type_counts: dict[str, int] = {"number": 0, "date": 0, "text": 0, "bool": 0}
        sample: list[str] = []
        for row in data_rows:
            cell = row[c] if c < len(row) else {}
            if _cell_is_empty(cell):
                continue
            non_empty += 1
            if _cell_has_formula(cell):
                formula_cnt += 1
            type_counts[_classify_cell_type(cell)] += 1
            disp = _cell_display(cell)
            if disp and disp not in sample and len(sample) < _DISTINCT_SAMPLE_LIMIT:
                sample.append(disp)

        col_out["type"] = _infer_column_type(non_empty, formula_cnt, type_counts)
        col_out["density"] = (
            round(non_empty / data_row_count, 3) if data_row_count else 0.0
        )
        col_out["formulaRatio"] = (
            round(formula_cnt / non_empty, 3) if non_empty else 0.0
        )
        if sample:
            col_out["distinctSample"] = sample
        columns.append(col_out)

    return n_rows, n_cols, columns


def _cell_is_empty(cell: dict) -> bool:
    """True when a raw Google cell carries no value (no effective/formatted value, no formula)."""
    if not cell:
        return True
    if cell.get("effectiveValue"):
        return False
    fv = cell.get("formattedValue")
    if isinstance(fv, str) and fv != "":
        return False
    return not _cell_has_formula(cell)


def _cell_has_formula(cell: dict) -> bool:
    """True when the cell's ``userEnteredValue`` is a formula string (begins with ``=``)."""
    uev = cell.get("userEnteredValue")
    if isinstance(uev, dict):
        formula = uev.get("formulaValue")
        return isinstance(formula, str) and formula.startswith("=")
    return False


def _classify_cell_type(cell: dict) -> str:
    """Classify a non-empty cell's VALUE type: ``bool`` | ``date`` | ``number`` | ``text``.

    Reads the typed ``effectiveValue`` (a boolean is a bool, a number is a number UNLESS the cell's
    ``effectiveFormat.numberFormat.type`` marks it a DATE/TIME/DATE_TIME serial, a string is text) and
    treats an error value as text. Falls back to ``text`` when only a formatted string is present.
    """
    ev = cell.get("effectiveValue")
    if isinstance(ev, dict):
        if "boolValue" in ev:
            return "bool"
        if "numberValue" in ev:
            nf_type = (
                ((cell.get("effectiveFormat") or {}).get("numberFormat") or {}).get(
                    "type"
                )
            )
            return "date" if nf_type in _DATE_NUMBER_FORMATS else "number"
        if "stringValue" in ev:
            return "text"
        if "errorValue" in ev:
            return "text"
    return "text"


def _infer_column_type(
    non_empty: int, formula_cnt: int, type_counts: dict[str, int]
) -> str:
    """Reduce a column's per-cell tallies to one label (``empty``/``formula-heavy``/value type).

    An all-empty column is ``empty``; a column where at least half the non-empty cells are formulas
    is ``formula-heavy`` (the formula-ness dominates the value type); otherwise the majority value
    type wins, ties broken by the fixed order ``number > date > bool > text`` (so a truly mixed
    column falls to the most specific present, ending at ``text``).
    """
    if non_empty == 0:
        return "empty"
    if formula_cnt * 2 >= non_empty:
        return "formula-heavy"
    best_type = "text"
    best_count = -1
    for candidate in ("number", "date", "bool", "text"):
        if type_counts.get(candidate, 0) > best_count:
            best_count = type_counts[candidate]
            best_type = candidate
    return best_type


def _cell_display(cell: dict) -> str:
    """A cell's readable display string for headers / distinct samples (prefers ``formattedValue``).

    Uses ``formattedValue`` (what the viewer sees) when present; else the typed ``effectiveValue``
    scalar stringified; else ``""`` (an empty/absent cell).
    """
    fv = cell.get("formattedValue")
    if isinstance(fv, str) and fv != "":
        return fv
    ev = cell.get("effectiveValue")
    if isinstance(ev, dict):
        for key in ("stringValue", "numberValue", "boolValue"):
            if key in ev:
                return str(ev[key])
        error = ev.get("errorValue")
        if isinstance(error, dict):
            return str(error.get("type") or error.get("message") or "")
    return ""


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
