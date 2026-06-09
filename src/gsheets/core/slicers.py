"""Slicer read serialization — flatten a Google ``Slicer`` to a terse, flat dict (DESIGN §X.0f).

Feature #16 (feature-gap-analysis). A *slicer* is an on-grid filter control: it points at a
data range, filters one column of it, and is anchored at a cell on a (usually different) sheet.
The owning read fn (``structure(action="read")``, structure-ext) attaches the serialized dict to
the host sheet's ``slicers`` list, emitting per-sheet rich data only when present (token-safe).

This is **read-only v1**: writing a slicer (``addSlicer`` / ``updateSlicerSpec``) stays in the
``batch`` escape hatch (DESIGN §X.0f / §X.3 note). The serializer flattens Google's nested
``Slicer`` (``slicerId`` + ``SlicerSpec`` + ``EmbeddedObjectPosition``) into::

    { "slicerId": 4, "title": "Region", "range": "Data!A1:F500", "columnIndex": 0,
      "anchor": { "sheet": "Dash", "row": 0, "col": 8 }, "criteria": "ONE_OF_LIST(...)",
      "line": 'slicer 4 "Region" col 0 [Data!A1:F500] @ Dash!I1' }

Range handling (DESIGN §X.0 / §4 boundary): this serializer holds a ``services`` handle ONLY to
resolve the two ``GridRange`` / ``GridCoordinate`` references — the slicer's ``dataRange`` -> A1
(``gridrange_to_a1``) and the anchor cell's ``sheetId`` -> sheet name (the slicer's anchor sheet
differs from its data sheet, so both must resolve). Every other field is serviceless and
flattened in place. The ``filterCriteria`` condition reuses the SAME condformat condition
serializer so a slicer criterion reads exactly like a CF / filter condition
(``NUMBER_GREATER(0)``). Keys are omitted when absent (token efficiency).

This module is PURE core: stdlib + sibling core modules only. It must NEVER import ``fastmcp``,
``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models`` (DESIGN §1 boundary).
"""

from __future__ import annotations

from . import addressing, condformat
from .addressing import gridrange_to_a1
from .errors import SheetsError
from .service import SheetsServices


def serialize_slicer(
    slicer: dict,
    services: SheetsServices,
    spreadsheet_id: str,
) -> dict:
    """Flatten a Google ``Slicer`` into the terse, flattened read shape (DESIGN §X.0f).

    Produces::

        { "slicerId": 4, "title": "Region", "range": "Data!A1:F500", "columnIndex": 0,
          "anchor": { "sheet": "Dash", "row": 0, "col": 8 },
          "criteria": "ONE_OF_LIST(X,Y)",
          "line": 'slicer 4 "Region" col 0 [Data!A1:F500] @ Dash!I1' }

    The slicer's ``spec.dataRange`` ``GridRange`` is resolved to a sheet-qualified A1 string and
    its anchor ``GridCoordinate`` (``spec.position.overlayPosition.anchorCell``) is flattened to
    ``{sheet, row, col}`` with the ``sheetId`` resolved to a sheet name (a slicer's anchor sheet
    is usually a *different* tab from its data sheet). ``filterCriteria`` is rendered to the SAME
    terse condition one-liner ``inspect`` / filter reads surface. Unset keys are omitted (token
    efficiency); ``line`` is always present.

    Args:
        slicer: A Google ``Slicer`` dict (``slicerId`` + ``spec`` + the spec's ``position``).
        services: The authed handle — used ONLY to resolve the ``dataRange`` -> A1 and the
            anchor ``sheetId`` -> sheet name.
        spreadsheet_id: Target spreadsheet id (for those resolutions).

    Returns:
        A flat, JSON-serializable dict (see above). ``slicerId``/``title``/``range``/
        ``columnIndex``/``anchor``/``criteria`` are each present only when the slicer carries
        them; ``line`` is always present.

    Raises:
        SheetsError: ``bad_slicer`` if ``slicer`` is not a dict.
    """
    if not isinstance(slicer, dict):
        raise SheetsError(
            "bad_slicer", f"slicer must be a dict, got {type(slicer).__name__}"
        )

    out: dict = {}

    slicer_id = slicer.get("slicerId")
    if slicer_id is not None:
        out["slicerId"] = slicer_id

    spec = slicer.get("spec")
    if not isinstance(spec, dict):
        spec = {}

    title = spec.get("title")
    if title is not None:
        out["title"] = title

    data_range = spec.get("dataRange")
    a1_range = None
    if isinstance(data_range, dict):
        a1_range = gridrange_to_a1(services, spreadsheet_id, data_range)
        out["range"] = a1_range

    # ``columnIndex`` is the 0-based offset (into the data range) of the filtered column. Surface
    # it verbatim; 0 is a meaningful value, so test presence rather than truthiness.
    column_index = spec.get("columnIndex")
    if column_index is not None:
        out["columnIndex"] = column_index

    anchor = _serialize_anchor(spec, services, spreadsheet_id)
    if anchor is not None:
        out["anchor"] = anchor

    criteria = _serialize_criteria(spec.get("filterCriteria"))
    if criteria is not None:
        out["criteria"] = criteria

    out["line"] = _serialize_line(out, anchor, services, spreadsheet_id)
    return out


# --------------------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------------------


def _serialize_anchor(
    spec: dict, services: SheetsServices, spreadsheet_id: str
) -> dict | None:
    """Flatten the slicer's anchor ``GridCoordinate`` to ``{sheet, row, col}``.

    The anchor lives at ``spec.position.overlayPosition.anchorCell`` (an
    ``EmbeddedObjectPosition`` -> ``OverlayPosition`` -> ``GridCoordinate``). The
    ``GridCoordinate``'s ``sheetId`` is resolved to a sheet NAME (a slicer is typically anchored
    on a dashboard tab distinct from its data tab); ``rowIndex``/``columnIndex`` are 0-based and
    surfaced verbatim as ``row``/``col`` (omitted when absent — a top-left anchor is row 0/col 0,
    both meaningful). Returns ``None`` when no anchor cell is present.
    """
    position = spec.get("position")
    if not isinstance(position, dict):
        return None
    overlay = position.get("overlayPosition")
    if not isinstance(overlay, dict):
        return None
    cell = overlay.get("anchorCell")
    if not isinstance(cell, dict):
        return None

    out: dict = {}
    sheet_id = cell.get("sheetId")
    if sheet_id is not None:
        out["sheet"] = _resolve_sheet_name(services, spreadsheet_id, sheet_id)

    row = cell.get("rowIndex")
    if row is not None:
        out["row"] = row
    col = cell.get("columnIndex")
    if col is not None:
        out["col"] = col

    return out or None


def _resolve_sheet_name(
    services: SheetsServices, spreadsheet_id: str, sheet_id: object
) -> str | int:
    """Resolve a ``sheetId`` to its sheet name, reusing the addressing layer's cached index.

    Falls back to the raw ``sheetId`` (rather than raising) when the id matches no sheet, so a
    dangling/cross-spreadsheet anchor never breaks an otherwise-valid read.
    """
    try:
        sheets = addressing._sheet_index(services, spreadsheet_id)
        return addressing._resolve_sheet_title(sheet_id, sheets)
    except SheetsError:
        return sheet_id


def _serialize_criteria(filter_criteria: object) -> str | None:
    """Render a slicer's ``filterCriteria`` (a ``FilterCriteria``) to a terse one-liner.

    A slicer filters one column via a ``FilterCriteria`` (the SAME shape filter views use):
    ``hiddenValues`` / ``visibleValues`` and/or a ``BooleanCondition``. The condition is rendered
    via the SHARED condformat condition serializer so it reads exactly like a CF / filter
    condition (``NUMBER_GREATER(0)``). Hidden/visible value lists render as
    ``hide v1,v2`` / ``show v1,v2``. Multiple facets join with ``"; "``. Returns ``None`` when no
    criterion is set (no facet emitted).
    """
    if not isinstance(filter_criteria, dict):
        return None

    facets: list[str] = []

    condition = filter_criteria.get("condition")
    if isinstance(condition, dict) and condition.get("type"):
        # Reuse the SAME condformat condition serializer (DESIGN §X.0f) so a slicer condition
        # renders identically to a CF / filter condition.
        facets.append(condformat._serialize_condition(condition))

    hidden = filter_criteria.get("hiddenValues")
    if hidden:
        facets.append("hide " + ",".join(str(v) for v in hidden))

    visible = filter_criteria.get("visibleValues")
    if visible:
        facets.append("show " + ",".join(str(v) for v in visible))

    if not facets:
        return None
    return "; ".join(facets)


def _serialize_line(
    out: dict,
    anchor: dict | None,
    services: SheetsServices,
    spreadsheet_id: str,
) -> str:
    """Build the terse one-line summary (condformat line style, DESIGN §X.0f).

    Form: ``slicer 4 "Region" col 0 [Data!A1:F500] @ Dash!I1``. Each segment is omitted when its
    slot is absent. The anchor renders as ``@ <Sheet>!<A1cell>`` (the ``{sheet,row,col}`` anchor
    converted to a sheet-qualified single-cell A1 reference); when the criterion is present it is
    appended as `` -> <criterion>`` so the line stays parseable left-to-right.
    """
    parts: list[str] = ["slicer"]

    slicer_id = out.get("slicerId")
    if slicer_id is not None:
        parts.append(str(slicer_id))

    title = out.get("title")
    if title is not None:
        parts.append(f'"{title}"')

    column_index = out.get("columnIndex")
    if column_index is not None:
        parts.append(f"col {column_index}")

    a1_range = out.get("range")
    if a1_range is not None:
        parts.append(f"[{a1_range}]")

    anchor_ref = _anchor_a1(anchor, services, spreadsheet_id)
    if anchor_ref is not None:
        parts.append(f"@ {anchor_ref}")

    line = " ".join(parts)

    criteria = out.get("criteria")
    if criteria is not None:
        line = f"{line} -> {criteria}"

    return line


def _anchor_a1(
    anchor: dict | None, services: SheetsServices, spreadsheet_id: str
) -> str | None:
    """Render a flattened ``{sheet, row, col}`` anchor as ``Sheet!A1`` (single-cell A1).

    Reuses the addressing layer's column-index -> letter conversion and its sheet-title quoting
    so the anchor cell reads exactly like every other A1 reference (``Dash!I1``;
    ``'My Sheet'!I1`` when the title needs quoting). Returns ``None`` when there is no addressable
    anchor (no row/col), so the line simply omits the ``@ ...`` segment.
    """
    if not isinstance(anchor, dict):
        return None
    row = anchor.get("row")
    col = anchor.get("col")
    if row is None or col is None:
        return None

    col_letters = addressing._index_to_col(int(col))
    cell = f"{col_letters}{int(row) + 1}"  # 0-based GridCoordinate -> 1-based A1 row

    sheet = anchor.get("sheet")
    if sheet is None:
        return cell
    return f"{addressing._quote_sheet(str(sheet))}!{cell}"
