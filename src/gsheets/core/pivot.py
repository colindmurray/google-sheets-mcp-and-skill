"""Pivot-table definition read — serialize a ``PivotTable`` to a terse, flat dict (DESIGN §X.0b).

Feature #6 (feature-gap-analysis). Only the **anchor (top-left) cell** of a pivot table carries
its ``pivotTable`` definition; the owning read fn (``reads.inspect`` with ``include_pivot=True``)
attaches the serialized dict to that cell as ``"pivot": {…}`` ONLY when present (per-cell rich
data emitted only when set — token-safe, DESIGN §X.6).

This is **read-only** (writing a pivot table stays in the ``batch`` escape hatch, per the
verified analysis: ``pivotTable`` is technically writable via ``updateCells`` but heavy). The
serializer flattens Google's nested ``PivotTable`` into:

```jsonc
{ "source": "Data!A1:F500",
  "rows":    [ { "field": "Region",  "sourceColumnOffset": 0, "showTotals": true, "sortOrder": "ASCENDING" } ],
  "columns": [ { "field": "Quarter", "sourceColumnOffset": 2, "showTotals": true } ],
  "values":  [ { "name": "Sum of Sales", "sourceColumnOffset": 4, "summarize": "SUM" } ],
  "filters": [ { "sourceColumnOffset": 1, "visibleValues": ["X", "Y"] } ],
  "valueLayout": "HORIZONTAL",
  "line": "pivot <- Data!A1:F500 | rows: Region | cols: Quarter | values: SUM(Sales)" }
```

Range handling (DESIGN §X.0 / §4 boundary): this serializer takes a ``services`` handle ONLY to
resolve the ``source`` ``GridRange`` -> A1 (``gridrange_to_a1``), exactly the one resolution the
contract names. Every other field is serviceless and flattened in place. Keys are omitted when
absent (token efficiency); ``rows``/``columns``/``values``/``filters`` are omitted when empty.

This module is PURE core: stdlib + sibling core modules only. It must NEVER import ``fastmcp``,
``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models`` (DESIGN §1 boundary).
"""

from __future__ import annotations

from .addressing import gridrange_to_a1
from .service import SheetsServices

# A short, human/AI-facing summary verb per Google ``PivotValueSummarizeFunction``. We pass the
# enum through verbatim (it is already terse and stable); the dict is here only to document the
# expected vocabulary, not to remap it.
#   SUM | COUNTA | COUNT | COUNTUNIQUE | AVERAGE | MAX | MIN | MEDIAN | PRODUCT | STDEV | STDEVP
#   | VAR | VARP | CUSTOM | NONE


def serialize_pivot(
    pivot: dict,
    services: SheetsServices,
    spreadsheet_id: str,
) -> dict:
    """Flatten a Google ``PivotTable`` into the terse read-side dict (DESIGN §X.0b).

    Resolves the pivot ``source`` ``GridRange`` to an A1 string (the single resolution this
    serializer needs ``services`` for); every other field is flattened in place. Emits a terse
    ``line`` in the condformat line style alongside the structured fields.

    Args:
        pivot: The Google ``PivotTable`` dict (from a cell's ``pivotTable`` field).
        services: The authed handle — used ONLY to resolve ``source`` -> A1.
        spreadsheet_id: Target spreadsheet id (for the ``source`` resolution).

    Returns:
        A flat, JSON-serializable dict. Always carries ``valueLayout`` (defaulting to the
        Google default ``HORIZONTAL`` when absent) and a terse ``line``. ``source`` is present
        only when the pivot carries one; ``rows``/``columns``/``values``/``filters`` are present
        only when non-empty.
    """
    out: dict = {}

    source = pivot.get("source")
    if isinstance(source, dict):
        out["source"] = gridrange_to_a1(services, spreadsheet_id, source)

    rows = [_serialize_group(g) for g in (pivot.get("rows") or [])]
    if rows:
        out["rows"] = rows

    columns = [_serialize_group(g) for g in (pivot.get("columns") or [])]
    if columns:
        out["columns"] = columns

    values = [_serialize_value(v) for v in (pivot.get("values") or [])]
    if values:
        out["values"] = values

    filters = _serialize_filters(pivot)
    if filters:
        out["filters"] = filters

    # ``valueLayout`` governs whether multiple value fields render across columns (HORIZONTAL,
    # the Google default) or down rows (VERTICAL). Always surface it so a consumer never has to
    # guess the layout.
    out["valueLayout"] = pivot.get("valueLayout") or "HORIZONTAL"

    out["line"] = _serialize_line(out)
    return out


# --------------------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------------------


def _serialize_group(group: dict) -> dict:
    """Flatten one ``PivotGroup`` (a row or column dimension).

    Carries the human ``field`` label, the ``sourceColumnOffset`` (0-based offset into the
    source range; absent for data-source pivots, which use a column name reference instead),
    ``showTotals``, and ``sortOrder`` — each emitted only when present. The ``field`` label is
    taken from the explicit ``label`` when set, else from a data-source column reference name,
    else omitted (the consumer can fall back to the offset).
    """
    out: dict = {}

    field = group.get("label")
    if not field:
        # Data-source pivots name their dimension via dataSourceColumnReference.name.
        ref = group.get("dataSourceColumnReference")
        if isinstance(ref, dict) and ref.get("name"):
            field = ref["name"]
    if field:
        out["field"] = field

    if "sourceColumnOffset" in group:
        out["sourceColumnOffset"] = group["sourceColumnOffset"]

    if "showTotals" in group:
        out["showTotals"] = group["showTotals"]

    sort_order = group.get("sortOrder")
    if sort_order and sort_order != "SORT_ORDER_UNSPECIFIED":
        out["sortOrder"] = sort_order

    return out


def _serialize_value(value: dict) -> dict:
    """Flatten one ``PivotValue`` (an aggregated measure).

    ``name`` is the display name; ``summarize`` is the ``summarizeFunction`` enum (``SUM``,
    ``COUNTA``, …) passed through verbatim. A calculated/formula value carries ``formula``
    instead of an offset; both are surfaced when present.
    """
    out: dict = {}

    name = value.get("name")
    if name:
        out["name"] = name

    if "sourceColumnOffset" in value:
        out["sourceColumnOffset"] = value["sourceColumnOffset"]
    else:
        ref = value.get("dataSourceColumnReference")
        if isinstance(ref, dict) and ref.get("name"):
            out["field"] = ref["name"]

    formula = value.get("formula")
    if formula:
        out["formula"] = formula

    summarize = value.get("summarizeFunction")
    if summarize and summarize != "PIVOT_STANDARD_VALUE_FUNCTION_UNSPECIFIED":
        out["summarize"] = summarize

    return out


def _serialize_filters(pivot: dict) -> list[dict]:
    """Flatten pivot filters from BOTH the legacy ``criteria`` map and modern ``filterSpecs``.

    The legacy form is ``criteria``: a map keyed by the (stringified) source column offset to a
    ``PivotFilterCriteria`` (``visibleValues``/``condition``). The modern form is
    ``filterSpecs``: a list of ``PivotFilterSpec`` each carrying ``filterCriteria`` plus a
    ``columnOffsetIndex`` (or a data-source column reference). Both flatten to
    ``{sourceColumnOffset?, field?, visibleValues?}`` (visibleValues omitted when empty). Legacy
    criteria are emitted in ascending offset order for deterministic output.
    """
    out: list[dict] = []

    criteria = pivot.get("criteria")
    if isinstance(criteria, dict):
        for key in sorted(criteria, key=_offset_sort_key):
            crit = criteria.get(key) or {}
            entry: dict = {}
            offset = _to_int(key)
            if offset is not None:
                entry["sourceColumnOffset"] = offset
            _attach_visible(entry, crit)
            out.append(entry)

    for spec in pivot.get("filterSpecs") or []:
        if not isinstance(spec, dict):
            continue
        entry = {}
        if "columnOffsetIndex" in spec:
            entry["sourceColumnOffset"] = spec["columnOffsetIndex"]
        else:
            ref = spec.get("dataSourceColumnReference")
            if isinstance(ref, dict) and ref.get("name"):
                entry["field"] = ref["name"]
        crit = spec.get("filterCriteria") or {}
        _attach_visible(entry, crit)
        out.append(entry)

    return out


def _attach_visible(entry: dict, criteria: dict) -> None:
    """Attach a non-empty ``visibleValues`` list from a ``PivotFilterCriteria`` to ``entry``."""
    visible = criteria.get("visibleValues")
    if visible:
        entry["visibleValues"] = list(visible)


def _offset_sort_key(key: object) -> tuple[int, object]:
    """Sort criteria-map keys numerically when possible, else lexically (stable, deterministic)."""
    val = _to_int(key)
    if val is not None:
        return (0, val)
    return (1, str(key))


def _to_int(key: object) -> int | None:
    """Best-effort int coercion for a criteria-map key (Google keys offsets as strings)."""
    if isinstance(key, bool):
        return None
    if isinstance(key, int):
        return key
    if isinstance(key, str):
        try:
            return int(key)
        except ValueError:
            return None
    return None


def _serialize_line(out: dict) -> str:
    """Build the terse one-line summary (condformat line style, DESIGN §X.0b).

    Form: ``pivot <- Data!A1:F500 | rows: Region | cols: Quarter | values: SUM(Sales)``. Each
    segment is omitted when its slot is empty. Field labels fall back to ``col<offset>`` so a
    label-less dimension still reads. A value renders as ``SUMMARIZE(name)`` (e.g. ``SUM(Sales)``);
    a value with no summarize function renders by name alone.
    """
    parts: list[str] = []

    source = out.get("source")
    parts.append(f"pivot <- {source}" if source else "pivot")

    rows = out.get("rows")
    if rows:
        parts.append("rows: " + ", ".join(_group_label(g) for g in rows))

    columns = out.get("columns")
    if columns:
        parts.append("cols: " + ", ".join(_group_label(g) for g in columns))

    values = out.get("values")
    if values:
        parts.append("values: " + ", ".join(_value_label(v) for v in values))

    filters = out.get("filters")
    if filters:
        parts.append("filters: " + ", ".join(_filter_label(f) for f in filters))

    return " | ".join(parts)


def _group_label(group: dict) -> str:
    """A row/column dimension's display token for the terse line."""
    field = group.get("field")
    if field:
        return str(field)
    offset = group.get("sourceColumnOffset")
    return f"col{offset}" if offset is not None else "col?"


def _value_label(value: dict) -> str:
    """A measure's display token: ``SUMMARIZE(name)`` (or the bare name when un-summarized)."""
    name = value.get("name") or value.get("field")
    if name is None:
        offset = value.get("sourceColumnOffset")
        name = f"col{offset}" if offset is not None else "col?"
    summarize = value.get("summarize")
    return f"{summarize}({name})" if summarize else str(name)


def _filter_label(flt: dict) -> str:
    """A filter's display token: ``<field>[v1,v2]`` (visible values), or just the field."""
    field = flt.get("field")
    if not field:
        offset = flt.get("sourceColumnOffset")
        field = f"col{offset}" if offset is not None else "col?"
    visible = flt.get("visibleValues")
    if visible:
        return f"{field}[{','.join(str(v) for v in visible)}]"
    return str(field)
