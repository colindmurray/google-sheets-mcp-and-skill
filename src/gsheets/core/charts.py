"""Embedded chart create/update/delete/read (DESIGN §3.3 ``charts``).

v1 scope (LOCKED): ``read`` returns chart METADATA only (``chartId``/``title``/``type``/
``anchor``), NOT the full ``EmbeddedChartSpec``; ``create``/``update`` take the locked minimal
flat ``spec``. Full chart-spec round-trip is the sole deliberate exception to CRUD symmetry —
callers needing it use ``batch``.

PURE core module: imports only stdlib + ``googleapiclient`` + sibling core modules. It must
NEVER import ``fastmcp``, ``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models``
(DESIGN §1 boundary).
"""

from __future__ import annotations

from googleapiclient.errors import HttpError

from .addressing import a1_to_gridrange
from .errors import SheetsError, classify_google_error
from .service import SheetsServices
from .structure import capture_new_ids

# Valid actions for the public ``charts`` surface.
_ACTIONS = frozenset({"create", "update", "delete", "read"})

# Flat ``type`` token -> Google chart family. PIE is its own ``pieChart`` block; every other
# supported type is a ``basicChart`` ``chartType`` enum value verbatim (DESIGN §3.3 ``charts``
# LOCKED spec). The set is deliberately small (the rich union is out of v1; use ``batch``).
_BASIC_CHART_TYPES = frozenset({"LINE", "COLUMN", "BAR", "SCATTER", "AREA"})
_PIE_CHART_TYPE = "PIE"
_ALL_TYPES = _BASIC_CHART_TYPES | {_PIE_CHART_TYPE}

# The ONLY keys the locked minimal flat ``spec`` accepts (DESIGN §3.3). Anything else raises
# ``unknown_param`` so the small façade never silently swallows a richer key the caller
# expected to take effect.
_SPEC_KEYS = frozenset({"title", "type", "series", "domain", "anchor"})

# The ONLY keys the anchor sub-dict accepts.
_ANCHOR_KEYS = frozenset({"sheet", "row", "col"})


def charts(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    action: str,
    sheet: str | None = None,
    chart_id: int | None = None,
    spec: dict | None = None,
) -> dict:
    """Create/update/delete/read embedded charts (DESIGN §3.3, v1 scope).

    ``spec`` keys for ``create``/``update`` (unknown key -> ``SheetsError("unknown_param")``):
    ``{"title", "type", "series", "domain", "anchor"}`` where ``type`` is one of
    ``LINE``/``COLUMN``/``BAR``/``PIE``/``SCATTER``/``AREA``. ``create`` captures the new
    ``chartId`` from ``replies[]``. ``read`` lists charts (metadata only).

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        action: ``"create"`` | ``"update"`` | ``"delete"`` | ``"read"``.
        sheet: Target tab name (for read / anchor resolution).
        chart_id: Existing chart id for update/delete.
        spec: Minimal flat chart spec for create/update.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "action": ..., "chartId": ...}`` (or
        ``"charts": [...]`` for read).
    """
    if action not in _ACTIONS:
        raise SheetsError(
            "unknown_action",
            f"unknown charts action {action!r}; expected one of "
            "'create', 'update', 'delete', 'read'",
        )

    if action == "read":
        return _read(services, spreadsheet_id, sheet)
    if action == "create":
        return _create(services, spreadsheet_id, spec)
    if action == "update":
        return _update(services, spreadsheet_id, chart_id, spec)
    return _delete(services, spreadsheet_id, chart_id)


# --------------------------------------------------------------------------------------
# create / update / delete
# --------------------------------------------------------------------------------------


def _create(services: SheetsServices, spreadsheet_id: str, spec: dict | None) -> dict:
    """Add an embedded chart from the locked minimal flat spec; capture the new chartId."""
    chart_spec, anchor = _build_chart_spec(services, spreadsheet_id, spec)
    request = {
        "addChart": {
            "chart": {
                "spec": chart_spec,
                "position": {"overlayPosition": {"anchorCell": anchor}},
            }
        }
    }
    resp = _batch_update(services, spreadsheet_id, [request])

    chart_ids = capture_new_ids(resp.get("replies", []) or []).get("chartIds") or []
    chart_id = chart_ids[0] if chart_ids else None
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "create",
        "chartId": chart_id,
    }


def _update(
    services: SheetsServices, spreadsheet_id: str, chart_id: int | None, spec: dict | None
) -> dict:
    """Replace an existing chart's spec via ``updateChartSpec`` (DESIGN §3.3)."""
    if chart_id is None:
        raise SheetsError("missing_chart_id", "update requires chart_id")
    chart_spec, _anchor = _build_chart_spec(
        services, spreadsheet_id, spec, require_anchor=False
    )
    request = {"updateChartSpec": {"chartId": chart_id, "spec": chart_spec}}
    _batch_update(services, spreadsheet_id, [request])
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "update",
        "chartId": chart_id,
    }


def _delete(
    services: SheetsServices, spreadsheet_id: str, chart_id: int | None
) -> dict:
    """Delete an embedded chart via ``deleteEmbeddedObject`` (DESIGN §3.3)."""
    if chart_id is None:
        raise SheetsError("missing_chart_id", "delete requires chart_id")
    request = {"deleteEmbeddedObject": {"objectId": chart_id}}
    _batch_update(services, spreadsheet_id, [request])
    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "delete",
        "chartId": chart_id,
    }


# --------------------------------------------------------------------------------------
# read (metadata only — v1 scope)
# --------------------------------------------------------------------------------------


def _read(
    services: SheetsServices, spreadsheet_id: str, sheet: str | None
) -> dict:
    """List embedded charts as METADATA ONLY (chartId/title/type/anchor) (DESIGN §3.3 v1).

    Reads a tight mask covering only the chart fields the v1 metadata view surfaces — never
    the full ``EmbeddedChartSpec`` (token efficiency, invariant #3). Optionally filtered to a
    single tab by ``sheet``.
    """
    fields = (
        "sheets(properties(sheetId,title),"
        "charts(chartId,spec.title,spec.basicChart.chartType,spec.pieChart,position))"
    )
    try:
        resp = (
            services.sheets.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields=fields)
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(
            exc, account_email=services.account_email
        ) from exc

    # sheetId -> title, so a chart's anchor (addressed by sheetId) maps back to a name.
    id_to_title: dict[object, str] = {}
    for entry in resp.get("sheets", []) or []:
        props = (entry or {}).get("properties", {}) or {}
        if props.get("sheetId") is not None:
            id_to_title[props.get("sheetId")] = props.get("title")

    out_charts: list[dict] = []
    for entry in resp.get("sheets", []) or []:
        props = (entry or {}).get("properties", {}) or {}
        sheet_title = props.get("title")
        if sheet is not None and sheet_title != sheet:
            continue
        for chart in (entry or {}).get("charts", []) or []:
            out_charts.append(_chart_metadata(chart, id_to_title))

    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": "read",
        "charts": out_charts,
    }


def _chart_metadata(chart: dict, id_to_title: dict[object, str]) -> dict:
    """Flatten one Google embedded-chart object to the v1 metadata view."""
    spec = (chart or {}).get("spec", {}) or {}
    meta: dict = {"chartId": chart.get("chartId")}

    title = spec.get("title")
    meta["title"] = title if title else None

    meta["type"] = _read_chart_type(spec)
    meta["anchor"] = _read_anchor(chart.get("position"), id_to_title)
    return meta


def _read_chart_type(spec: dict) -> str | None:
    """Derive the flat ``type`` token from a Google chart spec (basicChart or pieChart)."""
    basic = spec.get("basicChart")
    if isinstance(basic, dict):
        ctype = basic.get("chartType")
        return ctype if ctype else None
    if "pieChart" in spec:
        return _PIE_CHART_TYPE
    return None


def _read_anchor(
    position: dict | None, id_to_title: dict[object, str]
) -> dict | None:
    """Map a chart ``position.overlayPosition.anchorCell`` to ``{sheet,row,col}``."""
    if not isinstance(position, dict):
        return None
    overlay = position.get("overlayPosition") or {}
    cell = overlay.get("anchorCell") or {}
    if not cell:
        return None
    sheet_id = cell.get("sheetId")
    return {
        "sheet": id_to_title.get(sheet_id),
        "row": cell.get("rowIndex", 0),
        "col": cell.get("columnIndex", 0),
    }


# --------------------------------------------------------------------------------------
# spec construction (flat façade -> Google EmbeddedChartSpec)
# --------------------------------------------------------------------------------------


def _build_chart_spec(
    services: SheetsServices,
    spreadsheet_id: str,
    spec: dict | None,
    *,
    require_anchor: bool = True,
) -> tuple[dict, dict | None]:
    """Translate the locked flat ``spec`` to a Google ``EmbeddedChartSpec`` (+ anchor cell).

    Returns ``(chart_spec, anchor_cell_or_None)``. The anchor cell is a ``GridCoordinate``
    (``{sheetId,rowIndex,columnIndex}``) resolved from the flat ``{sheet,row,col}`` anchor; it
    is only required for ``create`` (``update`` keeps the chart's existing position).
    """
    if not isinstance(spec, dict) or not spec:
        raise SheetsError("empty_payload", "charts create/update requires a spec")

    unknown = set(spec) - _SPEC_KEYS
    if unknown:
        raise SheetsError(
            "unknown_param",
            f"unknown spec key(s): {', '.join(sorted(unknown))}; "
            f"allowed: {', '.join(sorted(_SPEC_KEYS))}",
        )

    ctype = spec.get("type")
    if ctype is None:
        raise SheetsError("missing_param", "spec requires a 'type'")
    if ctype not in _ALL_TYPES:
        raise SheetsError(
            "bad_chart_type",
            f"unknown chart type {ctype!r}; expected one of "
            f"{', '.join(sorted(_ALL_TYPES))}",
        )

    series = spec.get("series")
    if not series:
        raise SheetsError("missing_param", "spec requires at least one 'series' range")
    if not isinstance(series, (list, tuple)):
        raise SheetsError("bad_series", "'series' must be a list of A1 ranges")

    domain = spec.get("domain")

    chart_spec: dict = {}
    title = spec.get("title")
    if title:
        chart_spec["title"] = title

    if ctype == _PIE_CHART_TYPE:
        chart_spec["pieChart"] = _build_pie_chart(
            services, spreadsheet_id, series, domain
        )
    else:
        chart_spec["basicChart"] = _build_basic_chart(
            services, spreadsheet_id, ctype, series, domain
        )

    anchor_cell = _resolve_anchor(services, spreadsheet_id, spec.get("anchor"))
    if require_anchor and anchor_cell is None:
        raise SheetsError(
            "missing_param",
            "spec requires an 'anchor' ({'sheet','row','col'}) for create",
        )

    return chart_spec, anchor_cell


def _build_basic_chart(
    services: SheetsServices,
    spreadsheet_id: str,
    ctype: str,
    series: list,
    domain: str | None,
) -> dict:
    """Build a Google ``BasicChartSpec`` from the flat series/domain A1 ranges."""
    basic: dict = {
        "chartType": ctype,
        "series": [
            {"series": _source_range(services, spreadsheet_id, s)} for s in series
        ],
    }
    if domain:
        basic["domains"] = [
            {"domain": _source_range(services, spreadsheet_id, domain)}
        ]
    return basic


def _build_pie_chart(
    services: SheetsServices,
    spreadsheet_id: str,
    series: list,
    domain: str | None,
) -> dict:
    """Build a Google ``PieChartSpec`` (single series + domain) from flat A1 ranges."""
    pie: dict = {
        "series": _source_range(services, spreadsheet_id, series[0]),
    }
    if domain:
        pie["domain"] = _source_range(services, spreadsheet_id, domain)
    return pie


def _source_range(
    services: SheetsServices, spreadsheet_id: str, a1: str
) -> dict:
    """Wrap an A1 range as a Google ``ChartSourceRange`` (one GridRange source)."""
    grid = a1_to_gridrange(services, spreadsheet_id, a1)
    return {"sourceRange": {"sources": [grid]}}


def _resolve_anchor(
    services: SheetsServices, spreadsheet_id: str, anchor: dict | None
) -> dict | None:
    """Resolve the flat ``{sheet,row,col}`` anchor to a Google ``GridCoordinate``.

    ``sheet`` (name) resolves to a ``sheetId`` via the addressing layer (callers never pass a
    ``sheetId``); ``row``/``col`` are 0-based and pass through as ``rowIndex``/``columnIndex``.
    Returns ``None`` when no anchor is supplied (the ``update`` path keeps the chart's existing
    position).
    """
    if anchor is None:
        return None
    if not isinstance(anchor, dict):
        raise SheetsError(
            "bad_anchor", "anchor must be a dict {'sheet','row','col'}"
        )

    unknown = set(anchor) - _ANCHOR_KEYS
    if unknown:
        raise SheetsError(
            "unknown_param",
            f"unknown anchor key(s): {', '.join(sorted(unknown))}; "
            f"allowed: {', '.join(sorted(_ANCHOR_KEYS))}",
        )

    sheet_name = anchor.get("sheet")
    if not sheet_name:
        raise SheetsError("missing_param", "anchor requires a 'sheet' name")

    sheet_id = _resolve_sheet_id(services, spreadsheet_id, sheet_name)
    return {
        "sheetId": sheet_id,
        "rowIndex": anchor.get("row", 0),
        "columnIndex": anchor.get("col", 0),
    }


def _resolve_sheet_id(
    services: SheetsServices, spreadsheet_id: str, sheet_name: str
) -> int:
    """Resolve a sheet NAME to its ``sheetId`` by reusing the addressing layer.

    Builds an always-quoted ``'<sheet>'!A1`` reference (doubling any embedded ``'``) so any
    sheet title — including ones with spaces or punctuation — resolves through
    :func:`a1_to_gridrange` without the caller ever handling a ``sheetId`` (DESIGN §5.2).
    """
    quoted = sheet_name.replace("'", "''")
    grid = a1_to_gridrange(services, spreadsheet_id, f"'{quoted}'!A1")
    return grid["sheetId"]


# --------------------------------------------------------------------------------------
# shared batchUpdate wrapper
# --------------------------------------------------------------------------------------


def _batch_update(
    services: SheetsServices, spreadsheet_id: str, requests: list[dict]
) -> dict:
    """Issue one ``spreadsheets.batchUpdate`` and classify any ``HttpError`` (DESIGN §6)."""
    try:
        return (
            services.sheets.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests},
            )
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(
            exc, account_email=services.account_email
        ) from exc
