"""Unit tests for ``gsheets.core.colors`` (DESIGN §5.3).

Pure leaf module — no Google service, no network. We pin:
- hex -> ColorStyle (``rgbColor`` float channels = byte/255) and ``theme:NAME`` -> ``themeColor``;
- the inverse ``color_style_to_hex`` (uppercase ``#RRGGBB`` via ``round(channel*255)``,
  ``theme:NAME``);
- exact round-trip fidelity (golden-master style: representative ColorStyle in, exact string out);
- legacy flat ``Color`` input, omitted-channel default-to-zero, float-drift clamping;
- error handling (plain ``ValueError`` — colors is a zero-dep leaf and must NOT import errors).
"""

from __future__ import annotations

import pytest

from gsheets.core.colors import color_style_to_hex, hex_to_color_style

# --------------------------------------------------------------------------------------------
# hex_to_color_style — rgb
# --------------------------------------------------------------------------------------------


def test_hex_to_rgb_basic_pink():
    # #FFCDD2 -> red=255/255, green=205/255, blue=210/255 (DESIGN §5.3 worked example).
    result = hex_to_color_style("#FFCDD2")
    assert set(result) == {"rgbColor"}
    rgb = result["rgbColor"]
    assert rgb["red"] == pytest.approx(1.0)
    assert rgb["green"] == pytest.approx(205 / 255)
    assert rgb["blue"] == pytest.approx(210 / 255)
    # ~0.804 per the design's truncated display, but stored full-precision.
    assert rgb["green"] == pytest.approx(0.803921568, abs=1e-6)


def test_hex_to_rgb_all_channels_emitted_even_when_zero():
    # Black: every channel present and 0.0 (no omission), so output is stable/golden-able.
    rgb = hex_to_color_style("#000000")["rgbColor"]
    assert rgb == {"red": 0.0, "green": 0.0, "blue": 0.0}


def test_hex_to_rgb_white():
    rgb = hex_to_color_style("#FFFFFF")["rgbColor"]
    assert rgb == {"red": 1.0, "green": 1.0, "blue": 1.0}


def test_hex_to_rgb_pure_channels():
    assert hex_to_color_style("#FF0000")["rgbColor"]["red"] == pytest.approx(1.0)
    assert hex_to_color_style("#00FF00")["rgbColor"]["green"] == pytest.approx(1.0)
    assert hex_to_color_style("#0000FF")["rgbColor"]["blue"] == pytest.approx(1.0)


def test_hex_to_rgb_is_case_insensitive():
    assert hex_to_color_style("#ffcdd2") == hex_to_color_style("#FFCDD2")
    assert hex_to_color_style("#FfCdD2") == hex_to_color_style("#FFCDD2")


def test_hex_to_rgb_strips_whitespace():
    assert hex_to_color_style("  #FFCDD2  ") == hex_to_color_style("#FFCDD2")


# --------------------------------------------------------------------------------------------
# hex_to_color_style — theme
# --------------------------------------------------------------------------------------------


def test_theme_to_color_style():
    assert hex_to_color_style("theme:ACCENT1") == {"themeColor": "ACCENT1"}


def test_theme_is_case_insensitive_and_uppercased():
    assert hex_to_color_style("theme:accent1") == {"themeColor": "ACCENT1"}
    assert hex_to_color_style("THEME:Accent1") == {"themeColor": "ACCENT1"}


@pytest.mark.parametrize(
    "name",
    ["TEXT", "BACKGROUND", "ACCENT1", "ACCENT6", "LINK"],
)
def test_theme_accepts_known_names(name):
    assert hex_to_color_style(f"theme:{name}") == {"themeColor": name}


def test_theme_unknown_name_raises():
    with pytest.raises(ValueError, match="unknown theme color"):
        hex_to_color_style("theme:NEON_PURPLE")


def test_theme_empty_name_raises():
    with pytest.raises(ValueError, match="theme color name is empty"):
        hex_to_color_style("theme:")


# --------------------------------------------------------------------------------------------
# hex_to_color_style — errors
# --------------------------------------------------------------------------------------------


def test_hex_missing_hash_raises():
    with pytest.raises(ValueError):
        hex_to_color_style("FFCDD2")


def test_hex_wrong_length_raises():
    with pytest.raises(ValueError, match="6 digits"):
        hex_to_color_style("#FFF")  # shorthand intentionally unsupported
    with pytest.raises(ValueError, match="6 digits"):
        hex_to_color_style("#FFCDD2AA")


def test_hex_non_hex_chars_raises():
    with pytest.raises(ValueError, match="invalid hex color"):
        hex_to_color_style("#GGGGGG")


def test_non_string_input_raises():
    with pytest.raises(ValueError, match="must be a string"):
        hex_to_color_style(0xFFCDD2)  # type: ignore[arg-type]


def test_empty_string_raises():
    with pytest.raises(ValueError, match="empty"):
        hex_to_color_style("   ")


def test_colors_does_not_raise_sheetserror_type():
    # Leaf module: errors are plain ValueError, NOT gsheets SheetsError (no errors import).
    from gsheets.core import errors as errors_mod

    with pytest.raises(ValueError) as exc:
        hex_to_color_style("#ZZZZZZ")
    assert not isinstance(exc.value, errors_mod.SheetsError)


# --------------------------------------------------------------------------------------------
# color_style_to_hex — rgb (ColorStyle)
# --------------------------------------------------------------------------------------------


def test_color_style_to_hex_basic():
    style = {"rgbColor": {"red": 1.0, "green": 205 / 255, "blue": 210 / 255}}
    assert color_style_to_hex(style) == "#FFCDD2"


def test_color_style_to_hex_is_uppercase():
    style = {"rgbColor": {"red": 0xAB / 255, "green": 0xCD / 255, "blue": 0xEF / 255}}
    assert color_style_to_hex(style) == "#ABCDEF"


def test_color_style_to_hex_rounds_channel_times_255():
    # round(0.803921568 * 255) == round(205.0) == 205 -> 0xCD. Pins the rounding contract.
    style = {"rgbColor": {"red": 1.0, "green": 0.803921568, "blue": 0.823529411}}
    assert color_style_to_hex(style) == "#FFCDD2"


def test_color_style_to_hex_half_rounds_to_even_banker():
    # Python's round() is banker's rounding; 0.5/255 boundary -> assert documented behavior.
    # 100.5/255 channel: round(100.5) -> 100 (even) per Python semantics.
    style = {"rgbColor": {"red": 100.5 / 255, "green": 0.0, "blue": 0.0}}
    assert color_style_to_hex(style) == "#640000"  # 100 == 0x64


def test_color_style_to_hex_omitted_channels_default_zero():
    # Google omits a channel whose value is 0 — e.g. pure red comes back as {"red": 1.0}.
    assert color_style_to_hex({"rgbColor": {"red": 1.0}}) == "#FF0000"
    assert color_style_to_hex({"rgbColor": {"green": 1.0}}) == "#00FF00"
    assert color_style_to_hex({"rgbColor": {}}) == "#000000"


def test_color_style_to_hex_clamps_float_drift():
    # Tiny over/under-range drift Google sometimes echoes is clamped, never overflowing the byte.
    style = {"rgbColor": {"red": 1.0000001, "green": -0.0000001, "blue": 0.5}}
    assert color_style_to_hex(style) == "#FF0080"  # round(0.5*255)=128=0x80


def test_color_style_to_hex_explicit_none_channel_is_zero():
    style = {"rgbColor": {"red": 1.0, "green": None, "blue": None}}
    assert color_style_to_hex(style) == "#FF0000"


# --------------------------------------------------------------------------------------------
# color_style_to_hex — legacy flat Color + theme
# --------------------------------------------------------------------------------------------


def test_color_style_to_hex_legacy_flat_color():
    # Some payloads carry channels at top level (flat ``Color`` rather than nested ``rgbColor``).
    flat = {"red": 1.0, "green": 205 / 255, "blue": 210 / 255}
    assert color_style_to_hex(flat) == "#FFCDD2"


def test_color_style_to_hex_theme():
    assert color_style_to_hex({"themeColor": "ACCENT1"}) == "theme:ACCENT1"


def test_color_style_to_hex_theme_uppercased():
    assert color_style_to_hex({"themeColor": "accent2"}) == "theme:ACCENT2"


def test_color_style_to_hex_theme_takes_precedence_over_rgb():
    # If both appear, the theme is authoritative.
    style = {"themeColor": "TEXT", "rgbColor": {"red": 1.0}}
    assert color_style_to_hex(style) == "theme:TEXT"


# --------------------------------------------------------------------------------------------
# color_style_to_hex — errors
# --------------------------------------------------------------------------------------------


def test_color_style_to_hex_non_dict_raises():
    with pytest.raises(ValueError, match="must be a dict"):
        color_style_to_hex("#FFCDD2")  # type: ignore[arg-type]


def test_color_style_to_hex_empty_dict_raises():
    with pytest.raises(ValueError, match="neither themeColor nor rgb"):
        color_style_to_hex({})


def test_color_style_to_hex_unrelated_keys_raise():
    with pytest.raises(ValueError, match="neither themeColor nor rgb"):
        color_style_to_hex({"style": "SOLID"})


def test_color_style_to_hex_bad_theme_raises():
    with pytest.raises(ValueError, match="invalid themeColor"):
        color_style_to_hex({"themeColor": ""})


def test_color_style_to_hex_non_dict_rgb_raises():
    with pytest.raises(ValueError, match="rgbColor must be a dict"):
        color_style_to_hex({"rgbColor": [1, 2, 3]})


def test_color_style_to_hex_non_numeric_channel_raises():
    with pytest.raises(ValueError, match="must be a number"):
        color_style_to_hex({"rgbColor": {"red": "ff"}})


def test_color_style_to_hex_bool_channel_rejected():
    # bool is an int subclass; reject it so a stray True/False can't masquerade as a channel.
    with pytest.raises(ValueError, match="must be a number"):
        color_style_to_hex({"rgbColor": {"red": True}})


# --------------------------------------------------------------------------------------------
# Round-trip golden-master (DESIGN §5.3 — full CRUD symmetry: writes <-> reads)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hex_in",
    [
        "#FFCDD2",
        "#000000",
        "#FFFFFF",
        "#FF0000",
        "#00FF00",
        "#0000FF",
        "#1B5E20",
        "#4285F4",
        "#C8E6C9",
        "#ECEFF1",
        "#ABCDEF",
        "#123456",
    ],
)
def test_hex_round_trips_exactly(hex_in):
    # hex -> ColorStyle -> hex reproduces the canonical uppercase #RRGGBB exactly.
    assert color_style_to_hex(hex_to_color_style(hex_in)) == hex_in


def test_hex_round_trip_normalizes_lowercase_to_uppercase():
    assert color_style_to_hex(hex_to_color_style("#ffcdd2")) == "#FFCDD2"


def test_every_byte_round_trips_per_channel():
    # Exhaustive: every 0..255 byte survives byte -> float (/255) -> round(*255) -> byte.
    for byte in range(256):
        hex_in = f"#{byte:02X}{byte:02X}{byte:02X}"
        assert color_style_to_hex(hex_to_color_style(hex_in)) == hex_in


@pytest.mark.parametrize(
    "name", ["TEXT", "BACKGROUND", "ACCENT1", "ACCENT2", "ACCENT5", "LINK"]
)
def test_theme_round_trips_exactly(name):
    token = f"theme:{name}"
    assert color_style_to_hex(hex_to_color_style(token)) == token


def test_color_style_golden_for_known_palette():
    # Golden-master: representative Sheets-API ColorStyle JSON -> exact serialized hex.
    golden = {
        "#4285F4": {"rgbColor": {"red": 66 / 255, "green": 133 / 255, "blue": 244 / 255}},
        "#1B5E20": {"rgbColor": {"red": 27 / 255, "green": 94 / 255, "blue": 32 / 255}},
        "theme:ACCENT3": {"themeColor": "ACCENT3"},
        # omitted-channel + flat-Color forms from real payloads:
        "#FF0000": {"rgbColor": {"red": 1.0}},
        "#00FF00": {"green": 1.0},
    }
    for expected_hex, style in golden.items():
        assert color_style_to_hex(style) == expected_hex


# --------------------------------------------------------------------------------------------
# Boundary: colors is a transport-free, errors-free leaf
# --------------------------------------------------------------------------------------------


def test_colors_module_attrs_pull_in_no_transport_or_models():
    # The colors module's own object graph must not reference adapter/transport modules.
    # We inspect what names the loaded module actually bound (a source-text grep would false-
    # positive on the docstring, which legitimately *names* the modules it avoids). The full
    # cross-process boundary guard lives in tests_boundary_guard; this is a fast leaf check.
    import gsheets.core.colors as colors_mod

    referenced = {
        getattr(v, "__module__", "")
        for v in vars(colors_mod).values()
        if callable(v)
    }
    referenced.discard("")
    for ref in referenced:
        assert not ref.startswith("fastmcp"), ref
        assert not ref.startswith("mcp"), ref
        assert ref != "argparse" and not ref.startswith("argparse."), ref
        assert ref != "gsheets.models", ref
    # colors is a zero-dependency leaf (manifest depends_on == []): no SheetsError usage.
    assert not hasattr(colors_mod, "SheetsError")
