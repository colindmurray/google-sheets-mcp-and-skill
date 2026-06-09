"""Atomic cell formatting (DESIGN §3.3 ``format``, §5.1).

``format`` translates the flat ``CellFormat`` payload to a ``repeatCell.cell.userEnteredFormat``
(plus ``note`` as a cell-level field and ``padding`` as an atomic-leaf), with borders applied
via ``updateBorders``. Both requests are issued as ONE ``batchUpdate`` (all-or-nothing). The
``fields`` mask is auto-built from the payload via ``build_fields_mask``.
"""

from __future__ import annotations

from .service import SheetsServices


def format(services: SheetsServices, spreadsheet_id: str, range: str, fmt: dict) -> dict:
    """Apply formatting to a range atomically with an auto-built fields mask (DESIGN §3.3).

    ``fmt`` accepts the flat ``CellFormat`` keys (e.g.
    ``{"bg": "#FFCDD2", "bold": True, "numberFormat": "0.00%",
    "padding": {"top": 2, "left": 3}, "borders": {"top": "SOLID #000000"}, "note": "reviewed"}``).
    Core translates flat -> Google ``repeatCell.cell`` and auto-builds the ``fields`` mask.
    ``note`` writes ``repeatCell.cell.note``; ``borders`` apply via ``updateBorders``; both
    requests go in ONE ``batchUpdate`` so the operation is all-or-nothing.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        range: A1 range to format.
        fmt: Flat ``CellFormat`` payload (see DESIGN §3.1).

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "range": ..., "appliedFields": ...}``.
    """
    raise NotImplementedError
