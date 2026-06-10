"""Slicer read serialization + add/update/delete write builders (DESIGN §X.0f, §X.3/§X.16).

A *slicer* is an on-grid filter control: it points at a
data range, filters one column of it, and is anchored at a cell on a (usually different) sheet.
The owning read fn (``structure(action="read")``, structure-ext) attaches the serialized dict to
the host sheet's ``slicers`` list, emitting per-sheet rich data only when present (token-safe).

This module owns two halves, both PURE core:

- :func:`serialize_slicer` — flatten a Google ``Slicer`` (``slicerId`` + ``SlicerSpec`` +
  ``EmbeddedObjectPosition``) into the terse, flattened read shape::

      { "slicerId": 4, "title": "Region", "range": "Data!A1:F500", "columnIndex": 0,
        "anchor": { "sheet": "Dash", "row": 0, "col": 8 }, "criteria": "ONE_OF_LIST(...)",
        "line": 'slicer 4 "Region" col 0 [Data!A1:F500] @ Dash!I1' }

- :func:`build_add_slicer_request` / :func:`build_update_slicer_request` /
  :func:`build_delete_slicer_request` — return ready-to-send ``batchUpdate`` request dicts
  (``addSlicer`` / ``updateSlicerSpec`` / ``deleteEmbeddedObject``). ``update`` auto-builds its
  ``fields`` mask from the payload via :func:`gsheets.core.fieldsmask.build_fields_mask`. These
  mirror the tables/banding/filter write builders and are consumed by ``core/structure.py``'s
  new ``add_slicer`` / ``update_slicer`` / ``delete_slicer`` actions (which own the
  action->handler dispatch); the ``addSlicer`` reply's ``slicerId`` is captured by
  ``structure.capture_new_ids`` (its ``_REPLY_ID_SPECS`` is extended there). Slicers share the
  *embedded-object* id space, so delete maps to ``deleteEmbeddedObject`` (not a slicer-specific
  delete request).

Range handling (DESIGN §X.0 / §4 boundary): the serializer holds a ``services`` handle ONLY to
resolve the two ``GridRange`` / ``GridCoordinate`` references — the slicer's ``dataRange`` -> A1
(``gridrange_to_a1``) and the anchor cell's ``sheetId`` -> sheet name (the slicer's anchor sheet
differs from its data sheet, so both must resolve). The write builders do the inverse: a flat
``dataRange`` A1 -> ``GridRange`` (``addressing.a1_to_gridrange``, the SAME fn tables/filters
use) and a single-cell ``anchor`` A1 -> ``GridCoordinate`` ``{sheetId,rowIndex,columnIndex}``.
The ``filterCriteria`` condition reuses the SAME condformat condition (de)serializer so a slicer
criterion reads/writes exactly like a CF / filter condition (``NUMBER_GREATER(0)``). Keys are
omitted when absent (token efficiency).

This module is PURE core: stdlib + sibling core modules only. It must NEVER import ``fastmcp``,
``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models`` (DESIGN §1 boundary).
"""

from __future__ import annotations

from . import addressing, condformat
from .addressing import a1_to_gridrange, gridrange_to_a1
from .errors import SheetsError
from .fieldsmask import build_fields_mask
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

    anchor = _serialize_anchor(slicer, services, spreadsheet_id)
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
    slicer: dict, services: SheetsServices, spreadsheet_id: str
) -> dict | None:
    """Flatten the slicer's anchor ``GridCoordinate`` to ``{sheet, row, col}``.

    The anchor lives at the slicer's TOP-LEVEL ``position.overlayPosition.anchorCell`` (an
    ``EmbeddedObjectPosition`` -> ``OverlayPosition`` -> ``GridCoordinate``) — ``position`` is a
    SIBLING of ``spec`` on the ``Slicer`` resource, NOT nested inside ``spec`` (verified against
    the live ``spreadsheets.get`` shape). The ``GridCoordinate``'s ``sheetId`` is resolved to a
    sheet NAME (a slicer is typically anchored on a dashboard tab distinct from its data tab);
    ``rowIndex``/``columnIndex`` are 0-based and surfaced verbatim as ``row``/``col`` (omitted when
    absent — a top-left anchor is row 0/col 0, both meaningful). Returns ``None`` when no anchor
    cell is present.
    """
    position = slicer.get("position")
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

    # ``rowIndex``/``columnIndex`` are 0-based, and a ``GridCoordinate`` OMITS them when 0 — so an
    # absent index means 0. An anchorCell always has a definite position and 0 (top row / first
    # column) is meaningful, so surface both explicitly (defaulting absent to 0). This keeps the
    # anchor dict complete and the terse line always renderable (e.g. ``@ Sheet!E1`` for an anchor
    # the API returned as ``{columnIndex: 4}`` with ``rowIndex`` omitted).
    out["row"] = int(cell.get("rowIndex") or 0)
    out["col"] = int(cell.get("columnIndex") or 0)

    return out


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


# ===========================================================================
# build: flat slicer spec -> Google batchUpdate request dicts
# ===========================================================================


def build_add_slicer_request(
    services: SheetsServices,
    spreadsheet_id: str,
    spec: dict,
) -> dict:
    """Build an ``addSlicer`` ``batchUpdate`` request (DESIGN §X.3 add_slicer).

    ``spec`` is the FLAT public slicer shape::

        { "title"?: str, "dataRange": <A1>, "columnIndex"?: int,
          "anchor": <A1 single cell>, "criteria"?: {"hidden"?, "visible"?, "condition"?} }

    ``dataRange`` (REQUIRED) is an A1 range resolved to a Google ``GridRange`` via
    ``addressing.a1_to_gridrange`` (the SAME fn tables/filters use). ``anchor`` (REQUIRED) is a
    single A1 cell resolved to a ``GridCoordinate`` ``{sheetId,rowIndex,columnIndex}`` — the
    slicer is positioned via ``position.overlayPosition.anchorCell`` (its anchor tab is usually a
    different sheet from the data tab). ``criteria`` builds a Google ``FilterCriteria``
    (``hiddenValues``/``visibleValues`` and/or a ``BooleanCondition``) reusing the SAME condformat
    condition builder when a ``condition`` is given — mirroring ``filters._build_filter_specs``.
    The caller (``structure.add_slicer``) captures the new ``slicerId`` from the reply.

    Args:
        services: The authed handle (resolves ``dataRange`` -> ``GridRange`` and the ``anchor``
            cell's ``sheetId``).
        spreadsheet_id: Target spreadsheet id.
        spec: The flat slicer spec (see above).

    Returns:
        ``{"addSlicer": {"slicer": {"spec": {SlicerSpec}, "position": {...anchorCell...}}}}`` —
        ready for ``spreadsheets.batchUpdate``.

    Raises:
        SheetsError: ``missing_param`` when ``dataRange`` or ``anchor`` is absent;
            ``bad_slicer`` when ``spec`` is not a dict.
    """
    if not isinstance(spec, dict):
        raise SheetsError(
            "bad_slicer", f"slicer spec must be a dict, got {type(spec).__name__}"
        )

    data_range = spec.get("dataRange")
    if not data_range:
        raise SheetsError(
            "missing_param", "add_slicer requires params={'dataRange': <A1 range>}"
        )
    anchor = spec.get("anchor")
    if not anchor:
        raise SheetsError(
            "missing_param", "add_slicer requires params={'anchor': <A1 single cell>}"
        )

    grid_range = a1_to_gridrange(services, spreadsheet_id, data_range)
    anchor_cell = _anchor_to_grid_coordinate(services, spreadsheet_id, anchor)

    slicer_spec: dict = {"dataRange": grid_range}

    title = spec.get("title")
    if title is not None:
        slicer_spec["title"] = title

    # ``columnIndex`` is the 0-based offset (into the data range) of the filtered column. 0 is a
    # meaningful value, so test presence rather than truthiness.
    column_index = spec.get("columnIndex")
    if column_index is not None:
        slicer_spec["columnIndex"] = int(column_index)

    criteria = spec.get("criteria")
    if criteria is not None:
        slicer_spec["filterCriteria"] = _build_filter_criteria(criteria)

    return {
        "addSlicer": {
            "slicer": {
                "spec": slicer_spec,
                "position": {"overlayPosition": {"anchorCell": anchor_cell}},
            }
        }
    }


def build_update_slicer_request(
    services: SheetsServices,
    spreadsheet_id: str,
    slicer_id: int,
    spec: dict,
) -> dict:
    """Build an ``updateSlicerSpec`` request with an AUTO fields mask (DESIGN §X.3 update_slicer).

    Only the keys present in ``spec`` are written; the ``fields`` mask is derived from the built
    ``SlicerSpec`` payload via :func:`gsheets.core.fieldsmask.build_fields_mask`, so unspecified
    spec subfields are never wiped and an empty change is refused (``empty_payload``). The
    settable keys mirror the flat add shape (minus the immutable anchor):
    ``title`` / ``dataRange`` (A1 -> ``GridRange``) / ``columnIndex`` / ``criteria`` (->
    ``FilterCriteria``). ``dataRange`` and ``filterCriteria`` mask atomically (each is one logical
    field that Google replaces whole).

    Args:
        services: The authed handle (resolves ``dataRange`` -> ``GridRange`` when present).
        spreadsheet_id: Target spreadsheet id.
        slicer_id: The ``slicerId`` to update.
        spec: ``{"title"?, "dataRange"?, "columnIndex"?, "criteria"?}`` (at least one).

    Returns:
        ``{"updateSlicerSpec": {"slicerId": <id>, "spec": {SlicerSpec}, "fields": "<mask>"}}``.

    Raises:
        SheetsError: ``missing_param`` when ``slicer_id`` is ``None``; ``bad_slicer`` when
            ``spec`` is not a dict; ``empty_payload`` when no settable key is present.
    """
    if slicer_id is None:
        raise SheetsError(
            "missing_param", "update_slicer requires params={'slicerId': <int>}"
        )
    if not isinstance(spec, dict):
        raise SheetsError(
            "bad_slicer", f"slicer spec must be a dict, got {type(spec).__name__}"
        )

    slicer_spec: dict = {}
    # The masked payload mirrors ``slicer_spec`` so the auto mask covers only the changed fields.
    masked: dict = {}

    if spec.get("title") is not None:
        slicer_spec["title"] = spec["title"]
        masked["title"] = spec["title"]

    if spec.get("dataRange") is not None:
        grid_range = a1_to_gridrange(services, spreadsheet_id, spec["dataRange"])
        slicer_spec["dataRange"] = grid_range
        # ``dataRange`` is one logical field on ``SlicerSpec`` — the whole GridRange is replaced
        # atomically, so mask it via a scalar sentinel (a leaf) rather than recursing its
        # ``sheetId,...`` subfields (which Google would reject / partially apply).
        masked["dataRange"] = True

    if spec.get("columnIndex") is not None:
        slicer_spec["columnIndex"] = int(spec["columnIndex"])
        masked["columnIndex"] = slicer_spec["columnIndex"]

    if spec.get("criteria") is not None:
        filter_criteria = _build_filter_criteria(spec["criteria"])
        slicer_spec["filterCriteria"] = filter_criteria
        # ``filterCriteria`` masks atomically (the whole criteria object is one field).
        masked["filterCriteria"] = True

    fields = build_fields_mask(masked)  # raises empty_payload when nothing to change
    return {
        "updateSlicerSpec": {
            "slicerId": slicer_id,
            "spec": slicer_spec,
            "fields": fields,
        }
    }


def build_delete_slicer_request(slicer_id: int) -> dict:
    """Build a ``deleteEmbeddedObject`` request for a slicer (DESIGN §X.3 delete_slicer).

    Slicers share the *embedded-object* id space (charts/slicers), so a slicer is deleted by its
    ``slicerId`` via ``deleteEmbeddedObject`` (there is no slicer-specific delete request).

    Args:
        slicer_id: The ``slicerId`` (== embedded object id) to delete.

    Returns:
        ``{"deleteEmbeddedObject": {"objectId": <slicer_id>}}``.

    Raises:
        SheetsError: ``missing_param`` when ``slicer_id`` is ``None``.
    """
    if slicer_id is None:
        raise SheetsError(
            "missing_param", "delete_slicer requires params={'slicerId': <int>}"
        )
    return {"deleteEmbeddedObject": {"objectId": slicer_id}}


# --------------------------------------------------------------------------------------
# build helpers (flat spec -> Google sub-objects)
# --------------------------------------------------------------------------------------


def _anchor_to_grid_coordinate(
    services: SheetsServices, spreadsheet_id: str, anchor: str
) -> dict:
    """Resolve a single-cell A1 ``anchor`` to a Google ``GridCoordinate``.

    A slicer is positioned at ``position.overlayPosition.anchorCell``, a ``GridCoordinate``
    ``{sheetId, rowIndex, columnIndex}`` (0-based). The A1 cell is resolved through the SAME
    ``a1_to_gridrange`` path used everywhere else (a single cell ``Dash!I1`` resolves to a
    GridRange with ``sheetId``/``startRowIndex``/``startColumnIndex``), then collapsed to the
    range's top-left corner. A range (rather than a single cell) is rejected so the anchor stays
    an unambiguous cell.
    """
    if not isinstance(anchor, str) or not anchor.strip():
        raise SheetsError(
            "bad_slicer", f"anchor must be a single A1 cell string, got {anchor!r}"
        )
    gr = a1_to_gridrange(services, spreadsheet_id, anchor)
    row = gr.get("startRowIndex")
    col = gr.get("startColumnIndex")
    if row is None or col is None:
        raise SheetsError(
            "bad_slicer",
            f"anchor must be a single bounded A1 cell (e.g. 'Dash!I1'), got {anchor!r}",
        )
    # Guard against a multi-cell range masquerading as an anchor (anchor must be ONE cell).
    end_row = gr.get("endRowIndex")
    end_col = gr.get("endColumnIndex")
    if (end_row is not None and end_row - row != 1) or (
        end_col is not None and end_col - col != 1
    ):
        raise SheetsError(
            "bad_slicer",
            f"anchor must be a single cell, not a multi-cell range: {anchor!r}",
        )
    return {
        "sheetId": gr["sheetId"],
        "rowIndex": row,
        "columnIndex": col,
    }


def _build_filter_criteria(criteria: object) -> dict:
    """Build a Google ``FilterCriteria`` from the flat ``criteria`` dict.

    Mirrors ``filters._build_filter_specs`` (one column's ``filterCriteria``):
    ``{"hidden"?, "visible"?, "condition"?}`` -> ``hiddenValues`` / ``visibleValues`` /
    ``condition`` (a ``BooleanCondition``). ``condition`` may be a terse string
    (``"NUMBER_GREATER(0)"``) — parsed by the SAME condformat condition parser — or an
    already-structured ``{type, values}`` dict. At least one facet is required so a slicer is
    never given an empty criterion.
    """
    if not isinstance(criteria, dict):
        raise SheetsError(
            "bad_slicer",
            "slicer 'criteria' must be a {hidden?, visible?, condition?} dict",
        )

    filter_criteria: dict = {}
    hidden = criteria.get("hidden")
    if hidden:
        filter_criteria["hiddenValues"] = list(hidden)
    visible = criteria.get("visible")
    if visible:
        filter_criteria["visibleValues"] = list(visible)
    condition = criteria.get("condition")
    if condition is not None:
        filter_criteria["condition"] = _build_condition(condition)

    if not filter_criteria:
        raise SheetsError(
            "bad_slicer",
            "slicer 'criteria' has no hidden/visible/condition to apply",
        )
    return filter_criteria


def _build_condition(condition: object) -> dict:
    """Build a Google ``BooleanCondition`` from a terse string OR a structured dict.

    Reuses the SAME condformat condition (de)serializer so slicer conditions accept exactly the
    CF / filter condition grammar (``NUMBER_GREATER(0)``, ``TEXT_CONTAINS(done)``, ``BLANK``).
    Mirrors ``filters._build_condition``.
    """
    if isinstance(condition, str):
        structured = condformat._parse_condition(condition.strip())
    elif isinstance(condition, dict):
        structured = condition
    else:
        raise SheetsError(
            "bad_slicer",
            "condition must be a terse string (e.g. 'NUMBER_GREATER(0)') or a "
            "{type, values} dict",
        )
    return condformat._build_condition(structured)
