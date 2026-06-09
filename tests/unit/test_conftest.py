"""Tests for the shared pytest fixtures in ``tests/conftest.py`` (build unit: tests_conftest).

The fixtures (``mock_sheets_service``, ``load_golden``, ``live_service``) are leaf test
scaffolding (DESIGN §10; research §5). Their behavior is non-trivial enough to pin: the mock must
support the Google client's chained-accessor call shape, ``load_golden`` must resolve names with
and without the ``.json`` suffix and fail loudly on a miss, and ``live_service`` must skip cleanly
(never touch credentials) unless both env gates are satisfied.

Test functions are prefixed ``test_tests_conftest_*`` so ``pytest -k tests_conftest`` selects
exactly this unit's tests.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _load_conftest_module():
    """Load ``tests/conftest.py`` by file path.

    The ``tests/`` tree has no ``__init__.py`` (pytest ``prepend`` import mode), so a plain
    ``import tests.conftest`` is not reliable across invocations. Loading by path is robust and
    keeps the test independent of rootdir/cwd.
    """
    conftest_path = Path(__file__).resolve().parent.parent / "conftest.py"
    spec = importlib.util.spec_from_file_location("_gsheets_conftest_under_test", conftest_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


conftest_mod = _load_conftest_module()


# --------------------------------------------------------------------------------------------- #
# mock_sheets_service                                                                           #
# --------------------------------------------------------------------------------------------- #


def test_tests_conftest_mock_service_is_magicmock(mock_sheets_service):
    assert isinstance(mock_sheets_service, MagicMock)


def test_tests_conftest_mock_service_supports_chained_get_execute(mock_sheets_service):
    """The canonical ``spreadsheets().get(...).execute()`` chain resolves without setup."""
    payload = {"properties": {"title": "Demo"}, "sheets": []}
    mock_sheets_service.spreadsheets.return_value.get.return_value.execute.return_value = payload

    result = mock_sheets_service.spreadsheets().get(spreadsheetId="x", fields="properties.title").execute()
    assert result is payload


def test_tests_conftest_mock_service_supports_batch_update_execute(mock_sheets_service):
    """The write chain ``spreadsheets().batchUpdate(...).execute()`` resolves too."""
    reply = {"replies": [{"addSheet": {"properties": {"sheetId": 7}}}]}
    mock_sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = reply

    out = mock_sheets_service.spreadsheets().batchUpdate(spreadsheetId="x", body={}).execute()
    assert out["replies"][0]["addSheet"]["properties"]["sheetId"] == 7


def test_tests_conftest_mock_service_values_namespace_chains(mock_sheets_service):
    """``spreadsheets().values().batchGet(...).execute()`` (the values sub-resource) chains."""
    payload = {"valueRanges": [{"range": "Sheet1!A1:B2", "values": [["1", "2"]]}]}
    (
        mock_sheets_service.spreadsheets.return_value.values.return_value.batchGet.return_value.execute.return_value
    ) = payload

    out = mock_sheets_service.spreadsheets().values().batchGet(spreadsheetId="x", ranges=["A1"]).execute()
    assert out is payload


def test_tests_conftest_mock_service_unconfigured_execute_returns_a_mock(mock_sheets_service):
    """An un-pinned terminal call still returns a MagicMock (never raises)."""
    assert isinstance(mock_sheets_service.spreadsheets().get().execute(), MagicMock)


def test_tests_conftest_mock_service_is_fresh_per_test(mock_sheets_service):
    """Function-scoped: no return_value bleed across tests (assert default state)."""
    assert isinstance(mock_sheets_service.spreadsheets().get().execute(), MagicMock)
    # If state had leaked from an earlier test that set a dict payload, this would be that dict.


# --------------------------------------------------------------------------------------------- #
# load_golden                                                                                   #
# --------------------------------------------------------------------------------------------- #


def test_tests_conftest_golden_dir_points_at_unit_golden():
    assert conftest_mod.GOLDEN_DIR == Path(conftest_mod.__file__).parent / "unit" / "golden"
    assert conftest_mod.GOLDEN_DIR.is_dir()


def test_tests_conftest_load_golden_reads_committed_fixture(load_golden):
    data = load_golden("flatten_full")
    assert isinstance(data, dict)
    assert "input" in data and "expected" in data


def test_tests_conftest_load_golden_suffix_is_optional(load_golden):
    """Name with and without ``.json`` resolve to the identical file content."""
    assert load_golden("flatten_full") == load_golden("flatten_full.json")


def test_tests_conftest_load_golden_returns_parsed_json(load_golden):
    """Returns real parsed JSON, equal to reading the file directly."""
    data = load_golden("a1_to_gridrange")
    raw = json.loads((conftest_mod.GOLDEN_DIR / "a1_to_gridrange.json").read_text(encoding="utf-8"))
    assert data == raw


def test_tests_conftest_load_golden_missing_file_raises_clear_error(load_golden):
    with pytest.raises(AssertionError, match="golden file not found"):
        load_golden("definitely_not_a_real_golden_fixture")


# --------------------------------------------------------------------------------------------- #
# live_service (env-gated; must skip cleanly without touching creds)                            #
# --------------------------------------------------------------------------------------------- #


def _live_service_impl():
    """Unwrap the underlying plain function from the pytest fixture wrapper.

    Pytest wraps a ``@pytest.fixture`` function in a marker object; across pytest versions the
    raw callable is reachable via ``__wrapped__`` (and ``_fixture_function`` in pytest 8/9).
    """
    fixture_obj = conftest_mod.live_service
    return getattr(fixture_obj, "__wrapped__", None) or fixture_obj._fixture_function


def test_tests_conftest_live_service_skips_when_live_unset(monkeypatch):
    """Without ``GSHEETS_LIVE=1`` the fixture skips and never imports/uses auth."""
    monkeypatch.delenv("GSHEETS_LIVE", raising=False)
    monkeypatch.setenv("GSHEETS_TEST_SPREADSHEET_ID", "ignored")

    with pytest.raises(pytest.skip.Exception, match="live tests disabled"):
        _live_service_impl()()


def test_tests_conftest_live_service_skips_when_sheet_id_missing(monkeypatch):
    """With LIVE=1 but no test sheet id, the fixture still skips (never Production)."""
    monkeypatch.setenv("GSHEETS_LIVE", "1")
    monkeypatch.delenv("GSHEETS_TEST_SPREADSHEET_ID", raising=False)

    with pytest.raises(pytest.skip.Exception, match="GSHEETS_TEST_SPREADSHEET_ID not set"):
        _live_service_impl()()
