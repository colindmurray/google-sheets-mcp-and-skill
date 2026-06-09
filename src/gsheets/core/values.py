"""Value reads/writes and the jagged-array helper (DESIGN §3.3, §5.5).

Houses ``read_values`` / ``write_values`` / ``append_rows`` / ``clear`` plus :func:`pad_jagged`.
Writes default to ``USER_ENTERED`` (formulas work). ``clear`` routes any
formats/validation/notes clearing through ``build_fields_mask``.
"""

from __future__ import annotations

from .service import SheetsServices


def pad_jagged(values: list[list], width: int | None = None) -> list[list]:
    """Pad every row of a jagged values array to a uniform width with ``""``.

    Values reads omit empty trailing cells; this fills each row to the max width (or an
    explicit ``width``) so downstream consumers see a rectangle (DESIGN §5.5).

    Args:
        values: A possibly-jagged list of rows.
        width: Explicit target width; defaults to the max row length.

    Returns:
        A rectangular list of rows, each of equal length.
    """
    raise NotImplementedError


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
    a COMMON rectangle so they are index-aligned; non-formula cells return their literal value
    under FORMULA render (DESIGN §3.3 LOCKED note).

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        ranges: One or more A1 ranges.
        render: ``"plain"`` | ``"unformatted"`` | ``"formula"`` | ``"all"``.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "render": ..., "ranges": [...]}``.
    """
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    ``batchUpdate`` with ``updateCells`` + an auto fields mask covering only the requested
    subfields over the ``GridRange``.

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
    raise NotImplementedError
