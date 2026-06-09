"""Structural ops, tab management, developer metadata + reply-id capture (DESIGN §3.3, §5.4).

Houses ``structure`` / ``manage_sheets`` / ``metadata`` plus :func:`capture_new_ids`.
``structure(action="read")`` returns a shape-stable multi-sheet envelope (``sheets`` always a
list; spreadsheet-scoped ``namedRanges`` at top level) shared with ``read_conditional_formats``.
"""

from __future__ import annotations

from .service import SheetsServices


def capture_new_ids(replies: list[dict]) -> dict:
    """Surface new ids returned only in ``batchUpdate`` ``replies[]`` (DESIGN §5.4).

    ``addSheet``/``duplicateSheet``/``addChart``/``addNamedRange``/``addProtectedRange``/
    ``createDeveloperMetadata`` return new ids in ``replies[]``; this matches reply to request
    by order and extracts ``sheetId``/``chartId``/``namedRangeId``/``protectedRangeId``/
    ``metadataId`` so create+populate is one batch.

    Args:
        replies: The ``replies`` list from a ``batchUpdate`` response.

    Returns:
        A dict of captured id lists, e.g.
        ``{"sheetIds": [7], "chartIds": [], "namedRangeIds": []}``.
    """
    raise NotImplementedError


def structure(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    action: str,
    sheet: str | None = None,
    range: str | None = None,
    params: dict | None = None,
) -> dict:
    """Read or modify spreadsheet structure through one interface (DESIGN §3.3).

    ``action="read"`` returns the shape-stable envelope (``sheets`` always a list;
    spreadsheet-scoped ``namedRanges`` at top level; ``sheet`` optional ⇒ every tab). Mutating
    actions (``merge``/``unmerge``/``add_named``/``delete_named``/``protect``/``unprotect``/
    ``freeze``/``tab_color``/``group``/``ungroup``) require ``sheet`` and use the matching
    ``batchUpdate`` request with an auto fields mask where applicable. Each action consumes only
    its documented ``params`` keys; unknown keys raise ``SheetsError("unknown_param")``.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        action: One of the structural actions (see DESIGN §3.3 ``structure`` table).
        sheet: Target tab name (optional for read; required for mutate).
        range: A1 range for range-scoped actions (merge/unmerge/add_named/protect).
        params: Per-action parameter dict (see the LOCKED key table).

    Returns:
        For read: the multi-sheet envelope. For mutate:
        ``{"ok": True, "spreadsheetId": ..., ...ids/ranges affected...}``.
    """
    raise NotImplementedError


def manage_sheets(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    action: str,
    sheet: str | None = None,
    params: dict | None = None,
) -> dict:
    """Add/delete/duplicate/rename/reorder tabs; capture new ``sheetId``s (DESIGN §3.3).

    Per-action ``params`` keys (unknown key -> ``SheetsError("unknown_param")``): ``add`` ->
    ``{"title", "index", "rows", "cols"}`` (all optional); ``delete`` -> none (target via
    ``sheet``); ``duplicate`` -> ``{"newName", "newIndex"}``; ``rename`` -> ``{"newName"}``
    (required); ``reorder`` -> ``{"newIndex"}`` (required).

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        action: ``"add"`` | ``"delete"`` | ``"duplicate"`` | ``"rename"`` | ``"reorder"``.
        sheet: Target tab name for delete/duplicate/rename/reorder.
        params: Per-action parameter dict.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "action": ...,
        "sheet": {"sheetId", "title", "index"}}``.
    """
    raise NotImplementedError


def metadata(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    action: str,
    key: str | None = None,
    value: str | None = None,
    location: dict | None = None,
    visibility: str = "DOCUMENT",
    metadata_id: int | None = None,
) -> dict:
    """Read/write developer metadata for durable row/column/sheet anchors (DESIGN §3.3).

    ``read`` -> ``developerMetadata.search`` (or all for a key). ``create`` ->
    ``createDeveloperMetadata`` with a dimension-range / whole-sheet / spreadsheet anchor;
    captures the assigned ``metadataId``. ``location`` is one of a dimension anchor
    ``{"sheet", "dimension", "start", "end"}``, a whole-sheet anchor ``{"sheet"}``, or a
    spreadsheet anchor ``{}`` (unknown key -> ``SheetsError("unknown_param")``).

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        action: ``"read"`` | ``"create"`` | ``"update"`` | ``"delete"``.
        key: Metadata key.
        value: Metadata value.
        location: Anchor location dict (see above).
        visibility: ``"DOCUMENT"`` (default) or ``"PROJECT"``.
        metadata_id: Existing metadata id for update/delete.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "action": ..., "metadata": [...]}``.
    """
    raise NotImplementedError
