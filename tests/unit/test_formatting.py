"""Unit tests for ``gsheets.core.formatting`` (DESIGN §3.3 ``format``, §5.1).

All tests run against a MOCKED Sheets service — no network. A ``_Recorder`` captures the
request body passed to ``spreadsheets().batchUpdate(...)`` so we can golden-master the OUTBOUND
request shape (the flat -> Google ``repeatCell.cell.userEnteredFormat`` translation, the
``updateBorders`` body, the auto-built ``fields`` mask, and the all-or-nothing single
``batchUpdate``) as well as the serialized RETURN dict.

The sibling collaborator ``a1_to_gridrange`` (implemented by another build unit) is PATCHED to
a deterministic stub so these tests stay isolated from that unit's on-disk state. ``colors``,
``fieldsmask``, and ``errors`` are real leaves and exercised genuinely.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gsheets.core import formatting as formatting_mod
from gsheets.core.errors import SheetsError
from gsheets.core.formatting import format as core_format
from gsheets.core.service import SheetsServices

SHEET_ID = "<TEST_SHEET_ID>"

# Deterministic GridRange the patched ``a1_to_gridrange`` returns for "Cliff!A1:A10".
GR = {
    "sheetId": 0,
    "startRowIndex": 0,
    "endRowIndex": 10,
    "startColumnIndex": 0,
    "endColumnIndex": 1,
}


# --------------------------------------------------------------------------- helpers


class _Recorder:
    """A callable recording its kwargs; ``.execute()`` yields a queued response."""

    def __init__(self, responses: list[dict] | None = None):
        self._responses = list(responses or [{}])
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        resp = self._responses.pop(0) if self._responses else {}
        request_obj = MagicMock(name="request")
        request_obj.execute.return_value = resp
        return request_obj


def _make_service(*, account_email: str | None = None):
    sheets = MagicMock(name="sheets_v4")
    services = SheetsServices(sheets=sheets, drive=None, account_email=account_email)
    return services


def _wire_batch_update(services: SheetsServices, responses: list[dict] | None = None) -> _Recorder:
    """Attach a recorder to ``spreadsheets().batchUpdate`` and return it."""
    rec = _Recorder(responses)
    services.sheets.spreadsheets.return_value.batchUpdate = rec
    return rec


@pytest.fixture(autouse=True)
def _patch_addressing(monkeypatch):
    """Patch ``a1_to_gridrange`` (sibling unit) to a deterministic stub.

    Returns :data:`GR` for the canonical range and records the (spreadsheet_id, a1) it was
    called with so tests can assert the range was resolved.
    """
    calls: list[tuple] = []

    def _fake(services, spreadsheet_id, a1):
        calls.append((spreadsheet_id, a1))
        return dict(GR)

    monkeypatch.setattr(formatting_mod, "a1_to_gridrange", _fake)
    return calls


def _single_request(rec: _Recorder) -> list[dict]:
    """Assert exactly ONE batchUpdate call and return its ``requests`` list."""
    assert len(rec.calls) == 1, f"expected one batchUpdate, got {len(rec.calls)}"
    body = rec.calls[0]["body"]
    return body["requests"]


def _make_http_error(status: int = 403):
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = status
    resp.reason = "Forbidden"
    content = (
        b'{"error": {"code": %d, "status": "PERMISSION_DENIED", "message": "nope"}}' % status
    )
    return HttpError(resp=resp, content=content)


# =========================================================================== translation


class TestCellFormatTranslation:
    """Golden-master the flat -> Google ``repeatCell.cell.userEnteredFormat`` translation."""

    def test_background_color_to_color_style(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        core_format(services, SHEET_ID, "Cliff!A1:A10", {"bg": "#FFCDD2"})
        reqs = _single_request(rec)
        assert len(reqs) == 1
        cell = reqs[0]["repeatCell"]["cell"]
        assert cell == {
            "userEnteredFormat": {
                "backgroundColorStyle": {
                    "rgbColor": {"red": 1.0, "green": 0xCD / 255, "blue": 0xD2 / 255}
                }
            }
        }
        assert reqs[0]["repeatCell"]["fields"] == "userEnteredFormat.backgroundColorStyle"
        assert reqs[0]["repeatCell"]["range"] == GR

    def test_text_styles_lift_into_text_format(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        core_format(
            services,
            SHEET_ID,
            "Cliff!A1:A10",
            {"bold": True, "italic": False, "fontSize": 12, "fontFamily": "Arial"},
        )
        cell = _single_request(rec)[0]["repeatCell"]["cell"]
        assert cell == {
            "userEnteredFormat": {
                "textFormat": {
                    "bold": True,
                    "italic": False,
                    "fontSize": 12,
                    "fontFamily": "Arial",
                }
            }
        }

    def test_foreground_color_becomes_text_format_color_style(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        core_format(services, SHEET_ID, "Cliff!A1:A10", {"fg": "#1B5E20", "bold": True})
        cell = _single_request(rec)[0]["repeatCell"]["cell"]
        tf = cell["userEnteredFormat"]["textFormat"]
        assert tf["bold"] is True
        assert tf["foregroundColorStyle"] == {
            "rgbColor": {"red": 0x1B / 255, "green": 0x5E / 255, "blue": 0x20 / 255}
        }

    def test_number_format_with_explicit_type(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        core_format(
            services,
            SHEET_ID,
            "Cliff!A1:A10",
            {"numberFormat": "0.00%", "numberFormatType": "PERCENT"},
        )
        cell = _single_request(rec)[0]["repeatCell"]["cell"]
        assert cell == {
            "userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.00%"}}
        }

    def test_number_format_pattern_only_defaults_type_number(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        core_format(services, SHEET_ID, "Cliff!A1:A10", {"numberFormat": "#,##0"})
        cell = _single_request(rec)[0]["repeatCell"]["cell"]
        assert cell["userEnteredFormat"]["numberFormat"] == {
            "type": "NUMBER",
            "pattern": "#,##0",
        }

    def test_number_format_type_only_no_pattern(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        core_format(services, SHEET_ID, "Cliff!A1:A10", {"numberFormatType": "TEXT"})
        cell = _single_request(rec)[0]["repeatCell"]["cell"]
        assert cell["userEnteredFormat"]["numberFormat"] == {"type": "TEXT"}

    def test_alignment_and_wrap_scalars(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        core_format(
            services,
            SHEET_ID,
            "Cliff!A1:A10",
            {"halign": "CENTER", "valign": "MIDDLE", "wrap": "WRAP"},
        )
        cell = _single_request(rec)[0]["repeatCell"]["cell"]
        assert cell == {
            "userEnteredFormat": {
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "WRAP",
            }
        }

    def test_padding_atomic_leaf(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        core_format(
            services, SHEET_ID, "Cliff!A1:A10", {"padding": {"top": 2, "left": 3}}
        )
        req = _single_request(rec)[0]["repeatCell"]
        assert req["cell"] == {"userEnteredFormat": {"padding": {"top": 2, "left": 3}}}
        # padding is an atomic leaf: mask stops at the parent.
        assert req["fields"] == "userEnteredFormat.padding"

    def test_text_rotation_atomic_leaf(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        core_format(services, SHEET_ID, "Cliff!A1:A10", {"textRotation": {"angle": 45}})
        req = _single_request(rec)[0]["repeatCell"]
        assert req["cell"] == {"userEnteredFormat": {"textRotation": {"angle": 45}}}
        assert req["fields"] == "userEnteredFormat.textRotation"

    def test_theme_color_background(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        core_format(services, SHEET_ID, "Cliff!A1:A10", {"bg": "theme:ACCENT1"})
        cell = _single_request(rec)[0]["repeatCell"]["cell"]
        assert cell["userEnteredFormat"]["backgroundColorStyle"] == {"themeColor": "ACCENT1"}


# =========================================================================== note


class TestNote:
    def test_note_is_cell_level_sibling(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        out = core_format(services, SHEET_ID, "Cliff!A1:A10", {"note": "reviewed"})
        req = _single_request(rec)[0]["repeatCell"]
        assert req["cell"] == {"note": "reviewed"}
        assert req["fields"] == "note"
        assert out["appliedFields"] == "note"

    def test_note_alongside_format_sibling_mask(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        out = core_format(
            services,
            SHEET_ID,
            "Cliff!A1:A10",
            {"bg": "#FFCDD2", "bold": True, "note": "reviewed"},
        )
        req = _single_request(rec)[0]["repeatCell"]
        assert req["cell"] == {
            "userEnteredFormat": {
                "backgroundColorStyle": {
                    "rgbColor": {"red": 1.0, "green": 0xCD / 255, "blue": 0xD2 / 255}
                },
                "textFormat": {"bold": True},
            },
            "note": "reviewed",
        }
        # note is a sibling token after the userEnteredFormat group.
        assert req["fields"] == (
            "userEnteredFormat(backgroundColorStyle,textFormat.bold),note"
        )
        assert out["appliedFields"] == req["fields"]

    def test_empty_string_note_clears_note(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        core_format(services, SHEET_ID, "Cliff!A1:A10", {"note": ""})
        req = _single_request(rec)[0]["repeatCell"]
        assert req["cell"] == {"note": ""}
        assert req["fields"] == "note"


# =========================================================================== borders


class TestBorders:
    def test_single_border_via_update_borders(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        out = core_format(
            services, SHEET_ID, "Cliff!A1:A10", {"borders": {"top": "SOLID #000000"}}
        )
        reqs = _single_request(rec)
        # ONLY an updateBorders request (no repeatCell, since no userEnteredFormat/note).
        assert len(reqs) == 1
        ub = reqs[0]["updateBorders"]
        assert ub == {
            "range": GR,
            "top": {
                "style": "SOLID",
                "colorStyle": {"rgbColor": {"red": 0.0, "green": 0.0, "blue": 0.0}},
            },
        }
        assert out["appliedFields"] == "borders"

    def test_border_style_only_no_color(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        core_format(services, SHEET_ID, "Cliff!A1:A10", {"borders": {"bottom": "SOLID"}})
        ub = _single_request(rec)[0]["updateBorders"]
        assert ub == {"range": GR, "bottom": {"style": "SOLID"}}

    def test_border_theme_color(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        core_format(
            services, SHEET_ID, "Cliff!A1:A10", {"borders": {"left": "DASHED theme:TEXT"}}
        )
        ub = _single_request(rec)[0]["updateBorders"]
        assert ub["left"] == {"style": "DASHED", "colorStyle": {"themeColor": "TEXT"}}

    def test_all_four_sides(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        core_format(
            services,
            SHEET_ID,
            "Cliff!A1:A10",
            {
                "borders": {
                    "top": "SOLID #000000",
                    "bottom": "SOLID #000000",
                    "left": "SOLID #000000",
                    "right": "SOLID #000000",
                }
            },
        )
        ub = _single_request(rec)[0]["updateBorders"]
        assert set(ub) == {"range", "top", "bottom", "left", "right"}

    def test_unknown_border_side_rejected(self):
        services = _make_service()
        _wire_batch_update(services)
        with pytest.raises(SheetsError) as exc:
            core_format(
                services, SHEET_ID, "Cliff!A1:A10", {"borders": {"diagonal": "SOLID #000000"}}
            )
        assert exc.value.code == "bad_format"

    def test_bad_border_color_rejected(self):
        services = _make_service()
        _wire_batch_update(services)
        with pytest.raises(SheetsError) as exc:
            core_format(
                services, SHEET_ID, "Cliff!A1:A10", {"borders": {"top": "SOLID notacolor"}}
            )
        assert exc.value.code == "bad_format"

    def test_malformed_border_string_too_many_parts(self):
        services = _make_service()
        _wire_batch_update(services)
        with pytest.raises(SheetsError) as exc:
            core_format(
                services, SHEET_ID, "Cliff!A1:A10", {"borders": {"top": "SOLID #000 extra"}}
            )
        assert exc.value.code == "bad_format"


# =========================================================================== atomicity


class TestAtomicBatchUpdate:
    """Format + borders MUST be ONE batchUpdate (all-or-nothing) (DESIGN §5.1(4))."""

    def test_format_and_borders_in_one_batch_update(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        out = core_format(
            services,
            SHEET_ID,
            "Cliff!A1:A10",
            {"bg": "#FFCDD2", "bold": True, "borders": {"top": "SOLID #000000"}},
        )
        reqs = _single_request(rec)  # asserts exactly ONE batchUpdate
        kinds = [next(iter(r)) for r in reqs]
        # repeatCell first, then updateBorders, in the SAME requests[] list.
        assert kinds == ["repeatCell", "updateBorders"]
        assert out["appliedFields"] == (
            "userEnteredFormat(backgroundColorStyle,textFormat.bold),borders"
        )

    def test_never_two_separate_api_calls(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        core_format(
            services,
            SHEET_ID,
            "Cliff!A1:A10",
            {"bg": "#FFCDD2", "borders": {"top": "SOLID #000000"}},
        )
        # Exactly one outbound batchUpdate — never two (which could partial-fail).
        assert len(rec.calls) == 1

    def test_note_plus_borders_one_batch(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        out = core_format(
            services,
            SHEET_ID,
            "Cliff!A1:A10",
            {"note": "x", "borders": {"top": "SOLID #000000"}},
        )
        reqs = _single_request(rec)
        kinds = [next(iter(r)) for r in reqs]
        assert kinds == ["repeatCell", "updateBorders"]
        assert out["appliedFields"] == "note,borders"


# =========================================================================== fields mask


class TestAutoFieldsMask:
    """The fields mask is auto-built from the payload (golden-master, DESIGN §5.1)."""

    def test_full_payload_group_mask(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        out = core_format(
            services,
            SHEET_ID,
            "Cliff!A1:A10",
            {
                "bg": "#FFCDD2",
                "bold": True,
                "numberFormat": "0.00%",
                "numberFormatType": "PERCENT",
                "padding": {"top": 2, "right": 3, "bottom": 2, "left": 3},
            },
        )
        req = _single_request(rec)[0]["repeatCell"]
        assert req["fields"] == (
            "userEnteredFormat(backgroundColorStyle,textFormat.bold,numberFormat,padding)"
        )
        assert out["appliedFields"] == req["fields"]

    def test_text_format_multiple_children_group(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        core_format(
            services,
            SHEET_ID,
            "Cliff!A1:A10",
            {"bold": True, "italic": True, "fg": "#1B5E20"},
        )
        req = _single_request(rec)[0]["repeatCell"]
        # Insertion order: fg (foregroundColorStyle) is built before the scalar text styles,
        # and build_fields_mask preserves insertion order.
        assert req["fields"] == (
            "userEnteredFormat.textFormat(foregroundColorStyle,bold,italic)"
        )


# =========================================================================== return shape


class TestReturnShape:
    def test_return_dict_shape(self):
        services = _make_service()
        _wire_batch_update(services)
        out = core_format(services, SHEET_ID, "Cliff!A1:A10", {"bg": "#FFCDD2"})
        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "range": "Cliff!A1:A10",
            "appliedFields": "userEnteredFormat.backgroundColorStyle",
        }

    def test_range_echoed_verbatim(self):
        services = _make_service()
        _wire_batch_update(services)
        out = core_format(services, SHEET_ID, "Cliff!B2:D5", {"bold": True})
        assert out["range"] == "Cliff!B2:D5"

    def test_resolves_range_via_addressing(self, _patch_addressing):
        services = _make_service()
        _wire_batch_update(services)
        core_format(services, SHEET_ID, "Cliff!A1:A10", {"bold": True})
        assert _patch_addressing == [(SHEET_ID, "Cliff!A1:A10")]


# =========================================================================== validation/errors


class TestValidationAndErrors:
    def test_empty_fmt_raises_empty_payload(self):
        services = _make_service()
        _wire_batch_update(services)
        with pytest.raises(SheetsError) as exc:
            core_format(services, SHEET_ID, "Cliff!A1:A10", {})
        assert exc.value.code == "empty_payload"

    def test_non_dict_fmt_raises_empty_payload(self):
        services = _make_service()
        _wire_batch_update(services)
        with pytest.raises(SheetsError) as exc:
            core_format(services, SHEET_ID, "Cliff!A1:A10", None)  # type: ignore[arg-type]
        assert exc.value.code == "empty_payload"

    def test_unknown_key_rejected(self):
        services = _make_service()
        _wire_batch_update(services)
        with pytest.raises(SheetsError) as exc:
            core_format(services, SHEET_ID, "Cliff!A1:A10", {"colour": "#FFF"})
        assert exc.value.code == "unknown_param"

    def test_fmt_with_only_none_values_is_a_noop(self):
        # All recognized keys but every value is None -> no writable field.
        services = _make_service()
        _wire_batch_update(services)
        with pytest.raises(SheetsError) as exc:
            core_format(services, SHEET_ID, "Cliff!A1:A10", {"bg": None, "bold": None})
        assert exc.value.code == "empty_payload"

    def test_padding_must_be_dict(self):
        services = _make_service()
        _wire_batch_update(services)
        with pytest.raises(SheetsError) as exc:
            core_format(services, SHEET_ID, "Cliff!A1:A10", {"padding": "2px"})
        assert exc.value.code == "bad_format"

    def test_http_error_classified(self):
        services = _make_service(account_email="op@example.com")
        rec = _Recorder()

        def _raise(**kwargs):
            request = MagicMock()
            request.execute.side_effect = _make_http_error(403)
            return request

        services.sheets.spreadsheets.return_value.batchUpdate = _raise
        with pytest.raises(SheetsError) as exc:
            core_format(services, SHEET_ID, "Cliff!A1:A10", {"bg": "#FFCDD2"})
        assert exc.value.code == "google_api_error"
        assert exc.value.status == 403
        # 403 hint is generic by default; never embeds the operator email.
        assert "op@example.com" not in (exc.value.hint or "")

    def test_no_batch_update_issued_on_validation_failure(self):
        services = _make_service()
        rec = _wire_batch_update(services)
        with pytest.raises(SheetsError):
            core_format(services, SHEET_ID, "Cliff!A1:A10", {})
        assert rec.calls == []


# =========================================================================== round-trip symmetry


class TestRoundTripSymmetry:
    """The border write form mirrors what ``flatten_cell_format`` reads back (CRUD symmetry)."""

    def test_border_write_form_matches_flatten_read_form(self):
        from gsheets.core.flatten import flatten_cell_format

        services = _make_service()
        rec = _wire_batch_update(services)
        # Write "SOLID #000000" on top.
        core_format(services, SHEET_ID, "Cliff!A1:A10", {"borders": {"top": "SOLID #000000"}})
        ub = _single_request(rec)[0]["updateBorders"]
        google_border = ub["top"]
        # Feed the Google Border back through the read-side flattener: same string.
        read_back = flatten_cell_format({"borders": {"top": google_border}})
        assert read_back == {"borders": {"top": "SOLID #000000"}}

    def test_user_entered_format_write_matches_flatten_read(self):
        from gsheets.core.flatten import flatten_cell_format

        services = _make_service()
        rec = _wire_batch_update(services)
        fmt_in = {
            "bg": "#FFCDD2",
            "bold": True,
            "numberFormat": "0.00%",
            "numberFormatType": "PERCENT",
            "halign": "CENTER",
        }
        core_format(services, SHEET_ID, "Cliff!A1:A10", fmt_in)
        cell = _single_request(rec)[0]["repeatCell"]["cell"]
        # The written userEnteredFormat flattens back to the same flat keys we wrote.
        flat = flatten_cell_format(cell["userEnteredFormat"])
        assert flat == {
            "bg": "#FFCDD2",
            "bold": True,
            "numberFormat": "0.00%",
            "numberFormatType": "PERCENT",
            "halign": "CENTER",
        }
