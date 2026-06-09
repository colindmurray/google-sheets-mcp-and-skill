"""Flatten Google's nested ``CellFormat`` to the compact flat shape (DESIGN §3.1/§3.2).

:func:`flatten_cell_format` maps a Google ``CellFormat`` (``userEnteredFormat`` OR
``effectiveFormat``) to the flat ``CellFormat`` dict: colors -> hex, ``textFormat`` children
lifted to top level, ``numberFormat`` -> ``numberFormatType``/``numberFormat``, borders ->
``"<style> <hex>"`` per side, ``padding``/``textRotation`` preserved. Unset keys are omitted
for token efficiency. ``effectiveFormat`` flattens identically (read-only; never written back).
"""

from __future__ import annotations


def flatten_cell_format(google_format_dict: dict) -> dict:
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

    Unset keys are omitted.

    Args:
        google_format_dict: A Google ``CellFormat`` dict (user-entered or effective).

    Returns:
        A flat ``CellFormat`` dict (see DESIGN §3.1).
    """
    raise NotImplementedError
