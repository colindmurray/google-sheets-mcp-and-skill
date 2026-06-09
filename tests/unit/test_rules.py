"""Unit tests for ``gsheets.core.rules`` (DESIGN §3.3, §3.1, §4).

All tests run against a MOCKED Sheets service — no network. The mock records the request
bodies passed to ``spreadsheets.batchUpdate`` (and the cached ``spreadsheets.get`` used by
addressing) so we can golden-master the OUTBOUND request shape (CF add/update/delete requests,
batch high->low ordering, setDataValidation payloads) as well as the serialized RETURN dicts.

For the validation (de)serializers (``validation_to_rule`` / ``rule_to_validation``) we use
GOLDEN-MASTER style: representative Google ``DataValidationRule`` JSON in, assert the exact
structured ``ValidationRule`` out, and the inspect<->set_validation round-trip
(``validation_to_rule`` -> ``rule_to_validation`` -> identical Google rule).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gsheets.core import rules as rules_mod
from gsheets.core.errors import SheetsError
from gsheets.core.rules import (
    rule_to_validation,
    set_conditional_format,
    set_validation,
    validation_to_rule,
)
from gsheets.core.service import SheetsServices


# --------------------------------------------------------------------------- helpers


class _Recorder:
    """A callable that records its kwargs and returns an object whose ``.execute()``
    yields a queued response (cycling the last one). Lets a test assert exactly what was
    sent to Google."""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self._responses:
            resp = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        else:
            resp = {}
        request_obj = MagicMock(name="request")
        request_obj.execute.return_value = resp
        return request_obj


# A canonical sheet index for the cached ``spreadsheets.get`` that addressing issues.
_SHEET_INDEX_RESPONSE = {
    "sheets": [
        {"properties": {"sheetId": 0, "title": "Cliff", "index": 0}},
        {"properties": {"sheetId": 7, "title": "Sheet1", "index": 1}},
    ]
}


def _make_service(*, account_email: str | None = None):
    """Build a SheetsServices whose chained accessors route to per-method ``_Recorder``s.

    Wires ``spreadsheets().get`` (the addressing cache) and ``spreadsheets().batchUpdate``
    recorders. Returns ``(services, get_rec, batch_rec)``.
    """
    sheets = MagicMock(name="sheets_v4")
    services = SheetsServices(sheets=sheets, drive=None, account_email=account_email)

    spreadsheets = services.sheets.spreadsheets.return_value
    get_rec = _Recorder([_SHEET_INDEX_RESPONSE])
    spreadsheets.get = get_rec
    batch_rec = _Recorder([{"replies": []}])
    spreadsheets.batchUpdate = batch_rec
    return services, get_rec, batch_rec


def _make_http_error(status: int = 403):
    """A minimal stand-in for ``googleapiclient.errors.HttpError`` that classify can read."""
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = status
    resp.reason = "Forbidden"
    content = (
        b'{"error": {"code": %d, "status": "PERMISSION_DENIED", "message": "nope"}}'
        % status
    )
    return HttpError(resp=resp, content=content)


SHEET_ID = "<TEST_SHEET_ID>"


def _last_requests(batch_rec: _Recorder) -> list[dict]:
    """Return the ``requests`` list from the most recent batchUpdate call."""
    return batch_rec.calls[-1]["body"]["requests"]


# ===========================================================================
# set_conditional_format — single form (add / update / delete)
# ===========================================================================


class TestSetConditionalFormatAdd:
    def test_add_from_body_line(self):
        services, _get, batch = _make_service()
        line = "[Cliff!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold"

        result = set_conditional_format(
            services, SHEET_ID, action="add", sheet="Cliff", index=0, rule=line
        )

        assert result["ok"] is True
        assert result["spreadsheetId"] == SHEET_ID
        assert result["action"] == "add"
        assert result["sheet"] == "Cliff"
        assert result["index"] == 0
        # The returned rule is the canonical body-only line (round-trippable).
        assert result["rule"] == line

        reqs = _last_requests(batch)
        assert len(reqs) == 1
        add = reqs[0]["addConditionalFormatRule"]
        assert add["index"] == 0
        # Ranges resolved to a GridRange on the right sheet.
        gr = add["rule"]["ranges"][0]
        assert gr["sheetId"] == 0
        assert gr["startRowIndex"] == 1
        assert gr["endRowIndex"] == 100
        assert gr["startColumnIndex"] == 0
        assert gr["endColumnIndex"] == 1
        # Body carries the booleanRule built from the line.
        assert add["rule"]["booleanRule"]["condition"]["type"] == "CUSTOM_FORMULA"
        assert (
            add["rule"]["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
            == "=$B2>10"
        )

    def test_add_defaults_index_to_zero_when_omitted(self):
        services, _get, batch = _make_service()
        line = "[Cliff!C2:C100] if NUMBER_GREATER(0) -> fg #1B5E20 bold"

        result = set_conditional_format(services, SHEET_ID, action="add", rule=line)

        assert result["index"] == 0
        assert _last_requests(batch)[0]["addConditionalFormatRule"]["index"] == 0

    def test_add_from_structured_dict(self):
        services, _get, batch = _make_service()
        structured = {
            "ranges": ["Cliff!E2:E100"],
            "kind": "boolean",
            "condition": {"type": "BLANK", "values": []},
            "format": {"bg": "#ECEFF1", "italic": True},
        }

        result = set_conditional_format(
            services, SHEET_ID, action="add", index=2, rule=structured
        )

        assert result["rule"] == "[Cliff!E2:E100] if BLANK -> bg #ECEFF1 italic"
        add = _last_requests(batch)[0]["addConditionalFormatRule"]
        assert add["index"] == 2
        assert add["rule"]["booleanRule"]["condition"]["type"] == "BLANK"

    def test_add_gradient_rule(self):
        services, _get, batch = _make_service()
        line = "[Cliff!H2:H100] gradient min=#F44336 | mid:num:50=#FFEB3B | max=#4CAF50"

        result = set_conditional_format(services, SHEET_ID, action="add", index=0, rule=line)

        assert result["rule"] == line
        grad = _last_requests(batch)[0]["addConditionalFormatRule"]["rule"]["gradientRule"]
        assert grad["minpoint"]["type"] == "MIN"
        assert grad["midpoint"]["type"] == "NUMBER"
        assert grad["midpoint"]["value"] == "50"
        assert grad["maxpoint"]["type"] == "MAX"


class TestSetConditionalFormatUpdate:
    def test_update_requires_index(self):
        services, _get, _batch = _make_service()
        line = "[Cliff!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold"
        with pytest.raises(SheetsError) as ei:
            set_conditional_format(services, SHEET_ID, action="update", rule=line)
        assert ei.value.code == "missing_index"

    def test_update_emits_sheet_id_and_index(self):
        services, _get, batch = _make_service()
        line = "[Cliff!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold"

        result = set_conditional_format(
            services, SHEET_ID, action="update", sheet="Cliff", index=3, rule=line
        )

        assert result["action"] == "update"
        assert result["index"] == 3
        upd = _last_requests(batch)[0]["updateConditionalFormatRule"]
        assert upd["index"] == 3
        assert upd["sheetId"] == 0
        assert upd["rule"]["ranges"][0]["sheetId"] == 0

    def test_update_infers_sheet_id_from_rule_when_sheet_omitted(self):
        services, _get, batch = _make_service()
        line = "[Sheet1!A2:A100] if NOT_BLANK -> bg #C8E6C9"

        set_conditional_format(services, SHEET_ID, action="update", index=1, rule=line)

        upd = _last_requests(batch)[0]["updateConditionalFormatRule"]
        # Sheet1 -> sheetId 7 in the fixture; inferred from the rule's resolved range.
        assert upd["sheetId"] == 7


class TestSetConditionalFormatDelete:
    def test_delete_requires_index(self):
        services, _get, _batch = _make_service()
        with pytest.raises(SheetsError) as ei:
            set_conditional_format(services, SHEET_ID, action="delete", sheet="Cliff")
        assert ei.value.code == "missing_index"

    def test_delete_requires_sheet(self):
        services, _get, _batch = _make_service()
        with pytest.raises(SheetsError) as ei:
            set_conditional_format(services, SHEET_ID, action="delete", index=2)
        assert ei.value.code == "missing_sheet"

    def test_delete_emits_index_and_sheet_id_no_rule(self):
        services, _get, batch = _make_service()

        result = set_conditional_format(
            services, SHEET_ID, action="delete", sheet="Cliff", index=5
        )

        assert result["action"] == "delete"
        assert result["index"] == 5
        # Delete carries no serialized rule line.
        assert "rule" not in result
        delete = _last_requests(batch)[0]["deleteConditionalFormatRule"]
        assert delete == {"index": 5, "sheetId": 0}

    def test_delete_resolves_sheet_named_like_a_cell(self):
        # A sheet literally named "Sheet1" must resolve as a SHEET, not parse as cell A1.
        services, _get, batch = _make_service()
        set_conditional_format(services, SHEET_ID, action="delete", sheet="Sheet1", index=0)
        delete = _last_requests(batch)[0]["deleteConditionalFormatRule"]
        assert delete["sheetId"] == 7


class TestSetConditionalFormatValidation:
    def test_unknown_action_raises(self):
        services, _get, _batch = _make_service()
        with pytest.raises(SheetsError) as ei:
            set_conditional_format(services, SHEET_ID, action="frobnicate", index=0)
        assert ei.value.code == "bad_action"

    def test_missing_action_and_rules_raises(self):
        services, _get, _batch = _make_service()
        with pytest.raises(SheetsError) as ei:
            set_conditional_format(services, SHEET_ID)
        assert ei.value.code == "missing_action"

    def test_add_without_rule_raises(self):
        services, _get, _batch = _make_service()
        with pytest.raises(SheetsError) as ei:
            set_conditional_format(services, SHEET_ID, action="add", index=0)
        assert ei.value.code == "missing_rule"

    def test_negative_index_raises(self):
        services, _get, _batch = _make_service()
        with pytest.raises(SheetsError) as ei:
            set_conditional_format(services, SHEET_ID, action="delete", sheet="Cliff", index=-1)
        assert ei.value.code == "bad_index"

    def test_http_error_is_classified(self):
        services, _get, batch = _make_service()
        batch.__call__ = None  # ensure we override below

        def _raise(**kwargs):
            req = MagicMock()
            req.execute.side_effect = _make_http_error(403)
            return req

        services.sheets.spreadsheets.return_value.batchUpdate = _raise
        line = "[Cliff!A2:A100] if BLANK -> bg #ECEFF1"
        with pytest.raises(SheetsError) as ei:
            set_conditional_format(services, SHEET_ID, action="add", index=0, rule=line)
        assert ei.value.code == "google_api_error"
        assert ei.value.status == 403


# ===========================================================================
# set_conditional_format — batch form (rules=[...], high->low ordering)
# ===========================================================================


class TestSetConditionalFormatBatch:
    def test_conflicting_args_single_and_batch(self):
        services, _get, _batch = _make_service()
        with pytest.raises(SheetsError) as ei:
            set_conditional_format(
                services,
                SHEET_ID,
                action="delete",
                index=0,
                rules=[{"action": "delete", "index": 1}],
            )
        assert ei.value.code == "conflicting_args"

    def test_batch_sorts_high_to_low_in_one_batch_update(self):
        services, _get, batch = _make_service()
        items = [
            {"action": "delete", "index": 1},
            {"action": "delete", "index": 5},
            {"action": "delete", "index": 2},
        ]

        result = set_conditional_format(services, SHEET_ID, sheet="Cliff", rules=items)

        # One batchUpdate only.
        assert len(batch.calls) == 1
        reqs = _last_requests(batch)
        emitted_indices = [r["deleteConditionalFormatRule"]["index"] for r in reqs]
        assert emitted_indices == [5, 2, 1]
        # The results echo the applied (high->low) order.
        assert [r["index"] for r in result["results"]] == [5, 2, 1]
        assert all(r["action"] == "delete" for r in result["results"])

    def test_batch_mixed_actions_high_to_low(self):
        services, _get, batch = _make_service()
        items = [
            {"action": "add", "index": 0, "rule": "[Cliff!A2:A100] if BLANK -> bg #ECEFF1"},
            {"action": "update", "index": 4, "rule": "[Cliff!B2:B100] if NOT_BLANK -> bold"},
            {"action": "delete", "index": 2},
        ]

        result = set_conditional_format(services, SHEET_ID, sheet="Cliff", rules=items)

        reqs = _last_requests(batch)
        # high->low: delete@2 should not shift update@4; ordering is 4, 2, 0.
        assert [r["index"] for r in result["results"]] == [4, 2, 0]
        # First emitted request targets index 4 (the update).
        assert "updateConditionalFormatRule" in reqs[0]
        assert reqs[0]["updateConditionalFormatRule"]["index"] == 4
        assert "deleteConditionalFormatRule" in reqs[1]
        assert "addConditionalFormatRule" in reqs[2]
        # update/add carry serialized rule lines; delete does not.
        by_index = {r["index"]: r for r in result["results"]}
        assert "rule" in by_index[4]
        assert "rule" in by_index[0]
        assert "rule" not in by_index[2]

    def test_batch_per_item_sheet_overrides_default(self):
        services, _get, batch = _make_service()
        items = [
            {"action": "delete", "index": 0, "sheet": "Sheet1"},
            {"action": "delete", "index": 1, "sheet": "Cliff"},
        ]
        set_conditional_format(services, SHEET_ID, rules=items)
        reqs = _last_requests(batch)
        # high->low: index 1 (Cliff) first, then index 0 (Sheet1).
        assert reqs[0]["deleteConditionalFormatRule"] == {"index": 1, "sheetId": 0}
        assert reqs[1]["deleteConditionalFormatRule"] == {"index": 0, "sheetId": 7}

    def test_empty_rules_list_raises(self):
        services, _get, _batch = _make_service()
        with pytest.raises(SheetsError) as ei:
            set_conditional_format(services, SHEET_ID, rules=[])
        assert ei.value.code == "empty_payload"

    def test_batch_item_not_a_dict_raises(self):
        services, _get, _batch = _make_service()
        with pytest.raises(SheetsError) as ei:
            set_conditional_format(services, SHEET_ID, rules=["not-a-dict"])
        assert ei.value.code == "bad_rule"


# ===========================================================================
# set_validation — set / clear
# ===========================================================================


class TestSetValidation:
    def test_set_one_of_list(self):
        services, _get, batch = _make_service()
        result = set_validation(
            services, SHEET_ID, "Cliff!A2:A100", rule={"type": "ONE_OF_LIST", "values": ["Yes", "No"]}
        )

        assert result["ok"] is True
        assert result["range"] == "Cliff!A2:A100"
        assert result["validation"] == "ONE_OF_LIST(Yes,No)"
        assert result["validationRule"] == {
            "type": "ONE_OF_LIST",
            "values": ["Yes", "No"],
            "strict": True,
            "showDropdown": True,
        }

        req = _last_requests(batch)[0]["setDataValidation"]
        assert req["range"]["sheetId"] == 0
        assert req["range"]["startRowIndex"] == 1
        assert req["range"]["endRowIndex"] == 100
        cond = req["rule"]["condition"]
        assert cond["type"] == "ONE_OF_LIST"
        assert [v["userEnteredValue"] for v in cond["values"]] == ["Yes", "No"]
        assert req["rule"]["strict"] is True
        assert req["rule"]["showCustomUi"] is True

    def test_set_one_of_range_uses_source_and_formula(self):
        services, _get, batch = _make_service()
        result = set_validation(
            services, SHEET_ID, "Cliff!A2:A100", rule={"type": "ONE_OF_RANGE", "source": "Cliff!Z1:Z10"}
        )
        assert result["validationRule"]["source"] == "Cliff!Z1:Z10"
        assert "values" not in result["validationRule"]
        assert result["validation"] == "ONE_OF_RANGE(Cliff!Z1:Z10)"
        cond = _last_requests(batch)[0]["setDataValidation"]["rule"]["condition"]
        # Google stores the range as a leading-"=" formula.
        assert cond["values"][0]["userEnteredValue"] == "=Cliff!Z1:Z10"

    def test_set_boolean_checkbox_no_values(self):
        services, _get, batch = _make_service()
        result = set_validation(services, SHEET_ID, "Cliff!B2:B100", rule={"type": "BOOLEAN"})
        assert result["validation"] == "BOOLEAN"
        assert "values" not in result["validationRule"]
        cond = _last_requests(batch)[0]["setDataValidation"]["rule"]["condition"]
        assert cond["type"] == "BOOLEAN"
        assert "values" not in cond

    def test_set_number_between(self):
        services, _get, batch = _make_service()
        result = set_validation(
            services, SHEET_ID, "Cliff!C2:C100", rule={"type": "NUMBER_BETWEEN", "values": [0, 100]}
        )
        assert result["validation"] == "NUMBER_BETWEEN(0,100)"
        cond = _last_requests(batch)[0]["setDataValidation"]["rule"]["condition"]
        assert [v["userEnteredValue"] for v in cond["values"]] == ["0", "100"]

    def test_set_custom_formula(self):
        services, _get, batch = _make_service()
        result = set_validation(
            services, SHEET_ID, "Cliff!D2", rule={"type": "CUSTOM_FORMULA", "values": ["=ISNUMBER(A1)"]}
        )
        assert result["validation"] == "CUSTOM_FORMULA(=ISNUMBER(A1))"
        cond = _last_requests(batch)[0]["setDataValidation"]["rule"]["condition"]
        assert cond["values"][0]["userEnteredValue"] == "=ISNUMBER(A1)"

    def test_clear_validation_sends_no_rule(self):
        services, _get, batch = _make_service()
        result = set_validation(services, SHEET_ID, "Cliff!A2:A100", rule=None)
        assert result["validation"] is None
        assert result["validationRule"] is None
        req = _last_requests(batch)[0]["setDataValidation"]
        assert "rule" not in req
        assert req["range"]["sheetId"] == 0

    def test_kwarg_no_dropdown_and_no_strict(self):
        services, _get, batch = _make_service()
        result = set_validation(
            services,
            SHEET_ID,
            "Cliff!A2:A100",
            rule={"type": "ONE_OF_LIST", "values": ["A", "B"]},
            strict=False,
            show_dropdown=False,
        )
        assert result["validationRule"]["strict"] is False
        assert result["validationRule"]["showDropdown"] is False
        rule = _last_requests(batch)[0]["setDataValidation"]["rule"]
        assert rule["strict"] is False
        assert rule["showCustomUi"] is False

    def test_in_rule_flags_honored_when_kwargs_default(self):
        # Round-trip path: a structured rule carrying strict/showDropdown is written back
        # without re-passing kwargs; the in-rule flags must survive.
        services, _get, batch = _make_service()
        set_validation(
            services,
            SHEET_ID,
            "Cliff!A2:A100",
            rule={"type": "ONE_OF_LIST", "values": ["A"], "strict": False, "showDropdown": False},
        )
        rule = _last_requests(batch)[0]["setDataValidation"]["rule"]
        assert rule["strict"] is False
        assert rule["showCustomUi"] is False

    def test_non_dict_rule_raises(self):
        services, _get, _batch = _make_service()
        with pytest.raises(SheetsError) as ei:
            set_validation(services, SHEET_ID, "Cliff!A2", rule="ONE_OF_LIST")
        assert ei.value.code == "bad_validation"

    def test_http_error_is_classified(self):
        services, _get, _batch = _make_service()

        def _raise(**kwargs):
            req = MagicMock()
            req.execute.side_effect = _make_http_error(404)
            return req

        services.sheets.spreadsheets.return_value.batchUpdate = _raise
        with pytest.raises(SheetsError) as ei:
            set_validation(services, SHEET_ID, "Cliff!A2", rule={"type": "BOOLEAN"})
        assert ei.value.code == "google_api_error"
        assert ei.value.status == 404


# ===========================================================================
# validation (de)serializers — GOLDEN-MASTER + round-trip
# ===========================================================================


class TestValidationToRule:
    def test_one_of_list_golden(self):
        google = {
            "condition": {
                "type": "ONE_OF_LIST",
                "values": [{"userEnteredValue": "Yes"}, {"userEnteredValue": "No"}],
            },
            "strict": True,
            "showCustomUi": True,
        }
        assert validation_to_rule(google) == {
            "type": "ONE_OF_LIST",
            "values": ["Yes", "No"],
            "strict": True,
            "showDropdown": True,
        }

    def test_one_of_range_golden_strips_leading_equals(self):
        google = {
            "condition": {
                "type": "ONE_OF_RANGE",
                "values": [{"userEnteredValue": "=Cliff!Z1:Z10"}],
            },
            "strict": True,
            "showCustomUi": True,
        }
        out = validation_to_rule(google)
        assert out["type"] == "ONE_OF_RANGE"
        assert out["source"] == "Cliff!Z1:Z10"
        assert "values" not in out

    def test_boolean_golden_no_values(self):
        google = {"condition": {"type": "BOOLEAN"}, "strict": True}
        out = validation_to_rule(google)
        assert out["type"] == "BOOLEAN"
        assert "values" not in out
        assert "source" not in out

    def test_number_between_golden(self):
        google = {
            "condition": {
                "type": "NUMBER_BETWEEN",
                "values": [{"userEnteredValue": "0"}, {"userEnteredValue": "100"}],
            },
            "strict": False,
            "showCustomUi": False,
        }
        out = validation_to_rule(google)
        assert out["values"] == ["0", "100"]
        assert out["strict"] is False
        assert out["showDropdown"] is False

    def test_strict_and_dropdown_default_true_when_absent(self):
        google = {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": "A"}]}}
        out = validation_to_rule(google)
        assert out["strict"] is True
        assert out["showDropdown"] is True

    def test_relative_date_value_extracted(self):
        google = {
            "condition": {
                "type": "DATE_AFTER",
                "values": [{"relativeDate": "PAST_MONTH"}],
            }
        }
        out = validation_to_rule(google)
        assert out["values"] == ["PAST_MONTH"]

    def test_non_dict_raises(self):
        with pytest.raises(SheetsError) as ei:
            validation_to_rule("nope")
        assert ei.value.code == "bad_validation"

    def test_missing_condition_raises(self):
        with pytest.raises(SheetsError) as ei:
            validation_to_rule({"strict": True})
        assert ei.value.code == "bad_validation"


class TestRuleToValidation:
    def test_one_of_list(self):
        out = rule_to_validation({"type": "ONE_OF_LIST", "values": ["Yes", "No"]})
        assert out["condition"]["type"] == "ONE_OF_LIST"
        assert [v["userEnteredValue"] for v in out["condition"]["values"]] == ["Yes", "No"]
        assert out["strict"] is True
        assert out["showCustomUi"] is True

    def test_one_of_range_source_to_formula(self):
        out = rule_to_validation({"type": "ONE_OF_RANGE", "source": "Cliff!Z1:Z10"})
        assert out["condition"]["values"][0]["userEnteredValue"] == "=Cliff!Z1:Z10"

    def test_one_of_range_already_has_equals(self):
        out = rule_to_validation({"type": "ONE_OF_RANGE", "source": "=Cliff!Z1:Z10"})
        assert out["condition"]["values"][0]["userEnteredValue"] == "=Cliff!Z1:Z10"

    def test_one_of_range_missing_source_raises(self):
        with pytest.raises(SheetsError) as ei:
            rule_to_validation({"type": "ONE_OF_RANGE"})
        assert ei.value.code == "bad_validation"

    def test_boolean_no_values_field(self):
        out = rule_to_validation({"type": "BOOLEAN"})
        assert "values" not in out["condition"]

    def test_number_values_stringified(self):
        out = rule_to_validation({"type": "NUMBER_BETWEEN", "values": [0, 100]})
        assert [v["userEnteredValue"] for v in out["condition"]["values"]] == ["0", "100"]

    def test_float_integer_stringified_bare(self):
        out = rule_to_validation({"type": "NUMBER_BETWEEN", "values": [0.0, 100.0]})
        assert [v["userEnteredValue"] for v in out["condition"]["values"]] == ["0", "100"]

    def test_missing_type_raises(self):
        with pytest.raises(SheetsError) as ei:
            rule_to_validation({"values": ["A"]})
        assert ei.value.code == "bad_validation"

    def test_non_dict_raises(self):
        with pytest.raises(SheetsError) as ei:
            rule_to_validation(["not", "a", "dict"])
        assert ei.value.code == "bad_validation"

    def test_kwarg_false_wins(self):
        out = rule_to_validation(
            {"type": "BOOLEAN", "strict": True, "showDropdown": True},
            strict=False,
            show_dropdown=False,
        )
        assert out["strict"] is False
        assert out["showCustomUi"] is False

    def test_in_rule_false_honored_when_kwarg_default(self):
        out = rule_to_validation({"type": "BOOLEAN", "strict": False, "showDropdown": False})
        assert out["strict"] is False
        assert out["showCustomUi"] is False


class TestValidationRoundTrip:
    """``validation_to_rule`` -> ``rule_to_validation`` reproduces the Google rule exactly."""

    @pytest.mark.parametrize(
        "google",
        [
            {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": "Yes"}, {"userEnteredValue": "No"}],
                },
                "strict": True,
                "showCustomUi": True,
            },
            {
                "condition": {
                    "type": "ONE_OF_RANGE",
                    "values": [{"userEnteredValue": "=Cliff!Z1:Z10"}],
                },
                "strict": True,
                "showCustomUi": True,
            },
            {
                "condition": {"type": "BOOLEAN"},
                "strict": True,
                "showCustomUi": True,
            },
            {
                "condition": {
                    "type": "NUMBER_BETWEEN",
                    "values": [{"userEnteredValue": "0"}, {"userEnteredValue": "100"}],
                },
                "strict": False,
                "showCustomUi": False,
            },
            {
                "condition": {
                    "type": "CUSTOM_FORMULA",
                    "values": [{"userEnteredValue": "=ISNUMBER(A1)"}],
                },
                "strict": True,
                "showCustomUi": False,
            },
        ],
    )
    def test_round_trip_google_rule_identical(self, google):
        structured = validation_to_rule(google)
        rebuilt = rule_to_validation(structured)
        assert rebuilt == google
