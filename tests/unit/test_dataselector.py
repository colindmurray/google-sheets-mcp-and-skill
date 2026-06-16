"""Unit tests for ``gsheets.core.dataselector`` (SPEC §6 P2 — metadata-addressed reads).

The selector translator is PURE and serviceless except for the ``a1`` branch (which calls
``addressing.a1_to_gridrange``). These tests patch that one collaborator so the module is exercised
in isolation, and golden-master each selector branch + every error path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gsheets.core import dataselector
from gsheets.core.dataselector import build_data_filter, build_data_filters
from gsheets.core.errors import SheetsError

SHEET_ID = "<TEST_SHEET_ID>"


@pytest.fixture
def services():
    return MagicMock(name="services")


def test_a1_selector_resolves_to_gridrange(services, monkeypatch):
    monkeypatch.setattr(
        dataselector,
        "a1_to_gridrange",
        lambda svc, sid, a1: {"sheetId": 7, "startRowIndex": 1, "endRowIndex": 5},
    )
    out = build_data_filter(services, SHEET_ID, {"a1": "Cliff!A2:B5"})
    assert out == {"gridRange": {"sheetId": 7, "startRowIndex": 1, "endRowIndex": 5}}


def test_gridrange_selector_passes_through(services):
    gr = {"sheetId": 0, "startColumnIndex": 0, "endColumnIndex": 3}
    out = build_data_filter(services, SHEET_ID, {"gridRange": gr})
    assert out == {"gridRange": gr}


def test_developer_metadata_lookup_passes_through(services):
    lookup = {"metadataKey": "block:totals", "visibility": "DOCUMENT"}
    out = build_data_filter(services, SHEET_ID, {"developerMetadataLookup": lookup})
    assert out == {"developerMetadataLookup": lookup}


def test_non_dict_selector_raises(services):
    with pytest.raises(SheetsError) as exc:
        build_data_filter(services, SHEET_ID, "Cliff!A1")  # type: ignore[arg-type]
    assert exc.value.code == "bad_data_filters"


def test_no_known_key_raises(services):
    with pytest.raises(SheetsError) as exc:
        build_data_filter(services, SHEET_ID, {"namedRange": "Totals"})
    assert exc.value.code == "bad_data_filters"


def test_multiple_keys_raises(services):
    with pytest.raises(SheetsError) as exc:
        build_data_filter(
            services, SHEET_ID, {"a1": "S!A1", "gridRange": {"sheetId": 0}}
        )
    assert exc.value.code == "bad_data_filters"


def test_empty_a1_raises(services):
    with pytest.raises(SheetsError) as exc:
        build_data_filter(services, SHEET_ID, {"a1": "  "})
    assert exc.value.code == "bad_data_filters"


def test_empty_gridrange_raises(services):
    with pytest.raises(SheetsError) as exc:
        build_data_filter(services, SHEET_ID, {"gridRange": {}})
    assert exc.value.code == "bad_data_filters"


def test_empty_lookup_raises(services):
    with pytest.raises(SheetsError) as exc:
        build_data_filter(services, SHEET_ID, {"developerMetadataLookup": {}})
    assert exc.value.code == "bad_data_filters"


def test_build_data_filters_translates_each_item(services):
    out = build_data_filters(
        services,
        SHEET_ID,
        [
            {"gridRange": {"sheetId": 0}},
            {"developerMetadataLookup": {"metadataKey": "k"}},
        ],
    )
    assert out == [
        {"gridRange": {"sheetId": 0}},
        {"developerMetadataLookup": {"metadataKey": "k"}},
    ]


def test_build_data_filters_empty_list_raises(services):
    with pytest.raises(SheetsError) as exc:
        build_data_filters(services, SHEET_ID, [])
    assert exc.value.code == "bad_data_filters"


def test_build_data_filters_non_list_raises(services):
    with pytest.raises(SheetsError) as exc:
        build_data_filters(services, SHEET_ID, {"a1": "S!A1"})  # type: ignore[arg-type]
    assert exc.value.code == "bad_data_filters"
