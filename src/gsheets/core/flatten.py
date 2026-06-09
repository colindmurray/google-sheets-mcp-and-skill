"""Flatten Google's nested ``CellFormat`` to the compact flat shape (DESIGN §3.1/§3.2).

:func:`flatten_cell_format` maps a Google ``CellFormat`` (``userEnteredFormat`` OR
``effectiveFormat``) to the flat ``CellFormat`` dict: colors -> hex, ``textFormat`` children
lifted to top level, ``numberFormat`` -> ``numberFormatType``/``numberFormat``, borders ->
``"<style> <hex>"`` per side, ``padding``/``textRotation`` preserved. Unset keys are omitted
for token efficiency. ``effectiveFormat`` flattens identically (read-only; never written back).

PURE leaf module. Imports only stdlib + the sibling ``colors`` leaf (its sole dependency per
the manifest). It must never import ``fastmcp``/``mcp``/``argparse`` or ``gsheets.models``.
"""

from __future__ import annotations

from . import colors

# ``textFormat`` simple (scalar) children that lift verbatim to the top level. ``fontSize`` and
# ``fontFamily`` ride along here too; ``foregroundColorStyle`` is handled separately (it becomes
# ``fg`` via the color helper).
_TEXT_FORMAT_KEYS = (
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "fontSize",
    "fontFamily",
)

# The four borders Google carries on ``CellFormat.borders``, emitted in this canonical order.
_BORDER_SIDES = ("top", "bottom", "left", "right")


def flatten_cell_format(google_format_dict: dict | None) -> dict:
    """Flatten a Google ``CellFormat`` to the compact flat ``CellFormat`` shape.

    Mappings:
        - ``backgroundColorStyle`` -> ``bg`` (hex via :func:`colors.color_style_to_hex`)
        - ``textFormat.foregroundColorStyle`` -> ``fg`` (hex)
        - ``textFormat.{bold,italic,underline,strikethrough,fontSize,fontFamily}`` -> top level
        - ``numberFormat.{type,pattern}`` -> ``numberFormatType`` / ``numberFormat``
        - ``horizontalAlignment``/``verticalAlignment`` -> ``halign`` / ``valign``
        - ``wrapStrategy`` -> ``wrap``
        - ``borders.<side>`` -> ``borders.<side>`` as ``"<style> <hex>"``
        - ``padding`` preserved as ``{top,right,bottom,left}``
        - ``textRotation`` preserved as ``{angle}`` or ``{vertical}``

    Unset keys are omitted (token efficiency). ``effectiveFormat`` flattens identically.

    Args:
        google_format_dict: A Google ``CellFormat`` dict (user-entered or effective). ``None``
            or an empty dict yields an empty flat dict.

    Returns:
        A flat ``CellFormat`` dict (see DESIGN §3.1).
    """
    if not google_format_dict:
        return {}

    fmt = google_format_dict
    out: dict = {}

    # --- background color -> bg ---------------------------------------------------------------
    bg = _flatten_color(fmt.get("backgroundColorStyle"))
    if bg is None:
        # Legacy ``backgroundColor`` (flat Color) fallback when ColorStyle is absent.
        bg = _flatten_color(fmt.get("backgroundColor"))
    if bg is not None:
        out["bg"] = bg

    # --- textFormat (foreground color + lifted scalar styles) ---------------------------------
    text_format = fmt.get("textFormat")
    if isinstance(text_format, dict):
        fg = _flatten_color(text_format.get("foregroundColorStyle"))
        if fg is None:
            fg = _flatten_color(text_format.get("foregroundColor"))
        if fg is not None:
            out["fg"] = fg
        for key in _TEXT_FORMAT_KEYS:
            if key in text_format and text_format[key] is not None:
                out[key] = text_format[key]

    # --- numberFormat -> numberFormatType + numberFormat --------------------------------------
    number_format = fmt.get("numberFormat")
    if isinstance(number_format, dict):
        nf_type = number_format.get("type")
        if nf_type is not None:
            out["numberFormatType"] = nf_type
        pattern = number_format.get("pattern")
        if pattern is not None:
            out["numberFormat"] = pattern

    # --- alignment / wrap ---------------------------------------------------------------------
    halign = fmt.get("horizontalAlignment")
    if halign is not None:
        out["halign"] = halign
    valign = fmt.get("verticalAlignment")
    if valign is not None:
        out["valign"] = valign
    wrap = fmt.get("wrapStrategy")
    if wrap is not None:
        out["wrap"] = wrap

    # --- borders -> "<style> <hex>" per side --------------------------------------------------
    borders = fmt.get("borders")
    if isinstance(borders, dict):
        flat_borders: dict = {}
        for side in _BORDER_SIDES:
            line = _flatten_border(borders.get(side))
            if line is not None:
                flat_borders[side] = line
        if flat_borders:
            out["borders"] = flat_borders

    # --- padding / textRotation (preserved verbatim) ------------------------------------------
    padding = fmt.get("padding")
    if isinstance(padding, dict) and padding:
        out["padding"] = dict(padding)
    text_rotation = fmt.get("textRotation")
    if isinstance(text_rotation, dict) and text_rotation:
        out["textRotation"] = dict(text_rotation)

    return out


def _flatten_color(color_style: object) -> str | None:
    """Flatten a Google ``ColorStyle``/``Color`` dict to a hex/theme string, else ``None``.

    Returns ``None`` for a missing/empty/unflattenable color so the caller omits the key (token
    efficiency) rather than emitting a sentinel. A ``ColorStyle`` carrying only the unspecified
    sentinel (no ``rgbColor``/``themeColor``/channels) is treated as unset.
    """
    if not isinstance(color_style, dict) or not color_style:
        return None
    try:
        return colors.color_style_to_hex(color_style)
    except ValueError:
        # Unrecognizable color payload (e.g. ``{"themeColorType": "UNSPECIFIED"}``) -> omit.
        return None


def _flatten_border(border: object) -> str | None:
    """Flatten one Google ``Border`` to ``"<style> <hex>"``, else ``None``.

    A ``Border`` whose ``style`` is ``NONE`` (or absent) carries no visible edge; we omit it so
    "no border" never shows up as a token-wasting line. The color falls back to black
    (``#000000``) when Google omits it (the API default for a styled border).
    """
    if not isinstance(border, dict) or not border:
        return None
    style = border.get("style")
    if not style or style == "NONE":
        return None
    hex_color = _flatten_color(border.get("colorStyle"))
    if hex_color is None:
        hex_color = _flatten_color(border.get("color"))
    if hex_color is None:
        hex_color = "#000000"
    return f"{style} {hex_color}"
