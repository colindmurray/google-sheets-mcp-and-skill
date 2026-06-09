"""Unit tests for the single error envelope (DESIGN §6).

All tests run against MOCKED / synthetic ``HttpError`` objects — no network. We build
``googleapiclient.errors.HttpError`` instances from a fake ``httplib2.Response`` + a JSON
body that mirrors what the real Sheets API returns, exercising:

- ``SheetsError`` construction, ``str()`` shape, structured fields, and ``to_dict``.
- ``classify_google_error`` status/reason extraction and per-status hints.
- the privacy invariant: the operator email is NEVER in the default hint and appears ONLY
  when ``GSHEETS_VERBOSE_ERRORS`` is set (DESIGN §6.1).
- graceful degradation on malformed / empty / non-JSON error bodies.
"""

from __future__ import annotations

import json

import pytest

from googleapiclient.errors import HttpError

from gsheets.core.errors import SheetsError, classify_google_error


# --------------------------------------------------------------------------- helpers


class _FakeResponse:
    """Minimal stand-in for an ``httplib2.Response`` (a dict subclass in real life).

    ``HttpError`` only touches ``.status`` and ``.reason`` on the response, so this is all
    the surface we need.
    """

    def __init__(self, status: int, reason: str = "") -> None:
        self.status = status
        self.reason = reason


def make_http_error(
    status: int,
    *,
    message: str = "boom",
    google_status: str | None = None,
    errors: list[dict] | None = None,
    raw_content: bytes | str | None = None,
) -> HttpError:
    """Build a realistic ``HttpError`` the way googleapiclient would.

    When ``raw_content`` is given it is used verbatim (to exercise non-JSON / empty bodies);
    otherwise a Sheets-API-shaped ``{"error": {...}}`` JSON body is synthesized.
    """
    if raw_content is not None:
        content = raw_content.encode("utf-8") if isinstance(raw_content, str) else raw_content
    else:
        error_obj: dict[str, object] = {"code": status, "message": message}
        if google_status is not None:
            error_obj["status"] = google_status
        if errors is not None:
            error_obj["errors"] = errors
        content = json.dumps({"error": error_obj}).encode("utf-8")
    return HttpError(_FakeResponse(status), content, uri="https://sheets.example/v4")


# --------------------------------------------------------------------------- SheetsError


def test_sheetserror_str_is_code_colon_message():
    err = SheetsError("bad_range", "Cliff!ZZ is not a valid A1 range")
    assert str(err) == "bad_range: Cliff!ZZ is not a valid A1 range"


def test_sheetserror_carries_structured_fields():
    err = SheetsError(
        "google_api_error",
        "no access",
        status=403,
        reason="PERMISSION_DENIED",
        hint="share the sheet",
    )
    assert err.code == "google_api_error"
    assert err.message == "no access"
    assert err.status == 403
    assert err.reason == "PERMISSION_DENIED"
    assert err.hint == "share the sheet"


def test_sheetserror_defaults_are_none():
    err = SheetsError("empty_payload", "refuse a no-op write")
    assert err.status is None
    assert err.reason is None
    assert err.hint is None


def test_sheetserror_is_an_exception_and_raisable():
    with pytest.raises(SheetsError) as excinfo:
        raise SheetsError("unknown_action", "no such action 'frobnicate'")
    assert excinfo.value.code == "unknown_action"
    assert isinstance(excinfo.value, Exception)


def test_sheetserror_to_dict_omits_none_fields():
    err = SheetsError("empty_payload", "refuse a no-op write")
    assert err.to_dict() == {"code": "empty_payload", "message": "refuse a no-op write"}


def test_sheetserror_to_dict_includes_set_fields():
    err = SheetsError(
        "google_api_error", "no access", status=403, reason="PERMISSION_DENIED", hint="share it"
    )
    assert err.to_dict() == {
        "code": "google_api_error",
        "message": "no access",
        "status": 403,
        "reason": "PERMISSION_DENIED",
        "hint": "share it",
    }


# --------------------------------------------------------- classify_google_error: status


def test_classify_sets_code_google_api_error():
    err = classify_google_error(make_http_error(404, message="Requested entity was not found."))
    assert isinstance(err, SheetsError)
    assert err.code == "google_api_error"


def test_classify_extracts_http_status_from_response():
    err = classify_google_error(make_http_error(404, message="not found"))
    assert err.status == 404


def test_classify_extracts_message_from_body():
    err = classify_google_error(
        make_http_error(400, message="Unable to parse range: Cliff!ZZ")
    )
    assert err.message == "Unable to parse range: Cliff!ZZ"


# --------------------------------------------------------- classify_google_error: reason


def test_classify_reads_google_status_as_reason():
    err = classify_google_error(
        make_http_error(403, message="The caller does not have permission",
                        google_status="PERMISSION_DENIED")
    )
    assert err.reason == "PERMISSION_DENIED"


def test_classify_falls_back_to_errors_reason():
    err = classify_google_error(
        make_http_error(
            403,
            message="forbidden",
            errors=[{"message": "no access", "domain": "global", "reason": "forbidden"}],
        )
    )
    # No top-level error.status -> fall back to error.errors[0].reason.
    assert err.reason == "forbidden"


def test_classify_prefers_google_status_over_errors_reason():
    err = classify_google_error(
        make_http_error(
            403,
            message="x",
            google_status="PERMISSION_DENIED",
            errors=[{"reason": "forbidden"}],
        )
    )
    assert err.reason == "PERMISSION_DENIED"


def test_classify_reason_none_when_absent():
    err = classify_google_error(make_http_error(500, message="backend error"))
    assert err.reason is None


# ----------------------------------------------------------- classify_google_error: hints


@pytest.mark.parametrize(
    "status,needle",
    [
        (400, "bad range"),
        (401, "credentials"),
        (403, "share the sheet"),
        (404, "spreadsheet id"),
        (429, "rate limit"),
        (500, "transient"),
        (503, "temporarily unavailable"),
    ],
)
def test_classify_hint_is_actionable_per_status(status, needle):
    err = classify_google_error(make_http_error(status, message="m"))
    assert err.hint is not None
    assert needle in err.hint


def test_classify_403_hint_is_generic_and_mentions_scopes():
    err = classify_google_error(
        make_http_error(403, message="denied", google_status="PERMISSION_DENIED")
    )
    assert "share the sheet with the authenticated account" in err.hint


def test_classify_unknown_status_gets_default_hint():
    err = classify_google_error(make_http_error(418, message="teapot"))
    assert err.hint == "see the API message above for details"
    assert err.status == 418


# ------------------------------------------------- privacy invariant: email gating (§6.1)


def test_403_hint_never_includes_email_by_default(monkeypatch):
    monkeypatch.delenv("GSHEETS_VERBOSE_ERRORS", raising=False)
    err = classify_google_error(
        make_http_error(403, message="denied", google_status="PERMISSION_DENIED"),
        account_email="operator@example.com",
    )
    assert "operator@example.com" not in err.hint
    assert "operator@example.com" not in str(err)
    assert "operator@example.com" not in json.dumps(err.to_dict())


def test_403_hint_includes_email_only_when_verbose(monkeypatch):
    monkeypatch.setenv("GSHEETS_VERBOSE_ERRORS", "1")
    err = classify_google_error(
        make_http_error(403, message="denied", google_status="PERMISSION_DENIED"),
        account_email="operator@example.com",
    )
    assert "operator@example.com" in err.hint


def test_401_hint_includes_email_only_when_verbose(monkeypatch):
    monkeypatch.setenv("GSHEETS_VERBOSE_ERRORS", "1")
    err = classify_google_error(
        make_http_error(401, message="unauthorized"),
        account_email="operator@example.com",
    )
    assert "operator@example.com" in err.hint


def test_verbose_email_not_added_for_non_permission_status(monkeypatch):
    # Verbose on, but a 404 is not a permission/credential failure -> no email.
    monkeypatch.setenv("GSHEETS_VERBOSE_ERRORS", "1")
    err = classify_google_error(
        make_http_error(404, message="not found"),
        account_email="operator@example.com",
    )
    assert "operator@example.com" not in err.hint


def test_verbose_without_email_is_safe(monkeypatch):
    monkeypatch.setenv("GSHEETS_VERBOSE_ERRORS", "1")
    err = classify_google_error(
        make_http_error(403, message="denied", google_status="PERMISSION_DENIED")
    )
    # No email available -> hint unchanged, no trailing parenthetical.
    assert err.hint == (
        "share the sheet with the authenticated account, or check that the granted "
        "OAuth scopes cover this operation"
    )


@pytest.mark.parametrize("flag", ["0", "false", "no", "off", "", " "])
def test_verbose_flag_falsy_values_keep_email_out(monkeypatch, flag):
    monkeypatch.setenv("GSHEETS_VERBOSE_ERRORS", flag)
    err = classify_google_error(
        make_http_error(403, message="denied", google_status="PERMISSION_DENIED"),
        account_email="operator@example.com",
    )
    assert "operator@example.com" not in err.hint


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on", "verbose"])
def test_verbose_flag_truthy_values_include_email(monkeypatch, flag):
    monkeypatch.setenv("GSHEETS_VERBOSE_ERRORS", flag)
    err = classify_google_error(
        make_http_error(403, message="denied", google_status="PERMISSION_DENIED"),
        account_email="operator@example.com",
    )
    assert "operator@example.com" in err.hint


# --------------------------------------------------- graceful degradation on bad bodies


def test_classify_non_json_body_still_classifies():
    err = classify_google_error(
        make_http_error(500, raw_content=b"<html>500 Internal Server Error</html>")
    )
    assert err.code == "google_api_error"
    assert err.status == 500
    assert err.reason is None
    # Falls back to HttpError.reason (httplib2 response reason) or stringified error.
    assert err.message


def test_classify_empty_body_still_classifies():
    err = classify_google_error(make_http_error(503, raw_content=b""))
    assert err.status == 503
    assert err.code == "google_api_error"
    assert err.hint is not None


def test_classify_json_array_body_does_not_crash():
    # Some Google endpoints return a top-level array; must not raise.
    err = classify_google_error(make_http_error(400, raw_content=b'[{"error": {"message": "x"}}]'))
    assert err.code == "google_api_error"
    assert err.status == 400


def test_classify_tolerates_object_without_resp():
    # A duck-typed error exposing only JSON content (no .resp) still classifies via body.
    class _Bare:
        content = json.dumps({"error": {"code": 404, "message": "gone",
                                        "status": "NOT_FOUND"}}).encode("utf-8")

    err = classify_google_error(_Bare())
    assert err.status == 404
    assert err.reason == "NOT_FOUND"
    assert "spreadsheet id" in err.hint


def test_classify_object_with_no_status_anywhere():
    class _Bare:
        content = b"totally not json"

    err = classify_google_error(_Bare())
    assert err.status is None
    assert err.code == "google_api_error"
    assert err.hint == "see the API message above for details"


# ------------------------------------------------------------------- golden-master shape


def test_classify_golden_403_envelope(monkeypatch):
    """Pin the exact SheetsError envelope for a representative 403 (default, non-verbose)."""
    monkeypatch.delenv("GSHEETS_VERBOSE_ERRORS", raising=False)
    err = classify_google_error(
        make_http_error(
            403,
            message="The caller does not have permission",
            google_status="PERMISSION_DENIED",
        ),
        account_email="operator@example.com",
    )
    assert err.to_dict() == {
        "code": "google_api_error",
        "message": "The caller does not have permission",
        "status": 403,
        "reason": "PERMISSION_DENIED",
        "hint": (
            "share the sheet with the authenticated account, or check that the granted "
            "OAuth scopes cover this operation"
        ),
    }


def test_classify_golden_404_envelope():
    err = classify_google_error(
        make_http_error(
            404,
            message="Requested entity was not found.",
            google_status="NOT_FOUND",
        )
    )
    assert err.to_dict() == {
        "code": "google_api_error",
        "message": "Requested entity was not found.",
        "status": 404,
        "reason": "NOT_FOUND",
        "hint": "check the spreadsheet id / sheet name — the spreadsheet or sheet was not found",
    }


# ------------------------------------- _decode_content edge cases (content shape variants)


def test_classify_content_attribute_absent_decodes_to_none():
    # An error object with NO ``content`` attribute at all -> _decode_content returns None
    # (errors.py:111). Status still comes off the response, hint is the per-status default.
    class _NoContent:
        resp = _FakeResponse(429)

    err = classify_google_error(_NoContent())
    assert err.status == 429
    assert err.reason is None  # no body -> no reason
    assert "rate limit" in err.hint


def test_classify_content_explicit_none_decodes_to_none():
    # ``content = None`` (not just absent) also short-circuits to None (errors.py:110-111).
    class _NoneContent:
        content = None
        resp = _FakeResponse(500)

    err = classify_google_error(_NoneContent())
    assert err.status == 500
    assert err.reason is None
    assert "transient" in err.hint


def test_classify_undecodable_bytes_body_does_not_crash():
    # Invalid UTF-8 bytes -> the .decode("utf-8") raises UnicodeDecodeError, which
    # _decode_content swallows (errors.py:115-116) -> treated as no body.
    bad_utf8 = b"\xff\xfe\x00bad"
    err = classify_google_error(make_http_error(400, raw_content=bad_utf8))
    assert err.code == "google_api_error"
    assert err.status == 400  # status survives off the response even with an undecodable body
    assert err.reason is None
    assert "bad range" in err.hint


def test_classify_non_str_non_bytes_content_decodes_to_none():
    # ``content`` that is neither bytes nor str (e.g. an int) -> the isinstance(str) guard
    # rejects it (errors.py:117-118) without raising.
    class _WeirdContent:
        content = 12345
        resp = _FakeResponse(404)

    err = classify_google_error(_WeirdContent())
    assert err.status == 404
    assert err.reason is None
    assert "spreadsheet id" in err.hint


# --------------------------------- _extract_status fallback chain (resp -> status_code -> body)


def test_classify_non_int_resp_status_falls_back_to_body_code():
    # ``resp.status`` is a non-numeric string -> int() raises (errors.py:134-135); with no
    # ``status_code`` either, the body's ``error.code`` supplies the status.
    class _BadRespStatus:
        resp = _FakeResponse("not-a-number")  # type: ignore[arg-type]
        content = json.dumps(
            {"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}}
        ).encode("utf-8")

    err = classify_google_error(_BadRespStatus())
    assert err.status == 403  # recovered from error.code in the JSON body
    assert err.reason == "PERMISSION_DENIED"


def test_classify_status_code_property_used_when_no_resp():
    # No ``resp`` at all, but a numeric ``status_code`` property (the googleapiclient mirror of
    # resp.status) -> int(status_code) supplies the status (errors.py:137-139).
    class _StatusCodeOnly:
        status_code = 429
        content = b"not json"

    err = classify_google_error(_StatusCodeOnly())
    assert err.status == 429
    assert "rate limit" in err.hint


def test_classify_non_int_status_code_falls_back_to_body():
    # ``status_code`` present but non-numeric -> int() raises and is swallowed
    # (errors.py:141-142); the body's error.code is the last resort.
    class _BadStatusCode:
        status_code = "oops"
        content = json.dumps({"error": {"code": 500, "message": "backend"}}).encode("utf-8")

    err = classify_google_error(_BadStatusCode())
    assert err.status == 500
    assert "transient" in err.hint


def test_classify_non_int_body_code_yields_no_status():
    # All three sources fail to produce an int: no resp/status_code, and ``error.code`` is a
    # non-numeric string -> int() raises in the body branch (errors.py:149-150) -> status None.
    class _BadBodyCode:
        content = json.dumps({"error": {"code": "NaN", "message": "weird"}}).encode("utf-8")

    err = classify_google_error(_BadBodyCode())
    assert err.status is None
    assert err.code == "google_api_error"
    # Unknown status -> the generic default hint.
    assert err.hint == "see the API message above for details"
    # The body message still flows through even though the code was unusable.
    assert err.message == "weird"


def test_classify_resp_status_none_falls_to_status_code():
    # ``resp`` present but ``resp.status`` is None -> skip to ``status_code`` (errors.py:131 false
    # branch then 137-139). Confirms the chain does not stop at a None resp.status.
    class _NoneRespStatus:
        resp = _FakeResponse(None)  # type: ignore[arg-type]
        status_code = 404
        content = b""

    err = classify_google_error(_NoneRespStatus())
    assert err.status == 404
