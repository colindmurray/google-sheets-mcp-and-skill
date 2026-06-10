"""Banded-range (``bandedRanges``) read serialization + add/update/delete write builders.

Covers DESIGN §X.0e and §X.3/§X.9. A ``BandedRange`` paints a rectangle with
alternating row and/or column band colors (a "this is a deliberate table" hint). The v4 REST
API exposes them as a per-sheet ``bandedRanges`` array and mutates them via the
``addBanding`` / ``updateBanding`` / ``deleteBanding`` ``batchUpdate`` requests. This module
owns:

- :func:`serialize_banding` — flatten a Google ``BandedRange`` (``bandedRangeId`` / ``range``
  already resolved to A1 by the caller / ``rowProperties`` / ``columnProperties``) into the
  terse, flattened, round-trippable read shape (condformat line style). Each band color
  (``headerColorStyle`` / ``firstBandColorStyle`` / ``secondBandColorStyle`` /
  ``footerColorStyle``) is flattened to a ``#RRGGBB`` hex / ``theme:NAME`` string via
  ``colors.color_style_to_hex``, surfaced under the keys ``header`` / ``first`` / ``second`` /
  ``footer``.
- :func:`build_add_banding_request` / :func:`build_update_banding_request` /
  :func:`build_delete_banding_request` — return ready-to-send ``batchUpdate`` request dicts
  (``addBanding`` / ``updateBanding`` / ``deleteBanding``). ``update`` auto-builds its
  ``fields`` mask from the payload via :func:`gsheets.core.fieldsmask.build_fields_mask`. These
  are consumed by ``core/structure.py``'s new ``add_banding`` / ``update_banding`` /
  ``delete_banding`` actions (which own the action->handler dispatch); the ``addBanding``
  reply's ``bandedRangeId`` is captured by ``structure.capture_new_ids`` (its
  ``_REPLY_ID_SPECS`` is extended there).

Boundary (DESIGN §1, §5.2): PURE core. Imports only stdlib + sibling core modules. Must NEVER
import ``fastmcp``, ``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models``.

Range handling mirrors the filters/condformat boundary: :func:`serialize_banding` takes an
*already-resolved A1 range string* (the owning read fn — ``structure._structure_read`` —
resolves the Google ``BandedRange.range`` ``GridRange`` -> A1 first, mirroring
``_serialize_sheet_structure``); the ``build_*`` request builders resolve the caller's A1
``range`` -> ``GridRange`` via ``addressing.a1_to_gridrange`` before emitting the request.
"""

from __future__ import annotations

from .addressing import a1_to_gridrange
from .colors import color_style_to_hex, hex_to_color_style
from .errors import SheetsError
from .fieldsmask import build_fields_mask
from .service import SheetsServices

#: The public band-color slot keys, in canonical order, mapped to the Google
#: ``BandingProperties`` ``*ColorStyle`` field they read from / write to. ``header``/``footer``
#: paint the first/last band; ``first``/``second`` are the alternating body bands. Legacy flat
#: ``*Color`` fields (without the ``Style`` suffix) are accepted on read as a fallback.
_BAND_SLOTS: tuple[tuple[str, str, str], ...] = (
    ("header", "headerColorStyle", "headerColor"),
    ("first", "firstBandColorStyle", "firstBandColor"),
    ("second", "secondBandColorStyle", "secondBandColor"),
    ("footer", "footerColorStyle", "footerColor"),
)

#: The two ``BandedRange`` property groups, mapped public output key -> Google field name.
_BANDING_GROUPS: tuple[tuple[str, str], ...] = (
    ("rowBanding", "rowProperties"),
    ("columnBanding", "columnProperties"),
)


# ===========================================================================
# serialize: Google BandedRange -> flattened read shape + terse line
# ===========================================================================


def serialize_banding(br: dict, range_a1: str | None = None) -> dict:
    """Flatten a Google ``BandedRange`` into the terse, round-trippable read shape (§X.0e).

    Produces::

        { "bandedRangeId": 7, "range": "Sheet1!A1:F500",
          "rowBanding": {"header": "#4285F4", "first": "#FFFFFF", "second": "#E8F0FE",
                         "footer": None},
          "columnBanding": None,
          "line": "banding 7 [Sheet1!A1:F500] rows: hdr #4285F4 / #FFFFFF / #E8F0FE" }

    Each band color (``headerColorStyle`` / ``firstBandColorStyle`` / ``secondBandColorStyle`` /
    ``footerColorStyle``, with the legacy flat ``*Color`` form as a fallback) is flattened to a
    ``#RRGGBB`` hex / ``theme:NAME`` string via ``colors.color_style_to_hex``, surfaced under the
    keys ``header`` / ``first`` / ``second`` / ``footer``. A slot Google does not set is surfaced
    as ``None`` (so the round-trippable shape names every slot explicitly). A property group
    (``rowBanding`` / ``columnBanding``) that Google does not set is surfaced as ``None``. The
    ``range`` key is omitted when ``range_a1`` is not supplied; ``line`` is always present.

    Args:
        br: A Google ``BandedRange`` dict (``bandedRangeId`` / ``range`` / ``rowProperties`` /
            ``columnProperties``). Any ``range`` left on ``br`` is ignored in favor of the
            pre-resolved ``range_a1``.
        range_a1: The banded range's range as a sheet-qualified A1 string (resolved by the
            caller via ``gridrange_to_a1``).

    Returns:
        The flattened banding dict described above.
    """
    if not isinstance(br, dict):
        raise SheetsError(
            "bad_banding", f"bandedRange must be a dict, got {type(br).__name__}"
        )

    out: dict = {"bandedRangeId": br.get("bandedRangeId")}
    if range_a1 is not None:
        out["range"] = range_a1

    for public_key, google_field in _BANDING_GROUPS:
        props = br.get(google_field)
        out[public_key] = _serialize_band_properties(props)

    out["line"] = _serialize_banding_line(
        br.get("bandedRangeId"), range_a1, out.get("rowBanding"), out.get("columnBanding")
    )
    return out


def _serialize_band_properties(props: object) -> dict | None:
    """Flatten one ``BandingProperties`` group to ``{header, first, second, footer}`` hexes.

    Returns ``None`` when the group is absent (Google omits ``rowProperties`` /
    ``columnProperties`` entirely when that axis has no banding), so the read shape distinguishes
    "no row banding" (``None``) from "row banding with some unset slots" (a dict whose unset
    slots are ``None``). Every slot key is always present in the returned dict (value ``None``
    when that band color is unset) so the structure is stable and round-trippable.
    """
    if props is None:
        return None
    if not isinstance(props, dict):
        raise SheetsError(
            "bad_banding",
            f"banding properties must be a dict, got {type(props).__name__}",
        )
    band: dict = {}
    for slot_key, style_field, legacy_field in _BAND_SLOTS:
        band[slot_key] = _band_color_hex(props, style_field, legacy_field)
    return band


def _band_color_hex(props: dict, style_field: str, legacy_field: str) -> str | None:
    """Flatten a single band ``*ColorStyle`` (or legacy ``*Color``) to a hex / theme string.

    Prefers the ``*ColorStyle`` form; falls back to the legacy flat ``*Color`` dict. Returns
    ``None`` when neither is set. A malformed color degrades to ``None`` (rather than raising) so
    one bad band never breaks an otherwise-valid read.
    """
    style = props.get(style_field)
    if style is None:
        style = props.get(legacy_field)
    if not isinstance(style, dict):
        return None
    try:
        return color_style_to_hex(style)
    except ValueError:
        return None


def _serialize_banding_line(
    banded_range_id: object,
    range_a1: str | None,
    row_band: dict | None,
    column_band: dict | None,
) -> str:
    """Build the terse condformat-style line for a banded range.

    Form: ``banding 7 [Sheet1!A1:F500] rows: hdr #4285F4 / #FFFFFF / #E8F0FE``. Each present
    property group appends a segment (``rows: ...`` / ``cols: ...``); within a segment the
    header (``hdr``) and footer (``ftr``) bands are labelled and the two body bands are the bare
    ``first / second`` hexes. Absent slots within a present group are dropped from the segment so
    a body-only banding reads as ``rows: #FFFFFF / #E8F0FE``.
    """
    id_part = "?" if banded_range_id is None else str(banded_range_id)
    range_part = f"[{range_a1}]" if range_a1 is not None else "[]"
    segments: list[str] = []
    for label, band in (("rows", row_band), ("cols", column_band)):
        if band is None:
            continue
        seg = _band_segment(band)
        segments.append(f"{label}: {seg}" if seg else f"{label}:")
    head = f"banding {id_part} {range_part}"
    if segments:
        return f"{head} {' | '.join(segments)}"
    return head


def _band_segment(band: dict) -> str:
    """Render one property group's colors: ``hdr #X / #first / #second / ftr #Y``.

    Header/footer carry the ``hdr``/``ftr`` labels; the alternating body bands are bare hexes.
    Unset (``None``) slots are dropped so the segment only lists colors that are actually set.
    """
    parts: list[str] = []
    header = band.get("header")
    if header is not None:
        parts.append(f"hdr {header}")
    first = band.get("first")
    if first is not None:
        parts.append(first)
    second = band.get("second")
    if second is not None:
        parts.append(second)
    footer = band.get("footer")
    if footer is not None:
        parts.append(f"ftr {footer}")
    return " / ".join(parts)


# ===========================================================================
# build: A1 + structured band colors -> Google batchUpdate request dicts
# ===========================================================================


def build_add_banding_request(
    services: SheetsServices,
    spreadsheet_id: str,
    range: str,
    params: dict,
) -> dict:
    """Build an ``addBanding`` ``batchUpdate`` request (DESIGN §X.9 add_banding).

    ``params`` is ``{"rowBanding"?: {header,first,second,footer hexes},
    "columnBanding"?: {...}}`` over the A1 ``range``. Each public group maps to a Google
    ``BandingProperties`` (``rowProperties`` / ``columnProperties``) whose band colors are
    written as ``*ColorStyle`` via ``colors.hex_to_color_style``. At least one of the two groups
    must be present (Google requires at least one of ``rowProperties`` / ``columnProperties``).
    The caller (``structure.add_banding``) captures the new ``bandedRangeId`` from the reply.

    Args:
        services: The authed handle (resolves the A1 ``range`` -> ``GridRange``).
        spreadsheet_id: Target spreadsheet id.
        range: A1 range the banding spans.
        params: ``{"rowBanding"?, "columnBanding"?}`` (see above).

    Returns:
        ``{"addBanding": {"bandedRange": {...}}}`` — ready for ``spreadsheets.batchUpdate``.
    """
    grid_range = a1_to_gridrange(services, spreadsheet_id, range)
    banded_range: dict = {"range": grid_range}

    groups = _build_banding_groups(params)
    if not groups:
        raise SheetsError(
            "missing_param",
            "add_banding requires at least one of rowBanding / columnBanding",
        )
    banded_range.update(groups)
    return {"addBanding": {"bandedRange": banded_range}}


def build_update_banding_request(
    services: SheetsServices,
    spreadsheet_id: str,
    params: dict,
) -> dict:
    """Build an ``updateBanding`` ``batchUpdate`` request with an AUTO fields mask (§X.9).

    ``params`` is ``{"bandedRangeId": int, "rowBanding"?, "columnBanding"?, "range"?}``. Only the
    supplied fields are masked (via :func:`gsheets.core.fieldsmask.build_fields_mask`) so
    unspecified banding properties are never wiped. ``range`` is masked atomically (the whole
    ``GridRange`` is replaced as one field), and each band ``*ColorStyle`` masks atomically too
    (the ``*ColorStyle`` family is an atomic leaf), so a partial color update masks down to e.g.
    ``rowProperties.firstBandColorStyle`` without wiping the other bands. An empty payload
    (``bandedRangeId`` only) raises ``empty_payload`` — refuse a no-op.

    Args:
        services: The authed handle (resolves ``range`` -> ``GridRange`` when present).
        spreadsheet_id: Target spreadsheet id.
        params: ``{"bandedRangeId", "rowBanding"?, "columnBanding"?, "range"?}``.

    Returns:
        ``{"updateBanding": {"bandedRange": {...}, "fields": "<mask>"}}``.
    """
    banded_range_id = params.get("bandedRangeId")
    if banded_range_id is None:
        raise SheetsError(
            "missing_param", "update_banding requires params={'bandedRangeId': <int>}"
        )

    banded_range: dict = {"bandedRangeId": banded_range_id}
    # The masked payload mirrors ``bandedRange`` but EXCLUDES the immutable ``bandedRangeId`` so
    # the auto mask covers only the fields actually being changed.
    masked: dict = {}

    if params.get("range") is not None:
        grid_range = a1_to_gridrange(services, spreadsheet_id, params["range"])
        banded_range["range"] = grid_range
        # ``range`` is one logical field on ``updateBanding`` — the whole GridRange is replaced
        # atomically. Mask it via a scalar sentinel so build_fields_mask treats ``range`` as a
        # leaf rather than recursing the GridRange dict (which Google would reject).
        masked["range"] = True

    for public_key, google_field in _BANDING_GROUPS:
        if public_key in params and params[public_key] is not None:
            props = _build_band_properties(public_key, params[public_key])
            banded_range[google_field] = props
            masked[google_field] = props

    fields = build_fields_mask(masked)  # raises empty_payload when nothing to change
    return {"updateBanding": {"bandedRange": banded_range, "fields": fields}}


def build_delete_banding_request(params: dict) -> dict:
    """Build a ``deleteBanding`` ``batchUpdate`` request (DESIGN §X.9 delete_banding).

    ``params`` is ``{"bandedRangeId": int}`` — addresses the banding by id (no range needed).

    Args:
        params: ``{"bandedRangeId": <int>}``.

    Returns:
        ``{"deleteBanding": {"bandedRangeId": <int>}}``.
    """
    banded_range_id = params.get("bandedRangeId")
    if banded_range_id is None:
        raise SheetsError(
            "missing_param", "delete_banding requires params={'bandedRangeId': <int>}"
        )
    return {"deleteBanding": {"bandedRangeId": banded_range_id}}


# --- band-property construction (write side) -----------------------------------------


def _build_banding_groups(params: dict) -> dict:
    """Build the present ``rowProperties`` / ``columnProperties`` from public ``*Banding`` keys.

    Returns a dict containing only the groups present (and non-``None``) in ``params``, each a
    Google ``BandingProperties`` with ``*ColorStyle`` band colors.
    """
    if not isinstance(params, dict):
        raise SheetsError(
            "bad_banding", f"params must be a dict, got {type(params).__name__}"
        )
    out: dict = {}
    for public_key, google_field in _BANDING_GROUPS:
        value = params.get(public_key)
        if value is not None:
            out[google_field] = _build_band_properties(public_key, value)
    return out


def _build_band_properties(public_key: str, band: object) -> dict:
    """Build a Google ``BandingProperties`` from a public ``{header,first,second,footer}`` dict.

    Each supplied slot is a hex / ``theme:NAME`` string converted to a ``*ColorStyle`` via
    ``colors.hex_to_color_style``. Unknown slot keys raise ``unknown_param`` so the typed surface
    stays strict (the band dict is NOT a raw escape hatch). An empty band dict raises
    ``empty_payload`` (refuse a no-op group).
    """
    if not isinstance(band, dict):
        raise SheetsError(
            "bad_banding",
            f"{public_key} must be a dict of band colors, got {type(band).__name__}",
        )
    allowed = {slot_key for slot_key, _style, _legacy in _BAND_SLOTS}
    unknown = set(band) - allowed
    if unknown:
        raise SheetsError(
            "unknown_param",
            f"unknown {public_key} keys: {sorted(unknown)}; allowed: {sorted(allowed)}",
        )
    props: dict = {}
    for slot_key, style_field, _legacy_field in _BAND_SLOTS:
        color = band.get(slot_key)
        if color is None:
            continue
        try:
            props[style_field] = hex_to_color_style(color)
        except ValueError as exc:
            raise SheetsError("bad_color", f"{public_key}.{slot_key}: {exc}") from exc
    if not props:
        raise SheetsError(
            "empty_payload",
            f"{public_key} has no band colors set (give at least one of "
            "header/first/second/footer)",
        )
    return props
