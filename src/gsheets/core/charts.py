"""Embedded chart create/update/delete/read (DESIGN §3.3 ``charts``).

v1 scope (LOCKED): ``read`` returns chart METADATA only (``chartId``/``title``/``type``/
``anchor``), NOT the full ``EmbeddedChartSpec``; ``create``/``update`` take the locked minimal
flat ``spec``. Full chart-spec round-trip is the sole deliberate exception to CRUD symmetry —
callers needing it use ``batch``.
"""

from __future__ import annotations

from .service import SheetsServices


def charts(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    action: str,
    sheet: str | None = None,
    chart_id: int | None = None,
    spec: dict | None = None,
) -> dict:
    """Create/update/delete/read embedded charts (DESIGN §3.3, v1 scope).

    ``spec`` keys for ``create``/``update`` (unknown key -> ``SheetsError("unknown_param")``):
    ``{"title", "type", "series", "domain", "anchor"}`` where ``type`` is one of
    ``LINE``/``COLUMN``/``BAR``/``PIE``/``SCATTER``/``AREA``. ``create`` captures the new
    ``chartId`` from ``replies[]``. ``read`` lists charts (metadata only).

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        action: ``"create"`` | ``"update"`` | ``"delete"`` | ``"read"``.
        sheet: Target tab name (for read / anchor resolution).
        chart_id: Existing chart id for update/delete.
        spec: Minimal flat chart spec for create/update.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "action": ..., "chartId": ...}`` (or
        ``"charts": [...]`` for read).
    """
    raise NotImplementedError
