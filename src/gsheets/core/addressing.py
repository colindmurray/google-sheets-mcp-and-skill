"""A1 <-> ``GridRange`` conversion and sheet-name -> ``sheetId`` resolution (DESIGN §5.2).

``GridRange`` is 0-based, half-open (``startRowIndex`` inclusive, ``endRowIndex`` exclusive);
A1 is 1-based, inclusive. These helpers centralize the conversion so callers NEVER pass a
``sheetId``. Sheet-name resolution uses a per-call cached
``spreadsheets.get(fields="sheets.properties(sheetId,title)")``. Unbounded ranges
(``A:A``, ``2:2``, whole sheet) map by omitting the corresponding start/end indices.
"""

from __future__ import annotations

from .service import SheetsServices


def parse_a1(a1: str) -> dict:
    """Parse an A1 string into its sheet + start/end components.

    Example:
        ``"Cliff!A2:D5"`` -> ``{"sheet": "Cliff", "start": "A2", "end": "D5"}``.
        The sheet prefix is optional; single-cell and unbounded forms are supported.

    Args:
        a1: An A1 range string (optionally sheet-qualified).

    Returns:
        A dict with ``sheet`` (or ``None``), ``start``, and ``end`` keys.
    """
    raise NotImplementedError


def a1_to_gridrange(services: SheetsServices, spreadsheet_id: str, a1: str) -> dict:
    """Convert an A1 range to a Google ``GridRange`` (0-based, half-open).

    Resolves the sheet NAME to a ``sheetId`` via a per-call cached
    ``spreadsheets.get``. Whole-column ``"A:A"``, whole-row ``"2:2"``, whole-sheet, and
    single-cell forms are all supported (unbounded forms omit the relevant indices).

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        a1: An A1 range string (optionally sheet-qualified).

    Returns:
        A ``GridRange`` dict, e.g.
        ``{"sheetId": 0, "startRowIndex": 1, "endRowIndex": 5,
        "startColumnIndex": 0, "endColumnIndex": 4}``.
    """
    raise NotImplementedError


def gridrange_to_a1(services: SheetsServices, spreadsheet_id: str, gr: dict) -> str:
    """Convert a Google ``GridRange`` back to a sheet-qualified A1 string.

    Inverse of :func:`a1_to_gridrange`; resolves ``sheetId`` -> sheet name.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        gr: A ``GridRange`` dict.

    Returns:
        A sheet-qualified A1 string (e.g. ``"Cliff!A2:D5"``).
    """
    raise NotImplementedError
