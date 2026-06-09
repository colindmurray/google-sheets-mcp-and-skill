"""Unit tests for ``gsheets.core.charts`` (DESIGN §3.3 ``charts``, v1 scope).

All tests run against a MOCKED Sheets service — no network. A ``_Recorder`` captures the
request bodies passed to each Google API method so we golden-master the OUTBOUND request
shape (``addChart``/``updateChartSpec``/``deleteEmbeddedObject`` bodies, the flat-spec ->
``EmbeddedChartSpec`` translation, anchor -> ``GridCoordinate``) as well as the serialized
RETURN dicts (and the metadata-only ``read``).

Sibling collaborators this unit calls but does NOT own — ``a1_to_gridrange`` (addressing) and
``capture_new_ids`` (structure, still a stub on disk) — are PATCHED in the ``charts`` module
namespace so these tests stay isolated from those units' on-disk state, matching the project
test convention (see ``test_values.py``).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import sys

import gsheets.core.charts  # noqa: F401  (ensure the submodule is imported)
from gsheets.core.charts import charts

# ``gsheets.core.__init__`` re-exports the ``charts`` FUNCTION as the ``charts`` attribute on
# the ``gsheets.core`` package, which shadows the submodule attribute. Reach the real MODULE
# (to monkeypatch its sibling-collaborator names) via ``sys.modules``.
charts_mod = sys.modules["gsheets.core.charts"]
from gsheets.core.errors import SheetsError
from gsheets.core.service import SheetsServices


# --------------------------------------------------------------------------- helpers


class _Recorder:
    """A callable that records its kwargs and returns an object whose ``.execute()`` yields a
    queued response. Lets a test assert exactly what was sent to Google."""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        resp = self._responses.pop(0) if self._responses else {}
        request_obj = MagicMock(name="request")
        request_obj.execute.return_value = resp
        return request_obj


def _make_service(*, account_email: str | None = None) -> SheetsServices:
    sheets = MagicMock(name="sheets_v4")
    return SheetsServices(sheets=sheets, drive=None, account_email=account_email)


def _wire_spreadsheets_method(
    services: SheetsServices, method: str, responses: list[dict]
) -> _Recorder:
    """Attach a recorder to ``spreadsheets().<method>`` and return it."""
    rec = _Recorder(responses)
    setattr(services.sheets.spreadsheets.return_value, method, rec)
    return rec


def _make_http_error(status: int = 403):
    """A minimal stand-in for ``googleapiclient.errors.HttpError`` classify can read."""
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = status
    resp.reason = "Forbidden"
    content = (
        b'{"error": {"code": %d, "status": "PERMISSION_DENIED", "message": "nope"}}'
        % status
    )
    return HttpError(resp=resp, content=content)


@pytest.fixture
def patch_addressing(monkeypatch):
    """Patch ``a1_to_gridrange`` in the charts module to a deterministic stub.

    Maps a sheet name to a fixed ``sheetId`` and parses the bare ``A1`` part into 0-based
    indices, so we can golden-master the outbound GridRange/GridCoordinate shapes WITHOUT
    depending on the addressing unit's on-disk state or a wired ``spreadsheets.get``.
    """
    sheet_ids = {"Cliff": 0, "Sheet1": 0, "Data": 7, "My Sheet": 11}

    def _fake(services, spreadsheet_id, a1):
        sheet, _, rng = a1.partition("!")
        sheet = sheet.strip()
        if sheet.startswith("'") and sheet.endswith("'"):
            sheet = sheet[1:-1].replace("''", "'")
        sid = sheet_ids.get(sheet, 99)
        gr: dict = {"sheetId": sid}
        # Parse a simple "A1:B100" / "A1" range into 0-based half-open indices.
        start, _, end = rng.partition(":")
        end = end or start

        def _split(tok):
            i = 0
            while i < len(tok) and tok[i].isalpha():
                i += 1
            col_letters = tok[:i]
            row_digits = tok[i:]
            col = 0
            for ch in col_letters.upper():
                col = col * 26 + (ord(ch) - ord("A") + 1)
            col -= 1
            row = int(row_digits) - 1 if row_digits else None
            return col, row

        sc, sr = _split(start)
        ec, er = _split(end)
        if sr is not None and er is not None:
            gr["startRowIndex"] = min(sr, er)
            gr["endRowIndex"] = max(sr, er) + 1
        gr["startColumnIndex"] = min(sc, ec)
        gr["endColumnIndex"] = max(sc, ec) + 1
        return gr

    monkeypatch.setattr(charts_mod, "a1_to_gridrange", _fake)
    return sheet_ids


@pytest.fixture
def patch_capture(monkeypatch):
    """Patch ``capture_new_ids`` (structure unit; a stub on disk) to a real extractor.

    Mirrors the DESIGN §5.4 contract for the chart slice: pull ``chartId`` out of an
    ``addChart`` reply into ``{"chartIds": [...]}`` so ``create`` can surface the scalar id.
    """

    def _fake(replies):
        out: dict = {"sheetIds": [], "chartIds": [], "namedRangeIds": []}
        for reply in replies or []:
            add = (reply or {}).get("addChart")
            if add:
                cid = (add.get("chart") or {}).get("chartId")
                if cid is not None:
                    out["chartIds"].append(cid)
        return out

    monkeypatch.setattr(charts_mod, "capture_new_ids", _fake)
    return _fake


SHEET_ID = "<TEST_SHEET_ID>"


# =========================================================================== action guard


class TestActionGuard:
    def test_unknown_action_raises(self):
        svc = _make_service()
        with pytest.raises(SheetsError) as exc:
            charts(svc, SHEET_ID, action="frobnicate")
        assert exc.value.code == "unknown_action"

    def test_unknown_action_does_not_touch_api(self):
        svc = _make_service()
        with pytest.raises(SheetsError):
            charts(svc, SHEET_ID, action="bogus")
        svc.sheets.spreadsheets.return_value.batchUpdate.assert_not_called()


# =========================================================================== create


class TestCreate:
    def test_basic_line_chart_outbound_request_golden(
        self, patch_addressing, patch_capture
    ):
        svc = _make_service()
        rec = _wire_spreadsheets_method(
            svc,
            "batchUpdate",
            [{"replies": [{"addChart": {"chart": {"chartId": 99}}}]}],
        )

        out = charts(
            svc,
            SHEET_ID,
            action="create",
            spec={
                "title": "Reps over time",
                "type": "LINE",
                "series": ["Cliff!B1:B100", "Cliff!C1:C100"],
                "domain": "Cliff!A1:A100",
                "anchor": {"sheet": "Cliff", "row": 0, "col": 5},
            },
        )

        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "create",
            "chartId": 99,
        }

        # Exactly one batchUpdate, with the golden addChart body.
        assert len(rec.calls) == 1
        body = rec.calls[0]["body"]
        assert rec.calls[0]["spreadsheetId"] == SHEET_ID
        assert body == {
            "requests": [
                {
                    "addChart": {
                        "chart": {
                            "spec": {
                                "title": "Reps over time",
                                "basicChart": {
                                    "chartType": "LINE",
                                    "series": [
                                        {
                                            "series": {
                                                "sourceRange": {
                                                    "sources": [
                                                        {
                                                            "sheetId": 0,
                                                            "startRowIndex": 0,
                                                            "endRowIndex": 100,
                                                            "startColumnIndex": 1,
                                                            "endColumnIndex": 2,
                                                        }
                                                    ]
                                                }
                                            }
                                        },
                                        {
                                            "series": {
                                                "sourceRange": {
                                                    "sources": [
                                                        {
                                                            "sheetId": 0,
                                                            "startRowIndex": 0,
                                                            "endRowIndex": 100,
                                                            "startColumnIndex": 2,
                                                            "endColumnIndex": 3,
                                                        }
                                                    ]
                                                }
                                            }
                                        },
                                    ],
                                    "domains": [
                                        {
                                            "domain": {
                                                "sourceRange": {
                                                    "sources": [
                                                        {
                                                            "sheetId": 0,
                                                            "startRowIndex": 0,
                                                            "endRowIndex": 100,
                                                            "startColumnIndex": 0,
                                                            "endColumnIndex": 1,
                                                        }
                                                    ]
                                                }
                                            }
                                        }
                                    ],
                                },
                            },
                            "position": {
                                "overlayPosition": {
                                    "anchorCell": {
                                        "sheetId": 0,
                                        "rowIndex": 0,
                                        "columnIndex": 5,
                                    }
                                }
                            },
                        }
                    }
                }
            ]
        }

    def test_pie_chart_uses_pieChart_block(self, patch_addressing, patch_capture):
        svc = _make_service()
        _wire_spreadsheets_method(
            svc,
            "batchUpdate",
            [{"replies": [{"addChart": {"chart": {"chartId": 3}}}]}],
        )
        rec = svc.sheets.spreadsheets.return_value.batchUpdate

        out = charts(
            svc,
            SHEET_ID,
            action="create",
            spec={
                "type": "PIE",
                "series": ["Data!B1:B5"],
                "domain": "Data!A1:A5",
                "anchor": {"sheet": "Data", "row": 2, "col": 0},
            },
        )

        assert out["chartId"] == 3
        body = rec.calls[0]["body"]
        chart_spec = body["requests"][0]["addChart"]["chart"]["spec"]
        # No title key when not supplied.
        assert "title" not in chart_spec
        assert "basicChart" not in chart_spec
        assert chart_spec["pieChart"] == {
            "series": {
                "sourceRange": {
                    "sources": [
                        {
                            "sheetId": 7,
                            "startRowIndex": 0,
                            "endRowIndex": 5,
                            "startColumnIndex": 1,
                            "endColumnIndex": 2,
                        }
                    ]
                }
            },
            "domain": {
                "sourceRange": {
                    "sources": [
                        {
                            "sheetId": 7,
                            "startRowIndex": 0,
                            "endRowIndex": 5,
                            "startColumnIndex": 0,
                            "endColumnIndex": 1,
                        }
                    ]
                }
            },
        }
        anchor = body["requests"][0]["addChart"]["chart"]["position"][
            "overlayPosition"
        ]["anchorCell"]
        assert anchor == {"sheetId": 7, "rowIndex": 2, "columnIndex": 0}

    def test_domain_optional(self, patch_addressing, patch_capture):
        svc = _make_service()
        _wire_spreadsheets_method(
            svc, "batchUpdate", [{"replies": [{"addChart": {"chart": {"chartId": 1}}}]}]
        )
        rec = svc.sheets.spreadsheets.return_value.batchUpdate
        charts(
            svc,
            SHEET_ID,
            action="create",
            spec={
                "type": "COLUMN",
                "series": ["Cliff!B1:B10"],
                "anchor": {"sheet": "Cliff", "row": 0, "col": 0},
            },
        )
        basic = rec.calls[0]["body"]["requests"][0]["addChart"]["chart"]["spec"][
            "basicChart"
        ]
        assert basic["chartType"] == "COLUMN"
        assert "domains" not in basic

    def test_anchor_row_col_default_to_zero(self, patch_addressing, patch_capture):
        svc = _make_service()
        _wire_spreadsheets_method(
            svc, "batchUpdate", [{"replies": [{"addChart": {"chart": {"chartId": 1}}}]}]
        )
        rec = svc.sheets.spreadsheets.return_value.batchUpdate
        charts(
            svc,
            SHEET_ID,
            action="create",
            spec={
                "type": "BAR",
                "series": ["Cliff!B1:B10"],
                "anchor": {"sheet": "Cliff"},
            },
        )
        anchor = rec.calls[0]["body"]["requests"][0]["addChart"]["chart"]["position"][
            "overlayPosition"
        ]["anchorCell"]
        assert anchor == {"sheetId": 0, "rowIndex": 0, "columnIndex": 0}

    def test_quoted_sheet_name_resolves(self, patch_addressing, patch_capture):
        svc = _make_service()
        _wire_spreadsheets_method(
            svc, "batchUpdate", [{"replies": [{"addChart": {"chart": {"chartId": 1}}}]}]
        )
        rec = svc.sheets.spreadsheets.return_value.batchUpdate
        charts(
            svc,
            SHEET_ID,
            action="create",
            spec={
                "type": "AREA",
                "series": ["Cliff!B1:B10"],
                "anchor": {"sheet": "My Sheet", "row": 1, "col": 1},
            },
        )
        anchor = rec.calls[0]["body"]["requests"][0]["addChart"]["chart"]["position"][
            "overlayPosition"
        ]["anchorCell"]
        # "My Sheet" -> sheetId 11 via the always-quoted reference.
        assert anchor["sheetId"] == 11

    def test_missing_chart_id_in_reply_yields_none(
        self, patch_addressing, patch_capture
    ):
        svc = _make_service()
        _wire_spreadsheets_method(svc, "batchUpdate", [{"replies": []}])
        out = charts(
            svc,
            SHEET_ID,
            action="create",
            spec={
                "type": "LINE",
                "series": ["Cliff!B1:B10"],
                "anchor": {"sheet": "Cliff", "row": 0, "col": 0},
            },
        )
        assert out["chartId"] is None

    # ---- validation ----

    def test_missing_spec_raises_empty_payload(self):
        svc = _make_service()
        with pytest.raises(SheetsError) as exc:
            charts(svc, SHEET_ID, action="create")
        assert exc.value.code == "empty_payload"

    def test_empty_spec_raises_empty_payload(self):
        svc = _make_service()
        with pytest.raises(SheetsError) as exc:
            charts(svc, SHEET_ID, action="create", spec={})
        assert exc.value.code == "empty_payload"

    def test_unknown_spec_key_raises(self, patch_addressing):
        svc = _make_service()
        with pytest.raises(SheetsError) as exc:
            charts(
                svc,
                SHEET_ID,
                action="create",
                spec={
                    "type": "LINE",
                    "series": ["Cliff!B1:B10"],
                    "anchor": {"sheet": "Cliff"},
                    "subtitle": "nope",
                },
            )
        assert exc.value.code == "unknown_param"
        assert "subtitle" in exc.value.message

    def test_missing_type_raises(self):
        svc = _make_service()
        with pytest.raises(SheetsError) as exc:
            charts(
                svc,
                SHEET_ID,
                action="create",
                spec={"series": ["Cliff!B1:B10"], "anchor": {"sheet": "Cliff"}},
            )
        assert exc.value.code == "missing_param"

    def test_bad_type_raises(self):
        svc = _make_service()
        with pytest.raises(SheetsError) as exc:
            charts(
                svc,
                SHEET_ID,
                action="create",
                spec={
                    "type": "DONUT",
                    "series": ["Cliff!B1:B10"],
                    "anchor": {"sheet": "Cliff"},
                },
            )
        assert exc.value.code == "bad_chart_type"

    def test_missing_series_raises(self):
        svc = _make_service()
        with pytest.raises(SheetsError) as exc:
            charts(
                svc,
                SHEET_ID,
                action="create",
                spec={"type": "LINE", "series": [], "anchor": {"sheet": "Cliff"}},
            )
        assert exc.value.code == "missing_param"

    def test_missing_anchor_on_create_raises(self, patch_addressing):
        svc = _make_service()
        with pytest.raises(SheetsError) as exc:
            charts(
                svc,
                SHEET_ID,
                action="create",
                spec={"type": "LINE", "series": ["Cliff!B1:B10"]},
            )
        assert exc.value.code == "missing_param"
        assert "anchor" in exc.value.message

    def test_unknown_anchor_key_raises(self, patch_addressing):
        svc = _make_service()
        with pytest.raises(SheetsError) as exc:
            charts(
                svc,
                SHEET_ID,
                action="create",
                spec={
                    "type": "LINE",
                    "series": ["Cliff!B1:B10"],
                    "anchor": {"sheet": "Cliff", "row": 0, "column": 5},
                },
            )
        assert exc.value.code == "unknown_param"
        assert "column" in exc.value.message

    def test_anchor_missing_sheet_raises(self, patch_addressing):
        svc = _make_service()
        with pytest.raises(SheetsError) as exc:
            charts(
                svc,
                SHEET_ID,
                action="create",
                spec={
                    "type": "LINE",
                    "series": ["Cliff!B1:B10"],
                    "anchor": {"row": 0, "col": 0},
                },
            )
        assert exc.value.code == "missing_param"

    def test_http_error_is_classified(self, patch_addressing, patch_capture):
        svc = _make_service(account_email="bot@example.com")

        def _boom(**kwargs):
            req = MagicMock()
            req.execute.side_effect = _make_http_error(403)
            return req

        svc.sheets.spreadsheets.return_value.batchUpdate = _boom
        with pytest.raises(SheetsError) as exc:
            charts(
                svc,
                SHEET_ID,
                action="create",
                spec={
                    "type": "LINE",
                    "series": ["Cliff!B1:B10"],
                    "anchor": {"sheet": "Cliff", "row": 0, "col": 0},
                },
            )
        assert exc.value.code == "google_api_error"
        assert exc.value.status == 403


# =========================================================================== update


class TestUpdate:
    def test_update_outbound_request_golden(self, patch_addressing):
        svc = _make_service()
        rec = _wire_spreadsheets_method(svc, "batchUpdate", [{"replies": [{}]}])

        out = charts(
            svc,
            SHEET_ID,
            action="update",
            chart_id=42,
            spec={
                "title": "Updated",
                "type": "COLUMN",
                "series": ["Cliff!B1:B100"],
                "domain": "Cliff!A1:A100",
            },
        )

        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "update",
            "chartId": 42,
        }
        body = rec.calls[0]["body"]
        req = body["requests"][0]
        assert "updateChartSpec" in req
        assert req["updateChartSpec"]["chartId"] == 42
        spec = req["updateChartSpec"]["spec"]
        assert spec["title"] == "Updated"
        assert spec["basicChart"]["chartType"] == "COLUMN"
        # update needs NO anchor / position.
        assert "position" not in req["updateChartSpec"]

    def test_update_without_anchor_succeeds(self, patch_addressing):
        svc = _make_service()
        _wire_spreadsheets_method(svc, "batchUpdate", [{"replies": [{}]}])
        out = charts(
            svc,
            SHEET_ID,
            action="update",
            chart_id=7,
            spec={"type": "LINE", "series": ["Cliff!B1:B10"]},
        )
        assert out["chartId"] == 7

    def test_update_requires_chart_id(self):
        svc = _make_service()
        with pytest.raises(SheetsError) as exc:
            charts(
                svc,
                SHEET_ID,
                action="update",
                spec={"type": "LINE", "series": ["Cliff!B1:B10"]},
            )
        assert exc.value.code == "missing_chart_id"

    def test_update_requires_spec(self):
        svc = _make_service()
        with pytest.raises(SheetsError) as exc:
            charts(svc, SHEET_ID, action="update", chart_id=5)
        assert exc.value.code == "empty_payload"

    def test_update_validates_chart_id_before_spec(self):
        # chart_id missing should raise even though spec is also missing.
        svc = _make_service()
        with pytest.raises(SheetsError) as exc:
            charts(svc, SHEET_ID, action="update")
        assert exc.value.code == "missing_chart_id"


# =========================================================================== delete


class TestDelete:
    def test_delete_outbound_request_golden(self):
        svc = _make_service()
        rec = _wire_spreadsheets_method(svc, "batchUpdate", [{"replies": [{}]}])
        out = charts(svc, SHEET_ID, action="delete", chart_id=42)
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "delete",
            "chartId": 42,
        }
        assert rec.calls[0]["body"] == {
            "requests": [{"deleteEmbeddedObject": {"objectId": 42}}]
        }

    def test_delete_requires_chart_id(self):
        svc = _make_service()
        with pytest.raises(SheetsError) as exc:
            charts(svc, SHEET_ID, action="delete")
        assert exc.value.code == "missing_chart_id"

    def test_delete_http_error_classified(self):
        svc = _make_service()

        def _boom(**kwargs):
            req = MagicMock()
            req.execute.side_effect = _make_http_error(404)
            return req

        svc.sheets.spreadsheets.return_value.batchUpdate = _boom
        with pytest.raises(SheetsError) as exc:
            charts(svc, SHEET_ID, action="delete", chart_id=1)
        assert exc.value.code == "google_api_error"
        assert exc.value.status == 404


# =========================================================================== read (metadata)


# Golden Sheets-API response: two sheets, three charts spanning basicChart + pieChart, with a
# title-less chart and a chart anchored on a DIFFERENT sheet than it is listed under (anchorCell
# sheetId resolves via the spreadsheet-wide id->title map, not the containing sheet).
_READ_GOLDEN = {
    "sheets": [
        {
            "properties": {"sheetId": 0, "title": "Cliff"},
            "charts": [
                {
                    "chartId": 11,
                    "spec": {
                        "title": "Reps over time",
                        "basicChart": {"chartType": "LINE"},
                    },
                    "position": {
                        "overlayPosition": {
                            "anchorCell": {
                                "sheetId": 0,
                                "rowIndex": 4,
                                "columnIndex": 6,
                            }
                        }
                    },
                },
                {
                    "chartId": 12,
                    "spec": {"pieChart": {}},
                    "position": {
                        "overlayPosition": {
                            "anchorCell": {"sheetId": 0, "rowIndex": 0, "columnIndex": 0}
                        }
                    },
                },
            ],
        },
        {
            "properties": {"sheetId": 7, "title": "Data"},
            "charts": [
                {
                    "chartId": 13,
                    "spec": {"basicChart": {"chartType": "COLUMN"}},
                    "position": {
                        "overlayPosition": {
                            "anchorCell": {"sheetId": 7, "rowIndex": 2, "columnIndex": 1}
                        }
                    },
                }
            ],
        },
    ]
}


class TestRead:
    def test_read_all_sheets_metadata_golden(self):
        svc = _make_service()
        rec = _wire_spreadsheets_method(svc, "get", [_READ_GOLDEN])

        out = charts(svc, SHEET_ID, action="read")

        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "action": "read",
            "charts": [
                {
                    "chartId": 11,
                    "title": "Reps over time",
                    "type": "LINE",
                    "anchor": {"sheet": "Cliff", "row": 4, "col": 6},
                },
                {
                    "chartId": 12,
                    "title": None,
                    "type": "PIE",
                    "anchor": {"sheet": "Cliff", "row": 0, "col": 0},
                },
                {
                    "chartId": 13,
                    "title": None,
                    "type": "COLUMN",
                    "anchor": {"sheet": "Data", "row": 2, "col": 1},
                },
            ],
        }

    def test_read_uses_tight_metadata_only_mask(self):
        """The read mask must NOT pull the full EmbeddedChartSpec (token efficiency)."""
        svc = _make_service()
        rec = _wire_spreadsheets_method(svc, "get", [_READ_GOLDEN])
        charts(svc, SHEET_ID, action="read")
        fields = rec.calls[0]["fields"]
        assert "charts(chartId" in fields
        assert "spec.basicChart.chartType" in fields
        assert "spec.pieChart" in fields
        # Must NOT request the whole spec / whole basicChart body.
        assert "spec.basicChart.series" not in fields
        assert "spec.basicChart.domains" not in fields

    def test_read_filtered_to_one_sheet(self):
        svc = _make_service()
        _wire_spreadsheets_method(svc, "get", [_READ_GOLDEN])
        out = charts(svc, SHEET_ID, action="read", sheet="Data")
        assert [c["chartId"] for c in out["charts"]] == [13]

    def test_read_filter_nonexistent_sheet_yields_empty(self):
        svc = _make_service()
        _wire_spreadsheets_method(svc, "get", [_READ_GOLDEN])
        out = charts(svc, SHEET_ID, action="read", sheet="Ghost")
        assert out["charts"] == []

    def test_read_empty_spreadsheet(self):
        svc = _make_service()
        _wire_spreadsheets_method(svc, "get", [{}])
        out = charts(svc, SHEET_ID, action="read")
        assert out["charts"] == []

    def test_read_sheet_without_charts(self):
        svc = _make_service()
        _wire_spreadsheets_method(
            svc, "get", [{"sheets": [{"properties": {"sheetId": 0, "title": "Cliff"}}]}]
        )
        out = charts(svc, SHEET_ID, action="read")
        assert out["charts"] == []

    def test_read_chart_without_position(self):
        svc = _make_service()
        _wire_spreadsheets_method(
            svc,
            "get",
            [
                {
                    "sheets": [
                        {
                            "properties": {"sheetId": 0, "title": "Cliff"},
                            "charts": [
                                {
                                    "chartId": 5,
                                    "spec": {"basicChart": {"chartType": "SCATTER"}},
                                }
                            ],
                        }
                    ]
                }
            ],
        )
        out = charts(svc, SHEET_ID, action="read")
        assert out["charts"] == [
            {"chartId": 5, "title": None, "type": "SCATTER", "anchor": None}
        ]

    def test_read_http_error_classified(self):
        svc = _make_service()

        def _boom(**kwargs):
            req = MagicMock()
            req.execute.side_effect = _make_http_error(403)
            return req

        svc.sheets.spreadsheets.return_value.get = _boom
        with pytest.raises(SheetsError) as exc:
            charts(svc, SHEET_ID, action="read")
        assert exc.value.code == "google_api_error"
        assert exc.value.status == 403


# =========================================================================== round-trip-ish


class TestAnchorRoundTrip:
    def test_create_anchor_matches_read_anchor_shape(
        self, patch_addressing, patch_capture
    ):
        """The {sheet,row,col} anchor written on create is the SAME shape read back.

        (Charts are metadata-only on read by design, but the anchor sub-shape DOES round-trip
        — DESIGN §3.3 v1 read surface.)
        """
        anchor = {"sheet": "Cliff", "row": 4, "col": 6}
        svc = _make_service()
        rec = _wire_spreadsheets_method(
            svc,
            "batchUpdate",
            [{"replies": [{"addChart": {"chart": {"chartId": 11}}}]}],
        )
        charts(
            svc,
            SHEET_ID,
            action="create",
            spec={
                "type": "LINE",
                "series": ["Cliff!B1:B100"],
                "anchor": anchor,
            },
        )
        written = rec.calls[0]["body"]["requests"][0]["addChart"]["chart"]["position"][
            "overlayPosition"
        ]["anchorCell"]
        # Read side surfaces the same row/col back from a matching anchorCell.
        read_back = charts_mod._read_anchor(
            {"overlayPosition": {"anchorCell": written}}, {0: "Cliff"}
        )
        assert read_back == anchor
