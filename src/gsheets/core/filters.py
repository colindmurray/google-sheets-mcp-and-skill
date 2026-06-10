"""Basic-filter + filter-view (de)serialization and write-request builders (DESIGN §X.0d, §X.3/§X.4).

This module reads ``sheets.basicFilter`` / ``sheets.filterViews`` into the terse, flattened
condformat-style shape, and builds the ``setBasicFilter`` / ``clearBasicFilter`` /
``addFilterView`` / ``updateFilterView`` / ``deleteFilterView`` ``batchUpdate`` request bodies
that ``core.structure``'s new actions issue.

Two halves, both PURE core:

* **Serializers** (``serialize_basic_filter`` / ``serialize_filter_view``) take an *already-
  resolved A1 range string* (the owning read fn resolves the Google ``GridRange`` -> A1 first,
  mirroring ``reads._serialize_cf_rule``, DESIGN §4 boundary). A per-column ``FilterSpec`` /
  legacy ``FilterCriteria`` flattens to ``{"col": <letter>, "hidden": [...], "condition": "..."}``
  and a ``SortSpec`` to ``{"col": <letter>, "order": "ASCENDING"}``. The condition string reuses
  the SAME condformat condition (de)serializer so a filter condition reads exactly like a CF
  condition (``NUMBER_GREATER(0)``).
* **Request builders** (``build_*_request``) take ``services`` + a resolved ``GridRange`` (or
  ids) + the public ``params`` and return a single ``batchUpdate`` request dict. ``col`` letters
  in incoming ``sorted``/``criteria`` are converted back to absolute column indices.
  ``build_update_filter_view_request`` AUTO-BUILDS its ``fields`` mask from the populated
  ``FilterView`` payload via :func:`gsheets.core.fieldsmask.build_fields_mask`.

Column addressing (LOCKED): Sheets filters address columns by an **absolute** 0-based sheet
column index (``SortSpec.dimensionIndex`` / ``FilterSpec.columnIndex`` / the legacy
``criteria`` map key). The terse line and the structured ``col`` field carry the **column
LETTER** for that absolute index (``0`` <-> ``A``), NOT an offset from the range start.

PURE core module: imports only stdlib + sibling core modules. It must NEVER import
``fastmcp``, ``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models`` (DESIGN §1 boundary).
"""

from __future__ import annotations

from . import addressing, condformat
from .errors import SheetsError
from .fieldsmask import build_fields_mask
from .service import SheetsServices

_SORT_ORDERS = frozenset({"ASCENDING", "DESCENDING"})


# ===========================================================================
# Column-index <-> column-letter helpers (absolute sheet column index)
# ===========================================================================


def _index_to_col(index: int) -> str:
    """Absolute 0-based column index -> column letter(s) (``0`` -> ``"A"``)."""
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise SheetsError("bad_filter", f"column index must be a non-negative int, got {index!r}")
    return addressing._index_to_col(index)


def _col_to_index(col: str) -> int:
    """Column letter(s) -> absolute 0-based column index (``"A"`` -> ``0``).

    Accepts a bare letter token (``"B"``); a stray ``$`` (from a pasted ref) is tolerated.
    """
    if not isinstance(col, str) or not col.strip():
        raise SheetsError("bad_filter", f"column must be a non-empty letter token, got {col!r}")
    token = col.strip().lstrip("$").upper()
    if not token.isalpha():
        raise SheetsError("bad_filter", f"column must be letters only (e.g. 'B'), got {col!r}")
    return addressing._col_to_index(token)


# ===========================================================================
# Serialize: Google BasicFilter / FilterView -> terse flattened dict
# ===========================================================================


def serialize_basic_filter(bf: dict, range_a1: str | None = None) -> dict:
    """Serialize a Google ``BasicFilter`` to the terse flattened shape (DESIGN §X.0d).

    Args:
        bf: A Google ``BasicFilter`` dict (``range`` already stripped — the owning read fn
            passes the resolved A1 string separately as ``range_a1``; any ``range`` left on
            ``bf`` is ignored in favor of ``range_a1``).
        range_a1: The basic filter's range as a sheet-qualified A1 string (resolved by the
            caller via ``gridrange_to_a1``).

    Returns:
        ``{"range": "Sheet1!A1:F500", "sorted": [{"col": "C", "order": "ASCENDING"}],
        "criteria": [{"col": "B", "hidden": ["Closed"], "condition": "NUMBER_GREATER(0)"}],
        "line": "basicFilter [Sheet1!A1:F500] sort C asc | B: hide Closed, NUMBER_GREATER(0)"}``.
        ``sorted``/``criteria`` are omitted when empty; ``line`` is always present.
    """
    if not isinstance(bf, dict):
        raise SheetsError("bad_filter", f"basicFilter must be a dict, got {type(bf).__name__}")

    out: dict = {}
    if range_a1 is not None:
        out["range"] = range_a1

    sorted_specs = _serialize_sort_specs(bf.get("sortSpecs"))
    if sorted_specs:
        out["sorted"] = sorted_specs

    criteria = _serialize_criteria(bf)
    if criteria:
        out["criteria"] = criteria

    out["line"] = _basic_filter_line(range_a1, sorted_specs, criteria)
    return out


def serialize_filter_view(fv: dict, range_a1: str | None = None) -> dict:
    """Serialize a Google ``FilterView`` to the terse flattened shape (DESIGN §X.0d).

    Args:
        fv: A Google ``FilterView`` dict (carries ``filterViewId``/``title``/``sortSpecs``/
            ``filterSpecs``/legacy ``criteria``).
        range_a1: The filter view's range as a sheet-qualified A1 string (resolved by the
            caller via ``gridrange_to_a1``).

    Returns:
        ``{"filterViewId": 123, "title": "Open only", "range": "Sheet1!A1:F500",
        "sorted": [...], "criteria": [...],
        "line": 'filterView 123 "Open only" [Sheet1!A1:F500] | B: hide Closed'}``.
        ``title``/``range``/``sorted``/``criteria`` are omitted when absent/empty.
    """
    if not isinstance(fv, dict):
        raise SheetsError("bad_filter", f"filterView must be a dict, got {type(fv).__name__}")

    out: dict = {"filterViewId": fv.get("filterViewId")}
    title = fv.get("title")
    if title is not None:
        out["title"] = title
    if range_a1 is not None:
        out["range"] = range_a1

    sorted_specs = _serialize_sort_specs(fv.get("sortSpecs"))
    if sorted_specs:
        out["sorted"] = sorted_specs

    criteria = _serialize_criteria(fv)
    if criteria:
        out["criteria"] = criteria

    out["line"] = _filter_view_line(
        fv.get("filterViewId"), title, range_a1, sorted_specs, criteria
    )
    return out


def _serialize_sort_specs(sort_specs: object) -> list[dict]:
    """Flatten ``[SortSpec]`` -> ``[{"col": <letter>, "order": "ASCENDING"|"DESCENDING"}]``."""
    out: list[dict] = []
    for spec in sort_specs or []:
        if not isinstance(spec, dict):
            continue
        index = spec.get("dimensionIndex")
        if index is None:
            # A SortSpec lacking a column index is not addressable; skip it.
            continue
        entry: dict = {"col": _index_to_col(int(index))}
        order = spec.get("sortOrder")
        if order is not None:
            entry["order"] = order
        out.append(entry)
    return out


def _serialize_criteria(container: dict) -> list[dict]:
    """Flatten a filter's per-column criteria into a terse list, sorted by column index.

    Reads the newer ``filterSpecs`` array first (each ``{columnIndex, filterCriteria}``) and
    falls back to the legacy ``criteria`` map (``{"<index>": FilterCriteria}``). Both flatten
    to ``{"col": <letter>, "hidden": [...], "condition": "<serialized>"}`` (each sub-key
    omitted when absent). Returned in ascending column order for stable golden masters.
    """
    by_index: dict[int, dict] = {}

    for spec in container.get("filterSpecs") or []:
        if not isinstance(spec, dict):
            continue
        index = spec.get("columnIndex")
        crit = spec.get("filterCriteria")
        if index is None or not isinstance(crit, dict):
            continue
        by_index[int(index)] = crit

    legacy = container.get("criteria")
    if isinstance(legacy, dict):
        for key, crit in legacy.items():
            if not isinstance(crit, dict):
                continue
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            # filterSpecs win over the deprecated map on conflict.
            by_index.setdefault(index, crit)

    out: list[dict] = []
    for index in sorted(by_index):
        out.append(_serialize_one_criterion(index, by_index[index]))
    return out


def _serialize_one_criterion(index: int, crit: dict) -> dict:
    """Flatten one ``FilterCriteria`` to ``{"col", "hidden"?, "condition"?}``."""
    entry: dict = {"col": _index_to_col(index)}

    hidden = crit.get("hiddenValues")
    if hidden:
        entry["hidden"] = list(hidden)

    # ``visibleValues`` is the inverse expression — normalize it onto a ``visible`` key so a
    # read never silently drops it (DESIGN §X.0d: "hiddenValues / visibleValues normalized").
    visible = crit.get("visibleValues")
    if visible:
        entry["visible"] = list(visible)

    condition = crit.get("condition")
    if isinstance(condition, dict) and condition.get("type"):
        # Reuse the SAME condformat condition serializer (DESIGN §X.0d) so a filter condition
        # renders identically to a CF condition (e.g. ``NUMBER_GREATER(0)``).
        entry["condition"] = condformat._serialize_condition(condition)

    return entry


def _basic_filter_line(
    range_a1: str | None, sorted_specs: list[dict], criteria: list[dict]
) -> str:
    """Terse line: ``basicFilter [range] sort C asc | B: hide Closed, NUMBER_GREATER(0)``."""
    head = "basicFilter"
    if range_a1:
        head += f" [{range_a1}]"
    return _join_body(head, sorted_specs, criteria)


def _filter_view_line(
    filter_view_id: object,
    title: object,
    range_a1: str | None,
    sorted_specs: list[dict],
    criteria: list[dict],
) -> str:
    """Terse line: ``filterView 123 "Open only" [range] | B: hide Closed``."""
    head = "filterView"
    if filter_view_id is not None:
        head += f" {filter_view_id}"
    if title is not None:
        head += f' "{title}"'
    if range_a1:
        head += f" [{range_a1}]"
    return _join_body(head, sorted_specs, criteria)


def _join_body(head: str, sorted_specs: list[dict], criteria: list[dict]) -> str:
    """Append the ``sort ...`` and ``col: ...`` segments to a line head."""
    segments: list[str] = []
    if sorted_specs:
        sort_parts = [
            f"{s['col']} {_order_word(s.get('order'))}" for s in sorted_specs
        ]
        segments.append("sort " + ", ".join(sort_parts))
    for crit in criteria:
        segments.append(_criterion_segment(crit))
    if not segments:
        return head
    return head + " " + " | ".join(segments)


def _order_word(order: object) -> str:
    """``ASCENDING`` -> ``asc``, ``DESCENDING`` -> ``desc`` (default ``asc``)."""
    if order == "DESCENDING":
        return "desc"
    return "asc"


def _criterion_segment(crit: dict) -> str:
    """``B: hide Closed, NUMBER_GREATER(0)`` for one flattened criterion (line form)."""
    parts: list[str] = []
    hidden = crit.get("hidden")
    if hidden:
        parts.append("hide " + ", ".join(str(v) for v in hidden))
    visible = crit.get("visible")
    if visible:
        parts.append("show " + ", ".join(str(v) for v in visible))
    condition = crit.get("condition")
    if condition:
        parts.append(str(condition))
    return f"{crit['col']}: " + ", ".join(parts) if parts else f"{crit['col']}:"


# ===========================================================================
# Build: public params -> Google batchUpdate request dicts
# ===========================================================================


def build_set_basic_filter_request(
    services: SheetsServices,
    spreadsheet_id: str,
    grid_range: dict,
    params: dict,
) -> dict:
    """Build a ``setBasicFilter`` request over a resolved ``GridRange`` (DESIGN §X.3 table).

    Args:
        services: The authed handle (unused here; accepted for a uniform builder signature
            and forward compatibility).
        spreadsheet_id: Target spreadsheet id (unused; uniform signature).
        grid_range: The basic filter's range as a Google ``GridRange`` (caller resolves A1 ->
            GridRange first).
        params: ``{"sorted"?: [{"col","order"?}], "criteria"?: [{"col","hidden"?,"condition"?,
            "visible"?}]}``.

    Returns:
        ``{"setBasicFilter": {"filter": {BasicFilter}}}``.
    """
    bf = _build_filter_payload(grid_range, params)
    return {"setBasicFilter": {"filter": bf}}


def build_clear_basic_filter_request(
    services: SheetsServices,
    spreadsheet_id: str,
    sheet_id: int,
) -> dict:
    """Build a ``clearBasicFilter`` request for one sheet (DESIGN §X.3 table).

    Args:
        services: The authed handle (unused; uniform signature).
        spreadsheet_id: Target spreadsheet id (unused; uniform signature).
        sheet_id: The ``sheetId`` whose basic filter is cleared.

    Returns:
        ``{"clearBasicFilter": {"sheetId": <id>}}``.
    """
    if sheet_id is None:
        raise SheetsError("missing_param", "clear_basic_filter requires a sheetId")
    return {"clearBasicFilter": {"sheetId": sheet_id}}


def build_add_filter_view_request(
    services: SheetsServices,
    spreadsheet_id: str,
    grid_range: dict,
    params: dict,
) -> dict:
    """Build an ``addFilterView`` request over a resolved ``GridRange`` (DESIGN §X.3 table).

    Args:
        services: The authed handle (unused; uniform signature).
        spreadsheet_id: Target spreadsheet id (unused; uniform signature).
        grid_range: The filter view's range as a Google ``GridRange``.
        params: ``{"title": str, "sorted"?, "criteria"?}``.

    Returns:
        ``{"addFilterView": {"filter": {FilterView}}}`` (the new ``filterViewId`` is captured
        by the caller from ``replies[]``).
    """
    fv = _build_filter_payload(grid_range, params)
    title = params.get("title")
    if title is not None:
        fv["title"] = title
    return {"addFilterView": {"filter": fv}}


def build_update_filter_view_request(
    services: SheetsServices,
    spreadsheet_id: str,
    params: dict,
    *,
    grid_range: dict | None = None,
) -> dict:
    """Build an ``updateFilterView`` request with an AUTO-built fields mask (DESIGN §X.3 table).

    Only the keys actually present in ``params`` are written; the ``fields`` mask is derived
    from the populated ``FilterView`` payload via :func:`build_fields_mask` so unspecified
    sub-fields are never wiped and an empty change is refused.

    Args:
        services: The authed handle (unused; uniform signature).
        spreadsheet_id: Target spreadsheet id (unused; uniform signature).
        params: ``{"filterViewId": int, "title"?, "sorted"?, "criteria"?}`` (a new ``range`` is
            passed pre-resolved via the ``grid_range`` kwarg, not inside ``params``).
        grid_range: Optional already-resolved new ``GridRange`` (when the caller wants to move
            the view). When given, ``range`` joins the mask.

    Returns:
        ``{"updateFilterView": {"filter": {FilterView}, "fields": "<mask>"}}``.
    """
    filter_view_id = params.get("filterViewId")
    if filter_view_id is None:
        raise SheetsError("missing_param", "update_filter_view requires a filterViewId")

    fv: dict = {"filterViewId": filter_view_id}
    # The id identifies the target; it is NOT a mutable field, so exclude it from the mask.
    mask_payload: dict = {}

    if "title" in params and params.get("title") is not None:
        fv["title"] = params["title"]
        mask_payload["title"] = params["title"]

    if grid_range is not None:
        fv["range"] = grid_range
        mask_payload["range"] = grid_range

    if "sorted" in params:
        sort_specs = _build_sort_specs(params.get("sorted"))
        fv["sortSpecs"] = sort_specs
        mask_payload["sortSpecs"] = sort_specs

    if "criteria" in params:
        filter_specs = _build_filter_specs(params.get("criteria"))
        fv["filterSpecs"] = filter_specs
        mask_payload["filterSpecs"] = filter_specs

    # build_fields_mask refuses an empty payload (empty_payload) — that is exactly the
    # "nothing to update" guard we want here too.
    fields = build_fields_mask(mask_payload)
    return {"updateFilterView": {"filter": fv, "fields": fields}}


def build_delete_filter_view_request(
    services: SheetsServices,
    spreadsheet_id: str,
    filter_view_id: int,
) -> dict:
    """Build a ``deleteFilterView`` request (DESIGN §X.3 table).

    Args:
        services: The authed handle (unused; uniform signature).
        spreadsheet_id: Target spreadsheet id (unused; uniform signature).
        filter_view_id: The ``filterViewId`` to delete.

    Returns:
        ``{"deleteFilterView": {"filterId": <id>}}``.
    """
    if filter_view_id is None:
        raise SheetsError("missing_param", "delete_filter_view requires a filterViewId")
    return {"deleteFilterView": {"filterId": filter_view_id}}


# --------------------------------------------------------------------------------------
# Build helpers (public params -> Google BasicFilter/FilterView body)
# --------------------------------------------------------------------------------------


def _build_filter_payload(grid_range: dict, params: dict) -> dict:
    """Build a ``BasicFilter``/``FilterView`` body (``range`` + ``sortSpecs`` + ``filterSpecs``).

    ``title``/``filterViewId`` are added by the FilterView callers; this builds the shared
    ``range``/sort/criteria portion.
    """
    if not isinstance(grid_range, dict) or "sheetId" not in grid_range:
        raise SheetsError("bad_range", "filter range must resolve to a GridRange with a sheetId")
    payload: dict = {"range": dict(grid_range)}

    sorted_in = params.get("sorted")
    if sorted_in:
        payload["sortSpecs"] = _build_sort_specs(sorted_in)

    criteria_in = params.get("criteria")
    if criteria_in:
        payload["filterSpecs"] = _build_filter_specs(criteria_in)

    return payload


def _build_sort_specs(sorted_in: object) -> list[dict]:
    """Build ``[SortSpec]`` from ``[{"col": "C", "order"?: "ASCENDING"}]``."""
    if sorted_in is None:
        return []
    if not isinstance(sorted_in, (list, tuple)):
        raise SheetsError("bad_filter", "'sorted' must be a list of {col, order?} dicts")
    out: list[dict] = []
    for spec in sorted_in:
        if not isinstance(spec, dict):
            raise SheetsError("bad_filter", "each sort spec must be a {col, order?} dict")
        col = spec.get("col")
        if col is None:
            raise SheetsError("bad_filter", "a sort spec requires a 'col' letter")
        sort: dict = {"dimensionIndex": _col_to_index(col)}
        order = spec.get("order")
        if order is not None:
            if order not in _SORT_ORDERS:
                raise SheetsError(
                    "bad_filter",
                    f"sort order must be ASCENDING/DESCENDING, got {order!r}",
                )
            sort["sortOrder"] = order
        out.append(sort)
    return out


def _build_filter_specs(criteria_in: object) -> list[dict]:
    """Build ``[FilterSpec]`` from ``[{"col","hidden"?,"visible"?,"condition"?}]``.

    The newer ``filterSpecs`` form is emitted (each ``{columnIndex, filterCriteria}``).
    ``condition`` may be a terse condition string (``NUMBER_GREATER(0)``) — parsed by the SAME
    condformat condition parser — or an already-structured ``{type, values}`` dict.
    """
    if criteria_in is None:
        return []
    if not isinstance(criteria_in, (list, tuple)):
        raise SheetsError(
            "bad_filter", "'criteria' must be a list of {col, hidden?, condition?} dicts"
        )
    out: list[dict] = []
    for crit in criteria_in:
        if not isinstance(crit, dict):
            raise SheetsError(
                "bad_filter", "each criterion must be a {col, hidden?, condition?} dict"
            )
        col = crit.get("col")
        if col is None:
            raise SheetsError("bad_filter", "a criterion requires a 'col' letter")
        column_index = _col_to_index(col)

        filter_criteria: dict = {}
        hidden = crit.get("hidden")
        if hidden:
            filter_criteria["hiddenValues"] = list(hidden)
        visible = crit.get("visible")
        if visible:
            filter_criteria["visibleValues"] = list(visible)
        condition = crit.get("condition")
        if condition is not None:
            filter_criteria["condition"] = _build_condition(condition)

        if not filter_criteria:
            raise SheetsError(
                "bad_filter",
                f"criterion for col {col!r} has no hidden/visible/condition to apply",
            )
        out.append(
            {"columnIndex": column_index, "filterCriteria": filter_criteria}
        )
    return out


def _build_condition(condition: object) -> dict:
    """Build a Google ``BooleanCondition`` from a terse string OR a structured dict.

    Reuses the SAME condformat condition (de)serializer so filter conditions accept exactly
    the CF condition grammar (``NUMBER_GREATER(0)``, ``TEXT_CONTAINS(done)``, ``BLANK``).
    """
    if isinstance(condition, str):
        structured = condformat._parse_condition(condition.strip())
    elif isinstance(condition, dict):
        structured = condition
    else:
        raise SheetsError(
            "bad_filter",
            "condition must be a terse string (e.g. 'NUMBER_GREATER(0)') or a {type, values} dict",
        )
    return condformat._build_condition(structured)
