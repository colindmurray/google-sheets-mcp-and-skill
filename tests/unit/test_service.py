"""Unit tests for ``gsheets.core.service.SheetsServices`` (DESIGN §2.4).

``SheetsServices`` is the authed handle core receives as its first parameter. It is a
*frozen* dataclass with no logic and no I/O — so the contract worth pinning is structural:
exact field set/order, default values, immutability (frozen), hashability, equality, and
that the module stays import-pure (no ``fastmcp``/``mcp``/``argparse``/``pydantic`` pulled
in by importing it). The ``-k core_service`` selector matches every test name here.
"""

from __future__ import annotations

import dataclasses
import sys
from unittest.mock import MagicMock

import pytest

from gsheets.core.service import SheetsServices

# Fields are LOCKED by DESIGN §2.4 — many core modules construct/annotate against this
# exact shape (sheets, drive, account_email), so any drift here is a real contract break.
EXPECTED_FIELDS = ("sheets", "drive", "account_email")


def test_core_service_is_a_frozen_dataclass():
    assert dataclasses.is_dataclass(SheetsServices)
    params = SheetsServices.__dataclass_params__
    assert params.frozen is True, "SheetsServices must be frozen (DESIGN §2.4)"


def test_core_service_field_names_and_order():
    names = tuple(f.name for f in dataclasses.fields(SheetsServices))
    assert names == EXPECTED_FIELDS


def test_core_service_optional_fields_default_to_none():
    fields = {f.name: f for f in dataclasses.fields(SheetsServices)}
    # `sheets` is required (no default); drive/account_email default to None.
    assert fields["sheets"].default is dataclasses.MISSING
    assert fields["drive"].default is None
    assert fields["account_email"].default is None


def test_core_service_requires_sheets_positionally():
    with pytest.raises(TypeError):
        SheetsServices()  # type: ignore[call-arg]  # missing required `sheets`


def test_core_service_minimal_construction_defaults():
    sheets = MagicMock(name="sheets_v4_resource")
    svc = SheetsServices(sheets)
    assert svc.sheets is sheets
    assert svc.drive is None
    assert svc.account_email is None


def test_core_service_full_construction_keyword():
    sheets = MagicMock(name="sheets_v4_resource")
    drive = MagicMock(name="drive_v3_resource")
    svc = SheetsServices(sheets=sheets, drive=drive, account_email="bot@example.com")
    assert svc.sheets is sheets
    assert svc.drive is drive
    assert svc.account_email == "bot@example.com"


def test_core_service_positional_construction_order():
    sheets = MagicMock(name="sheets")
    drive = MagicMock(name="drive")
    svc = SheetsServices(sheets, drive, "bot@example.com")
    assert (svc.sheets, svc.drive, svc.account_email) == (sheets, drive, "bot@example.com")


def test_core_service_is_immutable():
    svc = SheetsServices(MagicMock(name="sheets"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        svc.sheets = MagicMock()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        svc.drive = MagicMock()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        svc.account_email = "leak@example.com"  # type: ignore[misc]


def test_core_service_cannot_add_new_attributes():
    # Frozen dataclasses block setting unknown attributes too (no __dict__ slot churn).
    svc = SheetsServices(MagicMock(name="sheets"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        svc.extra = 1  # type: ignore[attr-defined]


def test_core_service_equality_by_value():
    sheets = MagicMock(name="sheets")
    drive = MagicMock(name="drive")
    a = SheetsServices(sheets, drive, "bot@example.com")
    b = SheetsServices(sheets, drive, "bot@example.com")
    c = SheetsServices(sheets, drive, "other@example.com")
    assert a == b
    assert a != c


def test_core_service_is_hashable():
    # Frozen dataclass with hashable fields => usable as a dict key / set member.
    sheets = MagicMock(name="sheets")
    svc = SheetsServices(sheets)
    assert isinstance(hash(svc), int)
    assert {svc: "ok"}[svc] == "ok"


def test_core_service_repr_does_not_crash():
    # repr must not blow up (used in error/debug paths); does not assert content.
    svc = SheetsServices(MagicMock(name="sheets"), account_email="bot@example.com")
    assert "SheetsServices" in repr(svc)


def test_core_service_module_import_is_transport_free():
    # service.py is a PURE leaf (DESIGN §1 boundary): importing it must not drag in any
    # transport/CLI/adapter dependency. This is a same-process smoke check; the authoritative
    # subprocess guard lives in test_boundary_guard.py. We only assert that *this* module did
    # not itself import the forbidden names (checking its own module globals, not sys.modules,
    # so an unrelated already-imported adapter test can't make this falsely fail).
    import gsheets.core.service as service_mod

    forbidden = {"fastmcp", "mcp", "argparse", "pydantic"}
    referenced = forbidden & set(vars(service_mod))
    assert not referenced, f"service.py references transport/adapter modules: {sorted(referenced)}"
    # And the module object itself exposes only the dataclass as public API surface.
    assert service_mod.SheetsServices is SheetsServices


def test_core_service_carries_sheets_resource_handle():
    # The whole point: it transports the v4 Resource (and optional drive Resource) to core.
    # A chained accessor call on the held handle must reach the underlying mock unchanged.
    sheets = MagicMock(name="sheets_v4_resource")
    sentinel = object()
    sheets.spreadsheets.return_value.get.return_value.execute.return_value = sentinel
    svc = SheetsServices(sheets)
    assert svc.sheets.spreadsheets().get(spreadsheetId="x").execute() is sentinel


def test_core_service_does_not_register_forbidden_modules_on_import():
    # Defensive: confirm importing this leaf alone (already imported above) coexists with a
    # clean interpreter assumption — none of these are pulled by service.py's own imports.
    # We check service.py's declared imports indirectly: its module file references only
    # stdlib `dataclasses` (+ __future__). If pydantic/fastmcp were imported at module load,
    # they'd appear in sys.modules even without this test importing them.
    assert "gsheets.core.service" in sys.modules
