"""A1 <-> ``GridRange`` conversion and sheet-name -> ``sheetId`` resolution (DESIGN §5.2).

``GridRange`` is 0-based, half-open (``startRowIndex`` inclusive, ``endRowIndex`` exclusive);
A1 is 1-based, inclusive. These helpers centralize the conversion so callers NEVER pass a
``sheetId``. Sheet-name resolution uses a per-call cached
``spreadsheets.get(fields="sheets.properties(sheetId,title)")``. Unbounded ranges
(``A:A``, ``2:2``, whole sheet) map by omitting the corresponding start/end indices.

This module is PURE core: stdlib only. It must NEVER import ``fastmcp``, ``mcp``,
``argparse``, ``pydantic``, or ``gsheets.models`` (DESIGN §1 boundary).
"""

from __future__ import annotations

import re

from .errors import SheetsError
from .service import SheetsServices

# An A1 cell ref: optional column letters, optional 1-based row number. At least one of the
# two must be present (validated by the caller). Used to split a range endpoint into its
# column / row parts so unbounded forms (``A``, ``2``) are recognised.
_CELL_RE = re.compile(r"^(?P<col>[A-Za-z]*)(?P<row>[0-9]*)$")


def parse_a1(a1: str) -> dict:
    """Parse an A1 string into its sheet + start/end components.

    Example:
        ``"Cliff!A2:D5"`` -> ``{"sheet": "Cliff", "start": "A2", "end": "D5"}``.
        The sheet prefix is optional; single-cell and unbounded forms are supported.

    A quoted sheet name (``'My Sheet'!A1``) is unquoted (and any doubled ``''`` collapsed to
    a single ``'``) so the returned ``sheet`` is the bare title. A single-cell range
    (``"A1"``) yields equal ``start``/``end``. Unbounded forms keep their bare endpoints
    (``"A:A"`` -> ``start="A"``, ``end="A"``; ``"2:2"`` -> ``start="2"``, ``end="2"``). A
    bare sheet reference (``"Cliff"`` with no ``!``) yields ``start``/``end`` of ``None``
    (whole-sheet).

    Args:
        a1: An A1 range string (optionally sheet-qualified).

    Returns:
        A dict with ``sheet`` (or ``None``), ``start``, and ``end`` keys.

    Raises:
        SheetsError: ``bad_range`` if ``a1`` is empty or malformed.
    """
    if not isinstance(a1, str) or not a1.strip():
        raise SheetsError("bad_range", "range must be a non-empty A1 string")
    text = a1.strip()

    sheet, rng = _split_sheet(text)

    # A bare sheet reference (no "!"): the whole sheet, no cell endpoints.
    if rng is None:
        return {"sheet": sheet, "start": None, "end": None}

    rng = rng.strip()
    if rng == "":
        # "Cliff!" with an empty range part -> treat as whole sheet.
        return {"sheet": sheet, "start": None, "end": None}

    if ":" in rng:
        start, _, end = rng.partition(":")
        start = start.strip()
        end = end.strip()
        if start == "" or end == "":
            raise SheetsError("bad_range", f"malformed A1 range: {a1!r}")
    else:
        start = end = rng

    # Validate each endpoint is a recognisable A1 cell / column / row token.
    for part in (start, end):
        if not _CELL_RE.match(part):
            raise SheetsError("bad_range", f"malformed A1 range: {a1!r}")

    return {"sheet": sheet, "start": start, "end": end}


def a1_to_gridrange(services: SheetsServices, spreadsheet_id: str, a1: str) -> dict:
    """Convert an A1 range to a Google ``GridRange`` (0-based, half-open).

    Resolves the sheet NAME to a ``sheetId`` via a per-call cached
    ``spreadsheets.get``. Whole-column ``"A:A"``, whole-row ``"2:2"``, whole-sheet, and
    single-cell forms are all supported (unbounded forms omit the relevant indices).

    The sheet name comes from the ``a1`` prefix when present; an unqualified range
    (``"A1:D5"``) resolves against the first sheet (index 0). Index conversion:
    ``startRowIndex = row - 1`` (inclusive), ``endRowIndex = row`` (exclusive);
    ``startColumnIndex = col0`` (inclusive), ``endColumnIndex = col0 + 1`` (exclusive).

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        a1: An A1 range string (optionally sheet-qualified).

    Returns:
        A ``GridRange`` dict, e.g.
        ``{"sheetId": 0, "startRowIndex": 1, "endRowIndex": 5,
        "startColumnIndex": 0, "endColumnIndex": 4}``.

    Raises:
        SheetsError: ``bad_range`` if ``a1`` is malformed; ``sheet_not_found`` if the named
            sheet does not exist in the spreadsheet.
    """
    parsed = parse_a1(a1)
    sheets = _sheet_index(services, spreadsheet_id)
    sheet_id = _resolve_sheet_id(parsed["sheet"], sheets, a1)

    gr: dict = {"sheetId": sheet_id}

    start, end = parsed["start"], parsed["end"]
    if start is None and end is None:
        # Whole sheet: omit all indices.
        return gr

    start_col, start_row = _split_cell(start, a1)
    end_col, end_row = _split_cell(end, a1)

    # Rows: present only when BOTH endpoints carry a row number. A whole-column range
    # ("A:A" / "A2:A") omits row indices entirely (unbounded over rows).
    if start_row is not None and end_row is not None:
        lo, hi = sorted((start_row, end_row))
        gr["startRowIndex"] = lo - 1
        gr["endRowIndex"] = hi

    # Columns: present only when BOTH endpoints carry a column. A whole-row range
    # ("2:2" / "A2:2") omits column indices entirely (unbounded over columns).
    if start_col is not None and end_col is not None:
        s = _col_to_index(start_col)
        e = _col_to_index(end_col)
        lo, hi = sorted((s, e))
        gr["startColumnIndex"] = lo
        gr["endColumnIndex"] = hi + 1

    return gr


def gridrange_to_a1(services: SheetsServices, spreadsheet_id: str, gr: dict) -> str:
    """Convert a Google ``GridRange`` back to a sheet-qualified A1 string.

    Inverse of :func:`a1_to_gridrange`; resolves ``sheetId`` -> sheet name. Omitted row
    indices render as an unbounded column range (``Cliff!A:A``); omitted column indices
    render as an unbounded row range (``Cliff!2:5``); all indices omitted render as the bare
    sheet name (``Cliff``). The sheet title is quoted (``'My Sheet'``) when it contains a
    character that would break a bare A1 reference.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        gr: A ``GridRange`` dict.

    Returns:
        A sheet-qualified A1 string (e.g. ``"Cliff!A2:D5"``).

    Raises:
        SheetsError: ``bad_range`` if ``gr`` is not a dict or lacks ``sheetId``;
            ``sheet_not_found`` if no sheet matches ``sheetId``.
    """
    if not isinstance(gr, dict) or "sheetId" not in gr:
        raise SheetsError("bad_range", "GridRange must be a dict with a 'sheetId' key")

    sheet_id = gr["sheetId"]
    sheets = _sheet_index(services, spreadsheet_id)
    title = _resolve_sheet_title(sheet_id, sheets)
    prefix = _quote_sheet(title)

    has_rows = "startRowIndex" in gr or "endRowIndex" in gr
    has_cols = "startColumnIndex" in gr or "endColumnIndex" in gr

    if not has_rows and not has_cols:
        return prefix

    if has_cols and not has_rows:
        # Whole-column range: "A:D".
        a1 = f"{_col_endpoints(gr)}"
    elif has_rows and not has_cols:
        # Whole-row range: "2:5".
        start_row = gr.get("startRowIndex")
        end_row = gr.get("endRowIndex")
        lo = (start_row + 1) if start_row is not None else 1
        hi = end_row if end_row is not None else lo
        a1 = f"{lo}:{hi}" if hi != lo else f"{lo}:{lo}"
    else:
        # Bounded rectangle (or single cell).
        start_row = gr.get("startRowIndex")
        end_row = gr.get("endRowIndex")
        start_col = gr.get("startColumnIndex")
        end_col = gr.get("endColumnIndex")

        lo_row = (start_row + 1) if start_row is not None else 1
        hi_row = end_row if end_row is not None else lo_row
        lo_col = start_col if start_col is not None else 0
        hi_col = (end_col - 1) if end_col is not None else lo_col

        start_a1 = f"{_index_to_col(lo_col)}{lo_row}"
        end_a1 = f"{_index_to_col(hi_col)}{hi_row}"
        a1 = start_a1 if start_a1 == end_a1 else f"{start_a1}:{end_a1}"

    return f"{prefix}!{a1}"


# --------------------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------------------


def _split_sheet(text: str) -> tuple[str | None, str | None]:
    """Split a sheet prefix off an A1 string.

    Returns ``(sheet, range_part)``. ``range_part`` is ``None`` when there is no ``!``
    (a bare sheet reference). Handles quoted sheet names (``'My!Sheet'!A1``), collapsing
    doubled ``''`` to a single ``'``.
    """
    if text.startswith("'"):
        # Quoted sheet name: scan to the closing quote, honouring doubled '' escapes.
        i = 1
        buf: list[str] = []
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                # Closing quote.
                i += 1
                break
            buf.append(ch)
            i += 1
        else:
            raise SheetsError("bad_range", f"unterminated quoted sheet name: {text!r}")
        sheet = "".join(buf)
        rest = text[i:]
        if rest == "":
            return sheet, None
        if not rest.startswith("!"):
            raise SheetsError("bad_range", f"expected '!' after quoted sheet name: {text!r}")
        return sheet, rest[1:]

    if "!" in text:
        sheet, _, rng = text.partition("!")
        sheet = sheet.strip()
        if sheet == "":
            raise SheetsError("bad_range", f"empty sheet name in {text!r}")
        return sheet, rng

    # No "!": disambiguate a bare cell/range ("A1:D5", "A1") from a bare sheet name
    # ("Cliff", "Sheet1"). A token containing ":" with no sheet prefix is unambiguously a
    # range ("A:A", "2:5", "A1:D5"). A single token with no ":" that contains a DIGIT
    # (e.g. "A1", "B2") is a cell; a single all-letters token ("Cliff") is a SHEET name
    # (whole-sheet), since a bare column ref like "A" only ever appears inside a ":" range.
    if ":" in text and _looks_like_range(text):
        return None, text
    if ":" not in text and _is_cell_token(text):
        return None, text
    return text, None


def _looks_like_range(text: str) -> bool:
    """True when ``text`` parses as a bare A1 cell/range (no sheet prefix).

    Used for the colon-bearing case (``"A:A"``, ``"2:5"``, ``"A1:D5"``), where each
    endpoint may legitimately be column-only (``"A"``), row-only (``"2"``), or a full cell
    (``"A1"``).
    """
    parts = text.split(":")
    if len(parts) > 2:
        return False
    for p in parts:
        p = p.strip()
        m = _CELL_RE.match(p)
        if not m or not (m.group("col") or m.group("row")):
            return False
    return True


def _is_cell_token(text: str) -> bool:
    """True when ``text`` is a single A1 cell endpoint carrying at least one digit.

    A token must contain a row number to count as a cell here (``"A1"`` yes, ``"A"`` no,
    ``"Cliff"`` no) so a bare all-letters token is treated as a sheet name, not a column.
    """
    m = _CELL_RE.match(text.strip())
    return bool(m and m.group("row"))


def _split_cell(token: str, original: str) -> tuple[str | None, int | None]:
    """Split an endpoint token into ``(column_letters_or_None, row_int_or_None)``."""
    m = _CELL_RE.match(token)
    if not m:
        raise SheetsError("bad_range", f"malformed A1 cell {token!r} in {original!r}")
    col = m.group("col") or None
    row_str = m.group("row") or None
    if col is None and row_str is None:
        raise SheetsError("bad_range", f"empty A1 endpoint in {original!r}")
    row = int(row_str) if row_str is not None else None
    if row is not None and row < 1:
        raise SheetsError("bad_range", f"row index must be >= 1 in {original!r}")
    return col, row


def _col_to_index(col: str) -> int:
    """Convert column letters (``"A"``, ``"AA"``) to a 0-based index."""
    idx = 0
    for ch in col.upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _index_to_col(idx: int) -> str:
    """Convert a 0-based column index to letters (``0`` -> ``"A"``, ``26`` -> ``"AA"``)."""
    if idx < 0:
        raise SheetsError("bad_range", f"column index must be >= 0, got {idx}")
    letters: list[str] = []
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(ord("A") + rem))
    return "".join(reversed(letters))


def _col_endpoints(gr: dict) -> str:
    """Render a whole-column range (``"A:D"``) from a GridRange's column indices."""
    start_col = gr.get("startColumnIndex")
    end_col = gr.get("endColumnIndex")
    lo = start_col if start_col is not None else 0
    hi = (end_col - 1) if end_col is not None else lo
    lo_letters = _index_to_col(lo)
    hi_letters = _index_to_col(hi)
    return f"{lo_letters}:{hi_letters}"


def _quote_sheet(title: str) -> str:
    """Quote a sheet title for an A1 reference when it is not a bare identifier.

    Google quotes a sheet name in A1 when it contains anything other than letters, digits,
    and underscores, or when it starts with a digit. Inside quotes a literal ``'`` is
    doubled.
    """
    if title and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", title):
        return title
    escaped = title.replace("'", "''")
    return f"'{escaped}'"


def _sheet_index(services: SheetsServices, spreadsheet_id: str) -> list[dict]:
    """Fetch the (title, sheetId, index) for every sheet, via a per-call cached get.

    Uses the cheapest possible mask: ``sheets.properties(sheetId,title)`` (DESIGN §5.2).
    The Google call is wrapped so an ``HttpError`` is classified into a ``SheetsError``.

    Returns:
        A list of ``{"title": str, "sheetId": int, "index": int}`` dicts in sheet order.
    """
    try:
        resp = (
            services.sheets.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                fields="sheets.properties(sheetId,title,index)",
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 - re-classified below
        _maybe_classify(exc)
        raise

    out: list[dict] = []
    for entry in resp.get("sheets", []) or []:
        props = (entry or {}).get("properties", {}) or {}
        out.append(
            {
                "title": props.get("title"),
                "sheetId": props.get("sheetId"),
                "index": props.get("index"),
            }
        )
    return out


def _resolve_sheet_id(sheet_name: str | None, sheets: list[dict], original: str) -> int:
    """Resolve a sheet NAME (or ``None`` => first sheet) to its ``sheetId``."""
    if sheet_name is None:
        if not sheets:
            raise SheetsError("sheet_not_found", "spreadsheet has no sheets")
        # Unqualified range: bind to the first sheet (index 0).
        first = min(sheets, key=lambda s: (s.get("index") is None, s.get("index", 0)))
        sid = first.get("sheetId")
        if sid is None:
            raise SheetsError("sheet_not_found", "could not resolve the first sheet's id")
        return sid

    for s in sheets:
        if s.get("title") == sheet_name:
            sid = s.get("sheetId")
            if sid is None:
                raise SheetsError(
                    "sheet_not_found", f"sheet {sheet_name!r} has no sheetId"
                )
            return sid

    available = ", ".join(repr(s.get("title")) for s in sheets) or "(none)"
    raise SheetsError(
        "sheet_not_found",
        f"sheet {sheet_name!r} not found in spreadsheet (from {original!r})",
        hint=f"available sheets: {available}",
    )


def _resolve_sheet_title(sheet_id: object, sheets: list[dict]) -> str:
    """Resolve a ``sheetId`` to its title."""
    for s in sheets:
        if s.get("sheetId") == sheet_id:
            title = s.get("title")
            if title is None:
                raise SheetsError(
                    "sheet_not_found", f"sheet id {sheet_id!r} has no title"
                )
            return title
    raise SheetsError(
        "sheet_not_found", f"no sheet with id {sheet_id!r} in spreadsheet"
    )


def _maybe_classify(exc: Exception) -> None:
    """Re-raise a Google ``HttpError`` as a classified ``SheetsError``.

    Imported lazily so the hot path stays dependency-light and core never hard-requires the
    googleapiclient symbol at import time. A non-HttpError is left for the caller to
    propagate.
    """
    try:
        from googleapiclient.errors import HttpError  # type: ignore
    except Exception:  # pragma: no cover - googleapiclient always present at runtime
        return
    if isinstance(exc, HttpError):
        from .errors import classify_google_error

        raise classify_google_error(exc)
