"""google-sheets-mcp-and-skill — best-in-class Google Sheets integration for AI tools.

Top-level package marker. Defines ``__version__`` only. Importing ``gsheets`` re-exports
NOTHING transport-bound (no ``fastmcp``/``mcp``/``argparse``/``pydantic``) so ``import gsheets``
stays cheap and boundary-clean (DESIGN §1).

The public surface lives in :mod:`gsheets.core` (the pure library) and the two thin
adapters :mod:`gsheets.cli` and :mod:`gsheets.mcp_server`. This module deliberately imports
NONE of them, so ``import gsheets`` never drags in the Sheets client, the auth stack, or
either adapter — keeping the bare package import as lightweight as possible.
"""

from __future__ import annotations

from importlib import metadata as _metadata

# Distribution (PyPI) name; differs from the import package name ``gsheets`` (DESIGN §9).
_DISTRIBUTION_NAME = "google-sheets-mcp-and-skill"

# Fallback used when the distribution metadata is unavailable (e.g. running straight from a
# source checkout that was never installed). Kept in lockstep with pyproject's ``version``.
_FALLBACK_VERSION = "0.4.1"


def _resolve_version() -> str:
    """Return the installed distribution version, falling back to the source default.

    Prefers :func:`importlib.metadata.version` so the runtime ``__version__`` tracks whatever
    was actually installed (single source of truth = the distribution metadata built from
    ``pyproject.toml``), and never drifts from a hand-maintained literal. Uses only the
    standard library, so the import boundary in DESIGN §1 stays intact.
    """
    try:
        return _metadata.version(_DISTRIBUTION_NAME)
    except _metadata.PackageNotFoundError:
        return _FALLBACK_VERSION


__version__ = _resolve_version()

__all__ = ["__version__"]
