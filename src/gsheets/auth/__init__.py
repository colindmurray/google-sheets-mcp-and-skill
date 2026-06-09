"""Adapter-facing auth entrypoints (DESIGN §2.4, §7.2).

Builds the :class:`~gsheets.core.service.SheetsServices` handle core receives, and provides
the CLI-only OAuth bootstrap + status helpers. Imports only stdlib + ``googleapiclient`` /
``google.auth*`` (plus sibling auth/core modules) — never ``fastmcp``/``mcp``/``argparse``.

Three entrypoints:

- :func:`build_services` — steady-state handle for BOTH adapters; resolves scopes +
  credentials (refreshing a present token in place) and builds the API Resources with
  ``cache_discovery=False``. It MUST NEVER trigger interactive consent.
- :func:`bootstrap` — the ONLY place ``InstalledAppFlow.run_local_server`` may run
  (``gsheets auth login``). Validates/refreshes an existing OAuth token or runs first-time
  desktop consent, then persists ``token.json`` to ``GSHEETS_TOKEN_FILE``.
- :func:`status` — reports resolved mode/scopes/token path/expiry without calling the
  Sheets API (``gsheets auth status``).
"""

from __future__ import annotations

import os
from pathlib import Path

from ..core.errors import SheetsError
from ..core.service import SheetsServices
from .resolver import resolve_credentials, resolve_scopes

__all__ = ["build_services", "bootstrap", "status"]

# Scope that grants whole-Drive access; presence of any drive scope means we can build a
# drive v3 Resource. ``drive.file`` (per-file) also counts — both let core touch Drive.
_DRIVE_SCOPE_MARKER = "auth/drive"

# Default config dir + token file, overridable via env (DESIGN §2.1). Read ONLY here in the
# auth layer (never in core); committed code hardcodes no real paths/IDs.
_DEFAULT_CONFIG_DIR = "~/.config/google-sheets-mcp"
_DEFAULT_TOKEN_BASENAME = "token.json"
_DEFAULT_CLIENT_BASENAME = "credentials.json"


# --------------------------------------------------------------------------- env helpers


def _config_dir() -> Path:
    """Resolve the config dir from ``GSHEETS_CONFIG_DIR`` (default ``~/.config/...``)."""
    raw = os.environ.get("GSHEETS_CONFIG_DIR") or _DEFAULT_CONFIG_DIR
    return Path(raw).expanduser()


def _token_path() -> Path:
    """Resolve the cached-token path from ``GSHEETS_TOKEN_FILE`` (default config-dir token)."""
    raw = os.environ.get("GSHEETS_TOKEN_FILE")
    if raw:
        return Path(raw).expanduser()
    return _config_dir() / _DEFAULT_TOKEN_BASENAME


def _client_path() -> Path:
    """Resolve the OAuth desktop-client path from ``GSHEETS_OAUTH_CLIENT_FILE``."""
    raw = os.environ.get("GSHEETS_OAUTH_CLIENT_FILE")
    if raw:
        return Path(raw).expanduser()
    return _config_dir() / _DEFAULT_CLIENT_BASENAME


def _auth_mode() -> str:
    """Resolve the forced/auto auth mode from ``GSHEETS_AUTH_MODE`` (default ``auto``)."""
    return (os.environ.get("GSHEETS_AUTH_MODE") or "auto").strip().lower()


def _has_drive_scope(scopes: list[str]) -> bool:
    """True when any resolved scope grants Drive access (whole-drive or per-file)."""
    return any(_DRIVE_SCOPE_MARKER in s for s in scopes)


def _account_email(creds: object) -> str | None:
    """Best-effort authenticated account email, for verbose-only error hints.

    Service-account creds expose ``service_account_email``; user creds may carry an
    ``id_token`` claim or a populated ``account``. Never required; failures are swallowed.
    """
    email = getattr(creds, "service_account_email", None)
    if isinstance(email, str) and email:
        return email
    account = getattr(creds, "account", None)
    if isinstance(account, str) and account:
        return account
    return None


def _is_authorized_user(creds: object) -> bool:
    """True for an OAuth authorized-user credential (i.e. has a ``to_json`` + refresh token).

    Service-account and ADC creds are NOT persisted to ``token.json``; only desktop
    authorized-user creds are. Detect by the presence of ``refresh_token`` together with the
    ``to_json`` serializer that :class:`google.oauth2.credentials.Credentials` provides.
    """
    return hasattr(creds, "to_json") and hasattr(creds, "refresh_token")


def _persist_token(creds: object) -> Path:
    """Write authorized-user ``creds`` to the token file (mkdir -p the parent). Returns path."""
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - best-effort on platforms without chmod
        pass
    return path


def _expiry_iso(creds: object) -> str | None:
    """ISO-8601 string for the credential expiry, or ``None`` when unknown/never-expiring."""
    expiry = getattr(creds, "expiry", None)
    if expiry is None:
        return None
    try:
        return expiry.isoformat()
    except (AttributeError, TypeError):  # pragma: no cover - defensive
        return None


def _creds_refreshable(creds: object) -> bool:
    """True when the credential carries a usable refresh token (can be renewed silently)."""
    return bool(getattr(creds, "refresh_token", None))


# --------------------------------------------------------------------------- public API


def build_services(scopes_mode: str | None = None) -> SheetsServices:
    """Build a :class:`SheetsServices` for steady-state use — NO interactive consent (DESIGN §2.4).

    Resolves scopes + credentials (refreshing a present token in place via the resolver),
    then builds the ``sheets`` v4 Resource and an optional ``drive`` v3 Resource with
    ``cache_discovery=False``. Used by both adapters (CLI per-invocation; MCP once in its
    lifespan). Must never trigger ``InstalledAppFlow.run_local_server`` (that lives in
    :func:`bootstrap`); the resolver raises rather than prompting when consent is required.

    Args:
        scopes_mode: Override for ``GSHEETS_SCOPES``; ``None`` reads the env var.

    Returns:
        A :class:`SheetsServices` handle (``sheets`` always present; ``drive`` present only
        when a Drive scope was granted).

    Raises:
        SheetsError: When no usable credentials can be resolved without consent.
    """
    # Imported lazily so ``import gsheets.auth`` stays cheap and the heavy discovery client
    # is only loaded when services are actually built.
    from googleapiclient.discovery import build

    scopes = resolve_scopes(scopes_mode)
    creds = resolve_credentials(scopes)

    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = None
    if _has_drive_scope(scopes):
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    return SheetsServices(
        sheets=sheets,
        drive=drive,
        account_email=_account_email(creds),
    )


def bootstrap(scopes_mode: str | None = None) -> dict:
    """Run/validate the OAuth desktop consent flow and persist ``token.json`` (DESIGN §7.2).

    The ONLY place interactive consent (``run_local_server``) is allowed — invoked by
    ``gsheets auth login``. First it tries the steady-state resolver: if a usable token
    already exists (valid or silently refreshed), it is re-persisted and reported (no browser
    prompt). Only when no usable token exists does it run first-time desktop consent, which
    REQUIRES ``GSHEETS_OAUTH_CLIENT_FILE`` to point at a desktop OAuth client JSON.

    Args:
        scopes_mode: Override for ``GSHEETS_SCOPES``; ``None`` reads the env var.

    Returns:
        A status dict describing the resulting token (mode, scopes, token path, validity,
        expiry, refreshability, account email when verbose).

    Raises:
        SheetsError: ``oauth_client_missing`` when consent is needed but no client file
            exists; ``google_api_error``/auth errors bubble from the resolver/flow.
    """
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow

    scopes = resolve_scopes(scopes_mode)

    # 1) Steady state: a usable (valid or refreshable) credential already resolves without
    #    any browser prompt. Re-persist authorized-user creds so the token file is current.
    try:
        creds = resolve_credentials(scopes)
    except SheetsError as exc:
        # Only fall through to interactive consent when the failure is specifically that
        # there is no usable token yet (the OAuth first-time-consent condition). Any other
        # auth failure (e.g. a misconfigured SA) is surfaced unchanged.
        if exc.code != "oauth_client_missing":
            raise
        creds = None

    if creds is not None:
        if _is_authorized_user(creds):
            _persist_token(creds)
        return _status_from_creds(creds, scopes, persisted=_is_authorized_user(creds))

    # 2) First-time consent. A desktop OAuth client file is mandatory here.
    client_path = _client_path()
    if not client_path.is_file():
        raise SheetsError(
            "oauth_client_missing",
            f"no usable token and no OAuth client file at {client_path}",
            hint=(
                "run `gsheets auth login` with GSHEETS_OAUTH_CLIENT_FILE pointing at a "
                "desktop OAuth client JSON"
            ),
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), scopes)
    creds = flow.run_local_server(port=0)

    # Ensure the freshly-minted token is fully valid before persisting (some flows return a
    # credential that still needs an initial refresh to populate the access token/expiry).
    if not getattr(creds, "valid", True) and _creds_refreshable(creds):
        creds.refresh(Request())

    _persist_token(creds)
    return _status_from_creds(creds, scopes, persisted=True)


def status(scopes_mode: str | None = None) -> dict:
    """Report resolved auth mode/scopes/token state — touches auth only (DESIGN §7.2).

    Backs ``gsheets auth status``: reports the resolved auth mode, scopes, token path,
    expiry/refreshability, and (verbose only) account email. Never calls the Sheets API and
    NEVER prompts for consent — it resolves credentials through the non-interactive resolver
    and degrades to ``ok: False`` (rather than raising) when none are usable.

    Args:
        scopes_mode: Override for ``GSHEETS_SCOPES``; ``None`` reads the env var.

    Returns:
        A status dict; ``ok`` is ``False`` (with an ``error`` block) when no usable
        credentials resolve. Callers map that to a non-zero exit.
    """
    scopes = resolve_scopes(scopes_mode)

    try:
        creds = resolve_credentials(scopes)
    except SheetsError as exc:
        return {
            "ok": False,
            "mode": _auth_mode(),
            "scopes": scopes,
            "tokenPath": str(_token_path()),
            "tokenExists": _token_path().is_file(),
            "error": exc.to_dict(),
        }

    return _status_from_creds(creds, scopes, persisted=False)


# --------------------------------------------------------------------------- internals


def _status_from_creds(creds: object, scopes: list[str], *, persisted: bool) -> dict:
    """Build the status dict for a resolved credential (shared by bootstrap/status).

    Reports validity, expiry, and refreshability without touching the Sheets API. The
    account email is included ONLY in verbose mode so it never leaks by default (DESIGN §6.1);
    ``persisted`` records whether a token file was (re)written by the caller.
    """
    valid = bool(getattr(creds, "valid", True))
    expired = bool(getattr(creds, "expired", False))
    refreshable = _creds_refreshable(creds)

    out: dict[str, object] = {
        "ok": True,
        "mode": _auth_mode(),
        "scopes": scopes,
        "tokenPath": str(_token_path()),
        "tokenExists": _token_path().is_file(),
        "tokenPersisted": persisted,
        "valid": valid,
        "expired": expired,
        "refreshable": refreshable,
        "expiry": _expiry_iso(creds),
    }

    if _verbose_errors_enabled():
        email = _account_email(creds)
        if email:
            out["accountEmail"] = email

    return out


def _verbose_errors_enabled() -> bool:
    """True when ``GSHEETS_VERBOSE_ERRORS`` is set to a truthy value (mirrors errors.py)."""
    val = os.environ.get("GSHEETS_VERBOSE_ERRORS")
    if val is None:
        return False
    return val.strip().lower() not in ("", "0", "false", "no", "off")
