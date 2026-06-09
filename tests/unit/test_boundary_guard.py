"""Boundary-guard test (DESIGN §1, §10).

The thesis of this repo is a PURE core/auth layer (zero ``fastmcp``/``mcp``/``argparse``/
``pydantic`` imports) wrapped by two thin adapters. This test pins that invariant: importing
``gsheets.core`` (and ``gsheets.auth``) must NOT pull any transport/CLI/pydantic module into
``sys.modules``.

Why a SUBPROCESS is mandatory (not an in-process assertion): by the time the full suite runs,
the MCP adapter tests (``test_mcp_server.py``) have already imported ``fastmcp``/``mcp``/
``pydantic`` into the shared interpreter, and the CLI adapter tests have imported ``argparse``.
An in-process ``set(sys.modules)`` check would therefore give a false PASS even if ``gsheets.core``
secretly imported a transport module. We shell out to a fresh interpreter so the check sees a
clean module table where ``import gsheets.core`` is the only thing that ran.

The probe interpreter is started in the *default* mode (NOT ``-I``/``-S``): isolated mode strips
the ``site-packages`` path of the editable install and ignores ``PYTHONPATH``, which would make
``gsheets`` unimportable. Instead we explicitly prepend the in-tree ``src/`` to ``PYTHONPATH`` so
the guard works in any checkout regardless of whether the package is installed editable.
"""

from __future__ import annotations

import os
import subprocess
import sys

# Modules a clean `import gsheets.core` / `import gsheets.auth` must NEVER drag in.
# Kept as a sorted tuple so the literal injected into the child interpreter is deterministic.
FORBIDDEN_MODULES = ("argparse", "fastmcp", "mcp", "pydantic")

# `-k tests_boundary_guard` is the orchestrator's selector. The locked public test-function
# names (test_core_import_is_transport_free / test_auth_import_is_transport_free) do not contain
# that string, so we attach it as a marker keyword; `-k` matches markers, so the selector picks
# up both tests. (The PytestUnknownMarkWarning this emits is benign — the mark is a selection
# keyword, not a behavioral marker, and the suite does not run under --strict-markers.)
import pytest  # noqa: E402  (imported after the module docstring/constants on purpose)

pytestmark = pytest.mark.tests_boundary_guard

# Absolute path to the repo's `src/` (this file lives at tests/unit/test_boundary_guard.py).
_SRC_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src")
)


def _probe_env() -> dict[str, str]:
    """Child-process env with the in-tree ``src/`` prepended to ``PYTHONPATH``."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        _SRC_DIR + os.pathsep + existing if existing else _SRC_DIR
    )
    return env


def _run_probe(module: str) -> subprocess.CompletedProcess[str]:
    """Import ``module`` in a fresh interpreter and report any forbidden-module leak.

    The child exits 0 only when none of ``FORBIDDEN_MODULES`` ended up in ``sys.modules`` after
    importing the target. On a leak it raises (non-zero exit) and prints the sorted leaked set.
    """
    forbidden = "{" + ", ".join(repr(m) for m in FORBIDDEN_MODULES) + "}"
    code = (
        f"import {module}, sys; "
        f"forbidden = {forbidden}; "
        "leaked = sorted(forbidden & set(sys.modules)); "
        "assert not leaked, 'LEAKED: ' + ', '.join(leaked)"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=_probe_env(),
    )


def _assert_clean_import(module: str) -> None:
    result = _run_probe(module)
    assert result.returncode == 0, (
        f"importing `{module}` pulled a transport/CLI/pydantic module into sys.modules "
        f"(boundary violation, DESIGN §1). Forbidden set: {sorted(FORBIDDEN_MODULES)}.\n"
        f"--- child stdout ---\n{result.stdout}"
        f"--- child stderr ---\n{result.stderr}"
    )


def test_core_import_is_transport_free():
    """`import gsheets.core` must not drag in fastmcp/mcp/argparse/pydantic."""
    _assert_clean_import("gsheets.core")


def test_auth_import_is_transport_free():
    """`import gsheets.auth` must not drag in fastmcp/mcp/argparse/pydantic."""
    _assert_clean_import("gsheets.auth")


def test_guard_mechanism_detects_a_real_leak():
    """Negative control: the probe MUST fail on a deliberately-leaky import.

    Without this, a bug in the forbidden-set logic (e.g. an empty set, or comparing against the
    wrong collection) could make the two guard tests pass vacuously. Importing ``fastmcp`` is a
    known leak — the probe has to catch it and exit non-zero, proving the detection is live and
    not a tautology.
    """
    code = (
        "import fastmcp, sys; "
        "forbidden = {'fastmcp', 'mcp', 'argparse', 'pydantic'}; "
        "leaked = sorted(forbidden & set(sys.modules)); "
        "assert not leaked, 'LEAKED: ' + ', '.join(leaked)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=_probe_env(),
    )
    assert result.returncode != 0, (
        "negative control failed: importing `fastmcp` did NOT register any forbidden module, "
        "so the boundary-guard detection logic is not actually exercising sys.modules.\n"
        f"--- child stdout ---\n{result.stdout}"
        f"--- child stderr ---\n{result.stderr}"
    )
    assert "fastmcp" in (result.stdout + result.stderr), (
        "negative control failed: probe exited non-zero but did not report `fastmcp` as leaked; "
        "the assertion message wiring is broken.\n"
        f"--- child stdout ---\n{result.stdout}"
        f"--- child stderr ---\n{result.stderr}"
    )
