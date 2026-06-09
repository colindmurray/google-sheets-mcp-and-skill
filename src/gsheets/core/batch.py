"""The raw ``batchUpdate`` escape hatch (DESIGN §3.3 ``batch``).

Power-user path presented LAST. Passes a raw ordered ``requests[]`` straight to
``spreadsheets.batchUpdate`` and returns the raw ``replies`` plus captured ``newIds`` (via
``capture_new_ids``).

PURE core module: imports only stdlib + sibling core modules + ``googleapiclient`` errors. It
must NEVER import ``fastmcp``, ``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models``
(DESIGN §1 boundary).
"""

from __future__ import annotations

from googleapiclient.errors import HttpError

from .errors import SheetsError, classify_google_error
from .service import SheetsServices
from .structure import capture_new_ids


def batch(services: SheetsServices, spreadsheet_id: str, requests: list[dict]) -> dict:
    """Run a raw ordered list of ``batchUpdate`` requests (DESIGN §3.3).

    The power-user escape hatch: ``requests`` is passed straight to
    ``spreadsheets.batchUpdate(body={"requests": requests})`` with no reshaping, so callers can
    issue any ``batchUpdate`` request the typed tools do not cover (and in any order). The raw
    ``replies`` come back untouched; new ids that the API returns only in ``replies[]``
    (``sheetId``/``chartId``/``namedRangeId``/``protectedRangeId``/``metadataId``) are surfaced
    under ``newIds`` via :func:`capture_new_ids` so a create+populate flow is one batch.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        requests: A raw ordered list of ``batchUpdate`` request dicts. Order is preserved
            exactly (the API applies them in order); core does NOT sort or rewrite them.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "replies": [...], "newIds": {...}}`` where
        ``newIds`` is the full :func:`capture_new_ids` shape
        (``sheetIds``/``chartIds``/``namedRangeIds``/``protectedRangeIds``/``metadataIds``,
        each a list, empty when absent).

    Raises:
        SheetsError: ``"bad_request"`` when ``requests`` is not a non-empty list, or
            ``"google_api_error"`` (via :func:`classify_google_error`) when the API rejects the
            batch.
    """
    if not isinstance(requests, list):
        raise SheetsError(
            "bad_request",
            f"requests must be a list of batchUpdate request dicts, got "
            f"{type(requests).__name__}",
        )
    if not requests:
        raise SheetsError(
            "empty_payload",
            "requests is empty — pass at least one batchUpdate request",
        )

    try:
        resp = (
            services.sheets.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests},
            )
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc

    replies = resp.get("replies", []) or []
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "replies": replies,
        "newIds": capture_new_ids(replies),
    }
