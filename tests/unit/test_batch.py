"""Unit tests for ``gsheets.core.batch`` — the raw ``batchUpdate`` escape hatch (DESIGN §3.3).

All tests run against a MOCKED Sheets service — no network. A ``_Recorder`` captures the
kwargs/body sent to ``spreadsheets().batchUpdate`` so we can assert the OUTBOUND request body
is passed through verbatim (no reshaping, order preserved), and golden-master the serialized
RETURN dict (raw ``replies`` + captured ``newIds``).

``capture_new_ids`` is a sibling collaborator this unit calls but does NOT own (it lives in
``gsheets.core.structure`` and has its own tests); here we only assert that ``batch`` wires it
to the response's ``replies`` and surfaces the result under ``newIds``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gsheets.core.batch import batch
from gsheets.core.errors import SheetsError
from gsheets.core.service import SheetsServices

SHEET_ID = "<TEST_SHEET_ID>"


# --------------------------------------------------------------------------- helpers


class _Recorder:
    """Callable recording its kwargs; returns an object whose ``.execute()`` yields a
    queued response (or raises a queued exception). Lets a test assert exactly what was sent
    to Google's ``batchUpdate``."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0) if self._responses else {}
        request_obj = MagicMock(name="request")
        if isinstance(item, Exception):
            request_obj.execute.side_effect = item
        else:
            request_obj.execute.return_value = item
        return request_obj


def _make_service(*, account_email: str | None = None) -> SheetsServices:
    sheets = MagicMock(name="sheets_v4")
    return SheetsServices(sheets=sheets, drive=None, account_email=account_email)


def _wire_batch_update(services: SheetsServices, responses) -> _Recorder:
    """Attach a recorder to ``spreadsheets().batchUpdate`` and return it."""
    rec = _Recorder(responses)
    services.sheets.spreadsheets.return_value.batchUpdate = rec
    return rec


def _make_http_error(status: int = 400, *, reason: str = "INVALID_ARGUMENT", message: str = "bad request"):
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = status
    resp.reason = "Bad Request"
    content = (
        b'{"error": {"code": %d, "status": "%s", "message": "%s"}}'
        % (status, reason.encode(), message.encode())
    )
    return HttpError(resp=resp, content=content)


# =========================================================================== happy path / passthrough


class TestRequestPassthrough:
    def test_requests_passed_verbatim_to_batch_update(self):
        services = _make_service()
        rec = _wire_batch_update(services, [{"replies": []}])

        requests = [
            {"addSheet": {"properties": {"title": "New"}}},
            {"updateBorders": {"range": {"sheetId": 0}}},
        ]
        batch(services, SHEET_ID, requests)

        assert len(rec.calls) == 1
        call = rec.calls[0]
        assert call["spreadsheetId"] == SHEET_ID
        # Body is exactly {"requests": <the same list>} — no reshaping.
        assert call["body"] == {"requests": requests}

    def test_request_order_preserved(self):
        """Core must NOT sort/rewrite the requests — order is the caller's contract."""
        services = _make_service()
        rec = _wire_batch_update(services, [{"replies": []}])

        requests = [
            {"deleteSheet": {"sheetId": 9}},
            {"deleteSheet": {"sheetId": 3}},
            {"deleteSheet": {"sheetId": 7}},
        ]
        batch(services, SHEET_ID, requests)

        sent = rec.calls[0]["body"]["requests"]
        assert sent == requests
        assert [r["deleteSheet"]["sheetId"] for r in sent] == [9, 3, 7]

    def test_single_request(self):
        services = _make_service()
        rec = _wire_batch_update(services, [{"replies": [{}]}])

        requests = [{"unmergeCells": {"range": {"sheetId": 0}}}]
        out = batch(services, SHEET_ID, requests)

        assert rec.calls[0]["body"] == {"requests": requests}
        assert out["ok"] is True


class TestReturnShape:
    def test_return_envelope_keys(self):
        services = _make_service()
        _wire_batch_update(services, [{"replies": []}])

        out = batch(services, SHEET_ID, [{"unmergeCells": {"range": {"sheetId": 0}}}])

        assert set(out.keys()) == {"ok", "spreadsheetId", "replies", "newIds"}
        assert out["ok"] is True
        assert out["spreadsheetId"] == SHEET_ID

    def test_raw_replies_passed_through_untouched(self):
        services = _make_service()
        raw_replies = [
            {"addSheet": {"properties": {"sheetId": 42, "title": "X", "index": 1}}},
            {},  # an empty reply (e.g. updateBorders has no body)
            {"addNamedRange": {"namedRange": {"namedRangeId": "nr-1", "name": "cfg"}}},
        ]
        _wire_batch_update(services, [{"replies": raw_replies}])

        out = batch(services, SHEET_ID, [{"x": 1}, {"y": 2}, {"z": 3}])

        # replies are surfaced EXACTLY as Google returned them.
        assert out["replies"] == raw_replies

    def test_missing_replies_in_response_yields_empty_list(self):
        services = _make_service()
        _wire_batch_update(services, [{"spreadsheetId": SHEET_ID}])  # no "replies" key

        out = batch(services, SHEET_ID, [{"x": 1}])

        assert out["replies"] == []
        # newIds still has the full bucket shape (all empty).
        assert out["newIds"] == {
            "sheetIds": [],
            "chartIds": [],
            "namedRangeIds": [],
            "protectedRangeIds": [],
            "metadataIds": [],
            # v0.2 §X.3/§X.4/§X.9 — new add-id buckets.
            "tableIds": [],
            "bandedRangeIds": [],
            "filterViewIds": [],
            "slicerIds": [],
        }

    def test_null_replies_in_response_yields_empty_list(self):
        services = _make_service()
        _wire_batch_update(services, [{"replies": None}])

        out = batch(services, SHEET_ID, [{"x": 1}])

        assert out["replies"] == []


# =========================================================================== newIds capture


class TestNewIdsCapture:
    def test_new_sheet_id_captured(self):
        services = _make_service()
        replies = [{"addSheet": {"properties": {"sheetId": 7, "title": "New"}}}]
        _wire_batch_update(services, [{"replies": replies}])

        out = batch(services, SHEET_ID, [{"addSheet": {"properties": {"title": "New"}}}])

        assert out["newIds"]["sheetIds"] == [7]
        # Other buckets remain present and empty.
        assert out["newIds"]["chartIds"] == []
        assert out["newIds"]["namedRangeIds"] == []
        assert out["newIds"]["protectedRangeIds"] == []
        assert out["newIds"]["metadataIds"] == []

    def test_mixed_ids_captured_across_buckets(self):
        services = _make_service()
        replies = [
            {"addSheet": {"properties": {"sheetId": 11}}},
            {"addChart": {"chart": {"chartId": 99}}},
            {"addNamedRange": {"namedRange": {"namedRangeId": "nr-9"}}},
            {"addProtectedRange": {"protectedRange": {"protectedRangeId": 5}}},
            {"createDeveloperMetadata": {"developerMetadata": {"metadataId": 123}}},
        ]
        _wire_batch_update(services, [{"replies": replies}])

        out = batch(services, SHEET_ID, [{"a": 1}] * 5)

        assert out["newIds"] == {
            "sheetIds": [11],
            "chartIds": [99],
            "namedRangeIds": ["nr-9"],
            "protectedRangeIds": [5],
            "metadataIds": [123],
            "tableIds": [],
            "bandedRangeIds": [],
            "filterViewIds": [],
            "slicerIds": [],
        }

    def test_duplicate_sheet_id_captured_in_sheet_bucket(self):
        services = _make_service()
        replies = [{"duplicateSheet": {"properties": {"sheetId": 8, "title": "Copy"}}}]
        _wire_batch_update(services, [{"replies": replies}])

        out = batch(services, SHEET_ID, [{"duplicateSheet": {"sourceSheetId": 0}}])

        assert out["newIds"]["sheetIds"] == [8]

    def test_multiple_same_kind_ids_in_order(self):
        services = _make_service()
        replies = [
            {"addSheet": {"properties": {"sheetId": 1}}},
            {"addSheet": {"properties": {"sheetId": 2}}},
        ]
        _wire_batch_update(services, [{"replies": replies}])

        out = batch(services, SHEET_ID, [{"a": 1}, {"a": 1}])

        assert out["newIds"]["sheetIds"] == [1, 2]

    def test_no_id_bearing_replies_all_buckets_empty(self):
        services = _make_service()
        _wire_batch_update(services, [{"replies": [{}, {"updateCells": {}}]}])

        out = batch(services, SHEET_ID, [{"a": 1}, {"b": 2}])

        assert out["newIds"] == {
            "sheetIds": [],
            "chartIds": [],
            "namedRangeIds": [],
            "protectedRangeIds": [],
            "metadataIds": [],
            "tableIds": [],
            "bandedRangeIds": [],
            "filterViewIds": [],
            "slicerIds": [],
        }


# =========================================================================== validation guards


class TestValidationGuards:
    def test_empty_requests_raises_without_calling_api(self):
        services = _make_service()
        rec = _wire_batch_update(services, [{"replies": []}])

        with pytest.raises(SheetsError) as ei:
            batch(services, SHEET_ID, [])

        assert ei.value.code == "empty_payload"
        # The API must not be touched on a refused no-op.
        assert rec.calls == []

    def test_non_list_requests_raises(self):
        services = _make_service()
        rec = _wire_batch_update(services, [{"replies": []}])

        with pytest.raises(SheetsError) as ei:
            batch(services, SHEET_ID, {"addSheet": {}})  # a dict, not a list

        assert ei.value.code == "bad_request"
        assert rec.calls == []

    def test_none_requests_raises(self):
        services = _make_service()
        rec = _wire_batch_update(services, [{"replies": []}])

        with pytest.raises(SheetsError) as ei:
            batch(services, SHEET_ID, None)

        assert ei.value.code == "bad_request"
        assert rec.calls == []


# =========================================================================== error classification


class TestErrorClassification:
    def test_http_error_is_classified(self):
        services = _make_service()
        _wire_batch_update(services, [_make_http_error(400, reason="INVALID_ARGUMENT")])

        with pytest.raises(SheetsError) as ei:
            batch(services, SHEET_ID, [{"addSheet": {"properties": {"title": "x"}}}])

        err = ei.value
        # classify_google_error maps HttpError -> code "google_api_error" with status/reason.
        assert err.code == "google_api_error"
        assert err.status == 400
        assert err.reason == "INVALID_ARGUMENT"

    def test_403_error_classified_with_generic_hint(self):
        services = _make_service(account_email="operator@example.com")
        _wire_batch_update(services, [_make_http_error(403, reason="PERMISSION_DENIED")])

        with pytest.raises(SheetsError) as ei:
            batch(services, SHEET_ID, [{"x": 1}])

        err = ei.value
        assert err.status == 403
        # Default (non-verbose) mode: the operator email MUST NOT leak in the hint.
        assert "operator@example.com" not in (err.hint or "")
