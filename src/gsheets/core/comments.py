"""Drive threaded-comments read (DESIGN §X.0g, §X.5 — Feature #5, read-only v1).

This is the ONE core capability that does NOT touch the Sheets API — Google Sheets has no
comment surface, so threaded comments live on the **Drive v3** file resource and are read via
``services.drive.comments().list(fileId=<spreadsheet_id>, ...)``.

Two pieces, both PURE core:

* :func:`serialize_comment` — GOLDEN-MASTER serializer. A Google Drive ``Comment`` JSON dict in
  -> a terse, flattened, condformat-style dict out. Flattens ``author/displayName`` ->
  ``author``, ``quotedFileContent/value`` -> ``quoted``, and each reply's ``author/displayName``
  + ``content`` + ``action``. Emits each sub-key ONLY when present (token-safe). A terse one-line
  ``line`` summary rides alongside, mirroring the existing serializer style
  (``comment AAAA by Jane Doe: "please verify Q3" (open, 1 reply)``).
* :func:`comments` — the top-level core fn. Paginates ``comments.list`` (the ``fields`` mask is
  REQUIRED — omitting it ERRORS, a verified Drive gotcha), following ``nextPageToken`` until it
  is absent, optionally filtering out ``resolved`` comments, and returns the flattened list.

The ``anchor`` field is an **opaque, document-type-specific JSON string** — NOT an A1 range and
with no documented Sheets cell mapping. It is surfaced raw under ``"anchorRaw"`` (document-level
only) and is NEVER claimed to address a cell.

PURE core module: imports only stdlib + ``googleapiclient`` + sibling core modules (``errors``,
``service``). It must NEVER import ``fastmcp``, ``mcp``, ``argparse``, ``pydantic``, or
``gsheets.models`` (DESIGN §1 boundary). It does NOT import ``addressing``/``colors`` — comments
carry no resolvable range or color.
"""

from __future__ import annotations

from googleapiclient.errors import HttpError

from .errors import SheetsError, classify_google_error
from .service import SheetsServices

# The REQUIRED Drive ``comments.list`` fields mask (DESIGN §X.0g, verified gotcha). Drive ERRORS
# if ``fields`` is omitted, so it is ALWAYS sent. ``nextPageToken`` rides the partial response so
# we can paginate; the per-comment subfields are exactly what ``serialize_comment`` surfaces.
COMMENTS_FIELDS = (
    "comments("
    "id,content,createdTime,modifiedTime,author/displayName,resolved,anchor,"
    "quotedFileContent/value,replies(content,author/displayName,action)"
    "),nextPageToken"
)

# One page = 100 comments (the Drive v3 ``pageSize`` max for ``comments.list``).
_PAGE_SIZE = 100


# ===========================================================================
# serialize: Google Drive Comment -> terse flattened dict
# ===========================================================================


def serialize_comment(comment: dict) -> dict:
    """Serialize a Google Drive ``Comment`` to the terse, flattened core shape (DESIGN §X.0g).

    Flattens the nested Drive objects (``author/displayName`` -> ``author``,
    ``quotedFileContent/value`` -> ``quoted``, each reply's ``author/displayName``) and emits each
    sub-key ONLY when present so a sparse comment stays token-cheap. A terse one-line ``line``
    summary is always present (mirroring the existing serializer style):
    ``comment AAAA by Jane Doe: "please verify Q3" (open, 1 reply)``.

    The ``anchor`` field is opaque/document-type-specific (NOT an A1 range); it is surfaced raw
    under ``"anchorRaw"`` and never mapped to a cell.

    Args:
        comment: A Google Drive v3 ``Comment`` resource dict (as returned by ``comments.list``
            with :data:`COMMENTS_FIELDS`).

    Returns:
        ``{"id": ..., "author": ..., "content": ..., "created": ..., "modified": ...,
        "resolved": bool, "quoted": ..., "anchorRaw": ..., "replies": [...], "line": ...}`` — with
        every optional key omitted when absent. ``resolved`` defaults to ``False`` (a Drive
        comment without the field is open). ``replies`` is omitted when there are none.
    """
    if not isinstance(comment, dict):
        raise SheetsError(
            "bad_comment", f"comment must be a dict, got {type(comment).__name__}"
        )

    out: dict = {}

    cid = comment.get("id")
    if cid is not None:
        out["id"] = cid

    author = _author_name(comment.get("author"))
    if author is not None:
        out["author"] = author

    content = comment.get("content")
    if content is not None:
        out["content"] = content

    created = comment.get("createdTime")
    if created is not None:
        out["created"] = created

    modified = comment.get("modifiedTime")
    if modified is not None:
        out["modified"] = modified

    # ``resolved`` is output-only on Drive ("resolved by one of its replies"). Always surface it
    # as a bool so callers do not have to distinguish absent-vs-open.
    out["resolved"] = bool(comment.get("resolved", False))

    quoted = _quoted_value(comment.get("quotedFileContent"))
    if quoted is not None:
        out["quoted"] = quoted

    # anchor is OPAQUE/document-type-specific (NOT an A1 range) -> surface document-level only.
    anchor = comment.get("anchor")
    if anchor is not None:
        out["anchorRaw"] = anchor

    replies = _serialize_replies(comment.get("replies"))
    if replies:
        out["replies"] = replies

    out["line"] = _comment_line(out, replies)
    return out


def _author_name(author: object) -> str | None:
    """Flatten a Drive ``User`` (``{"displayName": ...}``) to its display name, else ``None``."""
    if isinstance(author, dict):
        name = author.get("displayName")
        if name is not None:
            return name
    return None


def _quoted_value(quoted_file_content: object) -> str | None:
    """Flatten ``quotedFileContent`` (``{"value": ...}``) to the quoted snippet text.

    The snippet is an opaque document-extracted string (for Sheets, the rendered cell text); it
    is surfaced verbatim and never interpreted as a range.
    """
    if isinstance(quoted_file_content, dict):
        value = quoted_file_content.get("value")
        if value is not None:
            return value
    return None


def _serialize_replies(replies: object) -> list[dict]:
    """Flatten a comment's ``replies`` array to ``[{"author"?, "content"?, "action"?}, ...]``.

    Each reply emits only the sub-keys present (token-safe). ``action`` is the Drive reply verb
    (``"resolve"`` / ``"reopen"``) when a reply changed the comment's resolved state.
    """
    out: list[dict] = []
    for reply in replies or []:
        if not isinstance(reply, dict):
            continue
        entry: dict = {}
        author = _author_name(reply.get("author"))
        if author is not None:
            entry["author"] = author
        content = reply.get("content")
        if content is not None:
            entry["content"] = content
        action = reply.get("action")
        if action is not None:
            entry["action"] = action
        out.append(entry)
    return out


def _comment_line(serialized: dict, replies: list[dict]) -> str:
    """Build the terse one-line summary (DESIGN §X.0g).

    ``comment AAAA by Jane Doe: "please verify Q3" (open, 1 reply)`` — id and author degrade
    gracefully when absent; the reply count is pluralized; the state is ``resolved``/``open``.
    """
    head = "comment"
    cid = serialized.get("id")
    if cid is not None:
        head += f" {cid}"
    author = serialized.get("author")
    if author is not None:
        head += f" by {author}"

    content = serialized.get("content")
    body = f': "{content}"' if content is not None else ""

    state = "resolved" if serialized.get("resolved") else "open"
    n = len(replies)
    reply_word = "reply" if n == 1 else "replies"
    suffix = f" ({state}, {n} {reply_word})"

    return f"{head}{body}{suffix}"


# ===========================================================================
# comments — top-level core fn (Drive v3 comments.list, paginated)
# ===========================================================================


def comments(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    include_resolved: bool = True,
    include_deleted: bool = False,
) -> dict:
    """Read a spreadsheet's Drive threaded comments (DESIGN §X.5, read-only v1).

    Uses the **Drive v3** API (``services.drive.comments().list``), NOT the Sheets API — Sheets
    has no comment surface. The ``fields`` mask is REQUIRED (Drive errors without it) and is
    always sent as :data:`COMMENTS_FIELDS`. Results are paginated: each page follows
    ``nextPageToken`` until it is absent. ``include_resolved=False`` drops ``resolved`` comments
    in core (Drive has no server-side resolved filter on ``comments.list``).

    Args:
        services: The authed handle. ``services.drive`` MUST be non-``None`` — when it is
            ``None`` (no Drive scope granted), this raises ``SheetsError("drive_unavailable")``.
        spreadsheet_id: The spreadsheet's Drive file id.
        include_resolved: When ``False``, comments whose ``resolved`` is truthy are omitted.
        include_deleted: Passed through to Drive's ``includeDeleted`` (default ``False``).

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "comments": [ {Comment §X.0g}, ... ]}``. Each entry
        is a :func:`serialize_comment` dict.

    Raises:
        SheetsError: ``"drive_unavailable"`` when no Drive service is available; or
            ``"google_api_error"`` (via :func:`classify_google_error`) on a Drive ``HttpError``.
    """
    if services.drive is None:
        raise SheetsError(
            "drive_unavailable",
            "Drive comments require a Drive API service, but none is available",
            hint="enable a Drive scope (GSHEETS_SCOPES=broad) — comments use the Drive API",
        )

    serialized: list[dict] = []
    page_token: str | None = None
    try:
        while True:
            request = services.drive.comments().list(
                fileId=spreadsheet_id,
                fields=COMMENTS_FIELDS,
                pageSize=_PAGE_SIZE,
                includeDeleted=include_deleted,
                pageToken=page_token,
            )
            resp = request.execute()

            for raw in resp.get("comments", []) or []:
                if not isinstance(raw, dict):
                    continue
                if not include_resolved and raw.get("resolved"):
                    continue
                serialized.append(serialize_comment(raw))

            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc

    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "comments": serialized,
    }
