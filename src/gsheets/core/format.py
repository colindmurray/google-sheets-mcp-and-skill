"""Shared output-format layer (SPEC §1) — serialize a core result dict to a string.

PURE core module: imports ONLY stdlib (``csv``, ``io``, ``json``). It must NEVER import
``fastmcp``, ``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models`` (SPEC §0.2, DESIGN §1
boundary). Both adapters drive the SAME ``render`` so MCP file output and CLI piped output are
byte-identical.

:func:`render` serializes a core result dict to a string in one of the data formats:

* ``json``  — ``json.dumps(result, ensure_ascii=False, indent=2)``.
* ``jsonl`` — one JSON record per line. For a ``read_values`` result that is one
  ``{"range": <A1>, "row": [...]}`` per grid row; for a list-shaped result (e.g. ``read_many``,
  ``comments``) it is one top-level list element per line.
* ``csv`` / ``tsv`` — the stdlib ``csv`` module over the result's rectangular value grid(s).
  A single-range read is plain RFC-4180 CSV; a multi-range read emits each range as a block
  preceded by a ``# range: <A1>`` comment line. A non-tabular (structured) result raises
  ``SheetsError("format_unsupported")``.

``text`` is NOT handled here — it is the adapters' existing terse renderer (SPEC §1.5).

``export`` (``core/export.py``) delegates its single-sheet csv/tsv serialization to
:func:`render_grid` here, so there is ONE csv path, not two (its on-disk bytes are unchanged).
"""

from __future__ import annotations

import csv
import io
import json

from .errors import SheetsError

#: The data formats this module serializes. ``text`` lives in the adapters (SPEC §1.5);
#: ``markdown`` is gated to a later phase (SPEC §6) and is intentionally absent here.
SUPPORTED: tuple[str, ...] = ("json", "jsonl", "csv", "tsv")

#: Normalized format -> the csv-module delimiter for the tabular renderers.
_DELIMITER: dict[str, str] = {"csv": ",", "tsv": "\t"}


def render(result: dict, fmt: str) -> str:
    """Serialize a core result dict to a string in ``fmt`` (SPEC §1.2).

    Args:
        result: A plain JSON-serializable core result dict (``"ok": True``).
        fmt: One of ``"json"`` | ``"jsonl"`` | ``"csv"`` | ``"tsv"``. ``"text"`` is the
            adapters' job and is rejected here.

    Returns:
        The serialized string. csv/tsv use RFC-4180 ``\\r\\n`` line terminators (matching
        ``export``); json/jsonl use ``\\n``.

    Raises:
        SheetsError: ``"format_unsupported"`` when an unknown format is requested, or when a
            tabular format (csv/tsv) is asked for a structured (non-tabular) result.
    """
    if fmt == "json":
        return _render_json(result)
    if fmt == "jsonl":
        return _render_jsonl(result)
    if fmt in _DELIMITER:
        return _render_tabular(result, fmt)
    raise SheetsError(
        "format_unsupported",
        f"unknown output format {fmt!r}",
        hint="use one of: text, json, jsonl, csv, tsv",
    )


# --------------------------------------------------------------------------- json / jsonl


def _render_json(result: dict) -> str:
    """``json.dumps`` with ``ensure_ascii=False`` (token-efficient) and ``indent=2``."""
    return json.dumps(result, ensure_ascii=False, indent=2)


def _render_jsonl(result: dict) -> str:
    """One JSON record per line (SPEC §1.2).

    A ``read_values`` result (rectangular ``ranges[].values``) emits one
    ``{"range": <A1>, "row": [...]}`` per grid row — an embedded newline inside a value stays
    inside one physical line because ``json`` escapes it. Any other list-shaped result emits one
    top-level list element per line. A result with no obvious record list falls back to a single
    JSON object line (so the format never errors on a small confirmation).
    """
    records = _jsonl_records(result)
    return "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in records)


def _jsonl_records(result: dict) -> list:
    """Extract the per-line records for jsonl (SPEC §1.2)."""
    if _is_tabular(result):
        records: list = []
        for entry in result["ranges"]:
            a1 = entry.get("range")
            for row in entry.get("values", []) or []:
                records.append({"range": a1, "row": row})
        return records
    # List-shaped result: the single top-level list value (excluding scalars / envelope keys).
    list_value = _primary_list(result)
    if list_value is not None:
        return list(list_value)
    # No record list — emit the whole dict as one line.
    return [result]


def _primary_list(result: dict) -> list | None:
    """Return the result's single record-list value, or ``None`` if it isn't list-shaped.

    Scans the top-level values (skipping the ``ok``/``spreadsheetId`` envelope keys) for exactly
    one ``list`` value — e.g. ``comments`` on a comments read or ``results`` on a read_many
    envelope. If there is not exactly one list, the result isn't list-shaped for jsonl.
    """
    lists = [
        (key, val)
        for key, val in result.items()
        if key not in ("ok", "spreadsheetId") and isinstance(val, list)
    ]
    if len(lists) == 1:
        return lists[0][1]
    return None


# --------------------------------------------------------------------------- csv / tsv


def _render_tabular(result: dict, fmt: str) -> str:
    """Serialize the rectangular value grid(s) of a ``read_values`` result (SPEC §1.2).

    Single range -> clean RFC-4180 CSV (no header). Multiple ranges -> each range as a block
    preceded by a ``# range: <A1>`` comment line so the common single-range pipe stays clean
    while a multi-range read is still parseable. A structured (non-tabular) result raises
    ``format_unsupported`` — the agent learns the right tool.
    """
    if not _is_tabular(result):
        raise SheetsError(
            "format_unsupported",
            f"a {fmt} render needs a rectangular value read, but this result is structured",
            hint="use json or text; csv/tsv need a rectangular value read (e.g. read_values)",
        )
    delimiter = _DELIMITER[fmt]
    ranges = result.get("ranges") or []
    if len(ranges) == 1:
        return render_grid(ranges[0].get("values", []) or [], delimiter)

    blocks: list[str] = []
    for entry in ranges:
        a1 = entry.get("range")
        body = render_grid(entry.get("values", []) or [], delimiter)
        blocks.append(f"# range: {a1}\r\n{body}")
    return "".join(blocks)


def render_grid(rows: list[list], delimiter: str) -> str:
    """Serialize one rectangular grid of rows to a csv/tsv string (SPEC §1.2 shared path).

    Uses the stdlib ``csv`` module with RFC-4180 ``\\r\\n`` line terminators — the exact path
    ``export`` used inline, extracted here so there is ONE csv serializer. ``export`` calls this
    and encodes the result utf-8, so its on-disk bytes are byte-identical.

    Args:
        rows: A list of rows (each a list of cell values).
        delimiter: ``","`` (csv) or ``"\\t"`` (tsv).

    Returns:
        The serialized string (empty string for an empty grid).
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\r\n")
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


# --------------------------------------------------------------------------- address-keyed (§4.4)


def render_addressed(cells: list[dict]) -> str:
    """Render SPARSE cells as address-keyed lines — one ``"<A1>: <body>"`` per cell (SPEC §4.4).

    The natural shape for a sparse formula/format/note read (an inverted index), versus the dense
    rectangle+range. Each non-empty cell becomes one line: the formula (when set, e.g.
    ``"C5: =SUM(A5:B5)"``) else its value, with the terse validation one-liner ``[<rule>]`` and a
    ``note=<repr>`` fragment appended when present. A padded blank cell (a bare ``{"a1": ...}`` with
    no value/formula/note/validation) contributes NO line — that is what makes the rendering sparse.

    Args:
        cells: A list of per-cell dicts (each carrying ``a1`` plus optional
            ``value``/``formula``/``note``/``validation``).

    Returns:
        The newline-joined address-keyed lines (empty string when no cell carries content).
    """
    lines: list[str] = []
    for cell in cells:
        line = _addressed_line(cell)
        if line is not None:
            lines.append(line)
    return "\n".join(lines)


def addressed_records(cells: list[dict]) -> list[dict]:
    """The jsonl-friendly record form of :func:`render_addressed` — one dict per NON-empty cell.

    Drops the padded blank cells (a bare ``{"a1": ...}``) so a sparse read streams as only the
    cells that carry content, each keyed by its ``a1`` address (SPEC §4.4).
    """
    return [cell for cell in cells if _cell_has_content(cell)]


def _addressed_line(cell: dict) -> str | None:
    """Build one ``"<A1>: <body>"`` line for a cell, or ``None`` if the cell is empty."""
    if not _cell_has_content(cell):
        return None
    a1 = cell.get("a1", "?")
    formula = cell.get("formula")
    value = cell.get("value")
    if formula:
        body = str(formula)
    elif value is not None and value != "":
        body = str(value)
    else:
        body = ""
    parts = [f"{a1}: {body}".rstrip()]
    validation = cell.get("validation")
    if validation:
        parts.append(f"[{validation}]")
    note = cell.get("note")
    if note:
        parts.append(f"note={note!r}")
    return "  ".join(parts)


def render_sparse_values(result: dict) -> str:
    """Render a SPARSE ``read_values`` result as address-keyed lines (SPEC §4.4).

    A formula read (or any read the caller treats as sparse) reads best as ``"<A1>: <formula>"``
    lines, not a dense rectangle: each range's rectangular ``values`` grid is expanded to absolute
    A1 cells (anchored at the range's top-left), and only non-empty cells emit a line. Multiple
    ranges are rendered back-to-back (each cell already carries its sheet-qualified A1, so no
    separator is needed). This is the inverted-index shape for sparse data; dense numeric grids keep
    the rectangle+range form (csv/json) instead.
    """
    lines: list[str] = []
    for entry in result.get("ranges", []) or []:
        cells = cells_from_value_grid(entry.get("range"), entry.get("values", []) or [])
        rendered = render_addressed(cells)
        if rendered:
            lines.append(rendered)
    return "\n".join(lines)


def cells_from_value_grid(range_a1: str | None, values: list[list]) -> list[dict]:
    """Expand a rectangular value grid into absolute-A1 cell dicts anchored at ``range_a1`` (§4.4).

    Computes each cell's sheet-qualified A1 from the requested range's top-left anchor (parsed from
    ``range_a1`` — no ``sheetId`` resolution needed, just the prefix + start cell) so a ``read_values``
    rectangle becomes the per-cell ``[{a1, value}]`` shape :func:`render_addressed` /
    :func:`addressed_records` consume. The grid value lands under ``value`` (a formula read stores the
    formula string there); an empty-string cell still gets its ``a1`` so positional consumers can
    index, but :func:`render_addressed` drops it from a sparse render.
    """
    from .addressing import parse_a1

    if not values:
        return []

    sheet_prefix = ""
    start_col0 = 0
    start_row1 = 1
    if range_a1:
        parsed = parse_a1(range_a1)
        sheet = parsed.get("sheet")
        if sheet:
            sheet_prefix = f"{_quote_sheet_prefix(sheet)}!"
        start = parsed.get("start")
        if start:
            c0, r1 = _split_anchor(start)
            start_col0 = c0 if c0 is not None else 0
            start_row1 = r1 if r1 is not None else 1

    cells: list[dict] = []
    for r_off, row in enumerate(values):
        for c_off, val in enumerate(row):
            a1 = f"{sheet_prefix}{_col_letters(start_col0 + c_off)}{start_row1 + r_off}"
            cells.append({"a1": a1, "value": val})
    return cells


def _split_anchor(token: str) -> tuple[int | None, int | None]:
    """Split a start-cell token (``"C5"``) into ``(col0, row1)`` — 0-based col, 1-based row.

    A column-only anchor (``"C"``, from a whole-column range) yields ``row1=None`` (defaults to 1);
    a row-only anchor (``"5"``) yields ``col0=None`` (defaults to 0). Pure string math — no API.
    """
    col_letters = "".join(ch for ch in token if ch.isalpha())
    row_digits = "".join(ch for ch in token if ch.isdigit())
    col0 = _col_to_index(col_letters) if col_letters else None
    row1 = int(row_digits) if row_digits else None
    return col0, row1


def _col_to_index(col: str) -> int:
    """Convert column letters (``"A"`` / ``"AA"``) to a 0-based index."""
    idx = 0
    for ch in col.upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _col_letters(col0: int) -> str:
    """Convert a 0-based column index to letters (``0`` -> ``"A"``, ``26`` -> ``"AA"``)."""
    letters: list[str] = []
    n = col0 + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(ord("A") + rem))
    return "".join(reversed(letters))


def _quote_sheet_prefix(title: str) -> str:
    """Quote a sheet title for an A1 prefix when it is not a bare identifier (mirrors addressing)."""
    import re

    if title and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", title):
        return title
    return "'" + title.replace("'", "''") + "'"


def _cell_has_content(cell: dict) -> bool:
    """True iff a cell carries anything beyond its ``a1`` address (value/formula/note/validation).

    A padded blank cell (``{"a1": ...}``) has no content and is dropped from a sparse render; a
    cell whose ``value`` is the empty string ``""`` also counts as empty here (a placeholder).
    """
    value = cell.get("value")
    if value is not None and value != "":
        return True
    return bool(cell.get("formula") or cell.get("note") or cell.get("validation"))


def _is_tabular(result: dict) -> bool:
    """True iff ``result`` is a ``read_values``-style rectangular grid result (SPEC §1.2).

    The contract: a ``ranges`` key holding a list of entries that each carry a ``values`` grid.
    Structured reads (``inspect`` -> ``cells``, ``structure``/``read_conditional_formats`` ->
    ``sheets``) lack this shape and are therefore not tabular.
    """
    ranges = result.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        # An empty-ranges read_values result is still tabular (renders to "").
        return isinstance(ranges, list) and "render" in result
    return all(isinstance(entry, dict) and "values" in entry for entry in ranges)
