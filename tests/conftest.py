"""Shared pytest fixtures (DESIGN §10; research §5).

Provides:
- ``mock_sheets_service``: a ``MagicMock`` shaped like the ``sheets`` v4 Resource, wired so
  ``....execute()`` returns a settable value (the core unit tests drive this).
- ``load_golden``: helper reading golden-master JSON under ``tests/unit/golden/``.
- ``live_service``: env-gated real :class:`SheetsServices`, skipped unless ``GSHEETS_LIVE=1``;
  sheet id from ``GSHEETS_TEST_SPREADSHEET_ID``. NEVER references a committed/Production id.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

GOLDEN_DIR = Path(__file__).parent / "unit" / "golden"


@pytest.fixture
def mock_sheets_service() -> MagicMock:
    """A ``MagicMock`` standing in for the Google Sheets v4 Resource.

    Chained accessor calls (``service.spreadsheets().get(...).execute()``) return ``MagicMock``s
    by default; individual tests set ``....execute.return_value`` to a golden/fixture payload.
    """
    service = MagicMock(name="sheets_v4_service")
    return service


@pytest.fixture
def load_golden():
    """Return a loader for golden-master JSON files under ``tests/unit/golden/``.

    Usage: ``data = load_golden("flatten_basic")`` reads ``tests/unit/golden/flatten_basic.json``.
    """

    def _load(name: str) -> object:
        path = GOLDEN_DIR / (name if name.endswith(".json") else f"{name}.json")
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)

    return _load


@pytest.fixture
def live_service():
    """Real authed :class:`SheetsServices` for live integration tests.

    Skipped unless ``GSHEETS_LIVE=1``. The target sheet id comes from
    ``GSHEETS_TEST_SPREADSHEET_ID`` (never committed, never Production). Built lazily so the
    ``auth`` import is not required for the mocked unit suite.
    """
    if os.environ.get("GSHEETS_LIVE") != "1":
        pytest.skip("live tests disabled (set GSHEETS_LIVE=1 to enable)")
    if not os.environ.get("GSHEETS_TEST_SPREADSHEET_ID"):
        pytest.skip("GSHEETS_TEST_SPREADSHEET_ID not set")

    from gsheets import auth

    return auth.build_services()
