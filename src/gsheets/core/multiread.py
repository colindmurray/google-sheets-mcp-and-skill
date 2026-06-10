"""Multi-spreadsheet READ (DESIGN §3.3 — cross-file batch read, read-only by design).

One call that fans a values or summary read across MANY spreadsheets, capturing per-file
errors instead of aborting the whole batch. This is the capability that neutralizes xing5's
``get_multiple_spreadsheet_summary`` / ``get_multiple_sheet_data`` — a single round of
orientation/values across a set of sheet ids.

Read-only is the CORRECT shape here: the Sheets API has no cross-file atomic write (a
``batchUpdate`` is scoped to one ``spreadsheetId``), so a multi-file mutation could only be a
non-atomic loop that silently half-applies on failure. We expose only the safe read fan-out.

PURE core module: imports only stdlib + sibling core modules (``.errors``, ``.service``,
``.reads``, ``.values``). It must NEVER import ``fastmcp``, ``mcp``, ``argparse``,
``pydantic``, or ``gsheets.models`` (DESIGN §1 boundary).
"""

from __future__ import annotations

from .errors import SheetsError
from .reads import overview
from .service import SheetsServices
from .values import read_values

# The two supported fan-out modes (DESIGN §3.3 cross-file read).
_MODES = ("values", "summary")


def read_many(
    services: SheetsServices,
    requests: list[dict],
    *,
    mode: str = "values",
) -> dict:
    """Fan a values or summary read across many spreadsheets, capturing per-file errors.

    Each request names ONE ``spreadsheetId``; the read applied per file is chosen by ``mode``:

    - ``mode="summary"`` calls :func:`core.reads.overview` per id (cheap orientation, no grid
      data) — the analogue of xing5's ``get_multiple_spreadsheet_summary``.
    - ``mode="values"`` calls :func:`core.values.read_values` per id over that request's
      ``ranges`` (``render="plain"`` by default; a per-request ``"render"`` key overrides it) —
      the analogue of xing5's ``get_multiple_sheet_data``.

    The headline behavior is PER-ITEM error capture: each per-spreadsheet call is wrapped in
    ``try/except SheetsError`` so one bad id (404, permission denied, bad range) becomes a
    captured ``{"spreadsheetId", "ok": False, "error": {...}}`` entry in ``results`` instead of
    aborting the whole batch — the other files still read. Successful entries are the underlying
    core result dict (already ``ok: True``), guaranteed to carry their ``spreadsheetId``.

    This is read-only by design: the Sheets API has no cross-file atomic write, so a multi-file
    mutation could only half-apply; the safe, honest surface is a read fan-out.

    Args:
        services: The authed handle.
        requests: A NON-EMPTY list of request dicts. Each item requires ``"spreadsheetId"``
            (str); for ``mode="values"`` it also requires ``"ranges"`` (a list of A1 strings)
            and accepts an optional ``"render"`` override (``"plain"`` | ``"unformatted"`` |
            ``"formula"`` | ``"all"``). ``"ranges"`` is ignored for ``mode="summary"``.
        mode: ``"values"`` (default) or ``"summary"``.

    Returns:
        ``{"ok": True, "mode": ..., "count": ..., "succeeded": ..., "failed": ...,
        "partialFailure": ..., "results": [...]}`` where ``results`` mixes success result dicts
        (``ok: True``) and captured ``{"spreadsheetId", "ok": False, "error": {...}}`` entries,
        one per request, in request order. ``partialFailure`` is ``True`` when ANY inner request
        failed — top-level ``ok`` stays batch-level, so check ``partialFailure``/``failed`` (or
        each entry's ``ok``) rather than the top-level ``ok`` alone (ISSUES.md #3).

    Raises:
        SheetsError: ``bad_mode`` for an unknown mode; ``bad_requests`` when ``requests`` is not
            a non-empty list, an item is not a dict, an item lacks ``spreadsheetId``, or a
            ``values``-mode item lacks ``ranges``. (Per-file Google failures do NOT raise — they
            are captured into ``results``.)
    """
    if mode not in _MODES:
        raise SheetsError(
            "bad_mode",
            f"unknown mode {mode!r}; expected one of 'values', 'summary'",
        )
    if not isinstance(requests, list) or not requests:
        raise SheetsError(
            "bad_requests",
            "read_many requires a non-empty list of request dicts",
            hint="pass requests=[{'spreadsheetId': ..., 'ranges': [...]}, ...]",
        )

    # Validate the ENTIRE batch up front (a malformed request is a caller bug, not a per-file
    # Google failure) so we never issue a partial fan-out against a structurally bad batch.
    validated: list[tuple[str, dict]] = []
    for index, request in enumerate(requests):
        validated.append((_request_spreadsheet_id(request, index, mode), request))

    results: list[dict] = []
    for spreadsheet_id, request in validated:
        try:
            if mode == "summary":
                result = overview(services, spreadsheet_id)
            else:
                ranges = request["ranges"]
                render = request.get("render", "plain")
                result = read_values(
                    services, spreadsheet_id, ranges, render=render
                )
        except SheetsError as exc:
            # PER-ITEM capture: one file's failure never aborts the batch (DESIGN §3.3).
            results.append(
                {
                    "spreadsheetId": spreadsheet_id,
                    "ok": False,
                    "error": exc.to_dict(),
                }
            )
            continue
        # Success: the core result already carries ``ok: True``; ensure the id rides along so
        # every result is self-identifying regardless of which core fn produced it.
        result.setdefault("spreadsheetId", spreadsheet_id)
        results.append(result)

    # The batch call itself succeeded (top-level ``ok: True`` means "the fan-out ran"), but a
    # caller checking only that signal would be misled when an inner request failed (ISSUES.md
    # #3). Surface an explicit ``partialFailure`` flag + ``failed`` count so a partial result is
    # never mistaken for a clean one. (Top-level ``ok`` stays batch-level by design — the
    # documented contract is "check each results[] entry's ok".)
    failed = sum(1 for r in results if r.get("ok") is False)
    return {
        "ok": True,
        "mode": mode,
        "count": len(results),
        "succeeded": len(results) - failed,
        "failed": failed,
        "partialFailure": failed > 0,
        "results": results,
    }


def _request_spreadsheet_id(request: object, index: int, mode: str) -> str:
    """Validate one request dict and return its ``spreadsheetId`` (or raise ``bad_requests``).

    Enforces the structural contract a malformed batch violates: each item is a dict carrying a
    truthy ``spreadsheetId``, and a ``values``-mode item additionally carries ``ranges``. These
    are caller bugs (distinct from a live Google 404/permission failure, which is captured
    per-file inside :func:`read_many`).
    """
    if not isinstance(request, dict):
        raise SheetsError(
            "bad_requests",
            f"request #{index} is not a dict",
            hint="each request must be a dict like {'spreadsheetId': ..., 'ranges': [...]}",
        )
    spreadsheet_id = request.get("spreadsheetId")
    if not spreadsheet_id or not isinstance(spreadsheet_id, str):
        raise SheetsError(
            "bad_requests",
            f"request #{index} is missing a 'spreadsheetId'",
            hint="every request must name a 'spreadsheetId' (a non-empty string)",
        )
    if mode == "values" and "ranges" not in request:
        raise SheetsError(
            "bad_requests",
            f"request #{index} (spreadsheetId {spreadsheet_id!r}) is missing 'ranges'",
            hint="values mode requires per-request 'ranges' (a list of A1 strings)",
        )
    return spreadsheet_id
