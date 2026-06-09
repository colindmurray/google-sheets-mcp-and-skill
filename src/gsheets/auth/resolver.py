"""Credential resolution precedence: SA -> OAuth-desktop -> ADC (DESIGN §2.1/§2.2/§2.3).

Reads ONLY env vars (never hardcodes paths/IDs). The token-present path uses
``from_authorized_user_file`` + refresh and needs NO client file; a client file is required
ONLY for first-time ``InstalledAppFlow`` consent (a CLI-only path, NEVER triggered for the MCP
server). No ``fastmcp``/``mcp``/``argparse`` imports here.

This module is PURE auth: stdlib + ``google.auth*`` only. It must NEVER import ``fastmcp``,
``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models`` (DESIGN §1 boundary).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..core.errors import SheetsError

# ---------------------------------------------------------------------------
# Scopes (DESIGN §2.3)
# ---------------------------------------------------------------------------

SCOPE_SPREADSHEETS = "https://www.googleapis.com/auth/spreadsheets"
SCOPE_DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive"

#: Least-privilege default scope set (``GSHEETS_SCOPES=default``).
DEFAULT_SCOPES: list[str] = [SCOPE_SPREADSHEETS, SCOPE_DRIVE_FILE]
#: Broad scope set (``GSHEETS_SCOPES=broad``): default plus full Drive.
BROAD_SCOPES: list[str] = [SCOPE_SPREADSHEETS, SCOPE_DRIVE_FILE, SCOPE_DRIVE]

#: Default config dir (overridable via ``GSHEETS_CONFIG_DIR``); lives outside any repo.
_DEFAULT_CONFIG_DIR = "~/.config/google-sheets-mcp"


def resolve_scopes(scopes_mode: str | None = None) -> list[str]:
    """Resolve the OAuth scope list from ``scopes_mode`` / ``GSHEETS_SCOPES`` (DESIGN §2.3).

    ``"default"`` -> ``spreadsheets`` + ``drive.file`` (least privilege); ``"broad"`` -> those
    plus ``drive``; an explicit comma-separated list -> exactly those scopes.

    Args:
        scopes_mode: Override for ``GSHEETS_SCOPES``; ``None`` reads the env var (default
            ``"default"``).

    Returns:
        The resolved list of scope URLs.
    """
    raw = scopes_mode if scopes_mode is not None else os.environ.get("GSHEETS_SCOPES")
    if raw is None:
        raw = "default"
    value = raw.strip()
    if value == "" or value.lower() == "default":
        return list(DEFAULT_SCOPES)
    if value.lower() == "broad":
        return list(BROAD_SCOPES)
    # Explicit comma-separated scope list — exactly those scopes, order preserved, dups dropped.
    scopes: list[str] = []
    for part in value.split(","):
        scope = part.strip()
        if scope and scope not in scopes:
            scopes.append(scope)
    if not scopes:
        raise SheetsError(
            "bad_scopes",
            f"GSHEETS_SCOPES={raw!r} resolved to an empty scope list",
            hint="use `default`, `broad`, or a non-empty comma-separated list of scope URLs",
        )
    return scopes


# ---------------------------------------------------------------------------
# Path resolution (env-only; never hardcodes a real path)
# ---------------------------------------------------------------------------


def _config_dir() -> Path:
    """The config dir from ``GSHEETS_CONFIG_DIR`` or the default ``~/.config/google-sheets-mcp``."""
    raw = os.environ.get("GSHEETS_CONFIG_DIR") or _DEFAULT_CONFIG_DIR
    return Path(raw).expanduser()


def _expand(raw: str | None) -> Path | None:
    """Expand ``~`` / env vars in a path string; ``None``/empty -> ``None``."""
    if not raw:
        return None
    return Path(os.path.expandvars(raw)).expanduser()


def _token_path() -> Path:
    """Resolved cached-token path (``GSHEETS_TOKEN_FILE`` or ``<config_dir>/token.json``)."""
    explicit = _expand(os.environ.get("GSHEETS_TOKEN_FILE"))
    return explicit if explicit is not None else _config_dir() / "token.json"


def _client_path() -> Path:
    """Resolved OAuth desktop-client path (``GSHEETS_OAUTH_CLIENT_FILE`` or default)."""
    explicit = _expand(os.environ.get("GSHEETS_OAUTH_CLIENT_FILE"))
    return explicit if explicit is not None else _config_dir() / "credentials.json"


def _is_service_account_file(path: Path) -> bool:
    """True when ``path`` is a JSON file whose ``type == "service_account"``."""
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("type") == "service_account"


def _token_granted_scopes(path: Path) -> list[str] | None:
    """Read the scope list already granted to a cached authorized-user token.

    A desktop authorized-user ``token.json`` embeds the exact scopes the user consented to
    under a ``"scopes"`` key (list, or space-delimited string). Returns that list, or ``None``
    when the file is unreadable / carries no scopes (older tokens). Used to refresh against the
    GRANTED scope set rather than forcing a (possibly different) requested set onto the
    refresh-grant — Google rejects a refresh whose scope list is not a subset of the original
    grant with ``invalid_scope`` (DESIGN §2.2; e.g. requesting ``drive.file`` against a token
    granted only ``drive``).
    """
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("scopes")
    if isinstance(raw, str):
        scopes = [s for s in raw.split() if s]
        return scopes or None
    if isinstance(raw, list):
        scopes = [s for s in raw if isinstance(s, str) and s]
        return scopes or None
    return None


def _scopes_covered(required: list[str], granted: list[str]) -> bool:
    """True when every ``required`` scope is satisfied by the ``granted`` scope set.

    Direct membership counts; additionally the broad ``auth/drive`` scope is treated as a
    superset of ``auth/drive.file`` for COVERAGE purposes (a token granted whole-Drive can do
    anything ``drive.file`` can) — even though Google does NOT treat ``drive`` as a superset of
    ``drive.file`` for refresh-grant *scope validation* (which is exactly why we never push the
    requested list onto the refresh). Coverage is the read-side check; refresh uses the granted
    set verbatim.
    """
    granted_set = set(granted)
    drive_broad = granted_set and SCOPE_DRIVE in granted_set
    for scope in required:
        if scope in granted_set:
            continue
        if scope == SCOPE_DRIVE_FILE and drive_broad:
            continue
        return False
    return True


# ---------------------------------------------------------------------------
# Credential refresh
# ---------------------------------------------------------------------------


def _refresh_request():
    """Build a ``google.auth.transport.requests.Request`` for credential refresh."""
    from google.auth.transport.requests import Request

    return Request()


def _ensure_fresh(creds, *, persist_token: Path | None = None):
    """Refresh expired-but-refreshable creds in place; re-persist authorized-user tokens.

    If ``creds`` are valid, return them unchanged. If expired with a refresh token, refresh via
    a fresh ``Request()`` and (when ``persist_token`` is given) write the updated token back.
    """
    valid = getattr(creds, "valid", None)
    if valid:
        return creds
    expired = getattr(creds, "expired", False)
    refresh_token = getattr(creds, "refresh_token", None)
    # Refresh when explicitly expired-with-refresh-token, OR when not yet valid but refreshable
    # (e.g. a freshly loaded authorized-user token whose access token is absent/stale).
    if refresh_token and (expired or not valid):
        creds.refresh(_refresh_request())
        if persist_token is not None:
            _write_token(creds, persist_token)
    return creds


def _write_token(creds, path: Path) -> None:
    """Persist an authorized-user credential's JSON to ``path`` (creating parent dirs)."""
    to_json = getattr(creds, "to_json", None)
    if not callable(to_json):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_json(), encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-source resolvers
# ---------------------------------------------------------------------------


def _resolve_service_account(scopes: list[str]):
    """Resolve Service Account creds, or ``None`` when no SA input is configured.

    SA is selected when ``GSHEETS_SERVICE_ACCOUNT_FILE`` is set, OR
    ``GOOGLE_APPLICATION_CREDENTIALS`` points at a JSON file whose ``type`` is
    ``"service_account"`` (DESIGN §2.2 step 1).
    """
    from google.oauth2 import service_account

    sa_file = _expand(os.environ.get("GSHEETS_SERVICE_ACCOUNT_FILE"))
    gac = _expand(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))

    path: Path | None = None
    if sa_file is not None:
        path = sa_file
    elif gac is not None and gac.is_file() and _is_service_account_file(gac):
        path = gac

    if path is None:
        return None
    if not path.is_file():
        raise SheetsError(
            "service_account_missing",
            f"service account key file not found: {path}",
            hint="set GSHEETS_SERVICE_ACCOUNT_FILE to an existing service-account JSON key",
        )
    return service_account.Credentials.from_service_account_file(str(path), scopes=scopes)


def _resolve_oauth(scopes: list[str], *, allow_missing: bool):
    """Resolve OAuth desktop creds from a CACHED TOKEN ONLY — never interactive consent.

    The token-present path (DESIGN §2.2 step 2) loads
    ``Credentials.from_authorized_user_file`` and refreshes in place; a desktop authorized-user
    token embeds ``client_id``/``client_secret``/``refresh_token`` so NO separate client file is
    needed. Interactive first-time consent (``InstalledAppFlow``) is CLI-only and lives in
    :func:`gsheets.auth.bootstrap` — it is NEVER triggered here.

    **Scope reconciliation (DESIGN §2.2).** The token is loaded WITHOUT forcing the requested
    ``scopes`` onto it: it is loaded with ``scopes=None`` so the credential adopts the scope set
    actually embedded in the token (the user's original grant), and a refresh therefore re-grants
    against that GRANTED set. Forcing a different requested list onto the refresh (e.g. requesting
    ``drive.file`` against a token granted ``drive``) makes Google's refresh-grant reject it with
    ``invalid_scope`` — even though the granted scope is functionally broader. After loading, the
    granted set is checked to COVER what core needs (``_scopes_covered``); if it does not, a
    clear ``oauth_scope_insufficient`` error tells the operator to re-consent with the needed
    scopes. This makes the "token present → refresh works without a client file" steady state
    hold for tokens whose grant differs from the requested narrow/broad set.

    Args:
        scopes: Requested (required) scope list — what core needs, used for the coverage check.
        allow_missing: When ``True`` (``auto`` mode), return ``None`` if no usable token exists
            so resolution can fall through to ADC. When ``False`` (forced ``oauth`` mode), a
            missing/unusable token raises so the operator gets an actionable error.
    """
    from google.oauth2.credentials import Credentials

    token_path = _token_path()
    if not token_path.is_file():
        if allow_missing:
            return None
        client_path = _client_path()
        if not client_path.is_file():
            raise SheetsError(
                "oauth_client_missing",
                "no cached OAuth token and no OAuth client file for first-time consent",
                hint="run `gsheets auth login` with GSHEETS_OAUTH_CLIENT_FILE pointing at a "
                "desktop OAuth client JSON",
            )
        # A client file exists but consent has not been run; that interactive path is CLI-only.
        raise SheetsError(
            "oauth_consent_required",
            f"no cached OAuth token at {token_path}; first-time consent is required",
            hint="run `gsheets auth login` to complete OAuth consent and mint a token "
            "(consent never runs inside the MCP server)",
        )

    # Load WITHOUT the requested scopes so the credential adopts (and refreshes against) the
    # token's own granted scope set — never a different requested list (avoids `invalid_scope`).
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), None)
    except (OSError, ValueError) as exc:
        raise SheetsError(
            "oauth_token_invalid",
            f"failed to load cached OAuth token at {token_path}: {exc}",
            hint="re-run `gsheets auth login` to mint a fresh token",
        ) from exc

    try:
        creds = _ensure_fresh(creds, persist_token=token_path)
    except Exception as exc:  # google.auth.exceptions.RefreshError and friends
        raise SheetsError(
            "oauth_refresh_failed",
            f"cached OAuth token at {token_path} could not be refreshed: {exc}",
            hint="re-run `gsheets auth login` to re-consent and mint a fresh token",
        ) from exc

    # Verify the token's GRANTED scopes cover what core needs. Source order, most authoritative
    # first: the credential's ``granted_scopes`` (set by google-auth from the refresh grant's
    # ``scope`` response), then its requested ``scopes`` (``None`` here since we loaded with
    # ``scopes=None``), then the token file's embedded ``scopes``. When none expose a scope list
    # we cannot verify, so we trust the token rather than block a working credential.
    granted = (
        getattr(creds, "granted_scopes", None)
        or getattr(creds, "scopes", None)
        or _token_granted_scopes(token_path)
    )
    if granted and not _scopes_covered(scopes, granted):
        raise SheetsError(
            "oauth_scope_insufficient",
            "cached OAuth token does not cover the requested scopes "
            f"(granted={sorted(granted)}, required={sorted(scopes)})",
            hint="re-run `gsheets auth login` with GSHEETS_SCOPES set so consent grants the "
            "scopes you need (e.g. `broad` for whole-Drive)",
        )

    return creds


def _resolve_adc(scopes: list[str]):
    """Resolve Application Default Credentials (DESIGN §2.2 step 3).

    Honors ``GOOGLE_APPLICATION_CREDENTIALS``, gcloud user creds, and GCE/Cloud Run metadata.
    """
    import google.auth

    try:
        creds, _project = google.auth.default(scopes=scopes)
    except Exception as exc:  # google.auth.exceptions.DefaultCredentialsError
        raise SheetsError(
            "no_credentials",
            f"no usable credentials found (ADC fallback failed): {exc}",
            hint="set GSHEETS_SERVICE_ACCOUNT_FILE, run `gsheets auth login`, or configure "
            "Application Default Credentials",
        ) from exc
    return creds


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def resolve_credentials(scopes: list[str]):
    """Resolve credentials by precedence SA -> OAuth-desktop -> ADC (DESIGN §2.2).

    First match wins under ``GSHEETS_AUTH_MODE=auto``; a forced mode skips straight to that
    source (and raises a :class:`SheetsError` when its inputs are missing). The OAuth
    token-present sub-path loads ``from_authorized_user_file`` and refreshes in place (no client
    file needed); first-time consent (client file required) is handled by
    :func:`gsheets.auth.bootstrap`, never here. After resolving, expired-but-refreshable creds
    are refreshed before return.

    Args:
        scopes: The scope list to request.

    Returns:
        A ``google.auth`` credentials object.

    Raises:
        SheetsError: When the selected/required inputs are missing (e.g.
            ``oauth_client_missing``, ``oauth_consent_required``, ``no_credentials``) or the
            forced mode is unknown.
    """
    mode = (os.environ.get("GSHEETS_AUTH_MODE") or "auto").strip().lower()

    if mode == "service_account":
        creds = _resolve_service_account(scopes)
        if creds is None:
            raise SheetsError(
                "service_account_missing",
                "GSHEETS_AUTH_MODE=service_account but no service-account key is configured",
                hint="set GSHEETS_SERVICE_ACCOUNT_FILE (or GOOGLE_APPLICATION_CREDENTIALS) to a "
                "service-account JSON key",
            )
        return _ensure_fresh(creds)

    if mode == "oauth":
        # Forced OAuth: a missing/unusable token is an error (no silent ADC fallthrough).
        return _resolve_oauth(scopes, allow_missing=False)

    if mode == "adc":
        return _ensure_fresh(_resolve_adc(scopes))

    if mode != "auto":
        raise SheetsError(
            "unknown_auth_mode",
            f"GSHEETS_AUTH_MODE={mode!r} is not one of "
            "service_account | oauth | adc | auto",
            hint="set GSHEETS_AUTH_MODE to service_account, oauth, adc, or auto (default)",
        )

    # auto: first match wins.
    creds = _resolve_service_account(scopes)
    if creds is not None:
        return _ensure_fresh(creds)

    creds = _resolve_oauth(scopes, allow_missing=True)
    if creds is not None:
        return creds  # already refreshed inside _resolve_oauth

    return _ensure_fresh(_resolve_adc(scopes))
