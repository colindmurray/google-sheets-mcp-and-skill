"""``formula_patterns`` — collapse a column's repeated formulas to distinct templates (SPEC §4).

An 86-col tracker week is ~4,400 cells; a full formula dump blows the token cap, yet the
information is highly redundant — one formula repeated down 50 rows. ``formula_patterns`` fetches
ONLY formulas (``valueRenderOption=FORMULA``), column-major (``majorDimension=COLUMNS``), then per
column dedupes to the distinct formula TEMPLATES, the row span(s) each covers, and (optionally) one
sample computed value.

The normalization rewrites each relative ROW reference to ``{r}`` (the cell's own row) or
``{r±k}`` (an off-by-k relative row), so ``=SUM(J3:R3)`` at K3, ``=SUM(J4:R4)`` at K4, … all reduce
to one ``=SUM(J{r}:R{r})`` template spanning ``3:N``. Normalization is **best-effort and lossy**:
absolute (``$``-prefixed) rows are kept verbatim (they do not shift down a column); a column whose
formulas do not reduce cleanly — the template fails to round-trip back to EVERY original formula —
is emitted VERBATIM with ``reduced=false``. ``read_values(render="formula")`` stays the lossless
ground truth.

PURE core module: imports only stdlib + sibling core modules + ``googleapiclient`` (for the
``HttpError`` it classifies). It must NEVER import ``fastmcp``, ``mcp``, ``argparse``, ``pydantic``,
or ``gsheets.models`` (DESIGN §1 boundary).
"""

from __future__ import annotations

import re

from googleapiclient.errors import HttpError

from .addressing import a1_to_gridrange, parse_a1
from .errors import SheetsError, classify_google_error
from .service import SheetsServices

# A single A1 cell reference inside a formula. Captures an optional sheet prefix (bare or quoted),
# an optional ``$`` before the column, the column letters, an optional ``$`` before the row, and
# the row digits. The row group is what normalization rewrites (only when it is RELATIVE — no
# leading ``$``). A bare column ref (``A:A``) carries no row digits and is left untouched.
_REF_RE = re.compile(
    r"""
    (?P<sheet>                       # optional sheet prefix
        (?:'(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_]*)\!
    )?
    (?P<coldollar>\$)?               # optional absolute-column marker
    (?P<col>[A-Za-z]{1,3})           # column letters
    (?P<rowdollar>\$)?               # optional absolute-row marker
    (?P<row>\d+)                     # row digits (cell ref always has a row here)
    """,
    re.VERBOSE,
)

def formula_patterns(
    services: SheetsServices,
    spreadsheet_id: str,
    ranges: list[str],
    *,
    sample: bool = True,
) -> dict:
    """Per-column distinct formula templates with ``{r}``-normalized relative rows (SPEC §4.2).

    Issues ONE ``values.batchGet(valueRenderOption=FORMULA, majorDimension=COLUMNS)`` (formulas
    only, zero computed bloat) and — when ``sample`` is set — a second FORMATTED_VALUE pass for one
    sample computed value per template. Per column, dedupes consecutive cells sharing a normalized
    template into a row span; a column that does not reduce cleanly is emitted VERBATIM with
    ``reduced=false`` (``read_values(render="formula")`` is the lossless fallback).

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        ranges: One or more A1 ranges (multi-sheet allowed).
        sample: Attach one sample computed value (``{a1, value}``) to each template. Default True.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "columns": [{"col": "Cliff!K", "reduced": True,
        "templates": [{"formula": "=SUM(J{r}:R{r})", "rows": "3:52", "cells": 50,
        "sample": {"a1": "K3", "value": "185"}}]}]}`` — columns in left-to-right, request order.

    Column count (ISSUES.md #16): a BOUNDED or whole-COLUMN range (``A1:CH75``, ``A:CH``) returns
    exactly ONE entry per REQUESTED column — the GridRange's ``endColumnIndex`` fixes the width, and
    trailing columns the FORMULA response omitted (because they are blank across the requested rows)
    are padded with the empty ``{col, reduced: True, templates: []}`` shape. Only an inherently
    UNBOUNDED-column range (whole-row ``2:5`` / whole-sheet ``Cliff``, where ``endColumnIndex`` is
    absent) falls back to the data-extent count, since the requested width is unknowable there.
    """
    if not ranges:
        raise SheetsError("empty_ranges", "formula_patterns requires at least one range")

    # Resolve each requested range to a GridRange up front — validates A1 BEFORE any data get and
    # gives the (sheet, start column, start row) anchors used to address each column / cell.
    resolved = [
        (a1, a1_to_gridrange(services, spreadsheet_id, a1)) for a1 in ranges
    ]
    sheets = [parse_a1(a1)["sheet"] for a1 in ranges]

    values_api = services.sheets.spreadsheets().values()
    try:
        formula_resp = (
            values_api.batchGet(
                spreadsheetId=spreadsheet_id,
                ranges=list(ranges),
                valueRenderOption="FORMULA",
                majorDimension="COLUMNS",
            ).execute()
        )
        formatted_resp = None
        if sample:
            formatted_resp = (
                values_api.batchGet(
                    spreadsheetId=spreadsheet_id,
                    ranges=list(ranges),
                    valueRenderOption="FORMATTED_VALUE",
                    majorDimension="COLUMNS",
                ).execute()
            )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc

    formula_ranges = formula_resp.get("valueRanges") or []
    formatted_ranges = (
        (formatted_resp.get("valueRanges") or []) if formatted_resp else []
    )

    columns_out: list[dict] = []
    for idx, (a1, gr) in enumerate(resolved):
        sheet_name = sheets[idx]
        vr = formula_ranges[idx] if idx < len(formula_ranges) else {}
        comp_vr = formatted_ranges[idx] if idx < len(formatted_ranges) else {}
        start_col = gr.get("startColumnIndex", 0) or 0
        end_col = gr.get("endColumnIndex")  # None for an unbounded-column range
        start_row = (gr.get("startRowIndex", 0) or 0) + 1  # 1-based absolute first row

        formula_cols = vr.get("values") or []  # column-major
        formatted_cols = comp_vr.get("values") or []

        # ISSUES.md #16: iterate the FULL requested column span when it is known (bounded /
        # whole-column range → endColumnIndex set), padding any column the API omitted (trailing
        # blanks are absent from the column-major response) with the empty {reduced, templates: []}
        # shape so the result has exactly one entry per requested column — deterministic and equal
        # to the requested A1 width. When endColumnIndex is absent (whole-row / whole-sheet range,
        # genuinely unbounded), fall back to the response's data-extent column count.
        if end_col is not None:
            offsets = range(end_col - start_col)
        else:
            offsets = range(len(formula_cols))

        for c_offset in offsets:
            col_letter = _col_letters(start_col + c_offset)
            column_cells = (
                formula_cols[c_offset] if c_offset < len(formula_cols) else []
            )
            comp_column = (
                formatted_cols[c_offset] if c_offset < len(formatted_cols) else []
            )
            columns_out.append(
                _summarize_column(
                    sheet_name,
                    col_letter,
                    column_cells,
                    comp_column,
                    start_row=start_row,
                    sample=sample,
                )
            )

    return {"ok": True, "spreadsheetId": spreadsheet_id, "columns": columns_out}


def _summarize_column(
    sheet_name: str | None,
    col_letter: str,
    column_cells: list,
    comp_column: list,
    *,
    start_row: int,
    sample: bool,
) -> dict:
    """Summarize one column's formula cells into templates + row spans (SPEC §4.2).

    Walks the column top-to-bottom: skips blank/literal cells, normalizes each formula to a
    template, and groups CONSECUTIVE cells sharing a template into a row span. A column is emitted
    VERBATIM with ``reduced=false`` (the honesty fallback, SPEC §4.3) when EITHER a template fails
    to round-trip back to its source formula, OR the normalization achieves no compression — 2+
    distinct templates that each cover exactly one cell (just the raw formulas re-stated, no
    repeated pattern to collapse). A single template (however many rows) and any reduction with at
    least one multi-cell run are ``reduced=true``.
    """
    col_a1 = f"{sheet_name}!{col_letter}" if sheet_name else col_letter

    # Per-cell (absolute row, raw formula) for the formula cells only (skip blanks/literals).
    entries: list[tuple[int, str]] = []
    for r_offset, raw in enumerate(column_cells):
        if isinstance(raw, str) and raw.startswith("="):
            entries.append((start_row + r_offset, raw))

    if not entries:
        return {"col": col_a1, "reduced": True, "templates": []}

    # Normalize each formula and verify the template round-trips back to the source (the honesty
    # check, SPEC §4.3). A single failure flips the whole column to verbatim.
    normalized: list[tuple[int, str, str]] = []  # (row, template, raw)
    round_trips = True
    for row, raw in entries:
        template = _normalize(raw, row)
        if _denormalize(template, row) != raw:
            round_trips = False
            break
        normalized.append((row, template, raw))

    if round_trips:
        runs = _group_runs(normalized)
        # No compression: 2+ runs that each cover exactly one cell means the normalization found no
        # repeated pattern — fall back to verbatim so the output is honestly "these N raw formulas".
        if not (len(runs) >= 2 and all(count == 1 for *_rest, count in runs)):
            templates = [
                _template_entry(
                    template, lo, hi, count, col_letter,
                    first_raw, comp_column, start_row, sample,
                )
                for template, lo, hi, first_raw, count in runs
            ]
            return {"col": col_a1, "reduced": True, "templates": templates}

    # Verbatim fallback: one template entry per cell, its raw formula and 1-row span.
    templates = [
        _template_entry(raw, row, row, 1, col_letter, raw, comp_column, start_row, sample)
        for row, raw in entries
    ]
    return {"col": col_a1, "reduced": False, "templates": templates}


def _group_runs(
    normalized: list[tuple[int, str, str]]
) -> list[tuple[str, int, int, str, int]]:
    """Group consecutive same-template cells into ``(template, lo, hi, first_raw, count)`` runs.

    A run breaks when the normalized template changes from one cell to the next; ``lo``/``hi`` are
    the inclusive absolute-row span of the run's members and ``count`` is how many cells it covers
    (≤ span size when blanks interleave). ``first_raw`` is the run's first raw formula (the sample
    anchor).
    """
    runs: list[tuple[str, int, int, str, int]] = []
    run_start = normalized[0][0]
    run_template = normalized[0][1]
    run_first_raw = normalized[0][2]
    run_count = 1
    prev_row = normalized[0][0]
    for row, template, raw in normalized[1:]:
        if template != run_template:
            runs.append((run_template, run_start, prev_row, run_first_raw, run_count))
            run_start = row
            run_template = template
            run_first_raw = raw
            run_count = 1
        else:
            run_count += 1
        prev_row = row
    runs.append((run_template, run_start, prev_row, run_first_raw, run_count))
    return runs


def _template_entry(
    template: str,
    lo: int,
    hi: int,
    cells: int,
    col_letter: str,
    sample_raw: str,
    comp_column: list,
    start_row: int,
    sample: bool,
) -> dict:
    """Build one template dict (``{formula, rows, cells, sample?}``) (SPEC §4.2).

    ``rows`` is the inclusive ``"lo:hi"`` span; ``cells`` is how many cells the template actually
    covers (may be < span size when blanks interleave). The ``sample`` (when requested and a
    computed value exists for the run's FIRST row) is ``{"a1": "<col><lo>", "value": <computed>}``.
    """
    entry: dict = {"formula": template, "rows": f"{lo}:{hi}", "cells": cells}
    if sample:
        comp = _computed_at(comp_column, lo, start_row)
        if comp is not None:
            entry["sample"] = {"a1": f"{col_letter}{lo}", "value": comp}
    return entry


def _computed_at(comp_column: list, abs_row: int, start_row: int) -> object | None:
    """Return the FORMATTED computed value at absolute row ``abs_row``, or ``None``.

    The FORMATTED pass is column-major and index-aligned with the FORMULA pass, so the cell at
    absolute row ``abs_row`` sits at offset ``abs_row - start_row``. An out-of-range offset or an
    empty-string cell yields ``None`` (no sample attached).
    """
    offset = abs_row - start_row
    if 0 <= offset < len(comp_column):
        val = comp_column[offset]
        if val != "" and val is not None:
            return val
    return None


def _normalize(formula: str, row: int) -> str:
    """Rewrite RELATIVE row references in ``formula`` to ``{r}`` / ``{r±k}`` for the cell at ``row``.

    Each A1 cell ref whose row is RELATIVE (no leading ``$``) has its row digits replaced by the
    ``{r}`` token offset from this cell's own row: equal → ``{r}``, one more → ``{r+1}``, one less
    → ``{r-1}``. Absolute rows (``$5``) and bare column refs are untouched, since they do not shift
    down a column. The column letters / sheet prefix are preserved verbatim.
    """

    def _repl(m: re.Match) -> str:
        sheet = m.group("sheet") or ""
        coldollar = m.group("coldollar") or ""
        col = m.group("col")
        rowdollar = m.group("rowdollar") or ""
        row_digits = m.group("row")
        if rowdollar:
            # Absolute row — keep verbatim (it does not shift down a column).
            return f"{sheet}{coldollar}{col}{rowdollar}{row_digits}"
        delta = int(row_digits) - row
        return f"{sheet}{coldollar}{col}{{r{_delta_str(delta)}}}"

    # ``re.sub`` does a single left-to-right pass and never re-scans replacement text, so emitting
    # the ``{r±k}`` token directly is safe (a later cell ref can't match inside it).
    return _REF_RE.sub(_repl, formula)


def _delta_str(delta: int) -> str:
    """Render a row delta as the ``{r}`` token's suffix (``0`` → ``""``, ``+1`` / ``-1``)."""
    if delta == 0:
        return ""
    if delta > 0:
        return f"+{delta}"
    return str(delta)  # already carries the leading '-'


def _denormalize(template: str, row: int) -> str:
    """Inverse of :func:`_normalize`: expand ``{r}`` / ``{r±k}`` tokens back to row ``row``.

    Used to VERIFY a reduction round-trips (the honesty check, SPEC §4.3): if expanding a column's
    template back at each covered row does not reproduce the original formula, the column is
    non-reducible and is emitted verbatim instead.
    """
    def _repl(m: re.Match) -> str:
        delta_str = m.group("delta") or ""
        delta = int(delta_str) if delta_str else 0
        return str(row + delta)

    return re.sub(r"\{r(?P<delta>[+-]\d+)?\}", _repl, template)


def _col_letters(col0: int) -> str:
    """Convert a 0-based column index to letters (``0`` -> ``"A"``, ``26`` -> ``"AA"``)."""
    letters: list[str] = []
    n = col0 + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(ord("A") + rem))
    return "".join(reversed(letters))
