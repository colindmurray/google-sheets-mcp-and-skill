"""Shared data-filter selector → Google ``DataFilter`` translation (SPEC §6 P2).

Metadata-addressed reads let a caller name a region SYMBOLICALLY (insert-proof) instead of by a
literal A1 string that shifts when rows/columns are inserted. The public selector is one of:

- ``{"a1": "Sheet1!A1:B10"}`` — resolved to a ``GridRange`` (mirroring the literal-``ranges`` path);
- ``{"gridRange": {...}}`` — a raw Google ``GridRange`` passed straight through;
- ``{"developerMetadataLookup": {...}}`` — matches a block previously tagged via ``metadata``
  (the same ``developerMetadataLookup`` shape ``structure.metadata`` already uses for metadata CRUD).

Every reader that accepts ``data_filters`` (``read_values``, ``read_many``, ``describe``) funnels
through :func:`build_data_filter` so the translation lives in ONE place — no duplicated selector
logic across the readers (the DESIGN "no duplicated logic" rule).

PURE core module: imports only stdlib + sibling core modules. It must NEVER import ``fastmcp``,
``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models`` (DESIGN §1 boundary).
"""

from __future__ import annotations

from .addressing import a1_to_gridrange
from .errors import SheetsError
from .service import SheetsServices

# The selector keys a public ``data_filters`` item may carry — EXACTLY one per item.
_SELECTOR_KEYS = ("a1", "gridRange", "developerMetadataLookup")


def build_data_filter(
    services: SheetsServices,
    spreadsheet_id: str,
    selector: dict,
) -> dict:
    """Translate ONE public selector dict to a Google ``DataFilter`` (SPEC §6 P2).

    Accepts exactly one of ``a1`` / ``gridRange`` / ``developerMetadataLookup`` per selector:

    - ``{"a1": "Sheet1!A1:B10"}`` → ``{"gridRange": <GridRange resolved inside core>}`` (the A1 →
      ``GridRange`` conversion is the SAME ``addressing.a1_to_gridrange`` the literal path uses, so
      sheet-name resolution and unbounded forms behave identically);
    - ``{"gridRange": {...}}`` → ``{"gridRange": {...}}`` (passed through verbatim);
    - ``{"developerMetadataLookup": {...}}`` → ``{"developerMetadataLookup": {...}}`` (passed through —
      the same lookup shape ``metadata`` builds for create/update/delete).

    Args:
        services: The authed handle (used only to resolve an ``a1`` selector's sheet name).
        spreadsheet_id: Target spreadsheet id.
        selector: A single selector dict.

    Returns:
        A Google ``DataFilter`` dict suitable for ``*ByDataFilter`` request bodies.

    Raises:
        SheetsError: ``bad_data_filters`` when the selector is not a dict, carries none of the
            allowed keys, carries more than one, or carries a malformed value.
    """
    if not isinstance(selector, dict):
        raise SheetsError(
            "bad_data_filters",
            f"each data_filters item must be a dict, got {type(selector).__name__}",
        )
    present = [k for k in _SELECTOR_KEYS if k in selector]
    if not present:
        raise SheetsError(
            "bad_data_filters",
            f"a data_filters selector needs one of {list(_SELECTOR_KEYS)}",
            hint="e.g. {'a1': 'Sheet1!A1:B10'} | {'gridRange': {...}} | "
            "{'developerMetadataLookup': {'metadataKey': 'block:totals'}}",
        )
    if len(present) > 1:
        raise SheetsError(
            "bad_data_filters",
            f"a data_filters selector carries multiple keys {present}; pass exactly one",
        )

    key = present[0]
    if key == "a1":
        a1 = selector["a1"]
        if not isinstance(a1, str) or not a1.strip():
            raise SheetsError(
                "bad_data_filters", "the 'a1' selector must be a non-empty A1 string"
            )
        return {"gridRange": a1_to_gridrange(services, spreadsheet_id, a1)}

    if key == "gridRange":
        grid = selector["gridRange"]
        if not isinstance(grid, dict) or not grid:
            raise SheetsError(
                "bad_data_filters", "the 'gridRange' selector must be a non-empty GridRange dict"
            )
        return {"gridRange": grid}

    lookup = selector["developerMetadataLookup"]
    if not isinstance(lookup, dict) or not lookup:
        raise SheetsError(
            "bad_data_filters",
            "the 'developerMetadataLookup' selector must be a non-empty lookup dict",
        )
    return {"developerMetadataLookup": lookup}


def build_data_filters(
    services: SheetsServices,
    spreadsheet_id: str,
    data_filters: list[dict],
) -> list[dict]:
    """Translate a NON-EMPTY list of public selectors to Google ``DataFilter`` dicts (SPEC §6 P2).

    The list-level counterpart of :func:`build_data_filter`; every reader that takes ``data_filters``
    calls this so the empty-list guard and per-item translation are identical everywhere.

    Raises:
        SheetsError: ``bad_data_filters`` when the list is empty / not a list, or any item is invalid.
    """
    if not isinstance(data_filters, list) or not data_filters:
        raise SheetsError(
            "bad_data_filters",
            "data_filters must be a non-empty list of selector dicts",
            hint="each selector is one of {'a1': ...} | {'gridRange': ...} | "
            "{'developerMetadataLookup': ...}",
        )
    return [build_data_filter(services, spreadsheet_id, f) for f in data_filters]
