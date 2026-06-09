"""The raw ``batchUpdate`` escape hatch (DESIGN §3.3 ``batch``).

Power-user path presented LAST. Passes a raw ordered ``requests[]`` straight to
``spreadsheets.batchUpdate`` and returns the raw ``replies`` plus captured ``newIds`` (via
``capture_new_ids``).
"""

from __future__ import annotations

from .service import SheetsServices


def batch(services: SheetsServices, spreadsheet_id: str, requests: list[dict]) -> dict:
    """Run a raw ordered list of ``batchUpdate`` requests (DESIGN §3.3).

    Passes ``requests`` straight to ``spreadsheets.batchUpdate(body={"requests": requests})``
    and surfaces captured new ids.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        requests: A raw ordered list of ``batchUpdate`` request dicts.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "replies": [...], "newIds": {...}}``.
    """
    raise NotImplementedError
