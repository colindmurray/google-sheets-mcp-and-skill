"""Boundary-guard test (DESIGN §1, §10).

Asserts that importing the pure layers (``gsheets.core``, ``gsheets.auth``) does NOT pull any
transport/CLI/pydantic module into ``sys.modules``. This MUST run in a fresh SUBPROCESS:
an in-process check gives false passes once the MCP adapter tests have already imported
``fastmcp``/``pydantic`` into the shared interpreter.
"""

from __future__ import annotations

import subprocess
import sys

_FORBIDDEN = "{'fastmcp', 'mcp', 'argparse', 'pydantic'}"


def _assert_clean_import(module: str) -> None:
    code = (
        f"import {module}, sys; "
        f"forbidden = {_FORBIDDEN}; "
        "leaked = sorted(forbidden & set(sys.modules)); "
        "assert not leaked, leaked"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"importing {module} leaked transport/CLI/pydantic modules: "
        f"{result.stdout}{result.stderr}"
    )


def test_core_import_is_transport_free():
    """`import gsheets.core` must not drag in fastmcp/mcp/argparse/pydantic."""
    _assert_clean_import("gsheets.core")


def test_auth_import_is_transport_free():
    """`import gsheets.auth` must not drag in fastmcp/mcp/argparse/pydantic."""
    _assert_clean_import("gsheets.auth")
