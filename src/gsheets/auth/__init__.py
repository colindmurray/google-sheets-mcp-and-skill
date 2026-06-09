"""Adapter-facing auth entrypoints (DESIGN §2.4, §7.2).

Builds the :class:`~gsheets.core.service.SheetsServices` handle core receives, and provides
the CLI-only OAuth bootstrap + status helpers. Imports only stdlib + ``googleapiclient`` /
``google.auth*`` (plus sibling auth/core modules) — never ``fastmcp``/``mcp``/``argparse``.
"""

from __future__ import annotations

from ..core.service import SheetsServices
from .resolver import resolve_credentials, resolve_scopes  # noqa: F401

__all__ = ["build_services", "bootstrap", "status"]


def build_services(scopes_mode: str | None = None) -> SheetsServices:
    """Build a :class:`SheetsServices` for steady-state use — NO interactive consent (DESIGN §2.4).

    Resolves scopes + credentials (refreshing a present token in place), then builds the
    ``sheets`` v4 Resource and an optional ``drive`` v3 Resource with ``cache_discovery=False``.
    Used by both adapters (CLI per-invocation; MCP once in its lifespan). Must never trigger
    ``InstalledAppFlow.run_local_server`` (that lives in :func:`bootstrap`).

    Args:
        scopes_mode: Override for ``GSHEETS_SCOPES``; ``None`` reads the env var.

    Returns:
        A :class:`SheetsServices` handle.

    Raises:
        SheetsError: When no usable credentials can be resolved without consent.
    """
    raise NotImplementedError


def bootstrap(scopes_mode: str | None = None) -> dict:
    """Run/validate the OAuth desktop consent flow and persist ``token.json`` (DESIGN §7.2).

    The ONLY place interactive consent (``run_local_server``) is allowed — invoked by
    ``gsheets auth login``. Refreshes/validates an existing token, or runs first-time consent
    when none exists (requiring ``GSHEETS_OAUTH_CLIENT_FILE``), then writes the token to
    ``GSHEETS_TOKEN_FILE``.

    Args:
        scopes_mode: Override for ``GSHEETS_SCOPES``; ``None`` reads the env var.

    Returns:
        A status dict describing the resulting token (path, scopes, expiry).
    """
    raise NotImplementedError


def status(scopes_mode: str | None = None) -> dict:
    """Report resolved auth mode/scopes/token state — touches auth only (DESIGN §7.2).

    Backs ``gsheets auth status``: reports the resolved auth mode, scopes, token path,
    expiry/refreshability, and (verbose only) account email. Never calls the Sheets API.

    Args:
        scopes_mode: Override for ``GSHEETS_SCOPES``; ``None`` reads the env var.

    Returns:
        A status dict; callers map "no usable credentials" to a non-zero exit.
    """
    raise NotImplementedError
