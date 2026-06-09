"""Color helpers: hex <-> Google ``ColorStyle`` (DESIGN §5.3).

Writes always use ``ColorStyle`` (``rgbColor``/``themeColor``), never the deprecated flat
``Color``. Reads flatten to an uppercase ``#RRGGBB`` hex string (or ``theme:NAME``). Channel
rounding is ``round(channel * 255)``.

PURE leaf module: imports only stdlib. It must never import ``fastmcp``/``mcp``/``argparse``,
``gsheets.models``, or even sibling core modules (it is a zero-dependency leaf per the
manifest). Invalid input raises plain ``ValueError`` (``errors.SheetsError`` lives one layer
up and is wired in by callers, not here).
"""

from __future__ import annotations

# Channels Google may carry on a ``Color``/``rgbColor`` dict, in #RRGGBB byte order.
_CHANNELS = ("red", "green", "blue")

# Theme-color names Google's ``ColorStyle.themeColor`` enum accepts. Kept for validation so a
# typo'd ``theme:FOO`` fails loudly on write rather than silently producing an invalid request.
_THEME_COLORS = frozenset(
    {
        "TEXT",
        "BACKGROUND",
        "ACCENT1",
        "ACCENT2",
        "ACCENT3",
        "ACCENT4",
        "ACCENT5",
        "ACCENT6",
        "LINK",
        # Google also documents the unspecified sentinel; accept it for round-trip fidelity.
        "THEME_COLOR_TYPE_UNSPECIFIED",
    }
)

_THEME_PREFIX = "theme:"


def hex_to_color_style(hex_or_theme: str) -> dict:
    """Convert a hex string or ``theme:NAME`` token to a Google ``ColorStyle`` dict.

    Examples:
        ``"#FFCDD2"`` -> ``{"rgbColor": {"red": 1.0, "green": 0.803921..., "blue": 0.803921...}}``
        ``"theme:ACCENT1"`` -> ``{"themeColor": "ACCENT1"}``

    Each channel is normalized to ``byte / 255`` (so ``0xFF`` -> ``1.0``, ``0x00`` -> ``0.0``);
    the inverse :func:`color_style_to_hex` recovers the byte via ``round(channel * 255)``.

    Args:
        hex_or_theme: ``"#RRGGBB"`` (case-insensitive, leading ``#`` required) or
            ``"theme:<NAME>"`` (case-insensitive name).

    Returns:
        A ``ColorStyle`` dict suitable for ``*ColorStyle`` write fields:
        ``{"rgbColor": {...}}`` or ``{"themeColor": "<NAME>"}``.

    Raises:
        ValueError: if the input is not a string, is not a ``theme:`` token nor a valid
            6-digit ``#RRGGBB`` hex, or names an unknown theme color.
    """
    if not isinstance(hex_or_theme, str):
        raise ValueError(f"color must be a string, got {type(hex_or_theme).__name__}")

    token = hex_or_theme.strip()
    if not token:
        raise ValueError("color string is empty")

    if token.lower().startswith(_THEME_PREFIX):
        name = token[len(_THEME_PREFIX) :].strip().upper()
        if not name:
            raise ValueError("theme color name is empty")
        if name not in _THEME_COLORS:
            raise ValueError(
                f"unknown theme color {name!r}; expected one of {sorted(_THEME_COLORS)}"
            )
        return {"themeColor": name}

    rgb = _hex_to_rgb(token)
    return {"rgbColor": rgb}


def color_style_to_hex(color_style: dict) -> str:
    """Convert a Google ``ColorStyle`` (or legacy ``Color``) dict to a hex / theme string.

    Inverse of :func:`hex_to_color_style`. ``rgbColor`` -> ``"#RRGGBB"`` (uppercase);
    ``themeColor`` -> ``"theme:NAME"``. For read-side flattening (DESIGN §3.2) Google may hand
    back either a ``ColorStyle`` (``{"rgbColor"/"themeColor"}``) or, on older payloads, a flat
    ``Color`` dict (channels at the top level); both are accepted. Missing channels default to
    ``0.0`` (Google omits a channel whose value is ``0``). Each channel maps to a byte via
    ``round(channel * 255)``, clamped to ``0..255``.

    Args:
        color_style: A Google ``ColorStyle`` dict (``{"rgbColor": {...}}`` /
            ``{"themeColor": "ACCENT1"}``) or a flat ``Color`` dict (``{"red": 1.0, ...}``).

    Returns:
        ``"#RRGGBB"`` (uppercase) or ``"theme:NAME"``.

    Raises:
        ValueError: if the input is not a dict, or carries neither a theme color nor any
            recognizable rgb channels.
    """
    if not isinstance(color_style, dict):
        raise ValueError(
            f"color_style must be a dict, got {type(color_style).__name__}"
        )

    theme = color_style.get("themeColor")
    if theme is not None:
        if not isinstance(theme, str) or not theme.strip():
            raise ValueError(f"invalid themeColor value: {theme!r}")
        return f"{_THEME_PREFIX}{theme.strip().upper()}"

    # Prefer the nested ``rgbColor`` (ColorStyle); fall back to a flat ``Color`` dict whose
    # channels sit at the top level (legacy/effectiveFormat echoes).
    rgb = color_style.get("rgbColor")
    if rgb is None:
        if any(channel in color_style for channel in _CHANNELS):
            rgb = color_style
        else:
            raise ValueError(
                "color_style has neither themeColor nor rgb channels: "
                f"{sorted(color_style)}"
            )
    if not isinstance(rgb, dict):
        raise ValueError(f"rgbColor must be a dict, got {type(rgb).__name__}")

    return _rgb_to_hex(rgb)


def _hex_to_rgb(token: str) -> dict:
    """Parse ``#RRGGBB`` into a Google ``rgbColor`` dict with float channels in ``[0, 1]``.

    All three channels are always emitted (Google accepts an explicit ``0.0``), keeping the
    output stable and golden-masterable. ``#RGB`` shorthand is intentionally NOT supported —
    the design's canonical serialization is 6-digit hex, and accepting shorthand would create
    a second input form with no inverse.
    """
    if not token.startswith("#"):
        raise ValueError(f"hex color must start with '#': {token!r}")
    digits = token[1:]
    if len(digits) != 6:
        raise ValueError(
            f"hex color must be 6 digits (#RRGGBB), got {token!r}"
        )
    try:
        value = int(digits, 16)
    except ValueError as exc:
        raise ValueError(f"invalid hex color {token!r}") from exc

    r = (value >> 16) & 0xFF
    g = (value >> 8) & 0xFF
    b = value & 0xFF
    return {
        "red": r / 255,
        "green": g / 255,
        "blue": b / 255,
    }


def _rgb_to_hex(rgb: dict) -> str:
    """Render a Google ``rgbColor`` dict to uppercase ``#RRGGBB`` via ``round(channel*255)``."""
    parts = []
    for channel in _CHANNELS:
        raw = rgb.get(channel, 0.0)
        if raw is None:
            raw = 0.0
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            raise ValueError(
                f"rgb channel {channel!r} must be a number, got {raw!r}"
            )
        byte = round(raw * 255)
        # Clamp defensively: Google occasionally echoes tiny float drift outside [0,1].
        byte = max(0, min(255, byte))
        parts.append(f"{byte:02X}")
    return "#" + "".join(parts)
