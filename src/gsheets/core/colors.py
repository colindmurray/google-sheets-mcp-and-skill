"""Color helpers: hex <-> Google ``ColorStyle`` (DESIGN §5.3).

Writes always use ``ColorStyle`` (``rgbColor``/``themeColor``), never the deprecated flat
``Color``. Reads flatten to an uppercase ``#RRGGBB`` hex string (or ``theme:NAME``). Channel
rounding is ``round(channel * 255)``.
"""

from __future__ import annotations


def hex_to_color_style(hex_or_theme: str) -> dict:
    """Convert a hex string or ``theme:NAME`` token to a Google ``ColorStyle`` dict.

    Examples:
        ``"#FFCDD2"`` -> ``{"rgbColor": {"red": 1.0, "green": 0.804, "blue": 0.804}}``
        ``"theme:ACCENT1"`` -> ``{"themeColor": "ACCENT1"}``

    Args:
        hex_or_theme: ``"#RRGGBB"`` (case-insensitive) or ``"theme:<NAME>"``.

    Returns:
        A ``ColorStyle`` dict suitable for ``*ColorStyle`` write fields.
    """
    raise NotImplementedError


def color_style_to_hex(color_style: dict) -> str:
    """Convert a Google ``ColorStyle`` dict to an uppercase hex or ``theme:NAME`` string.

    Inverse of :func:`hex_to_color_style`. ``rgbColor`` -> ``"#RRGGBB"`` (uppercase);
    ``themeColor`` -> ``"theme:NAME"``.

    Args:
        color_style: A Google ``ColorStyle`` dict.

    Returns:
        ``"#RRGGBB"`` (uppercase) or ``"theme:NAME"``.
    """
    raise NotImplementedError
