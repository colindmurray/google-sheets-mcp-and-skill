"""Tests for the top-level ``gsheets`` package marker (build unit ``pkg_init``).

DESIGN §1: ``import gsheets`` must be cheap and boundary-clean — it defines ``__version__``
and re-exports NOTHING transport-bound (no ``fastmcp``/``mcp``/``argparse``/``pydantic``).
These tests pin that contract:

- ``__version__`` exists, is a non-empty string, and matches the installed distribution
  metadata (single source of truth = ``pyproject.toml`` via the built dist).
- ``__all__`` exposes exactly ``__version__``.
- The fallback path returns the source default when the distribution is not installed.
- Importing the bare package does not drag in any transport/CLI/pydantic module.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import subprocess
import sys

import gsheets


def test_version_is_nonempty_string() -> None:
    assert isinstance(gsheets.__version__, str)
    assert gsheets.__version__  # non-empty


def test_version_matches_distribution_metadata() -> None:
    """``__version__`` tracks the installed distribution version, not a stale literal."""
    expected = importlib.metadata.version("google-sheets-mcp-and-skill")
    assert gsheets.__version__ == expected


def test_all_exports_only_version() -> None:
    assert gsheets.__all__ == ["__version__"]


def test_resolve_version_falls_back_when_dist_missing(monkeypatch) -> None:
    """When the distribution metadata is absent, fall back to the source default."""

    def _raise(_name: str) -> str:
        raise gsheets._metadata.PackageNotFoundError("google-sheets-mcp-and-skill")

    monkeypatch.setattr(gsheets._metadata, "version", _raise)
    assert gsheets._resolve_version() == gsheets._FALLBACK_VERSION


def test_fallback_matches_distribution_version() -> None:
    """The hardcoded fallback must not drift from the real distribution version."""
    assert gsheets._FALLBACK_VERSION == importlib.metadata.version(
        "google-sheets-mcp-and-skill"
    )


def test_resolve_version_uses_distribution_name() -> None:
    """``_resolve_version`` queries the correct distribution name (not the import name)."""
    seen: dict[str, str] = {}

    def _spy(name: str) -> str:
        seen["name"] = name
        return "9.9.9"

    import unittest.mock

    with unittest.mock.patch.object(gsheets._metadata, "version", _spy):
        result = gsheets._resolve_version()

    assert seen["name"] == "google-sheets-mcp-and-skill"
    assert result == "9.9.9"


def test_import_gsheets_is_boundary_clean() -> None:
    """``import gsheets`` (the bare package) must not pull in transport/CLI/pydantic modules.

    Run in a FRESH subprocess: the in-process test session has already imported pydantic via
    other adapter tests, which would give a false pass (DESIGN §10).
    """
    forbidden = {"fastmcp", "mcp", "argparse", "pydantic"}
    code = (
        "import gsheets, sys; "
        f"leaked = {forbidden!r} & set(sys.modules); "
        "assert not leaked, sorted(leaked)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_import_gsheets_does_not_import_submodules() -> None:
    """Bare ``import gsheets`` must NOT eagerly import core/auth/cli/mcp_server submodules."""
    code = (
        "import gsheets, sys; "
        "leaked = {m for m in sys.modules "
        "if m.startswith('gsheets.') and m != 'gsheets'}; "
        "assert not leaked, sorted(leaked)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
