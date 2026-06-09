"""Unit tests for ``gsheets.core.comments`` (DESIGN §X.0g, §X.5 — Feature #5, read-only v1).

Two halves, both against a MOCKED Drive service (NO network):

* :func:`serialize_comment` — GOLDEN-MASTER style: representative Google Drive ``Comment`` JSON
  in -> the EXACT terse, flattened, condformat-style dict out (``author/displayName`` flattened,
  ``quotedFileContent/value`` -> ``quoted``, replies flattened, opaque ``anchor`` -> ``anchorRaw``,
  terse ``line`` summary). Sparse comments emit only the present sub-keys (token-safe).
* :func:`comments` — the top-level core fn. A ``_DriveCommentsRecorder`` stands in for
  ``services.drive.comments().list(...)`` and asserts: the REQUIRED ``fields`` mask is ALWAYS sent,
  pagination follows ``nextPageToken`` across pages, ``include_resolved=False`` filters resolved
  comments, ``include_deleted`` rides through, ``services.drive is None`` -> ``drive_unavailable``,
  and a Drive ``HttpError`` is classified through the single error envelope.

This module is pure test scaffolding: stdlib + ``pytest`` only; it never imports
``fastmcp``/``mcp``/``argparse``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gsheets.core.comments import COMMENTS_FIELDS, comments, serialize_comment
from gsheets.core.errors import SheetsError
from gsheets.core.service import SheetsServices

SHEET_ID = "<TEST_SPREADSHEET_ID>"


# --------------------------------------------------------------------------- mock Drive service


class _DriveCommentsRecorder:
    """Stands in for ``services.drive.comments().list(**kwargs)``.

    Queues one response dict per page; each ``list(**kwargs)`` call records its kwargs and pops
    the next queued page off ``_pages`` on ``.execute()``. With multi-page fixtures the queued
    pages must carry the appropriate ``nextPageToken`` so the core fn keeps paginating.
    """

    def __init__(self, pages):
        self._pages = list(pages)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        resp = self._pages.pop(0) if self._pages else {}
        request_obj = MagicMock(name="comments_list_request")
        request_obj.execute.return_value = resp
        return request_obj


def _make_service(pages=None, *, with_drive=True, account_email=None):
    """Build a ``SheetsServices`` whose ``drive.comments().list`` routes to a recorder.

    ``with_drive=False`` builds a service with ``drive=None`` (no Drive scope).
    """
    sheets = MagicMock(name="sheets_v4")
    if not with_drive:
        return SheetsServices(sheets=sheets, drive=None, account_email=account_email), None
    drive = MagicMock(name="drive_v3")
    rec = _DriveCommentsRecorder(pages or [{}])
    drive.comments.return_value.list = rec
    services = SheetsServices(sheets=sheets, drive=drive, account_email=account_email)
    return services, rec


def _make_http_error(status: int = 403):
    """A minimal stand-in for a Drive ``googleapiclient.errors.HttpError``."""
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = status
    resp.reason = "Forbidden"
    content = (
        b'{"error": {"code": %d, "status": "PERMISSION_DENIED", "message": "nope"}}' % status
    )
    return HttpError(resp=resp, content=content)


# --------------------------------------------------------------------------- golden fixtures


def _golden_comment_full() -> dict:
    """A fully-populated Drive ``Comment`` (author, quoted snippet, anchor, two replies)."""
    return {
        "id": "AAAA",
        "content": "please verify Q3",
        "createdTime": "2026-05-01T10:00:00.000Z",
        "modifiedTime": "2026-05-02T11:30:00.000Z",
        "author": {"displayName": "Jane Doe"},
        "resolved": False,
        "anchor": '{"r":"abc","a":[{"line":1}]}',
        "quotedFileContent": {"value": "1234", "mimeType": "text/html"},
        "replies": [
            {"content": "looking", "author": {"displayName": "Bob"}},
            {"content": "done", "author": {"displayName": "Bob"}, "action": "resolve"},
        ],
    }


def _golden_comment_sparse() -> dict:
    """A minimal open Drive ``Comment``: id + content + author only, no replies/quote/anchor."""
    return {
        "id": "BBBB",
        "content": "typo here",
        "author": {"displayName": "Carol"},
    }


# =========================================================================== serialize golden


class TestSerializeCommentGolden:
    def test_full_comment_exact_shape(self):
        out = serialize_comment(_golden_comment_full())
        assert out == {
            "id": "AAAA",
            "author": "Jane Doe",
            "content": "please verify Q3",
            "created": "2026-05-01T10:00:00.000Z",
            "modified": "2026-05-02T11:30:00.000Z",
            "resolved": False,
            "quoted": "1234",
            "anchorRaw": '{"r":"abc","a":[{"line":1}]}',
            "replies": [
                {"author": "Bob", "content": "looking"},
                {"author": "Bob", "content": "done", "action": "resolve"},
            ],
            "line": 'comment AAAA by Jane Doe: "please verify Q3" (open, 2 replies)',
        }

    def test_sparse_comment_omits_absent_keys(self):
        out = serialize_comment(_golden_comment_sparse())
        assert out == {
            "id": "BBBB",
            "author": "Carol",
            "content": "typo here",
            "resolved": False,
            "line": 'comment BBBB by Carol: "typo here" (open, 0 replies)',
        }
        # Token-safe: no empty quoted/anchorRaw/replies/created/modified keys.
        for absent in ("quoted", "anchorRaw", "replies", "created", "modified"):
            assert absent not in out


# =========================================================================== serialize details


class TestSerializeCommentDetails:
    def test_resolved_true_renders_resolved_state(self):
        c = _golden_comment_sparse()
        c["resolved"] = True
        out = serialize_comment(c)
        assert out["resolved"] is True
        assert out["line"] == 'comment BBBB by Carol: "typo here" (resolved, 0 replies)'

    def test_resolved_defaults_false_when_absent(self):
        out = serialize_comment({"id": "X", "content": "c"})
        assert out["resolved"] is False

    def test_single_reply_is_singular_in_line(self):
        c = _golden_comment_sparse()
        c["replies"] = [{"content": "ok", "author": {"displayName": "Dave"}}]
        out = serialize_comment(c)
        assert out["line"].endswith("(open, 1 reply)")
        assert out["replies"] == [{"author": "Dave", "content": "ok"}]

    def test_anchor_surfaced_raw_never_as_a1(self):
        c = _golden_comment_sparse()
        c["anchor"] = "kix.opaque.anchor.string"
        out = serialize_comment(c)
        assert out["anchorRaw"] == "kix.opaque.anchor.string"
        # The opaque anchor must NEVER be promoted to a range/cell key.
        assert "range" not in out
        assert "a1" not in out
        assert "anchor" not in out  # only the explicitly-raw key is emitted

    def test_quoted_flattened_from_value(self):
        c = _golden_comment_sparse()
        c["quotedFileContent"] = {"value": "snippet text"}
        out = serialize_comment(c)
        assert out["quoted"] == "snippet text"

    def test_quoted_omitted_when_value_missing(self):
        c = _golden_comment_sparse()
        c["quotedFileContent"] = {"mimeType": "text/html"}
        out = serialize_comment(c)
        assert "quoted" not in out

    def test_author_missing_displayname_drops_author(self):
        out = serialize_comment({"id": "X", "content": "c", "author": {}})
        assert "author" not in out
        assert out["line"] == 'comment X: "c" (open, 0 replies)'

    def test_no_id_degrades_line_head(self):
        out = serialize_comment({"content": "c", "author": {"displayName": "Z"}})
        assert "id" not in out
        assert out["line"] == 'comment by Z: "c" (open, 0 replies)'

    def test_no_content_omits_body_in_line(self):
        out = serialize_comment({"id": "X", "author": {"displayName": "Z"}})
        assert "content" not in out
        assert out["line"] == "comment X by Z (open, 0 replies)"

    def test_reply_emits_only_present_subkeys(self):
        c = _golden_comment_sparse()
        c["replies"] = [
            {"content": "no author"},
            {"author": {"displayName": "E"}},
            {"action": "reopen", "author": {"displayName": "F"}, "content": "back"},
        ]
        out = serialize_comment(c)
        assert out["replies"] == [
            {"content": "no author"},
            {"author": "E"},
            {"author": "F", "content": "back", "action": "reopen"},
        ]

    def test_non_dict_replies_entries_skipped(self):
        c = _golden_comment_sparse()
        c["replies"] = ["not a dict", {"content": "ok"}]
        out = serialize_comment(c)
        assert out["replies"] == [{"content": "ok"}]

    def test_empty_replies_omitted(self):
        c = _golden_comment_sparse()
        c["replies"] = []
        out = serialize_comment(c)
        assert "replies" not in out
        assert out["line"].endswith("(open, 0 replies)")

    def test_non_dict_comment_raises(self):
        with pytest.raises(SheetsError) as exc:
            serialize_comment(["not", "a", "dict"])
        assert exc.value.code == "bad_comment"


# =========================================================================== required fields mask


class TestRequiredFieldsMask:
    def test_fields_mask_constant_shape(self):
        # The verified REQUIRED mask (DESIGN §X.0g): document-level subfields + nextPageToken.
        assert COMMENTS_FIELDS == (
            "comments("
            "id,content,createdTime,modifiedTime,author/displayName,resolved,anchor,"
            "quotedFileContent/value,replies(content,author/displayName,action)"
            "),nextPageToken"
        )

    def test_list_always_sends_required_fields_mask(self):
        services, rec = _make_service([{"comments": []}])
        comments(services, SHEET_ID)
        # Drive ERRORS without ``fields`` — it MUST be present on every page request.
        assert rec.calls[0]["fields"] == COMMENTS_FIELDS

    def test_list_passes_fileid_and_pagesize(self):
        services, rec = _make_service([{"comments": []}])
        comments(services, SHEET_ID)
        sent = rec.calls[0]
        assert sent["fileId"] == SHEET_ID
        assert sent["pageSize"] == 100


# =========================================================================== comments() behavior


class TestComments:
    def test_returns_ok_envelope_with_serialized_comments(self):
        services, _ = _make_service(
            [{"comments": [_golden_comment_full(), _golden_comment_sparse()]}]
        )
        out = comments(services, SHEET_ID)
        assert out["ok"] is True
        assert out["spreadsheetId"] == SHEET_ID
        assert [c["id"] for c in out["comments"]] == ["AAAA", "BBBB"]
        # Each entry is the flattened serialize_comment shape.
        assert out["comments"][0]["line"].startswith("comment AAAA by Jane Doe")

    def test_empty_when_no_comments(self):
        services, _ = _make_service([{"comments": []}])
        out = comments(services, SHEET_ID)
        assert out == {"ok": True, "spreadsheetId": SHEET_ID, "comments": []}

    def test_missing_comments_key_treated_as_empty(self):
        services, _ = _make_service([{}])
        out = comments(services, SHEET_ID)
        assert out["comments"] == []

    def test_non_dict_comment_entries_skipped(self):
        services, _ = _make_service(
            [{"comments": ["bad", _golden_comment_sparse()]}]
        )
        out = comments(services, SHEET_ID)
        assert [c["id"] for c in out["comments"]] == ["BBBB"]

    def test_pagination_follows_next_page_token(self):
        page1 = {
            "comments": [_golden_comment_full()],
            "nextPageToken": "TOK2",
        }
        page2 = {
            "comments": [_golden_comment_sparse()],
            # no nextPageToken -> stop
        }
        services, rec = _make_service([page1, page2])
        out = comments(services, SHEET_ID)
        # Both pages were fetched and concatenated in order.
        assert [c["id"] for c in out["comments"]] == ["AAAA", "BBBB"]
        assert len(rec.calls) == 2
        # First call has no page token; second carries the token from page 1.
        assert rec.calls[0]["pageToken"] is None
        assert rec.calls[1]["pageToken"] == "TOK2"

    def test_stops_when_next_page_token_empty_string(self):
        services, rec = _make_service(
            [{"comments": [_golden_comment_sparse()], "nextPageToken": ""}]
        )
        out = comments(services, SHEET_ID)
        # An empty-string token is falsy -> single page, no second request.
        assert len(rec.calls) == 1
        assert [c["id"] for c in out["comments"]] == ["BBBB"]

    def test_include_resolved_false_filters_resolved(self):
        resolved = _golden_comment_full()
        resolved["id"] = "RES"
        resolved["resolved"] = True
        open_c = _golden_comment_sparse()
        services, _ = _make_service([{"comments": [resolved, open_c]}])
        out = comments(services, SHEET_ID, include_resolved=False)
        assert [c["id"] for c in out["comments"]] == ["BBBB"]

    def test_include_resolved_true_keeps_resolved(self):
        resolved = _golden_comment_full()
        resolved["id"] = "RES"
        resolved["resolved"] = True
        services, _ = _make_service([{"comments": [resolved]}])
        out = comments(services, SHEET_ID, include_resolved=True)
        assert [c["id"] for c in out["comments"]] == ["RES"]
        assert out["comments"][0]["resolved"] is True

    def test_include_deleted_default_false(self):
        services, rec = _make_service([{"comments": []}])
        comments(services, SHEET_ID)
        assert rec.calls[0]["includeDeleted"] is False

    def test_include_deleted_true_rides_through(self):
        services, rec = _make_service([{"comments": []}])
        comments(services, SHEET_ID, include_deleted=True)
        assert rec.calls[0]["includeDeleted"] is True


# =========================================================================== error paths


class TestCommentsErrors:
    def test_drive_none_raises_drive_unavailable(self):
        services, _ = _make_service(with_drive=False)
        with pytest.raises(SheetsError) as exc:
            comments(services, SHEET_ID)
        assert exc.value.code == "drive_unavailable"
        assert "Drive" in (exc.value.hint or "")

    def test_drive_none_does_not_touch_sheets_or_drive(self):
        services, _ = _make_service(with_drive=False)
        with pytest.raises(SheetsError):
            comments(services, SHEET_ID)
        # No Sheets call should have been attempted (comments never use the Sheets API).
        services.sheets.spreadsheets.assert_not_called()

    def test_http_error_classified_through_envelope(self):
        services, rec = _make_service()
        # Make the list().execute() raise a Drive HttpError.
        bad_request = MagicMock(name="bad_request")
        bad_request.execute.side_effect = _make_http_error(403)
        services.drive.comments.return_value.list = MagicMock(return_value=bad_request)
        with pytest.raises(SheetsError) as exc:
            comments(services, SHEET_ID)
        assert exc.value.code == "google_api_error"
        assert exc.value.status == 403


# =========================================================================== purity guard


class TestPurity:
    def test_module_imports_no_transport(self):
        # The comments module must not drag fastmcp/mcp/argparse/pydantic/gsheets.models in.
        import sys

        import gsheets.core.comments  # noqa: F401

        forbidden = ("fastmcp", "mcp", "argparse", "pydantic", "gsheets.models")
        src = sys.modules["gsheets.core.comments"].__dict__
        for name in src.values():
            mod = getattr(name, "__module__", "")
            assert not any(mod.startswith(f) for f in forbidden if isinstance(mod, str))
