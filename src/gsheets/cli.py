"""argparse CLI adapter (DESIGN §7.2).

The ONLY module importing ``argparse``. One subcommand per core function (flags map 1:1),
plus the adapter-only ``auth login|status`` subcommand (the only place interactive OAuth
consent is allowed). Global ``--json``; a :class:`~gsheets.core.errors.SheetsError` is caught
at the top of :func:`main` and rendered to stderr (or as an ``ok:false`` JSON envelope), exit 1.
"""

from __future__ import annotations

import argparse

from . import auth, core  # noqa: F401  (dispatch targets as subcommands land)
from .core.errors import SheetsError  # noqa: F401  (caught in main)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with all subcommands (DESIGN §7.2).

    Registers one subcommand per core function (names are the core fn names with hyphens) plus
    the ``auth`` subcommand, and the global ``--json`` flag. Flags map 1:1 to core args.

    Returns:
        The configured :class:`argparse.ArgumentParser`.
    """
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    """Console-script entrypoint (``gsheets``): parse, dispatch, render (DESIGN §7.2).

    Parses ``argv``; for Sheets subcommands calls :func:`gsheets.auth.build_services`,
    dispatches to the matching core function, and prints terse text or ``--json``. A
    :class:`SheetsError` is caught here and rendered to stderr (or as the ``ok:false`` JSON
    envelope under ``--json``), returning exit code 1.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (0 on success, 1 on a handled error).
    """
    raise NotImplementedError
