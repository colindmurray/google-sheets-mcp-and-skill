"""Credential resolution precedence: SA -> OAuth-desktop -> ADC (DESIGN §2.1/§2.2/§2.3).

Reads ONLY env vars (never hardcodes paths/IDs). The token-present path uses
``from_authorized_user_file`` + refresh and needs NO client file; a client file is required
ONLY for first-time ``InstalledAppFlow`` consent (a CLI-only path, NEVER triggered for the MCP
server). No ``fastmcp``/``mcp``/``argparse`` imports here.
"""

from __future__ import annotations

from ..core.errors import SheetsError  # noqa: F401  (used by implementations)


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
    raise NotImplementedError


def resolve_credentials(scopes: list[str]):
    """Resolve credentials by precedence SA -> OAuth-desktop -> ADC (DESIGN §2.2).

    First match wins under ``GSHEETS_AUTH_MODE=auto``; a forced mode skips straight to that
    source. The OAuth token-present sub-path loads ``from_authorized_user_file`` and refreshes
    in place (no client file needed); first-time consent (client file required) is handled by
    :func:`gsheets.auth.bootstrap`, never here. After resolving, expired-but-refreshable creds
    are refreshed before return.

    Args:
        scopes: The scope list to request.

    Returns:
        A ``google.auth`` credentials object.

    Raises:
        SheetsError: When the selected/required inputs are missing (e.g.
            ``oauth_client_missing``).
    """
    raise NotImplementedError
