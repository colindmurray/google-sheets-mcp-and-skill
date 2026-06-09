"""Shared pytest fixtures (DESIGN §10; research §5).

Provides the three fixtures every test in the suite leans on:

- ``mock_sheets_service``: a :class:`~unittest.mock.MagicMock` shaped like the Google Sheets
  v4 ``Resource``, wired so the terminal ``....execute()`` returns a settable value. Chained
  accessor calls (``service.spreadsheets().get(...).execute()``) auto-vivify ``MagicMock``s, so
  a test sets the payload at exactly one spot, e.g.::

      mock_sheets_service.spreadsheets.return_value.get.return_value.execute.return_value = payload

- ``load_golden``: helper reading golden-master JSON under ``tests/unit/golden/`` (with or
  without the ``.json`` suffix), used by the serializer golden-master tests (§10).

- ``live_service``: env-gated real :class:`~gsheets.core.service.SheetsServices`, skipped unless
  ``GSHEETS_LIVE=1``; the target sheet id comes from ``GSHEETS_TEST_SPREADSHEET_ID``. It NEVER
  references a committed/Production id and is built lazily so the mocked unit suite never imports
  the ``auth`` layer (keeping ``import gsheets.core`` boundary-clean per §1).

This module is pure test scaffolding: it imports only stdlib + ``pytest`` and never pulls in
``fastmcp``/``mcp``/``argparse`` or any transport symbol.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Golden-master JSON lives next to the unit tests that assert against it.
GOLDEN_DIR = Path(__file__).parent / "unit" / "golden"


@pytest.fixture
def mock_sheets_service() -> MagicMock:
    """A ``MagicMock`` standing in for the Google Sheets v4 ``Resource``.

    The Google client is a chain of callables: ``service.spreadsheets().get(...).execute()``.
    A bare ``MagicMock`` already returns a fresh child ``MagicMock`` for every attribute access
    and call, so the whole chain resolves without configuration and a test only needs to pin the
    terminal ``.execute.return_value`` for the request kind it exercises. No network, fully
    deterministic.

    Example::

        svc = mock_sheets_service
        svc.spreadsheets.return_value.get.return_value.execute.return_value = {"properties": {...}}
    """
    return MagicMock(name="sheets_v4_service")


@pytest.fixture
def load_golden():
    """Return a loader for golden-master JSON files under ``tests/unit/golden/``.

    ``load_golden("flatten_full")`` and ``load_golden("flatten_full.json")`` both read
    ``tests/unit/golden/flatten_full.json``. Raises a clear :class:`AssertionError` if the
    requested golden file does not exist, so a typo'd fixture name fails loudly rather than as an
    opaque ``FileNotFoundError`` mid-assertion.
    """

    def _load(name: str) -> object:
        filename = name if name.endswith(".json") else f"{name}.json"
        path = GOLDEN_DIR / filename
        assert path.is_file(), f"golden file not found: {path}"
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)

    return _load


@pytest.fixture
def live_service():
    """Real authed :class:`~gsheets.core.service.SheetsServices` for live integration tests.

    Skipped unless ``GSHEETS_LIVE=1`` AND ``GSHEETS_TEST_SPREADSHEET_ID`` is set (the target sheet
    id is supplied entirely from the environment — never committed, never Production). Built lazily
    so the ``gsheets.auth`` import is not required for the mocked unit suite; this keeps the unit
    runs free of any credential resolution and keeps the core boundary-guard meaningful (§1, §10).
    """
    if os.environ.get("GSHEETS_LIVE") != "1":
        pytest.skip("live tests disabled (set GSHEETS_LIVE=1 to enable)")
    if not os.environ.get("GSHEETS_TEST_SPREADSHEET_ID"):
        pytest.skip("GSHEETS_TEST_SPREADSHEET_ID not set")

    from gsheets import auth

    return auth.build_services()
