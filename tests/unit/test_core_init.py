"""Re-export contract for ``gsheets.core`` (build unit ``core_init``; DESIGN §1, §3.3, §Extensions).

``core/__init__.py`` is a PURE re-export module: it must surface exactly the 21 public core
functions for ``from gsheets.core import overview, inspect, ...`` and nothing transport-bound.
(15 base, DESIGN §3.3, plus the 5 v0.2 extension top-level fns ``data_ops``/``dimensions``/
``comments``/``export``/``read_many``, DESIGN §Extensions / §X.13 / §3.x, plus the v0.3 ``describe``
unified region read, SPEC §3.) These tests pin:

- all 21 public symbols are present, callable, and are the SAME objects defined in their
  owning sibling modules (no accidental shadowing / wrong wiring);
- ``__all__`` matches the locked spec set exactly (no missing, no extra);
- the §1 boundary holds: importing ``gsheets.core`` in a fresh interpreter pulls in none of
  ``fastmcp``/``mcp``/``argparse``/``pydantic``/``gsheets.models`` (the re-export must not drag
  any adapter/transport module in transitively). The subprocess is mandatory — an in-process
  check gives false passes once adapter tests have imported those modules into the runner.
"""

from __future__ import annotations

import subprocess
import sys

import gsheets.core as core

# The 21 public core functions, mapped to the sibling module that OWNS each one (DESIGN §1
# layout / §3.3 surface + §Extensions / §X.13 / §3.x + SPEC §3 describe). The re-export must hand
# back these objects.
_EXPECTED_OWNERS = {
    "overview": "gsheets.core.reads",
    "inspect": "gsheets.core.reads",
    "describe": "gsheets.core.reads",
    "read_conditional_formats": "gsheets.core.reads",
    "read_values": "gsheets.core.values",
    "write_values": "gsheets.core.values",
    "append_rows": "gsheets.core.values",
    "clear": "gsheets.core.values",
    "format": "gsheets.core.formatting",
    "set_conditional_format": "gsheets.core.rules",
    "set_validation": "gsheets.core.rules",
    "structure": "gsheets.core.structure",
    "manage_sheets": "gsheets.core.structure",
    "metadata": "gsheets.core.structure",
    "charts": "gsheets.core.charts",
    "batch": "gsheets.core.batch",
    # v0.2 extensions (DESIGN §Extensions): three NEW top-level core fns.
    "data_ops": "gsheets.core.dataops",
    "dimensions": "gsheets.core.dimensions",
    "comments": "gsheets.core.comments",
    # v0.2 cross-file + export extensions (DESIGN §3.x / §3.3): two MORE NEW top-level core fns.
    "export": "gsheets.core.export",
    "read_many": "gsheets.core.multiread",
}

_EXPECTED_SYMBOLS = set(_EXPECTED_OWNERS)


def test_core_init_all_matches_spec_exactly():
    """``__all__`` is exactly the 21 locked public symbols — no missing, no extras."""
    assert set(core.__all__) == _EXPECTED_SYMBOLS
    # __all__ also has no duplicates.
    assert len(core.__all__) == len(set(core.__all__)) == 21


def test_core_init_exposes_all_eighteen_callables():
    """Every spec symbol is importable off ``gsheets.core`` and is callable."""
    for name in _EXPECTED_SYMBOLS:
        assert hasattr(core, name), f"gsheets.core is missing {name!r}"
        assert callable(getattr(core, name)), f"gsheets.core.{name} is not callable"


def test_core_init_reexports_are_owning_module_objects():
    """Each re-export is the SAME object the owning sibling module defines (no shadowing)."""
    import importlib

    for name, owner_mod in _EXPECTED_OWNERS.items():
        owner = importlib.import_module(owner_mod)
        assert getattr(core, name) is getattr(owner, name), (
            f"gsheets.core.{name} is not the object defined in {owner_mod}"
        )


def test_core_init_does_not_export_helpers_or_modules():
    """Public surface is precisely ``__all__``: no helper fns / submodules leak as exports.

    Names like ``pad_jagged``, ``capture_new_ids``, ``validation_to_rule``, or the submodule
    names themselves live in siblings but must NOT be part of the curated ``core`` surface.
    """
    public = {n for n in dir(core) if not n.startswith("_")}
    extras = public - _EXPECTED_SYMBOLS
    # Submodules become attributes once imported, and ``from __future__ import annotations``
    # binds an ``annotations`` ``_Feature``; neither is a curated export. The curated *value*
    # surface (non-module, non-future, non-private) must be exactly the 15 functions.
    import __future__
    import types

    leaked_values = {
        n
        for n in extras
        if not isinstance(getattr(core, n), types.ModuleType)
        and getattr(core, n) is not __future__.annotations
    }
    assert not leaked_values, f"unexpected public non-module exports: {sorted(leaked_values)}"


def test_core_init_import_is_transport_and_models_free():
    """Importing ``gsheets.core`` in a clean interpreter pulls in no adapter/transport module.

    Runs in a SUBPROCESS (DESIGN §10): a same-process assertion is meaningless once other
    tests have imported ``pydantic``/``fastmcp`` into the shared runner. ``gsheets.models`` is
    included explicitly — the re-export must never reach the Pydantic adapter mirror (§1).
    """
    forbidden = "{'fastmcp', 'mcp', 'argparse', 'pydantic', 'gsheets.models'}"
    code = (
        "import gsheets.core, sys; "
        f"forbidden = {forbidden}; "
        "leaked = sorted(forbidden & set(sys.modules)); "
        "assert not leaked, leaked"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "importing gsheets.core leaked a transport/CLI/pydantic/models module: "
        f"{result.stdout}{result.stderr}"
    )
