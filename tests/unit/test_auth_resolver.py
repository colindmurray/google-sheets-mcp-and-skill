"""Unit tests for ``gsheets.auth.resolver`` (DESIGN §2.1/§2.2/§2.3, §10).

Everything is MOCKED — no network, no real credentials, no real Google calls. The Google
credential factories (``service_account.Credentials.from_service_account_file``,
``Credentials.from_authorized_user_file``, ``google.auth.default``) and the refresh
``Request`` are patched at the names ``resolver`` imports them through, so the precedence
logic, scope resolution, refresh/persist behavior, and error envelopes are exercised in
isolation.

A standing fixture wipes every auth-relevant env var so a developer's real environment can
never leak into (or break) these tests, and ``GSHEETS_CONFIG_DIR`` is pinned to a tmp dir so
the default token/client paths resolve under the sandbox, never ``~/.config``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gsheets.auth import resolver
from gsheets.core.errors import SheetsError

# ---------------------------------------------------------------------------
# Constants mirrored from DESIGN §2.3 (kept literal so a silent scope change fails a test)
# ---------------------------------------------------------------------------

SPREADSHEETS = "https://www.googleapis.com/auth/spreadsheets"
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
DRIVE = "https://www.googleapis.com/auth/drive"

AUTH_ENV_VARS = (
    "GSHEETS_AUTH_MODE",
    "GSHEETS_SERVICE_ACCOUNT_FILE",
    "GSHEETS_OAUTH_CLIENT_FILE",
    "GSHEETS_TOKEN_FILE",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GSHEETS_SCOPES",
    "GSHEETS_CONFIG_DIR",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_auth_env(monkeypatch, tmp_path):
    """Wipe all auth env vars and pin the config dir under a tmp sandbox.

    Autouse so no test inherits the developer's real credentials/paths. ``GSHEETS_CONFIG_DIR``
    points at a per-test dir so the default token/client paths resolve there.
    """
    for var in AUTH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("GSHEETS_CONFIG_DIR", str(config_dir))
    return config_dir


def _make_creds(
    *, valid=True, expired=False, refresh_token="rt", to_json='{"token": "x"}', scopes=None
):
    """A MagicMock standing in for a ``google.auth`` Credentials object.

    ``scopes`` is a REAL list (default ``[SPREADSHEETS, DRIVE_FILE]``) so the OAuth coverage
    check in the resolver (``_scopes_covered``) sees an iterable scope set, mirroring how
    google-auth populates ``creds.scopes`` from the loaded/refreshed token.
    """
    creds = MagicMock(name="credentials")
    creds.valid = valid
    creds.expired = expired
    creds.refresh_token = refresh_token
    creds.to_json.return_value = to_json
    scope_list = list(scopes) if scopes is not None else [SPREADSHEETS, DRIVE_FILE]
    # Mirror google-auth: ``granted_scopes`` is the authoritative post-refresh grant view the
    # resolver checks first; ``scopes`` is the requested list. Keep both consistent here so the
    # mock never leaks a truthy MagicMock into the resolver's coverage check.
    creds.granted_scopes = scope_list
    creds.scopes = scope_list

    # ``refresh`` flips the creds to valid (mimicking a successful token refresh).
    def _refresh(_request):
        creds.valid = True
        creds.expired = False

    creds.refresh.side_effect = _refresh
    return creds


def _write_sa_key(path: Path) -> Path:
    path.write_text(json.dumps({"type": "service_account", "client_email": "svc@x.iam"}))
    return path


def _write_user_token(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "client_id": "cid",
                "client_secret": "secret",
                "refresh_token": "rt",
                "type": "authorized_user",
            }
        )
    )
    return path


# ===========================================================================
# resolve_scopes (DESIGN §2.3)
# ===========================================================================


class TestResolveScopes:
    def test_default_mode_explicit(self):
        assert resolver.resolve_scopes("default") == [SPREADSHEETS, DRIVE_FILE]

    def test_broad_mode_explicit(self):
        assert resolver.resolve_scopes("broad") == [SPREADSHEETS, DRIVE_FILE, DRIVE]

    def test_none_reads_env_default_when_unset(self):
        # clean_auth_env has wiped GSHEETS_SCOPES -> default.
        assert resolver.resolve_scopes(None) == [SPREADSHEETS, DRIVE_FILE]

    def test_none_reads_env_broad(self, monkeypatch):
        monkeypatch.setenv("GSHEETS_SCOPES", "broad")
        assert resolver.resolve_scopes(None) == [SPREADSHEETS, DRIVE_FILE, DRIVE]

    def test_arg_overrides_env(self, monkeypatch):
        monkeypatch.setenv("GSHEETS_SCOPES", "broad")
        # Explicit arg wins over the env var.
        assert resolver.resolve_scopes("default") == [SPREADSHEETS, DRIVE_FILE]

    def test_explicit_comma_list(self):
        custom = "https://www.googleapis.com/auth/spreadsheets.readonly,https://example/x"
        assert resolver.resolve_scopes(custom) == [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://example/x",
        ]

    def test_explicit_comma_list_strips_and_dedups(self):
        assert resolver.resolve_scopes(f" {SPREADSHEETS} , {SPREADSHEETS} , {DRIVE} ") == [
            SPREADSHEETS,
            DRIVE,
        ]

    def test_case_insensitive_keywords(self):
        assert resolver.resolve_scopes("DEFAULT") == [SPREADSHEETS, DRIVE_FILE]
        assert resolver.resolve_scopes("Broad") == [SPREADSHEETS, DRIVE_FILE, DRIVE]

    def test_empty_string_is_default(self):
        assert resolver.resolve_scopes("") == [SPREADSHEETS, DRIVE_FILE]

    def test_returns_a_fresh_list_each_call(self):
        a = resolver.resolve_scopes("default")
        a.append("mutated")
        b = resolver.resolve_scopes("default")
        assert b == [SPREADSHEETS, DRIVE_FILE]

    def test_whitespace_only_commas_raises(self):
        with pytest.raises(SheetsError) as exc:
            resolver.resolve_scopes(" , , ")
        assert exc.value.code == "bad_scopes"


# ===========================================================================
# resolve_credentials — Service Account (DESIGN §2.2 step 1)
# ===========================================================================


class TestServiceAccount:
    def test_sa_file_env_selects_service_account(self, monkeypatch, tmp_path):
        key = _write_sa_key(tmp_path / "sa.json")
        monkeypatch.setenv("GSHEETS_SERVICE_ACCOUNT_FILE", str(key))

        sa_creds = _make_creds()
        from_file = MagicMock(return_value=sa_creds)
        monkeypatch.setattr(
            "google.oauth2.service_account.Credentials.from_service_account_file", from_file
        )

        out = resolver.resolve_credentials([SPREADSHEETS, DRIVE_FILE])

        assert out is sa_creds
        from_file.assert_called_once_with(str(key), scopes=[SPREADSHEETS, DRIVE_FILE])

    def test_gac_pointing_at_sa_json_selects_service_account(self, monkeypatch, tmp_path):
        key = _write_sa_key(tmp_path / "gac_sa.json")
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key))

        sa_creds = _make_creds()
        from_file = MagicMock(return_value=sa_creds)
        monkeypatch.setattr(
            "google.oauth2.service_account.Credentials.from_service_account_file", from_file
        )

        out = resolver.resolve_credentials([SPREADSHEETS])
        assert out is sa_creds
        from_file.assert_called_once()

    def test_gac_pointing_at_non_sa_json_does_not_select_sa(self, monkeypatch, tmp_path):
        # A user (authorized_user) JSON at GOOGLE_APPLICATION_CREDENTIALS must NOT trigger SA;
        # with no token and no ADC stub we expect it to fall through to ADC.
        user_json = tmp_path / "user.json"
        user_json.write_text(json.dumps({"type": "authorized_user"}))
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(user_json))

        sa_from_file = MagicMock()
        monkeypatch.setattr(
            "google.oauth2.service_account.Credentials.from_service_account_file", sa_from_file
        )
        adc_creds = _make_creds()
        monkeypatch.setattr("google.auth.default", MagicMock(return_value=(adc_creds, "proj")))

        out = resolver.resolve_credentials([SPREADSHEETS])

        assert out is adc_creds
        sa_from_file.assert_not_called()

    def test_sa_takes_precedence_over_token(self, monkeypatch, tmp_path):
        # Both SA file and a cached token present -> SA wins (precedence step 1 before 2).
        key = _write_sa_key(tmp_path / "sa.json")
        monkeypatch.setenv("GSHEETS_SERVICE_ACCOUNT_FILE", str(key))
        token = _write_user_token(tmp_path / "token.json")
        monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(token))

        sa_creds = _make_creds()
        monkeypatch.setattr(
            "google.oauth2.service_account.Credentials.from_service_account_file",
            MagicMock(return_value=sa_creds),
        )
        oauth_from_file = MagicMock()
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.from_authorized_user_file", oauth_from_file
        )

        out = resolver.resolve_credentials([SPREADSHEETS])
        assert out is sa_creds
        oauth_from_file.assert_not_called()

    def test_sa_file_env_set_but_missing_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GSHEETS_SERVICE_ACCOUNT_FILE", str(tmp_path / "nope.json"))
        with pytest.raises(SheetsError) as exc:
            resolver.resolve_credentials([SPREADSHEETS])
        assert exc.value.code == "service_account_missing"

    def test_forced_service_account_mode_without_input_raises(self, monkeypatch):
        monkeypatch.setenv("GSHEETS_AUTH_MODE", "service_account")
        with pytest.raises(SheetsError) as exc:
            resolver.resolve_credentials([SPREADSHEETS])
        assert exc.value.code == "service_account_missing"

    def test_sa_expired_creds_are_refreshed(self, monkeypatch, tmp_path):
        key = _write_sa_key(tmp_path / "sa.json")
        monkeypatch.setenv("GSHEETS_SERVICE_ACCOUNT_FILE", str(key))
        creds = _make_creds(valid=False, expired=True, refresh_token=None)
        # SA creds refresh against the token endpoint; refresh_token None but valid==False.
        creds.refresh_token = "sa"  # SA still refreshes; just exercise the refresh branch.
        monkeypatch.setattr(
            "google.oauth2.service_account.Credentials.from_service_account_file",
            MagicMock(return_value=creds),
        )
        request_obj = object()
        monkeypatch.setattr(
            "google.auth.transport.requests.Request", MagicMock(return_value=request_obj)
        )
        out = resolver.resolve_credentials([SPREADSHEETS])
        assert out is creds
        creds.refresh.assert_called_once_with(request_obj)


# ===========================================================================
# resolve_credentials — OAuth desktop, token-present (DESIGN §2.2 step 2)
# ===========================================================================


class TestOAuthTokenPresent:
    def test_valid_token_used_as_is_no_client_file(self, monkeypatch, tmp_path):
        token = _write_user_token(tmp_path / "token.json")
        monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(token))
        # Note: no GSHEETS_OAUTH_CLIENT_FILE set, and the default path does not exist.

        creds = _make_creds(valid=True)
        from_file = MagicMock(return_value=creds)
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.from_authorized_user_file", from_file
        )
        request_cls = MagicMock(name="Request")
        monkeypatch.setattr("google.auth.transport.requests.Request", request_cls)

        out = resolver.resolve_credentials([SPREADSHEETS, DRIVE_FILE])

        assert out is creds
        # Scope reconciliation (DESIGN §2.2): the token is loaded with scopes=None so it adopts
        # (and refreshes against) its OWN granted scope set — never the requested list, which
        # would make Google's refresh-grant reject a non-subset request with `invalid_scope`.
        from_file.assert_called_once_with(str(token), None)
        # Valid token -> no refresh, no consent.
        creds.refresh.assert_not_called()
        request_cls.assert_not_called()

    def test_expired_token_refreshed_and_repersisted(self, monkeypatch, tmp_path):
        token = tmp_path / "token.json"
        _write_user_token(token)
        monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(token))

        creds = _make_creds(valid=False, expired=True, to_json='{"token": "refreshed"}')
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.from_authorized_user_file",
            MagicMock(return_value=creds),
        )
        request_obj = object()
        request_cls = MagicMock(return_value=request_obj)
        monkeypatch.setattr("google.auth.transport.requests.Request", request_cls)

        out = resolver.resolve_credentials([SPREADSHEETS])

        assert out is creds
        creds.refresh.assert_called_once_with(request_obj)
        # Refreshed token re-persisted to the same path.
        assert token.read_text() == '{"token": "refreshed"}'

    def test_token_load_failure_raises_token_invalid(self, monkeypatch, tmp_path):
        token = tmp_path / "token.json"
        token.write_text("not json")
        monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(token))

        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.from_authorized_user_file",
            MagicMock(side_effect=ValueError("bad token")),
        )
        with pytest.raises(SheetsError) as exc:
            resolver.resolve_credentials([SPREADSHEETS])
        assert exc.value.code == "oauth_token_invalid"

    def test_refresh_failure_raises_refresh_failed(self, monkeypatch, tmp_path):
        token = tmp_path / "token.json"
        _write_user_token(token)
        monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(token))

        creds = _make_creds(valid=False, expired=True)
        creds.refresh.side_effect = RuntimeError("refresh boom")
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.from_authorized_user_file",
            MagicMock(return_value=creds),
        )
        monkeypatch.setattr(
            "google.auth.transport.requests.Request", MagicMock(return_value=object())
        )
        with pytest.raises(SheetsError) as exc:
            resolver.resolve_credentials([SPREADSHEETS])
        assert exc.value.code == "oauth_refresh_failed"

    def test_token_present_never_triggers_installed_app_flow(self, monkeypatch, tmp_path):
        # Guard: the token-present path must not import/run InstalledAppFlow consent.
        token = _write_user_token(tmp_path / "token.json")
        monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(token))
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.from_authorized_user_file",
            MagicMock(return_value=_make_creds(valid=True)),
        )
        flow_factory = MagicMock(name="InstalledAppFlow.from_client_secrets_file")
        monkeypatch.setattr(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file", flow_factory
        )

        resolver.resolve_credentials([SPREADSHEETS])
        flow_factory.assert_not_called()

    def test_token_loaded_with_none_scopes_to_honor_grant(self, monkeypatch, tmp_path):
        # Regression (DESIGN §2.2): a token granted the BROAD `drive` scope must satisfy a
        # request for the narrow default set WITHOUT forcing `drive.file` onto the refresh
        # (Google rejects that with `invalid_scope`). The token is loaded scopes=None and its
        # granted set (`drive` ⊇ `drive.file` for coverage) covers the default request.
        token = _write_user_token(tmp_path / "token.json")
        monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(token))

        # The live credential reports the broad grant Google actually issued.
        creds = _make_creds(valid=True, scopes=[SPREADSHEETS, DRIVE])
        from_file = MagicMock(return_value=creds)
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.from_authorized_user_file", from_file
        )

        out = resolver.resolve_credentials([SPREADSHEETS, DRIVE_FILE])

        assert out is creds
        # Loaded with scopes=None: the credential refreshes against its own grant, not the
        # requested narrow list — this is the fix for the `invalid_scope` refresh failure.
        from_file.assert_called_once_with(str(token), None)

    def test_token_with_insufficient_scopes_raises(self, monkeypatch, tmp_path):
        # A token granted only `spreadsheets` cannot satisfy a request that also needs Drive.
        token = _write_user_token(tmp_path / "token.json")
        monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(token))

        creds = _make_creds(valid=True, scopes=[SPREADSHEETS])
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.from_authorized_user_file",
            MagicMock(return_value=creds),
        )

        with pytest.raises(SheetsError) as exc:
            resolver.resolve_credentials([SPREADSHEETS, DRIVE_FILE])
        assert exc.value.code == "oauth_scope_insufficient"
        assert "gsheets auth login" in (exc.value.hint or "")

    def test_token_scopes_unknown_is_trusted(self, monkeypatch, tmp_path):
        # An older token exposing no scope view (neither live creds.scopes nor embedded scopes)
        # cannot be verified, so it is trusted rather than rejected.
        token = _write_user_token(tmp_path / "token.json")  # writes no "scopes" key
        monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(token))

        creds = _make_creds(valid=True, scopes=[])  # falsy live scope view -> fall back to file
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.from_authorized_user_file",
            MagicMock(return_value=creds),
        )

        out = resolver.resolve_credentials([SPREADSHEETS, DRIVE_FILE])
        assert out is creds  # no scope info anywhere -> trusted, no error


# ===========================================================================
# Scope reconciliation helpers (DESIGN §2.2)
# ===========================================================================


class TestScopeReconciliation:
    def test_token_granted_scopes_reads_list(self, tmp_path):
        path = tmp_path / "token.json"
        path.write_text(
            json.dumps(
                {
                    "client_id": "cid",
                    "client_secret": "secret",
                    "refresh_token": "rt",
                    "type": "authorized_user",
                    "scopes": [SPREADSHEETS, DRIVE],
                }
            )
        )
        assert resolver._token_granted_scopes(path) == [SPREADSHEETS, DRIVE]

    def test_token_granted_scopes_reads_space_delimited_string(self, tmp_path):
        path = tmp_path / "token.json"
        path.write_text(json.dumps({"scopes": f"{SPREADSHEETS} {DRIVE}"}))
        assert resolver._token_granted_scopes(path) == [SPREADSHEETS, DRIVE]

    def test_token_granted_scopes_none_when_absent(self, tmp_path):
        path = tmp_path / "token.json"
        path.write_text(json.dumps({"client_id": "cid"}))
        assert resolver._token_granted_scopes(path) is None

    def test_token_granted_scopes_none_on_bad_file(self, tmp_path):
        path = tmp_path / "missing.json"
        assert resolver._token_granted_scopes(path) is None

    def test_scopes_covered_direct_membership(self):
        assert resolver._scopes_covered([SPREADSHEETS], [SPREADSHEETS, DRIVE_FILE])

    def test_scopes_covered_broad_drive_covers_drive_file(self):
        # `drive` (whole-Drive) is a functional superset of `drive.file` for COVERAGE.
        assert resolver._scopes_covered([SPREADSHEETS, DRIVE_FILE], [SPREADSHEETS, DRIVE])

    def test_scopes_not_covered_when_missing(self):
        assert not resolver._scopes_covered([SPREADSHEETS, DRIVE_FILE], [SPREADSHEETS])

    def test_embedded_token_scopes_used_when_live_view_absent(self, monkeypatch, tmp_path):
        # creds.scopes falsy -> coverage falls back to the token file's embedded "scopes".
        path = tmp_path / "token.json"
        path.write_text(
            json.dumps(
                {
                    "client_id": "cid",
                    "client_secret": "secret",
                    "refresh_token": "rt",
                    "type": "authorized_user",
                    "scopes": [SPREADSHEETS, DRIVE],
                }
            )
        )
        monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(path))
        creds = _make_creds(valid=True, scopes=[])  # no live scope view
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.from_authorized_user_file",
            MagicMock(return_value=creds),
        )
        # Embedded `drive` covers the requested `drive.file`; no error.
        out = resolver.resolve_credentials([SPREADSHEETS, DRIVE_FILE])
        assert out is creds


# ===========================================================================
# resolve_credentials — OAuth missing token -> fall through / errors (DESIGN §2.2)
# ===========================================================================


class TestOAuthMissingToken:
    def test_auto_no_token_falls_through_to_adc(self, monkeypatch):
        # No SA, no token -> ADC fallback in auto mode (token-absent is NOT an error in auto).
        adc_creds = _make_creds(valid=True)
        adc_default = MagicMock(return_value=(adc_creds, "proj"))
        monkeypatch.setattr("google.auth.default", adc_default)
        oauth_from_file = MagicMock()
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.from_authorized_user_file", oauth_from_file
        )

        out = resolver.resolve_credentials([SPREADSHEETS])

        assert out is adc_creds
        adc_default.assert_called_once_with(scopes=[SPREADSHEETS])
        oauth_from_file.assert_not_called()

    def test_forced_oauth_no_token_no_client_raises_client_missing(self, monkeypatch):
        monkeypatch.setenv("GSHEETS_AUTH_MODE", "oauth")
        # No token file, no client file (default path does not exist) -> oauth_client_missing.
        with pytest.raises(SheetsError) as exc:
            resolver.resolve_credentials([SPREADSHEETS])
        assert exc.value.code == "oauth_client_missing"
        assert "gsheets auth login" in (exc.value.hint or "")

    def test_forced_oauth_no_token_but_client_present_raises_consent_required(
        self, monkeypatch, tmp_path
    ):
        # Client file exists but no token -> consent required; that path is CLI-only, NOT here.
        client = tmp_path / "client.json"
        client.write_text(json.dumps({"installed": {"client_id": "cid"}}))
        monkeypatch.setenv("GSHEETS_AUTH_MODE", "oauth")
        monkeypatch.setenv("GSHEETS_OAUTH_CLIENT_FILE", str(client))

        flow_factory = MagicMock()
        monkeypatch.setattr(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file", flow_factory
        )
        with pytest.raises(SheetsError) as exc:
            resolver.resolve_credentials([SPREADSHEETS])
        assert exc.value.code == "oauth_consent_required"
        # Critically: resolver must NOT run interactive consent.
        flow_factory.assert_not_called()

    def test_forced_oauth_uses_token_when_present(self, monkeypatch, tmp_path):
        token = _write_user_token(tmp_path / "token.json")
        monkeypatch.setenv("GSHEETS_AUTH_MODE", "oauth")
        monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(token))
        creds = _make_creds(valid=True)
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.from_authorized_user_file",
            MagicMock(return_value=creds),
        )
        out = resolver.resolve_credentials([SPREADSHEETS])
        assert out is creds


# ===========================================================================
# resolve_credentials — ADC (DESIGN §2.2 step 3)
# ===========================================================================


class TestADC:
    def test_forced_adc_mode(self, monkeypatch):
        monkeypatch.setenv("GSHEETS_AUTH_MODE", "adc")
        adc_creds = _make_creds(valid=True)
        adc_default = MagicMock(return_value=(adc_creds, "proj"))
        monkeypatch.setattr("google.auth.default", adc_default)

        out = resolver.resolve_credentials([SPREADSHEETS, DRIVE_FILE])
        assert out is adc_creds
        adc_default.assert_called_once_with(scopes=[SPREADSHEETS, DRIVE_FILE])

    def test_adc_failure_raises_no_credentials(self, monkeypatch):
        monkeypatch.setenv("GSHEETS_AUTH_MODE", "adc")
        monkeypatch.setattr(
            "google.auth.default", MagicMock(side_effect=Exception("no ADC here"))
        )
        with pytest.raises(SheetsError) as exc:
            resolver.resolve_credentials([SPREADSHEETS])
        assert exc.value.code == "no_credentials"

    def test_adc_expired_creds_are_refreshed(self, monkeypatch):
        monkeypatch.setenv("GSHEETS_AUTH_MODE", "adc")
        creds = _make_creds(valid=False, expired=True)
        monkeypatch.setattr("google.auth.default", MagicMock(return_value=(creds, "proj")))
        request_obj = object()
        monkeypatch.setattr(
            "google.auth.transport.requests.Request", MagicMock(return_value=request_obj)
        )
        out = resolver.resolve_credentials([SPREADSHEETS])
        assert out is creds
        creds.refresh.assert_called_once_with(request_obj)


# ===========================================================================
# resolve_credentials — mode handling
# ===========================================================================


class TestAuthMode:
    def test_unknown_mode_raises(self, monkeypatch):
        monkeypatch.setenv("GSHEETS_AUTH_MODE", "bogus")
        with pytest.raises(SheetsError) as exc:
            resolver.resolve_credentials([SPREADSHEETS])
        assert exc.value.code == "unknown_auth_mode"

    def test_mode_is_case_insensitive(self, monkeypatch, tmp_path):
        key = _write_sa_key(tmp_path / "sa.json")
        monkeypatch.setenv("GSHEETS_AUTH_MODE", "Service_Account")
        monkeypatch.setenv("GSHEETS_SERVICE_ACCOUNT_FILE", str(key))
        sa_creds = _make_creds()
        monkeypatch.setattr(
            "google.oauth2.service_account.Credentials.from_service_account_file",
            MagicMock(return_value=sa_creds),
        )
        out = resolver.resolve_credentials([SPREADSHEETS])
        assert out is sa_creds

    def test_empty_mode_defaults_to_auto(self, monkeypatch):
        monkeypatch.setenv("GSHEETS_AUTH_MODE", "")
        adc_creds = _make_creds(valid=True)
        monkeypatch.setattr("google.auth.default", MagicMock(return_value=(adc_creds, "p")))
        out = resolver.resolve_credentials([SPREADSHEETS])
        assert out is adc_creds


# ===========================================================================
# Full auto-precedence ordering (SA -> OAuth -> ADC), integrated assertions
# ===========================================================================


class TestAutoPrecedence:
    def test_precedence_sa_first(self, monkeypatch, tmp_path):
        key = _write_sa_key(tmp_path / "sa.json")
        token = _write_user_token(tmp_path / "token.json")
        monkeypatch.setenv("GSHEETS_SERVICE_ACCOUNT_FILE", str(key))
        monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(token))

        sa_creds = _make_creds()
        oauth_creds = _make_creds()
        adc_creds = _make_creds()
        monkeypatch.setattr(
            "google.oauth2.service_account.Credentials.from_service_account_file",
            MagicMock(return_value=sa_creds),
        )
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.from_authorized_user_file",
            MagicMock(return_value=oauth_creds),
        )
        monkeypatch.setattr("google.auth.default", MagicMock(return_value=(adc_creds, "p")))
        assert resolver.resolve_credentials([SPREADSHEETS]) is sa_creds

    def test_precedence_oauth_second(self, monkeypatch, tmp_path):
        token = _write_user_token(tmp_path / "token.json")
        monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(token))

        oauth_creds = _make_creds(valid=True)
        adc_creds = _make_creds()
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.from_authorized_user_file",
            MagicMock(return_value=oauth_creds),
        )
        adc_default = MagicMock(return_value=(adc_creds, "p"))
        monkeypatch.setattr("google.auth.default", adc_default)

        out = resolver.resolve_credentials([SPREADSHEETS])
        assert out is oauth_creds
        adc_default.assert_not_called()

    def test_precedence_adc_last(self, monkeypatch):
        adc_creds = _make_creds(valid=True)
        adc_default = MagicMock(return_value=(adc_creds, "p"))
        monkeypatch.setattr("google.auth.default", adc_default)
        assert resolver.resolve_credentials([SPREADSHEETS]) is adc_creds


# ===========================================================================
# Config-dir / path resolution honors GSHEETS_CONFIG_DIR default token path
# ===========================================================================


class TestPathResolution:
    def test_default_token_path_under_config_dir(self, monkeypatch, clean_auth_env):
        # No GSHEETS_TOKEN_FILE -> token resolves under the (tmp) config dir; if it exists it
        # is loaded. Place a token at <config_dir>/token.json and assert it is used.
        token = clean_auth_env / "token.json"
        _write_user_token(token)
        creds = _make_creds(valid=True)
        from_file = MagicMock(return_value=creds)
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.from_authorized_user_file", from_file
        )
        out = resolver.resolve_credentials([SPREADSHEETS])
        assert out is creds
        # Loaded from the default <config_dir>/token.json path.
        assert from_file.call_args.args[0] == str(token)


# ===========================================================================
# Boundary: resolver must stay transport/CLI free (DESIGN §1)
# ===========================================================================


def test_resolver_module_has_no_transport_imports():
    import gsheets.auth.resolver as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("import fastmcp", "import argparse", "from fastmcp", "import pydantic"):
        assert forbidden not in src
    # ``import mcp`` / ``from mcp`` (the PyPI transport) must not appear as import statements.
    assert "\nimport mcp" not in src
    assert "from mcp" not in src
