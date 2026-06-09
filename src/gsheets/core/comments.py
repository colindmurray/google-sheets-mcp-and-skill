"""Drive threaded-comments CRUD (DESIGN §X.0g, §X.5 — Feature #5).

This is the ONE core capability that does NOT touch the Sheets API — Google Sheets has no
comment surface, so threaded comments live on the **Drive v3** file resource and are read/written
via ``services.drive.comments()`` and ``services.drive.replies()`` against
``fileId=<spreadsheet_id>``.

Two pieces, both PURE core:

* :func:`serialize_comment` — GOLDEN-MASTER serializer. A Google Drive ``Comment`` JSON dict in
  -> a terse, flattened, condformat-style dict out. Flattens ``author/displayName`` ->
  ``author``, ``quotedFileContent/value`` -> ``quoted``, and each reply's ``author/displayName``
  + ``content`` + ``action``. Emits each sub-key ONLY when present (token-safe). A terse one-line
  ``line`` summary rides alongside, mirroring the existing serializer style
  (``comment AAAA by Jane Doe: "please verify Q3" (open, 1 reply)``).
* :func:`comments` — the top-level core fn, an ``action`` dispatch (default ``"read"``):
  ``read`` paginates ``comments.list`` (the ``fields`` mask is REQUIRED — omitting it ERRORS, a
  verified Drive gotcha), following ``nextPageToken`` until it is absent, optionally filtering
  out ``resolved`` comments, and returns the flattened list. ``create`` adds a top-level comment
  (returns it through the SAME serializer); ``reply`` posts a reply; ``resolve`` resolves a
  comment by posting a reply with ``action="resolve"`` (Drive has no standalone resolve
  endpoint); ``delete`` removes a comment (DESTRUCTIVE). EVERY action requires Drive.

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

# The single-comment fields mask for ``comments.create`` (DESIGN §X.5 write side). Drive ERRORS
# without ``fields`` here too; the subfields are exactly the (non-paginated) subset
# ``serialize_comment`` surfaces, so the created comment round-trips through the SAME serializer.
COMMENT_FIELDS = (
    "id,content,createdTime,modifiedTime,author/displayName,resolved,anchor,"
    "quotedFileContent/value,replies(content,author/displayName,action)"
)

# The reply fields mask for ``replies.create`` (create / resolve). ``action`` is the Drive reply
# verb that flipped the comment's resolved state (``"resolve"`` / ``"reopen"``).
_REPLY_FIELDS = "id,content,author/displayName,action"

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
        out.append(_serialize_reply(reply))
    return out


def _serialize_reply(reply: dict) -> dict:
    """Flatten ONE Drive ``Reply`` to ``{"author"?, "content"?, "action"?}`` (token-safe).

    Emits only the sub-keys present. Shared by the read path (each ``replies[]`` entry, via
    :func:`_serialize_replies`) and the write path (``reply`` / ``resolve`` returns) so a single
    reply is flattened IDENTICALLY everywhere. ``action`` is the Drive reply verb
    (``"resolve"`` / ``"reopen"``) when a reply changed the comment's resolved state.
    """
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
    return entry


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
# comments — top-level core fn (Drive v3 comments CRUD; read default)
# ===========================================================================

#: The full ``comments`` action surface (DESIGN §X.5). ``read`` (the default) is the original
#: paginated ``comments.list``; the rest are Drive write actions.
_ACTIONS: frozenset[str] = frozenset(
    {"read", "create", "reply", "resolve", "delete"}
)


def comments(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    action: str = "read",
    comment_id: str | None = None,
    content: str | None = None,
    anchor: str | None = None,
    include_resolved: bool = True,
    include_deleted: bool = False,
) -> dict:
    """Read or mutate a spreadsheet's Drive threaded comments (DESIGN §X.5, full CRUD).

    Uses the **Drive v3** API (``services.drive.comments()`` / ``services.drive.replies()``),
    NOT the Sheets API — Sheets has no comment surface. EVERY action (including ``read``)
    requires a Drive service; when ``services.drive`` is ``None`` this raises
    ``SheetsError("drive_unavailable")``. A Drive ``HttpError`` is always classified through the
    single error envelope (:func:`classify_google_error`).

    The ``action`` dispatch (default ``"read"`` preserves the original read-only behavior so
    existing callers never break):

    * ``"read"`` (DEFAULT) — paginated ``comments.list`` (the ``fields`` mask is REQUIRED and is
      always :data:`COMMENTS_FIELDS`); follows ``nextPageToken`` until absent. ``include_resolved``
      / ``include_deleted`` apply only here. Returns
      ``{"ok": True, "spreadsheetId": ..., "comments": [ {Comment §X.0g}, ... ]}``.
    * ``"create"`` — ``comments.create`` a new top-level comment (REQUIRES ``content``; optional
      opaque ``anchor``). Returns ``{"ok": True, "spreadsheetId": ..., "comment": {...}}`` where
      the comment is a :func:`serialize_comment` dict (it round-trips through the SAME serializer).
    * ``"reply"`` — ``replies.create`` a reply on ``comment_id`` (REQUIRES ``comment_id`` AND
      ``content``). Returns ``{"ok": True, "spreadsheetId": ..., "commentId": ...,
      "reply": {author?, content?, action?}}`` (the reply is flattened by the SAME helper a read
      uses for ``replies[]``).
    * ``"resolve"`` — resolve ``comment_id`` by posting a reply with ``action="resolve"``
      (REQUIRES ``comment_id``; optional ``content`` rides along as the reply body). Returns
      ``{"ok": True, "spreadsheetId": ..., "commentId": ..., "resolved": True, "reply": {...}}``.
    * ``"delete"`` — ``comments.delete`` ``comment_id`` (REQUIRES ``comment_id``). DESTRUCTIVE.
      Returns ``{"ok": True, "spreadsheetId": ..., "commentId": ..., "deleted": True}``.

    Args:
        services: The authed handle. ``services.drive`` MUST be non-``None`` for EVERY action —
            when it is ``None`` (no Drive scope granted), this raises
            ``SheetsError("drive_unavailable")``.
        spreadsheet_id: The spreadsheet's Drive file id.
        action: One of ``"read"`` / ``"create"`` / ``"reply"`` / ``"resolve"`` / ``"delete"``.
        comment_id: Target comment id (REQUIRED for ``reply`` / ``resolve`` / ``delete``).
        content: Comment / reply body (REQUIRED for ``create`` / ``reply``; optional for
            ``resolve``; ignored by ``read`` / ``delete``).
        anchor: Optional opaque Drive anchor string for ``create`` (passed through verbatim;
            never interpreted as an A1 range).
        include_resolved: ``read`` only — when ``False``, comments whose ``resolved`` is truthy
            are omitted.
        include_deleted: ``read`` only — passed through to Drive's ``includeDeleted``.

    Returns:
        A per-action ``ok`` envelope (see above).

    Raises:
        SheetsError: ``"drive_unavailable"`` when no Drive service is available;
            ``"bad_action"`` for an unknown ``action``; ``"missing_content"`` /
            ``"missing_comment_id"`` for a write missing its required argument; or
            ``"google_api_error"`` (via :func:`classify_google_error`) on a Drive ``HttpError``.
    """
    if action not in _ACTIONS:
        raise SheetsError(
            "bad_action",
            f"unknown comments action {action!r}; expected one of {sorted(_ACTIONS)}",
        )

    # EVERY action goes through Drive — comments never touch the Sheets API.
    if services.drive is None:
        raise SheetsError(
            "drive_unavailable",
            "Drive comments require a Drive API service, but none is available",
            hint="enable a Drive scope (GSHEETS_SCOPES=broad) — comments use the Drive API",
        )

    try:
        if action == "read":
            return _read_comments(
                services,
                spreadsheet_id,
                include_resolved=include_resolved,
                include_deleted=include_deleted,
            )
        if action == "create":
            return _create_comment(
                services, spreadsheet_id, content=content, anchor=anchor
            )
        if action == "reply":
            return _reply_comment(
                services, spreadsheet_id, comment_id=comment_id, content=content
            )
        if action == "resolve":
            return _resolve_comment(
                services, spreadsheet_id, comment_id=comment_id, content=content
            )
        # action == "delete" (DESTRUCTIVE)
        return _delete_comment(services, spreadsheet_id, comment_id=comment_id)
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc


def _read_comments(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    include_resolved: bool,
    include_deleted: bool,
) -> dict:
    """Paginate ``comments.list`` and return the flattened read envelope (DESIGN §X.5 read).

    The ``fields`` mask is REQUIRED (Drive errors without it) and is always sent as
    :data:`COMMENTS_FIELDS`. Each page follows ``nextPageToken`` until it is absent.
    ``include_resolved=False`` drops ``resolved`` comments in core (Drive has no server-side
    resolved filter on ``comments.list``).
    """
    serialized: list[dict] = []
    page_token: str | None = None
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

    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "comments": serialized,
    }


def _create_comment(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    content: str | None,
    anchor: str | None,
) -> dict:
    """Create a new top-level comment via ``comments.create`` (DESIGN §X.5 create).

    REQUIRES ``content`` (else ``missing_content``). The optional opaque ``anchor`` is passed
    through verbatim (never interpreted as a range). The created comment round-trips through the
    SAME :func:`serialize_comment` used on read, so its shape is identical.
    """
    if not content:
        raise SheetsError(
            "missing_content", "comments action 'create' requires content=<str>"
        )

    body: dict = {"content": content}
    if anchor:
        body["anchor"] = anchor

    resp = (
        services.drive.comments()
        .create(fileId=spreadsheet_id, body=body, fields=COMMENT_FIELDS)
        .execute()
    )
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "comment": serialize_comment(resp),
    }


def _reply_comment(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    comment_id: str | None,
    content: str | None,
) -> dict:
    """Post a reply on a comment via ``replies.create`` (DESIGN §X.5 reply).

    REQUIRES ``comment_id`` (else ``missing_comment_id``) AND ``content`` (else
    ``missing_content``). The reply is flattened by the SAME :func:`_serialize_reply` helper a
    read uses for each ``replies[]`` entry, so a reply return matches the read shape.
    """
    if not comment_id:
        raise SheetsError(
            "missing_comment_id", "comments action 'reply' requires comment_id=<str>"
        )
    if not content:
        raise SheetsError(
            "missing_content", "comments action 'reply' requires content=<str>"
        )

    resp = (
        services.drive.replies()
        .create(
            fileId=spreadsheet_id,
            commentId=comment_id,
            body={"content": content},
            fields=_REPLY_FIELDS,
        )
        .execute()
    )
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "commentId": comment_id,
        "reply": _serialize_reply(resp),
    }


def _resolve_comment(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    comment_id: str | None,
    content: str | None,
) -> dict:
    """Resolve a comment by posting a ``replies.create`` with ``action="resolve"`` (DESIGN §X.5).

    A Drive comment is resolved by one of its replies carrying ``action="resolve"`` — there is no
    standalone resolve endpoint. REQUIRES ``comment_id`` (else ``missing_comment_id``); ``content``
    is OPTIONAL and, when present, rides along as the resolving reply's body. The reply is
    flattened by the SAME :func:`_serialize_reply` helper used on read.
    """
    if not comment_id:
        raise SheetsError(
            "missing_comment_id", "comments action 'resolve' requires comment_id=<str>"
        )

    body: dict = {"action": "resolve"}
    if content:
        body["content"] = content

    resp = (
        services.drive.replies()
        .create(
            fileId=spreadsheet_id,
            commentId=comment_id,
            body=body,
            fields=_REPLY_FIELDS,
        )
        .execute()
    )
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "commentId": comment_id,
        "resolved": True,
        "reply": _serialize_reply(resp),
    }


def _delete_comment(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    comment_id: str | None,
) -> dict:
    """Delete a comment via ``comments.delete`` (DESIGN §X.5 delete). DESTRUCTIVE.

    REQUIRES ``comment_id`` (else ``missing_comment_id``). ``comments.delete`` returns an empty
    body on success, so the envelope just confirms the deletion.
    """
    if not comment_id:
        raise SheetsError(
            "missing_comment_id", "comments action 'delete' requires comment_id=<str>"
        )

    services.drive.comments().delete(
        fileId=spreadsheet_id, commentId=comment_id
    ).execute()
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "commentId": comment_id,
        "deleted": True,
    }
