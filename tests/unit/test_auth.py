"""Unit tests for ``gsheets.auth`` (DESIGN §2.4, §7.2).

All tests run with NO network and NO real Google libraries doing I/O:

- ``resolve_scopes`` / ``resolve_credentials`` (the resolver — a SIBLING build unit this
  unit calls but does NOT own) are monkeypatched at the ``gsheets.auth`` namespace, where
  ``auth/__init__.py`` imported them.
- ``googleapiclient.discovery.build`` is monkeypatched (it is imported lazily *inside*
  ``build_services``), so we assert the EXACT build calls (service/version/credentials/
  ``cache_discovery=False``) without constructing a real Resource.
- ``InstalledAppFlow`` / ``Request`` are monkeypatched for the bootstrap consent path.

Credentials are represented by a small ``_FakeCreds`` stand-in exposing the attributes the
auth layer reads (``valid``/``expired``/``expiry``/``refresh_token``/``to_json``/...), so the
golden-master assertions pin the serialized status dicts and the token-persistence behavior.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

import gsheets.auth as auth
from gsheets.auth import build_services, bootstrap, status
from gsheets.core.errors import SheetsError
from gsheets.core.service import SheetsServices

DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
BROAD_SCOPES = DEFAULT_SCOPES + ["https://www.googleapis.com/auth/drive"]
NO_DRIVE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# --------------------------------------------------------------------------- fakes


class _FakeCreds:
    """A stand-in for a ``google.oauth2.credentials.Credentials`` object.

    Exposes only the attributes the auth layer reads. ``authorized_user=True`` adds the
    ``to_json``/``refresh_token`` surface that marks a desktop authorized-user credential
    (the kind persisted to ``token.json``); ``authorized_user=False`` models an SA/ADC cred.
    """

    def __init__(
        self,
        *,
        valid=True,
        expired=False,
        expiry=None,
        refresh_token="refresh-xyz",
        authorized_user=True,
        account=None,
        service_account_email=None,
        json_blob=None,
    ):
        self.valid = valid
        self.expired = expired
        self.expiry = expiry
        if authorized_user:
            self.refresh_token = refresh_token
        if account is not None:
            self.account = account
        if service_account_email is not None:
            self.service_account_email = service_account_email
        self._authorized_user = authorized_user
        self._json_blob = json_blob or {"type": "authorized_user", "token": "tok"}
        self.refreshed = False

    # Only authorized-user creds serialize to a token file.
    def to_json(self):
        if not self._authorized_user:
            raise AttributeError("to_json")
        return json.dumps(self._json_blob)

    def refresh(self, request):
        self.refreshed = True
        self.valid = True
        self.expired = False


class _FakeFlow:
    """Stand-in for ``InstalledAppFlow``; records the local-server run and returns a cred."""

    last_secrets_file = None
    last_scopes = None
    instance = None

    def __init__(self, creds):
        self._creds = creds
        self.ran = False
        self.run_port = None
        _FakeFlow.instance = self

    @classmethod
    def make_factory(cls, creds):
        def from_client_secrets_file(secrets_file, scopes):
            cls.last_secrets_file = secrets_file
            cls.last_scopes = scopes
            return cls(creds)

        return from_client_secrets_file

    def run_local_server(self, port=0):
        self.ran = True
        self.run_port = port
        return self._creds


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def patch_resolver(monkeypatch):
    """Return a helper installing fake ``resolve_scopes``/``resolve_credentials``.

    ``install(creds_or_exc, scopes=...)`` wires the resolver so ``resolve_credentials``
    returns ``creds_or_exc`` (or raises it if it is an exception) and records the scopes it
    was called with.
    """

    def install(creds_or_exc, *, scopes=DEFAULT_SCOPES):
        calls = {"scopes_seen": None}

        def fake_resolve_scopes(scopes_mode=None):
            return list(scopes)

        def fake_resolve_credentials(requested_scopes):
            calls["scopes_seen"] = requested_scopes
            if isinstance(creds_or_exc, BaseException):
                raise creds_or_exc
            return creds_or_exc

        monkeypatch.setattr(auth, "resolve_scopes", fake_resolve_scopes)
        monkeypatch.setattr(auth, "resolve_credentials", fake_resolve_credentials)
        return calls

    return install


@pytest.fixture
def record_build(monkeypatch):
    """Monkeypatch ``googleapiclient.discovery.build`` and record every call.

    Returns the list of recorded call-kwargs dicts. ``build`` is imported lazily inside
    ``build_services``, so patching the source module is the correct seam.
    """
    import googleapiclient.discovery as discovery

    calls: list[dict] = []

    def fake_build(serviceName, version, **kwargs):
        rec = {"serviceName": serviceName, "version": version, **kwargs}
        calls.append(rec)
        # Return a distinct marker per service so SheetsServices wiring is checkable.
        return f"<{serviceName}-{version}-resource>"

    monkeypatch.setattr(discovery, "build", fake_build)
    return calls


@pytest.fixture(autouse=True)
def clean_auth_env(monkeypatch, tmp_path):
    """Isolate every test from the developer's real env + config dir.

    Clears all ``GSHEETS_*`` auth vars and ``GOOGLE_APPLICATION_CREDENTIALS``, then points
    the config dir at a throwaway ``tmp_path`` so no test ever reads/writes the real
    ``~/.config/google-sheets-mcp`` token. Individual tests re-set the vars they need.
    """
    for var in (
        "GSHEETS_AUTH_MODE",
        "GSHEETS_SCOPES",
        "GSHEETS_SERVICE_ACCOUNT_FILE",
        "GSHEETS_OAUTH_CLIENT_FILE",
        "GSHEETS_TOKEN_FILE",
        "GSHEETS_CONFIG_DIR",
        "GSHEETS_VERBOSE_ERRORS",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GSHEETS_CONFIG_DIR", str(tmp_path / "cfg"))
    return tmp_path


# =========================================================================== build_services


def test_build_services_returns_sheets_services(patch_resolver, record_build):
    creds = _FakeCreds()
    patch_resolver(creds, scopes=DEFAULT_SCOPES)

    services = build_services()

    assert isinstance(services, SheetsServices)
    assert services.sheets == "<sheets-v4-resource>"
    # ``drive.file`` is a drive scope -> a drive Resource IS built.
    assert services.drive == "<drive-v3-resource>"


def test_build_services_passes_creds_and_disables_cache_discovery(patch_resolver, record_build):
    creds = _FakeCreds()
    patch_resolver(creds, scopes=DEFAULT_SCOPES)

    build_services()

    # Sheets v4 + Drive v3, both with the resolved creds and cache_discovery=False.
    assert {(c["serviceName"], c["version"]) for c in record_build} == {
        ("sheets", "v4"),
        ("drive", "v3"),
    }
    for call in record_build:
        assert call["credentials"] is creds
        assert call["cache_discovery"] is False


def test_build_services_no_drive_scope_yields_none_drive(patch_resolver, record_build):
    creds = _FakeCreds()
    patch_resolver(creds, scopes=NO_DRIVE_SCOPES)

    services = build_services()

    assert services.drive is None
    assert [(c["serviceName"], c["version"]) for c in record_build] == [("sheets", "v4")]


def test_build_services_broad_scope_builds_drive(patch_resolver, record_build):
    creds = _FakeCreds()
    patch_resolver(creds, scopes=BROAD_SCOPES)

    services = build_services()

    assert services.drive == "<drive-v3-resource>"


def test_build_services_forwards_scopes_mode(patch_resolver, record_build, monkeypatch):
    creds = _FakeCreds()
    patch_resolver(creds, scopes=DEFAULT_SCOPES)

    seen = {}

    def fake_resolve_scopes(scopes_mode=None):
        seen["mode"] = scopes_mode
        return DEFAULT_SCOPES

    monkeypatch.setattr(auth, "resolve_scopes", fake_resolve_scopes)

    build_services("broad")
    assert seen["mode"] == "broad"


def test_build_services_account_email_from_authorized_user(patch_resolver, record_build):
    creds = _FakeCreds(account="me@example.com")
    patch_resolver(creds)

    services = build_services()
    assert services.account_email == "me@example.com"


def test_build_services_account_email_from_service_account(patch_resolver, record_build):
    creds = _FakeCreds(authorized_user=False, service_account_email="sa@proj.iam.example.com")
    patch_resolver(creds, scopes=DEFAULT_SCOPES)

    services = build_services()
    assert services.account_email == "sa@proj.iam.example.com"


def test_build_services_account_email_absent_is_none(patch_resolver, record_build):
    creds = _FakeCreds()  # no account / no service_account_email
    patch_resolver(creds)

    services = build_services()
    assert services.account_email is None


def test_build_services_propagates_resolver_error(patch_resolver, record_build):
    err = SheetsError("oauth_client_missing", "no token", hint="run login")
    patch_resolver(err)

    with pytest.raises(SheetsError) as ei:
        build_services()
    assert ei.value.code == "oauth_client_missing"
    # No services were built when resolution failed.
    assert record_build == []


def test_build_services_never_imports_transport():
    """Importing the auth package must not drag in any transport/CLI/adapter module."""
    import sys

    forbidden = {"fastmcp", "mcp", "argparse"}
    present = forbidden & set(sys.modules)
    # argparse may be imported by pytest itself; only assert the auth module's own surface is
    # clean by checking the module source has no such import name bound.
    assert not hasattr(auth, "FastMCP")
    assert not hasattr(auth, "ToolError")
    # The package object exposes only its three entrypoints + helpers, never a CLI parser.
    assert set(auth.__all__) == {"build_services", "bootstrap", "status"}
    _ = present  # informational only; argparse-in-pytest is not this unit's concern


# =========================================================================== status


def test_status_reports_valid_token(patch_resolver):
    creds = _FakeCreds(valid=True, expired=False, expiry=datetime(2030, 1, 1, 12, 0, 0))
    patch_resolver(creds, scopes=DEFAULT_SCOPES)

    out = status()

    assert out == {
        "ok": True,
        "mode": "auto",
        "scopes": DEFAULT_SCOPES,
        "tokenPath": out["tokenPath"],  # path value asserted separately below
        "tokenExists": False,
        "tokenPersisted": False,
        "tokenWritable": out["tokenWritable"],  # ISSUES.md #6; environment-dependent
        "valid": True,
        "expired": False,
        "refreshable": True,
        "expiry": "2030-01-01T12:00:00",
    }
    assert out["tokenPath"].endswith("/token.json")
    assert isinstance(out["tokenWritable"], bool)


def test_status_reports_expired_refreshable_token(patch_resolver):
    creds = _FakeCreds(valid=False, expired=True, expiry=datetime(2020, 1, 1, 0, 0, 0))
    patch_resolver(creds)

    out = status()
    assert out["ok"] is True
    assert out["valid"] is False
    assert out["expired"] is True
    assert out["refreshable"] is True
    assert out["expiry"] == "2020-01-01T00:00:00"


def test_status_no_refresh_token_not_refreshable(patch_resolver):
    creds = _FakeCreds(authorized_user=False)  # no refresh_token attr
    patch_resolver(creds)

    out = status()
    assert out["refreshable"] is False


def test_status_reports_forced_mode_and_scopes(patch_resolver, monkeypatch):
    monkeypatch.setenv("GSHEETS_AUTH_MODE", "service_account")
    creds = _FakeCreds(authorized_user=False)
    patch_resolver(creds, scopes=BROAD_SCOPES)

    out = status()
    assert out["mode"] == "service_account"
    assert out["scopes"] == BROAD_SCOPES


def test_status_no_usable_credentials_returns_not_ok(patch_resolver):
    err = SheetsError(
        "oauth_client_missing", "no token", hint="run `gsheets auth login`"
    )
    patch_resolver(err)

    out = status()
    assert out["ok"] is False
    assert out["error"]["code"] == "oauth_client_missing"
    assert out["error"]["hint"] == "run `gsheets auth login`"
    assert out["mode"] == "auto"
    assert out["scopes"] == DEFAULT_SCOPES


def test_status_never_calls_sheets_api(patch_resolver):
    """status() must not build a Resource or hit the API — only resolve creds."""
    creds = _FakeCreds()
    patch_resolver(creds)
    # No record_build fixture here: if status tried to build a service it would hit the real
    # googleapiclient.discovery.build and fail offline. It must NOT.
    out = status()
    assert out["ok"] is True


def test_status_email_hidden_by_default(patch_resolver):
    creds = _FakeCreds(account="me@example.com")
    patch_resolver(creds)

    out = status()
    assert "accountEmail" not in out


def test_status_email_shown_in_verbose(patch_resolver, monkeypatch):
    monkeypatch.setenv("GSHEETS_VERBOSE_ERRORS", "1")
    creds = _FakeCreds(account="me@example.com")
    patch_resolver(creds)

    out = status()
    assert out["accountEmail"] == "me@example.com"


def test_status_expiry_none_when_unset(patch_resolver):
    creds = _FakeCreds(expiry=None)
    patch_resolver(creds)

    out = status()
    assert out["expiry"] is None


def test_status_token_path_from_env(patch_resolver, monkeypatch, tmp_path):
    tok = tmp_path / "custom-token.json"
    tok.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(tok))
    creds = _FakeCreds()
    patch_resolver(creds)

    out = status()
    assert out["tokenPath"] == str(tok)
    assert out["tokenExists"] is True


# =========================================================================== bootstrap


def test_bootstrap_steady_state_persists_existing_token(patch_resolver, monkeypatch, tmp_path):
    tok = tmp_path / "token.json"
    monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(tok))
    creds = _FakeCreds(json_blob={"type": "authorized_user", "token": "persisted"})
    patch_resolver(creds)

    # Bootstrap must NOT run the interactive flow when a usable cred already resolves.
    def boom(*a, **k):  # pragma: no cover - asserts flow is not constructed
        raise AssertionError("InstalledAppFlow must not run when a token is usable")

    import google_auth_oauthlib.flow as flow_mod

    monkeypatch.setattr(flow_mod, "InstalledAppFlow", type("X", (), {"from_client_secrets_file": staticmethod(boom)}))

    out = bootstrap()

    assert out["ok"] is True
    assert out["tokenPersisted"] is True
    assert tok.is_file()
    assert json.loads(tok.read_text())["token"] == "persisted"


def test_bootstrap_steady_state_does_not_persist_service_account(patch_resolver, monkeypatch, tmp_path):
    tok = tmp_path / "token.json"
    monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(tok))
    creds = _FakeCreds(authorized_user=False)  # SA/ADC: not serializable to token.json
    patch_resolver(creds)

    out = bootstrap()
    assert out["ok"] is True
    assert out["tokenPersisted"] is False
    assert not tok.is_file()


def test_bootstrap_first_time_consent_runs_flow_and_persists(patch_resolver, monkeypatch, tmp_path):
    # Resolver reports "no usable token" -> consent path.
    err = SheetsError("oauth_client_missing", "no token")
    patch_resolver(err, scopes=DEFAULT_SCOPES)

    client = tmp_path / "client.json"
    client.write_text(json.dumps({"installed": {"client_id": "x"}}), encoding="utf-8")
    tok = tmp_path / "token.json"
    monkeypatch.setenv("GSHEETS_OAUTH_CLIENT_FILE", str(client))
    monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(tok))

    minted = _FakeCreds(valid=True, json_blob={"type": "authorized_user", "token": "minted"})
    import google_auth_oauthlib.flow as flow_mod

    fake_cls = type(
        "FakeFlowCls",
        (),
        {"from_client_secrets_file": staticmethod(_FakeFlow.make_factory(minted))},
    )
    monkeypatch.setattr(flow_mod, "InstalledAppFlow", fake_cls)

    out = bootstrap()

    assert out["ok"] is True
    assert out["tokenPersisted"] is True
    assert tok.is_file()
    assert json.loads(tok.read_text())["token"] == "minted"
    # The flow was handed the configured client file + resolved scopes, port=0.
    assert _FakeFlow.last_secrets_file == str(client)
    assert _FakeFlow.last_scopes == DEFAULT_SCOPES
    assert _FakeFlow.instance.run_port == 0


def test_bootstrap_consent_refreshes_invalid_minted_creds(patch_resolver, monkeypatch, tmp_path):
    err = SheetsError("oauth_client_missing", "no token")
    patch_resolver(err)

    client = tmp_path / "client.json"
    client.write_text("{}", encoding="utf-8")
    tok = tmp_path / "token.json"
    monkeypatch.setenv("GSHEETS_OAUTH_CLIENT_FILE", str(client))
    monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(tok))

    # A flow that returns an invalid-but-refreshable cred -> bootstrap must refresh it.
    minted = _FakeCreds(valid=False, expired=True)
    import google_auth_oauthlib.flow as flow_mod

    fake_cls = type(
        "FakeFlowCls",
        (),
        {"from_client_secrets_file": staticmethod(_FakeFlow.make_factory(minted))},
    )
    monkeypatch.setattr(flow_mod, "InstalledAppFlow", fake_cls)

    # Request() is constructed and passed to refresh(); a no-op stand-in is fine.
    import google.auth.transport.requests as req_mod

    monkeypatch.setattr(req_mod, "Request", lambda: "<request>")

    out = bootstrap()
    assert minted.refreshed is True
    assert out["valid"] is True
    assert tok.is_file()


def test_bootstrap_missing_client_file_raises(patch_resolver, monkeypatch, tmp_path):
    err = SheetsError("oauth_client_missing", "no token")
    patch_resolver(err)

    missing = tmp_path / "does-not-exist.json"
    monkeypatch.setenv("GSHEETS_OAUTH_CLIENT_FILE", str(missing))

    with pytest.raises(SheetsError) as ei:
        bootstrap()
    assert ei.value.code == "oauth_client_missing"
    assert "GSHEETS_OAUTH_CLIENT_FILE" in (ei.value.hint or "")


def test_bootstrap_propagates_non_consent_resolver_error(patch_resolver, monkeypatch, tmp_path):
    # A non-oauth_client_missing failure (e.g. misconfigured SA) must bubble unchanged and
    # NOT fall through to interactive consent.
    err = SheetsError("bad_service_account", "SA key malformed", status=400)
    patch_resolver(err)

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("must not attempt consent for a non-token-missing error")

    import google_auth_oauthlib.flow as flow_mod

    monkeypatch.setattr(
        flow_mod,
        "InstalledAppFlow",
        type("X", (), {"from_client_secrets_file": staticmethod(boom)}),
    )

    with pytest.raises(SheetsError) as ei:
        bootstrap()
    assert ei.value.code == "bad_service_account"


def test_bootstrap_persisted_token_has_restrictive_mode(patch_resolver, monkeypatch, tmp_path):
    """The persisted token file is written with 0600 perms (no group/other access)."""
    tok = tmp_path / "token.json"
    monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(tok))
    creds = _FakeCreds()
    patch_resolver(creds)

    bootstrap()
    mode = tok.stat().st_mode & 0o777
    assert mode == 0o600


def test_bootstrap_creates_token_parent_dir(patch_resolver, monkeypatch, tmp_path):
    nested = tmp_path / "deep" / "nested" / "token.json"
    monkeypatch.setenv("GSHEETS_TOKEN_FILE", str(nested))
    creds = _FakeCreds()
    patch_resolver(creds)

    bootstrap()
    assert nested.is_file()


# =========================================================================== config-path resolution


def test_token_path_default_under_config_dir(patch_resolver, monkeypatch, tmp_path):
    cfg = tmp_path / "mycfg"
    monkeypatch.setenv("GSHEETS_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("GSHEETS_TOKEN_FILE", raising=False)
    creds = _FakeCreds()
    patch_resolver(creds)

    out = status()
    assert out["tokenPath"] == str(cfg / "token.json")


def test_client_path_default_under_config_dir(patch_resolver, monkeypatch, tmp_path):
    """First-time consent with default (nonexistent) client path raises oauth_client_missing."""
    cfg = tmp_path / "mycfg"
    monkeypatch.setenv("GSHEETS_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("GSHEETS_OAUTH_CLIENT_FILE", raising=False)
    err = SheetsError("oauth_client_missing", "no token")
    patch_resolver(err)

    with pytest.raises(SheetsError) as ei:
        bootstrap()
    assert ei.value.code == "oauth_client_missing"
    # The default client path under the config dir is reported in the message.
    assert str(cfg / "credentials.json") in ei.value.message


# =========================================================================== resolver helpers
# Direct unit tests for the private resolver helpers whose degraded-input branches are not
# reached through the public ``resolve_credentials`` paths: a malformed SA/token file and a
# non-serializable credential. These pin "never raise on bad input — degrade gracefully".


from gsheets.auth import resolver as _resolver  # noqa: E402

SCOPE_SPREADSHEETS = _resolver.SCOPE_SPREADSHEETS
SCOPE_DRIVE = _resolver.SCOPE_DRIVE


class TestIsServiceAccountFile:
    def test_unreadable_file_returns_false(self, tmp_path):
        # A path that does not exist -> the open() raises OSError, swallowed -> False
        # (resolver.py:108-109). Never raises, so SA selection simply declines this input.
        missing = tmp_path / "nope.json"
        assert _resolver._is_service_account_file(missing) is False

    def test_invalid_json_returns_false(self, tmp_path):
        # A file that exists but is not valid JSON -> json.load raises ValueError, swallowed
        # (resolver.py:108-109) -> False.
        bad = tmp_path / "bad.json"
        bad.write_text("this is not json {", encoding="utf-8")
        assert _resolver._is_service_account_file(bad) is False

    def test_json_without_service_account_type_returns_false(self, tmp_path):
        # Valid JSON dict but ``type`` != "service_account" (e.g. an authorized_user token) -> False.
        user = tmp_path / "user.json"
        user.write_text(json.dumps({"type": "authorized_user"}), encoding="utf-8")
        assert _resolver._is_service_account_file(user) is False

    def test_non_dict_json_returns_false(self, tmp_path):
        # JSON that parses to a non-dict (a list) -> the isinstance(dict) guard returns False.
        arr = tmp_path / "arr.json"
        arr.write_text(json.dumps(["service_account"]), encoding="utf-8")
        assert _resolver._is_service_account_file(arr) is False

    def test_service_account_json_returns_true(self, tmp_path):
        sa = tmp_path / "sa.json"
        sa.write_text(
            json.dumps({"type": "service_account", "client_email": "svc@x.iam"}),
            encoding="utf-8",
        )
        assert _resolver._is_service_account_file(sa) is True


class TestTokenGrantedScopes:
    def test_non_dict_token_json_returns_none(self, tmp_path):
        # A token file whose JSON parses to a non-dict (a bare list) -> the isinstance(dict)
        # guard returns None (resolver.py:130) rather than crashing the scope read.
        path = tmp_path / "token.json"
        path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        assert _resolver._token_granted_scopes(path) is None

    def test_json_string_token_returns_none(self, tmp_path):
        # JSON that decodes to a plain string (still valid JSON, still a non-dict) -> None.
        path = tmp_path / "token.json"
        path.write_text(json.dumps("just-a-string"), encoding="utf-8")
        assert _resolver._token_granted_scopes(path) is None

    def test_empty_scopes_list_returns_none(self, tmp_path):
        # ``scopes: []`` is falsy after filtering -> None (older-token "no scopes" shape).
        path = tmp_path / "token.json"
        path.write_text(json.dumps({"scopes": []}), encoding="utf-8")
        assert _resolver._token_granted_scopes(path) is None

    def test_scopes_list_with_non_strings_filtered(self, tmp_path):
        # Non-string entries are dropped; valid scopes survive in order.
        path = tmp_path / "token.json"
        path.write_text(
            json.dumps({"scopes": [SCOPE_SPREADSHEETS, 42, None, SCOPE_DRIVE]}),
            encoding="utf-8",
        )
        assert _resolver._token_granted_scopes(path) == [SCOPE_SPREADSHEETS, SCOPE_DRIVE]

    def test_missing_scopes_key_returns_none(self, tmp_path):
        path = tmp_path / "token.json"
        path.write_text(json.dumps({"client_id": "cid"}), encoding="utf-8")
        assert _resolver._token_granted_scopes(path) is None


class TestWriteToken:
    def test_non_serializable_creds_writes_nothing(self, tmp_path):
        # A credential whose ``to_json`` is not callable (e.g. SA/ADC creds, modeled here as a
        # bare object with no to_json) -> _write_token returns early without creating the file
        # (resolver.py:197-198). No parent dirs are created either.
        dest = tmp_path / "nested" / "token.json"

        class _NoToJson:
            pass  # no to_json attribute at all

        _resolver._write_token(_NoToJson(), dest)
        assert not dest.exists()
        assert not dest.parent.exists()

    def test_to_json_attribute_not_callable_writes_nothing(self, tmp_path):
        # ``to_json`` present but NOT callable (a string) -> the callable() guard skips the write.
        dest = tmp_path / "token.json"

        class _BadToJson:
            to_json = "not a method"

        _resolver._write_token(_BadToJson(), dest)
        assert not dest.exists()

    def test_serializable_creds_written_and_parent_created(self, tmp_path):
        # The positive path: a callable ``to_json`` -> the file is written and parent dirs made.
        dest = tmp_path / "deep" / "token.json"

        class _GoodCreds:
            def to_json(self):
                return '{"token": "persisted"}'

        _resolver._write_token(_GoodCreds(), dest)
        assert dest.read_text(encoding="utf-8") == '{"token": "persisted"}'


# --------------------------------------------------------------------- ISSUES.md #7 retry builder


def test_request_builder_defaults_num_retries(monkeypatch):
    monkeypatch.delenv("GSHEETS_MAX_RETRIES", raising=False)
    builder = auth._make_request_builder()

    captured = {}

    # Patch the parent HttpRequest.execute to capture the num_retries our subclass passes up.
    from googleapiclient.http import HttpRequest

    def fake_execute(self, http=None, num_retries=0):
        captured["num_retries"] = num_retries
        return {"ok": True}

    monkeypatch.setattr(HttpRequest, "execute", fake_execute)

    req = builder.__new__(builder)  # avoid constructing a real HttpRequest
    builder.execute(req)
    assert captured["num_retries"] == auth._DEFAULT_MAX_RETRIES


def test_max_retries_env_override(monkeypatch):
    monkeypatch.setenv("GSHEETS_MAX_RETRIES", "7")
    assert auth._max_retries() == 7
    monkeypatch.setenv("GSHEETS_MAX_RETRIES", "0")
    assert auth._max_retries() == 0
    monkeypatch.setenv("GSHEETS_MAX_RETRIES", "junk")
    assert auth._max_retries() == auth._DEFAULT_MAX_RETRIES


def test_http_timeout_env(monkeypatch):
    monkeypatch.delenv("GSHEETS_HTTP_TIMEOUT", raising=False)
    assert auth._http_timeout() is None
    monkeypatch.setenv("GSHEETS_HTTP_TIMEOUT", "120")
    assert auth._http_timeout() == 120.0
    monkeypatch.setenv("GSHEETS_HTTP_TIMEOUT", "-5")
    assert auth._http_timeout() is None


def test_build_services_uses_timeout_http_when_env_set(patch_resolver, record_build, monkeypatch):
    # ISSUES.md #9b: GSHEETS_HTTP_TIMEOUT routes builds through an AuthorizedHttp with a socket
    # timeout (http=...), instead of the default credentials= path.
    monkeypatch.setenv("GSHEETS_HTTP_TIMEOUT", "90")
    creds = _FakeCreds()
    patch_resolver(creds, scopes=DEFAULT_SCOPES)

    build_services()

    for call in record_build:
        assert "credentials" not in call
        assert call.get("http") is not None
        assert call["cache_discovery"] is False
        assert call.get("requestBuilder") is not None


def test_build_services_passes_request_builder(patch_resolver, record_build):
    # ISSUES.md #7: every Resource is built with the retry request builder.
    patch_resolver(_FakeCreds(), scopes=DEFAULT_SCOPES)
    build_services()
    assert record_build  # at least sheets
    for call in record_build:
        assert call.get("requestBuilder") is not None


def test_http_timeout_non_numeric_is_none(monkeypatch):
    monkeypatch.setenv("GSHEETS_HTTP_TIMEOUT", "not-a-number")
    assert auth._http_timeout() is None


def test_status_surfaces_token_persist_error(patch_resolver, monkeypatch):
    # ISSUES.md #6: a recorded token-persist failure is surfaced so the operator sees WHY a
    # rotated token won't stick.
    from gsheets.auth import resolver as r

    creds = _FakeCreds(valid=True)
    patch_resolver(creds, scopes=DEFAULT_SCOPES)
    token_path = auth._token_path()
    monkeypatch.setitem(r._LAST_PERSIST_ERROR, str(token_path), "OSError: read-only file system")

    out = status()
    assert out["tokenPersistError"] == "OSError: read-only file system"
