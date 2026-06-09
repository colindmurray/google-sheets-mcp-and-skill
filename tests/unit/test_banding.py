"""Unit tests for ``gsheets.core.banding`` (DESIGN §X.0e, §X.3/§X.9; analysis #9).

All tests run against a MOCKED Sheets service — no network. Two flavours:

- GOLDEN-MASTER for :func:`serialize_banding`: representative Google ``BandedRange`` JSON in,
  assert the EXACT flattened read shape + terse condformat-style line out (row + column
  banding, a row-only-with-footer case, and a legacy flat ``*Color`` + theme-color case). The
  serializer takes an already-resolved A1 ``range_a1`` (the owning read fn resolves the Google
  ``GridRange`` -> A1 first), so these tests are serviceless.
- OUTBOUND-REQUEST assertions for the ``build_*`` builders: assert the exact ``addBanding`` /
  ``updateBanding`` / ``deleteBanding`` ``batchUpdate`` request dict (including the AUTO fields
  mask on update — atomic per-color masking — and the hex -> ``*ColorStyle`` conversion).

Addressing (A1 <-> GridRange) is the real implemented layer; its sheet-name resolution is
driven by wiring a ``spreadsheets().get`` recorder that returns a one-sheet index (``Sheet1``,
sheetId 0), so the builders resolve A1 -> GridRange deterministically.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gsheets.core import banding
from gsheets.core.banding import (
    build_add_banding_request,
    build_delete_banding_request,
    build_update_banding_request,
    serialize_banding,
)
from gsheets.core.errors import SheetsError
from gsheets.core.service import SheetsServices

GOLDEN_DIR = Path(__file__).parent / "golden"


def load_golden(name: str) -> dict:
    """Load a committed golden fixture (with or without the ``.json`` suffix)."""
    filename = name if name.endswith(".json") else f"{name}.json"
    return json.loads((GOLDEN_DIR / filename).read_text())


# --------------------------------------------------------------------------- helpers


class _Recorder:
    """Callable recording its kwargs; ``.execute()`` yields a queued response.

    Lets a test feed back a sheet index so the real addressing layer resolves names/ids
    without network.
    """

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        resp = self._responses.pop(0) if self._responses else {}
        request_obj = MagicMock(name="request")
        request_obj.execute.return_value = resp
        return request_obj


def _service_with_sheet_index(
    *, sheets_index: list[dict] | None = None
) -> SheetsServices:
    """Build a mocked service whose ``spreadsheets().get`` returns a sheet index.

    Default index: one sheet ``Sheet1`` (sheetId 0, index 0), which the real addressing layer
    uses to resolve both directions. The ``get`` recorder answers EVERY get call (the
    addressing cache may call it more than once).
    """
    if sheets_index is None:
        sheets_index = [{"properties": {"sheetId": 0, "title": "Sheet1", "index": 0}}]
    services = SheetsServices(sheets=MagicMock(name="sheets_v4"), drive=None)
    rec = _Recorder([{"sheets": sheets_index}] * 8)
    services.sheets.spreadsheets.return_value.get = rec
    return services


SPREADSHEET_ID = "<YOUR_SPREADSHEET_ID>"


# =========================================================================== serialize


def test_serialize_banding_row_and_column_golden():
    """Golden-master: row + column BandedRange JSON -> exact flattened shape + terse line."""
    golden = load_golden("banding_serialize")["row_and_column"]
    out = serialize_banding(golden["google"], golden["range_a1"])
    assert out == golden["expected"]


def test_serialize_banding_row_only_with_footer_golden():
    """Golden-master: a row-only banding with a footer band, columnBanding -> null."""
    golden = load_golden("banding_serialize")["row_only_with_footer"]
    out = serialize_banding(golden["google"], golden["range_a1"])
    assert out == golden["expected"]


def test_serialize_banding_legacy_flat_color_and_theme_golden():
    """Golden-master: legacy flat ``*Color`` dicts + a theme header flatten correctly."""
    golden = load_golden("banding_serialize")["legacy_flat_color_and_theme"]
    out = serialize_banding(golden["google"], golden["range_a1"])
    assert out == golden["expected"]


def test_serialize_banding_names_every_slot_explicitly():
    """A present group names header/first/second/footer — unset slots are ``None``, not absent."""
    google = {
        "bandedRangeId": 1,
        "rowProperties": {
            "firstBandColorStyle": {"rgbColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
            "secondBandColorStyle": {"rgbColor": {"red": 0.0, "green": 0.0, "blue": 0.0}},
        },
    }
    out = serialize_banding(google, "Sheet1!A1:C5")
    assert out["rowBanding"] == {
        "header": None,
        "first": "#FFFFFF",
        "second": "#000000",
        "footer": None,
    }
    # The body-only banding line drops the absent header/footer labels.
    assert out["line"] == "banding 1 [Sheet1!A1:C5] rows: #FFFFFF / #000000"


def test_serialize_banding_absent_group_is_none():
    """A ``BandedRange`` with no ``columnProperties`` surfaces ``columnBanding`` as ``None``."""
    google = {
        "bandedRangeId": 2,
        "rowProperties": {
            "firstBandColorStyle": {"rgbColor": {"red": 1.0, "green": 1.0, "blue": 1.0}}
        },
    }
    out = serialize_banding(google, "Sheet1!A1:A5")
    assert out["columnBanding"] is None


def test_serialize_banding_omits_range_when_not_supplied():
    """When the caller gives no ``range_a1``, the ``range`` key is omitted; line shows ``[]``."""
    google = {
        "bandedRangeId": 4,
        "rowProperties": {
            "firstBandColorStyle": {"rgbColor": {"red": 1.0, "green": 1.0, "blue": 1.0}}
        },
    }
    out = serialize_banding(google)
    assert "range" not in out
    assert out["line"].startswith("banding 4 []")


def test_serialize_banding_missing_id_uses_question_mark_in_line():
    """A BandedRange with no id still serializes (``bandedRangeId`` None; line shows ``?``)."""
    google = {
        "rowProperties": {
            "firstBandColorStyle": {"rgbColor": {"red": 1.0, "green": 1.0, "blue": 1.0}}
        }
    }
    out = serialize_banding(google, "Sheet1!A1:A5")
    assert out["bandedRangeId"] is None
    assert out["line"].startswith("banding ? [Sheet1!A1:A5]")


def test_serialize_banding_malformed_color_degrades_to_none():
    """A malformed band color degrades to ``None`` rather than breaking the whole read."""
    google = {
        "bandedRangeId": 5,
        "rowProperties": {
            "headerColorStyle": {"not": "a color"},
            "firstBandColorStyle": {"rgbColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
        },
    }
    out = serialize_banding(google, "Sheet1!A1:A5")
    assert out["rowBanding"]["header"] is None
    assert out["rowBanding"]["first"] == "#FFFFFF"


def test_serialize_banding_rejects_non_dict():
    with pytest.raises(SheetsError) as exc:
        serialize_banding(["not", "a", "dict"], "Sheet1!A1:A5")
    assert exc.value.code == "bad_banding"


def test_serialize_banding_rejects_non_dict_properties():
    with pytest.raises(SheetsError) as exc:
        serialize_banding({"bandedRangeId": 1, "rowProperties": ["nope"]}, "Sheet1!A1:A5")
    assert exc.value.code == "bad_banding"


# =========================================================================== add_banding


def test_build_add_banding_request_row_and_column():
    """addBanding carries the resolved GridRange + rowProperties/columnProperties *ColorStyle."""
    services = _service_with_sheet_index()
    req = build_add_banding_request(
        services,
        SPREADSHEET_ID,
        "Sheet1!A1:F500",
        {
            "rowBanding": {
                "header": "#4285F4",
                "first": "#FFFFFF",
                "second": "#E8F0FE",
            },
            "columnBanding": {"first": "#FFFFFF", "second": "#D9D9D9"},
        },
    )
    assert req == {
        "addBanding": {
            "bandedRange": {
                "range": {
                    "sheetId": 0,
                    "startRowIndex": 0,
                    "endRowIndex": 500,
                    "startColumnIndex": 0,
                    "endColumnIndex": 6,
                },
                "rowProperties": {
                    "headerColorStyle": {
                        "rgbColor": {
                            "red": 66 / 255,
                            "green": 133 / 255,
                            "blue": 244 / 255,
                        }
                    },
                    "firstBandColorStyle": {
                        "rgbColor": {"red": 1.0, "green": 1.0, "blue": 1.0}
                    },
                    "secondBandColorStyle": {
                        "rgbColor": {
                            "red": 232 / 255,
                            "green": 240 / 255,
                            "blue": 254 / 255,
                        }
                    },
                },
                "columnProperties": {
                    "firstBandColorStyle": {
                        "rgbColor": {"red": 1.0, "green": 1.0, "blue": 1.0}
                    },
                    "secondBandColorStyle": {
                        "rgbColor": {
                            "red": 217 / 255,
                            "green": 217 / 255,
                            "blue": 217 / 255,
                        }
                    },
                },
            }
        }
    }


def test_build_add_banding_request_row_only():
    """A row-only add omits columnProperties entirely."""
    services = _service_with_sheet_index()
    req = build_add_banding_request(
        services,
        SPREADSHEET_ID,
        "Sheet1!A1:F500",
        {"rowBanding": {"first": "#FFFFFF", "second": "#E8F0FE"}},
    )
    banded = req["addBanding"]["bandedRange"]
    assert "columnProperties" not in banded
    assert set(banded["rowProperties"]) == {
        "firstBandColorStyle",
        "secondBandColorStyle",
    }


def test_build_add_banding_request_theme_color():
    """A ``theme:NAME`` band color converts to ``{'themeColor': 'NAME'}``."""
    services = _service_with_sheet_index()
    req = build_add_banding_request(
        services,
        SPREADSHEET_ID,
        "Sheet1!A1:B10",
        {"rowBanding": {"header": "theme:ACCENT1", "first": "#FFFFFF"}},
    )
    props = req["addBanding"]["bandedRange"]["rowProperties"]
    assert props["headerColorStyle"] == {"themeColor": "ACCENT1"}


def test_build_add_banding_request_requires_a_group():
    """add with neither rowBanding nor columnBanding raises (Google requires at least one)."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_add_banding_request(services, SPREADSHEET_ID, "Sheet1!A1:F500", {})
    assert exc.value.code == "missing_param"


def test_build_add_banding_request_empty_group_is_empty_payload():
    """A present but empty band group (no colors) refuses the no-op group."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_add_banding_request(
            services, SPREADSHEET_ID, "Sheet1!A1:F500", {"rowBanding": {}}
        )
    assert exc.value.code == "empty_payload"


def test_build_add_banding_request_unknown_band_slot_rejected():
    """An unknown band-slot key raises ``unknown_param`` (the band dict is not an escape hatch)."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_add_banding_request(
            services,
            SPREADSHEET_ID,
            "Sheet1!A1:F500",
            {"rowBanding": {"first": "#FFFFFF", "middle": "#000000"}},
        )
    assert exc.value.code == "unknown_param"
    assert "middle" in exc.value.message


def test_build_add_banding_request_bad_color_rejected():
    """A malformed hex raises ``bad_color`` with the offending slot path."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_add_banding_request(
            services,
            SPREADSHEET_ID,
            "Sheet1!A1:F500",
            {"rowBanding": {"first": "not-a-hex"}},
        )
    assert exc.value.code == "bad_color"
    assert "rowBanding.first" in exc.value.message


# =========================================================================== update_banding


def test_build_update_banding_request_single_color_auto_masks_atomically():
    """Updating one band color masks down to that single ``*ColorStyle`` (atomic leaf)."""
    services = _service_with_sheet_index()
    req = build_update_banding_request(
        services,
        SPREADSHEET_ID,
        {"bandedRangeId": 7, "rowBanding": {"first": "#FFFFFF"}},
    )
    assert req == {
        "updateBanding": {
            "bandedRange": {
                "bandedRangeId": 7,
                "rowProperties": {
                    "firstBandColorStyle": {
                        "rgbColor": {"red": 1.0, "green": 1.0, "blue": 1.0}
                    }
                },
            },
            "fields": "rowProperties.firstBandColorStyle",
        }
    }


def test_build_update_banding_request_multi_color_group_mask():
    """Updating two colors in one group emits the group form ``rowProperties(a,b)``."""
    services = _service_with_sheet_index()
    req = build_update_banding_request(
        services,
        SPREADSHEET_ID,
        {"bandedRangeId": 7, "rowBanding": {"first": "#FFFFFF", "second": "#000000"}},
    )
    assert (
        req["updateBanding"]["fields"]
        == "rowProperties(firstBandColorStyle,secondBandColorStyle)"
    )


def test_build_update_banding_request_range_resolves_and_masks_atomically():
    """A new range resolves to a GridRange and masks as the atomic ``range`` field."""
    services = _service_with_sheet_index()
    req = build_update_banding_request(
        services,
        SPREADSHEET_ID,
        {"bandedRangeId": 7, "range": "Sheet1!A1:C10"},
    )
    bandedrange = req["updateBanding"]["bandedRange"]
    assert bandedrange["range"] == {
        "sheetId": 0,
        "startRowIndex": 0,
        "endRowIndex": 10,
        "startColumnIndex": 0,
        "endColumnIndex": 3,
    }
    assert req["updateBanding"]["fields"] == "range"


def test_build_update_banding_request_range_and_both_groups_mask_order():
    """A range + both groups mask each changed field in insertion order."""
    services = _service_with_sheet_index()
    req = build_update_banding_request(
        services,
        SPREADSHEET_ID,
        {
            "bandedRangeId": 7,
            "range": "Sheet1!A1:B2",
            "rowBanding": {"first": "#FFFFFF"},
            "columnBanding": {"second": "#000000"},
        },
    )
    assert (
        req["updateBanding"]["fields"]
        == "range,rowProperties.firstBandColorStyle,columnProperties.secondBandColorStyle"
    )


def test_build_update_banding_request_requires_id():
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_update_banding_request(
            services, SPREADSHEET_ID, {"rowBanding": {"first": "#FFFFFF"}}
        )
    assert exc.value.code == "missing_param"


def test_build_update_banding_request_no_changes_is_empty_payload():
    """``bandedRangeId`` only (nothing to change) refuses a no-op write."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_update_banding_request(services, SPREADSHEET_ID, {"bandedRangeId": 7})
    assert exc.value.code == "empty_payload"


def test_build_update_banding_request_empty_group_is_empty_payload():
    """An update naming a group with no colors refuses the no-op group."""
    services = _service_with_sheet_index()
    with pytest.raises(SheetsError) as exc:
        build_update_banding_request(
            services, SPREADSHEET_ID, {"bandedRangeId": 7, "rowBanding": {}}
        )
    assert exc.value.code == "empty_payload"


# =========================================================================== delete_banding


def test_build_delete_banding_request():
    assert build_delete_banding_request({"bandedRangeId": 7}) == {
        "deleteBanding": {"bandedRangeId": 7}
    }


def test_build_delete_banding_request_accepts_id_zero():
    """A ``bandedRangeId`` of 0 is a valid id (the guard checks ``is None``, not falsiness)."""
    assert build_delete_banding_request({"bandedRangeId": 0}) == {
        "deleteBanding": {"bandedRangeId": 0}
    }


def test_build_delete_banding_request_requires_id():
    with pytest.raises(SheetsError) as exc:
        build_delete_banding_request({})
    assert exc.value.code == "missing_param"


# =========================================================================== round-trip


def test_round_trip_built_add_serializes_back_to_same_colors():
    """A banding built via build_add_banding_request reads back to the SAME flattened colors.

    Closes the CRUD loop: structured hex colors -> Google *ColorStyle (write) ->
    serialize_banding reads them back to the SAME hex strings under header/first/second/footer.
    """
    services = _service_with_sheet_index()
    req = build_add_banding_request(
        services,
        SPREADSHEET_ID,
        "Sheet1!A1:F500",
        {
            "rowBanding": {
                "header": "#4285F4",
                "first": "#FFFFFF",
                "second": "#E8F0FE",
                "footer": "#000000",
            }
        },
    )
    built = req["addBanding"]["bandedRange"]
    built["bandedRangeId"] = 11
    out = serialize_banding(built, "Sheet1!A1:F500")
    assert out["rowBanding"] == {
        "header": "#4285F4",
        "first": "#FFFFFF",
        "second": "#E8F0FE",
        "footer": "#000000",
    }
    assert out["columnBanding"] is None
    assert (
        out["line"]
        == "banding 11 [Sheet1!A1:F500] rows: hdr #4285F4 / #FFFFFF / #E8F0FE / ftr #000000"
    )


# =========================================================================== boundary


def test_banding_module_imports_only_allowed_modules():
    """Static guard: every ``import`` in ``banding.py`` is stdlib or a sibling core module.

    The authoritative cross-interpreter boundary check lives in ``test_boundary_guard.py``
    (a fresh-interpreter ``import gsheets.core`` must not drag in fastmcp/mcp/argparse/
    pydantic). This is a cheap, local belt-and-suspenders that parses the module's actual
    import statements (not its prose) and asserts none names a forbidden transport/CLI/
    pydantic/models module.
    """
    import ast

    forbidden = {"fastmcp", "mcp", "argparse", "pydantic"}
    tree = ast.parse(Path(banding.__file__).read_text())
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported_roots.add(module.split(".")[0])
            # Reject a relative `from . import models`-style models pull, and any absolute
            # `from gsheets.models import ...` (level 0, module starting with gsheets.models).
            if module.startswith("gsheets.models"):
                raise AssertionError("banding.py must not import gsheets.models")
            if node.level and "models" in {a.name for a in node.names}:
                raise AssertionError("banding.py must not import the models sibling")

    leaked = forbidden & imported_roots
    assert not leaked, f"banding.py must not import {sorted(leaked)}"

    # The four public symbols are exported.
    for symbol in (
        "serialize_banding",
        "build_add_banding_request",
        "build_update_banding_request",
        "build_delete_banding_request",
    ):
        assert hasattr(banding, symbol)
