"""The rich read surface: overview / inspect / read_conditional_formats (DESIGN §3.3).

This is where read-side richness lives. ``overview`` is the cheap orientation call (no grid
data; counts ``len()``-ed in core). ``inspect`` is the flagship per-cell read (values +
formulas + both formats + merges + validation, optional compact rectangular RLE).
``read_conditional_formats`` is the PRIORITY read (CF rules serialized to body-only lines).
"""

from __future__ import annotations

from .service import SheetsServices


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError
