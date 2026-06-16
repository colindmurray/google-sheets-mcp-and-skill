"""Unit tests for ``gsheets.core.multiread`` (DESIGN §3.3 — cross-file batch READ).

All tests run against a MOCKED Sheets service (NO network). ``read_many`` is a pure fan-out
over two sibling core fns — :func:`core.reads.overview` (``mode="summary"``) and
:func:`core.values.read_values` (``mode="values"``) — so we patch those siblings in the
``multiread`` namespace where they are called and assert ONLY the aggregation/dispatch logic
this unit owns:

* per-mode dispatch (which sibling is called, with which args, and the ``render`` override);
* the envelope shape (``ok``/``mode``/``count``/``results``) and that ``count == len(results)``;
* the HEADLINE per-item error capture: a ``SheetsError`` from ONE request becomes a captured
  ``{ok: False, error}`` entry while the OTHER requests still succeed (the batch never aborts);
* that every success result is self-identifying (carries its ``spreadsheetId``);
* up-front batch validation (``bad_requests`` / ``bad_mode``) — caller bugs that DO raise.

This module is pure test scaffolding: stdlib + ``pytest`` only; it never imports
``fastmcp``/``mcp``/``argparse``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gsheets.core import multiread as multiread_mod
from gsheets.core.errors import SheetsError
from gsheets.core.multiread import read_many
from gsheets.core.service import SheetsServices

SID_A = "<SPREADSHEET_A>"
SID_B = "<SPREADSHEET_B>"
SID_C = "<SPREADSHEET_C>"


# --------------------------------------------------------------------------- helpers


def _service() -> SheetsServices:
    """A SheetsServices whose Sheets handle is a bare MagicMock (siblings are patched)."""
    return SheetsServices(sheets=MagicMock(name="sheets_v4"), drive=None)


def _overview_result(sid: str) -> dict:
    """A canned ``overview`` success dict (already ``ok: True`` and self-identifying)."""
    return {
        "ok": True,
        "spreadsheetId": sid,
        "title": f"Title for {sid}",
        "sheets": [{"sheetId": 0, "title": "Sheet1"}],
        "namedRanges": [],
    }


def _values_result(sid: str) -> dict:
    """A canned ``read_values`` success dict (already ``ok: True`` and self-identifying)."""
    return {
        "ok": True,
        "spreadsheetId": sid,
        "render": "plain",
        "ranges": [{"range": "Sheet1!A1:B2", "values": [["x", "y"]]}],
    }


def _patch_overview(monkeypatch, side_effect):
    """Patch ``multiread.overview`` with a recording mock; return it."""
    mock = MagicMock(name="overview", side_effect=side_effect)
    monkeypatch.setattr(multiread_mod, "overview", mock)
    return mock


def _patch_read_values(monkeypatch, side_effect):
    """Patch ``multiread.read_values`` with a recording mock; return it."""
    mock = MagicMock(name="read_values", side_effect=side_effect)
    monkeypatch.setattr(multiread_mod, "read_values", mock)
    return mock


def _http_error(status: int = 404):
    """A SheetsError as classify_google_error would produce (the per-file failure shape)."""
    return SheetsError(
        "google_api_error",
        "Requested entity was not found.",
        status=status,
        reason="NOT_FOUND",
        hint="check the spreadsheet id / sheet name",
    )


# =========================================================================== values mode


class TestValuesMode:
    def test_aggregates_each_request_in_order(self, monkeypatch):
        services = _service()
        read_values = _patch_read_values(
            monkeypatch,
            side_effect=lambda svc, sid, ranges=None, *, render="plain", major="rows", data_filters=None: _values_result(sid),
        )
        out = read_many(
            services,
            [
                {"spreadsheetId": SID_A, "ranges": ["Sheet1!A1:B2"]},
                {"spreadsheetId": SID_B, "ranges": ["Sheet1!A1"]},
            ],
            mode="values",
        )
        assert out["ok"] is True
        assert out["mode"] == "values"
        assert out["count"] == 2
        assert [r["spreadsheetId"] for r in out["results"]] == [SID_A, SID_B]
        assert all(r["ok"] is True for r in out["results"])
        # read_values was called once per request, with that request's id + ranges.
        assert read_values.call_count == 2
        first_call = read_values.call_args_list[0]
        assert first_call.args[1] == SID_A
        assert first_call.args[2] == ["Sheet1!A1:B2"]

    def test_default_render_is_plain(self, monkeypatch):
        services = _service()
        read_values = _patch_read_values(
            monkeypatch,
            side_effect=lambda svc, sid, ranges=None, *, render="plain", major="rows", data_filters=None: _values_result(sid),
        )
        read_many(
            services,
            [{"spreadsheetId": SID_A, "ranges": ["A1"]}],
            mode="values",
        )
        assert read_values.call_args_list[0].kwargs["render"] == "plain"

    def test_per_request_render_override(self, monkeypatch):
        services = _service()
        captured: list[str] = []

        def _spy(svc, sid, ranges=None, *, render="plain", major="rows", data_filters=None):
            captured.append(render)
            return _values_result(sid)

        _patch_read_values(monkeypatch, side_effect=_spy)
        read_many(
            services,
            [
                {"spreadsheetId": SID_A, "ranges": ["A1"], "render": "formula"},
                {"spreadsheetId": SID_B, "ranges": ["A1"]},
            ],
            mode="values",
        )
        # First request's override is honored; second falls back to the default.
        assert captured == ["formula", "plain"]

    def test_per_request_major_override(self, monkeypatch):
        # SPEC §6 P3: a per-request "major" rides through to read_values.
        services = _service()
        captured: list[str] = []

        def _spy(svc, sid, ranges=None, *, render="plain", major="rows", data_filters=None):
            captured.append(major)
            return _values_result(sid)

        _patch_read_values(monkeypatch, side_effect=_spy)
        read_many(
            services,
            [
                {"spreadsheetId": SID_A, "ranges": ["A1"], "major": "columns"},
                {"spreadsheetId": SID_B, "ranges": ["A1"]},
            ],
            mode="values",
        )
        assert captured == ["columns", "rows"]

    def test_per_request_data_filters_passthrough(self, monkeypatch):
        # SPEC §6 P2: a request can carry data_filters INSTEAD of ranges; it rides through to
        # read_values, which reads via batchGetByDataFilter.
        services = _service()
        captured: list[dict] = []

        def _spy(svc, sid, ranges=None, *, render="plain", major="rows", data_filters=None):
            captured.append({"ranges": ranges, "data_filters": data_filters})
            return _values_result(sid)

        _patch_read_values(monkeypatch, side_effect=_spy)
        out = read_many(
            services,
            [
                {
                    "spreadsheetId": SID_A,
                    "data_filters": [{"developerMetadataLookup": {"metadataKey": "block:totals"}}],
                }
            ],
            mode="values",
        )
        assert out["results"][0]["ok"] is True
        assert captured[0]["ranges"] is None
        assert captured[0]["data_filters"] == [
            {"developerMetadataLookup": {"metadataKey": "block:totals"}}
        ]

    def test_values_request_missing_both_ranges_and_data_filters_raises(self, monkeypatch):
        # A values-mode request with neither ranges nor data_filters is a caller bug (bad_requests).
        services = _service()
        _patch_read_values(
            monkeypatch,
            side_effect=lambda svc, sid, ranges=None, *, render="plain", major="rows", data_filters=None: _values_result(sid),
        )
        with pytest.raises(SheetsError) as exc:
            read_many(services, [{"spreadsheetId": SID_A}], mode="values")
        assert exc.value.code == "bad_requests"

    def test_values_is_the_default_mode(self, monkeypatch):
        services = _service()
        read_values = _patch_read_values(
            monkeypatch,
            side_effect=lambda svc, sid, ranges=None, *, render="plain", major="rows", data_filters=None: _values_result(sid),
        )
        out = read_many(services, [{"spreadsheetId": SID_A, "ranges": ["A1"]}])
        assert out["mode"] == "values"
        assert read_values.call_count == 1

    def test_success_result_carries_spreadsheet_id_even_if_core_omits_it(
        self, monkeypatch
    ):
        services = _service()

        def _no_id(svc, sid, ranges=None, *, render="plain", major="rows", data_filters=None):
            # A (hypothetical) core result missing its id must still come back identified.
            return {"ok": True, "render": "plain", "ranges": []}

        _patch_read_values(monkeypatch, side_effect=_no_id)
        out = read_many(services, [{"spreadsheetId": SID_A, "ranges": ["A1"]}])
        assert out["results"][0]["spreadsheetId"] == SID_A

    def test_setdefault_does_not_clobber_cores_own_spreadsheet_id(self, monkeypatch):
        # ``result.setdefault("spreadsheetId", ...)`` must NOT overwrite an id the core fn already
        # put on the result — only fill it when absent. If a core result carries a different id,
        # that id is preserved verbatim (the request id is only a fallback identifier).
        services = _service()

        def _core_own_id(svc, sid, ranges=None, *, render="plain", major="rows", data_filters=None):
            return {"ok": True, "spreadsheetId": "CORE_REPORTED_ID", "ranges": []}

        _patch_read_values(monkeypatch, side_effect=_core_own_id)
        out = read_many(services, [{"spreadsheetId": SID_A, "ranges": ["A1"]}])
        assert out["results"][0]["spreadsheetId"] == "CORE_REPORTED_ID"


# =========================================================================== summary mode


class TestSummaryMode:
    def test_calls_overview_per_request(self, monkeypatch):
        services = _service()
        overview = _patch_overview(
            monkeypatch, side_effect=lambda svc, sid: _overview_result(sid)
        )
        read_values = _patch_read_values(monkeypatch, side_effect=AssertionError)
        out = read_many(
            services,
            [{"spreadsheetId": SID_A}, {"spreadsheetId": SID_B}],
            mode="summary",
        )
        assert out["mode"] == "summary"
        assert out["count"] == 2
        assert [r["spreadsheetId"] for r in out["results"]] == [SID_A, SID_B]
        assert overview.call_count == 2
        # summary mode never touches read_values.
        read_values.assert_not_called()

    def test_summary_ignores_ranges_and_does_not_require_them(self, monkeypatch):
        services = _service()
        _patch_overview(monkeypatch, side_effect=lambda svc, sid: _overview_result(sid))
        # No "ranges" key at all -> summary mode tolerates it (ranges only matter for values).
        out = read_many(services, [{"spreadsheetId": SID_A}], mode="summary")
        assert out["count"] == 1
        assert out["results"][0]["ok"] is True


# =========================================================================== per-item capture


class TestPerItemErrorCapture:
    def test_one_failure_is_captured_while_others_succeed(self, monkeypatch):
        services = _service()

        def _maybe_fail(svc, sid, ranges=None, *, render="plain", major="rows", data_filters=None):
            if sid == SID_B:
                raise _http_error(404)
            return _values_result(sid)

        _patch_read_values(monkeypatch, side_effect=_maybe_fail)
        out = read_many(
            services,
            [
                {"spreadsheetId": SID_A, "ranges": ["A1"]},
                {"spreadsheetId": SID_B, "ranges": ["A1"]},
                {"spreadsheetId": SID_C, "ranges": ["A1"]},
            ],
            mode="values",
        )
        # The whole batch still returns ok; count covers every request.
        assert out["ok"] is True
        assert out["count"] == 3
        results = out["results"]
        # A and C succeeded; B is a captured failure (the batch did NOT abort on B).
        assert results[0]["ok"] is True and results[0]["spreadsheetId"] == SID_A
        assert results[2]["ok"] is True and results[2]["spreadsheetId"] == SID_C
        captured = results[1]
        assert captured["ok"] is False
        assert captured["spreadsheetId"] == SID_B
        assert captured["error"]["code"] == "google_api_error"
        assert captured["error"]["status"] == 404
        assert captured["error"]["reason"] == "NOT_FOUND"

    def test_capture_in_summary_mode(self, monkeypatch):
        services = _service()

        def _maybe_fail(svc, sid):
            if sid == SID_A:
                raise _http_error(403)
            return _overview_result(sid)

        _patch_overview(monkeypatch, side_effect=_maybe_fail)
        out = read_many(
            services,
            [{"spreadsheetId": SID_A}, {"spreadsheetId": SID_B}],
            mode="summary",
        )
        assert out["count"] == 2
        assert out["results"][0] == {
            "spreadsheetId": SID_A,
            "ok": False,
            "error": {
                "code": "google_api_error",
                "message": "Requested entity was not found.",
                "status": 403,
                "reason": "NOT_FOUND",
                "hint": "check the spreadsheet id / sheet name",
            },
        }
        assert out["results"][1]["ok"] is True

    def test_all_requests_failing_still_returns_ok_envelope(self, monkeypatch):
        services = _service()
        _patch_read_values(
            monkeypatch,
            side_effect=lambda svc, sid, ranges=None, *, render="plain", major="rows", data_filters=None: (_ for _ in ()).throw(
                _http_error(404)
            ),
        )
        out = read_many(
            services,
            [
                {"spreadsheetId": SID_A, "ranges": ["A1"]},
                {"spreadsheetId": SID_B, "ranges": ["A1"]},
            ],
        )
        assert out["ok"] is True
        assert out["count"] == 2
        assert all(r["ok"] is False for r in out["results"])

    def test_error_entry_uses_to_dict_payload(self, monkeypatch):
        services = _service()
        err = SheetsError("bad_range", "no such range", hint="fix the A1")
        _patch_read_values(
            monkeypatch,
            side_effect=lambda svc, sid, ranges=None, *, render="plain", major="rows", data_filters=None: (_ for _ in ()).throw(err),
        )
        out = read_many(services, [{"spreadsheetId": SID_A, "ranges": ["A1"]}])
        assert out["results"][0]["error"] == err.to_dict()

    def test_captured_values_error_entry_exact_shape(self, monkeypatch):
        # Pin the FULL captured-failure envelope in values mode: {spreadsheetId, ok:False, error}
        # where ``error`` omits None fields (no status/reason here) but keeps the hint.
        services = _service()
        err = SheetsError("bad_range", "Cliff!ZZ is not a valid A1 range", hint="fix the A1")
        _patch_read_values(
            monkeypatch,
            side_effect=lambda svc, sid, ranges=None, *, render="plain", major="rows", data_filters=None: (_ for _ in ()).throw(err),
        )
        out = read_many(services, [{"spreadsheetId": SID_A, "ranges": ["Cliff!ZZ"]}])
        assert out["results"][0] == {
            "spreadsheetId": SID_A,
            "ok": False,
            "error": {
                "code": "bad_range",
                "message": "Cliff!ZZ is not a valid A1 range",
                "hint": "fix the A1",
            },
        }

    def test_non_sheets_error_is_not_swallowed(self, monkeypatch):
        # Only ``SheetsError`` is captured per-item; an unexpected exception (a real bug) must
        # propagate so it is never silently masked as a per-file failure.
        services = _service()

        def _boom(svc, sid, ranges=None, *, render="plain", major="rows", data_filters=None):
            raise KeyError("unexpected programmer error")

        _patch_read_values(monkeypatch, side_effect=_boom)
        with pytest.raises(KeyError):
            read_many(services, [{"spreadsheetId": SID_A, "ranges": ["A1"]}])


# =========================================================================== validation


class TestValidation:
    def test_non_list_requests_raises_bad_requests(self):
        services = _service()
        with pytest.raises(SheetsError) as exc:
            read_many(services, {"spreadsheetId": SID_A, "ranges": ["A1"]})
        assert exc.value.code == "bad_requests"

    def test_empty_requests_raises_bad_requests(self):
        services = _service()
        with pytest.raises(SheetsError) as exc:
            read_many(services, [])
        assert exc.value.code == "bad_requests"

    def test_request_item_not_a_dict_raises_bad_requests(self):
        services = _service()
        with pytest.raises(SheetsError) as exc:
            read_many(services, ["not a dict"])
        assert exc.value.code == "bad_requests"

    def test_missing_spreadsheet_id_raises_bad_requests(self):
        services = _service()
        with pytest.raises(SheetsError) as exc:
            read_many(services, [{"ranges": ["A1"]}])
        assert exc.value.code == "bad_requests"

    def test_empty_spreadsheet_id_raises_bad_requests(self):
        services = _service()
        with pytest.raises(SheetsError) as exc:
            read_many(services, [{"spreadsheetId": "", "ranges": ["A1"]}])
        assert exc.value.code == "bad_requests"

    def test_values_mode_missing_ranges_raises_bad_requests(self):
        services = _service()
        with pytest.raises(SheetsError) as exc:
            read_many(services, [{"spreadsheetId": SID_A}], mode="values")
        assert exc.value.code == "bad_requests"

    def test_unknown_mode_raises_bad_mode(self):
        services = _service()
        with pytest.raises(SheetsError) as exc:
            read_many(services, [{"spreadsheetId": SID_A}], mode="bogus")
        assert exc.value.code == "bad_mode"

    def test_validation_runs_up_front_before_any_sibling_call(self, monkeypatch):
        services = _service()
        read_values = _patch_read_values(
            monkeypatch,
            side_effect=lambda svc, sid, ranges=None, *, render="plain", major="rows", data_filters=None: _values_result(sid),
        )
        # The SECOND request is malformed (no spreadsheetId): the whole batch must reject
        # up front, so the FIRST (valid) request's sibling call must NOT have fired.
        with pytest.raises(SheetsError) as exc:
            read_many(
                services,
                [
                    {"spreadsheetId": SID_A, "ranges": ["A1"]},
                    {"ranges": ["A1"]},
                ],
            )
        assert exc.value.code == "bad_requests"
        read_values.assert_not_called()


# =========================================================================== purity guard


class TestPurity:
    def test_module_imports_no_transport(self):
        """multiread must not drag fastmcp/mcp/argparse/pydantic/gsheets.models in."""
        import sys

        import gsheets.core.multiread  # noqa: F401

        forbidden = ("fastmcp", "mcp", "argparse", "pydantic", "gsheets.models")
        src = sys.modules["gsheets.core.multiread"].__dict__
        for value in src.values():
            mod = getattr(value, "__module__", "")
            assert not any(
                mod.startswith(f) for f in forbidden if isinstance(mod, str)
            )


# ----------------------------------------------------------------- ISSUES.md #3 partialFailure


class TestPartialFailureSignal:
    def test_partial_failure_flag_when_one_inner_fails(self, monkeypatch):
        import gsheets.core.multiread as mr

        def fake_overview(services, sid):
            if sid == "BAD":
                raise SheetsError("google_api_error", "429", status=429)
            return {"ok": True, "spreadsheetId": sid, "title": "T", "sheets": []}

        monkeypatch.setattr(mr, "overview", fake_overview)
        out = mr.read_many(
            object(),
            [{"spreadsheetId": "GOOD"}, {"spreadsheetId": "BAD"}],
            mode="summary",
        )
        assert out["ok"] is True  # the batch ran
        assert out["partialFailure"] is True
        assert out["failed"] == 1
        assert out["succeeded"] == 1

    def test_no_partial_failure_when_all_succeed(self, monkeypatch):
        import gsheets.core.multiread as mr

        monkeypatch.setattr(
            mr, "overview", lambda s, sid: {"ok": True, "spreadsheetId": sid, "sheets": []}
        )
        out = mr.read_many(object(), [{"spreadsheetId": "A"}], mode="summary")
        assert out["partialFailure"] is False
        assert out["failed"] == 0
        assert out["succeeded"] == 1
