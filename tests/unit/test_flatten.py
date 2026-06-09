"""Unit tests for ``gsheets.core.flatten`` (DESIGN §3.1/§3.2, §10).

Pure leaf module — no Google service, no network. ``flatten_cell_format`` takes a Google
``CellFormat`` (``userEnteredFormat`` OR ``effectiveFormat``) and emits the flat ``CellFormat``
shape. We pin, golden-master style (representative Sheets-API JSON in -> exact flat dict out):

- the full-fidelity flatten (``tests/unit/golden/flatten_full.json``): bg/fg colors -> hex,
  ``textFormat`` scalar styles lifted, ``numberFormat`` -> ``numberFormatType``/``numberFormat``,
  alignment/wrap renamed, borders -> ``"<style> <hex>"`` (NONE dropped, default-black), and
  ``padding``/``textRotation`` preserved verbatim;
- token-efficiency: every unset key is omitted (empty / ``None`` input -> ``{}``);
- per-mapping isolation (color slots, text styles, number format, alignment, borders);
- ``effectiveFormat`` flattens identically to ``userEnteredFormat`` (read-only parity);
- defensive edges: legacy flat ``Color``, theme colors, falsy-but-present values (``bold:false``,
  ``fontSize:0``), unspecified-color sentinels dropped.
"""

from __future__ import annotations

import pytest

from gsheets.core.flatten import flatten_cell_format

# --------------------------------------------------------------------------------------------
# Golden master: full-fidelity flatten
# --------------------------------------------------------------------------------------------


def test_flatten_full_golden_master(load_golden):
    """Representative Sheets ``CellFormat`` in -> exact flat ``CellFormat`` out (DESIGN §10)."""
    golden = load_golden("flatten_full")
    assert flatten_cell_format(golden["input"]) == golden["expected"]


def test_flatten_full_golden_is_pure(load_golden):
    """Flatten must not mutate its input (so re-flattening effectiveFormat is safe)."""
    golden = load_golden("flatten_full")
    import copy

    src = copy.deepcopy(golden["input"])
    flatten_cell_format(src)
    assert src == golden["input"]


def test_flatten_full_golden_padding_is_a_copy(load_golden):
    """The preserved ``padding`` dict is a copy, not an alias into the Google payload."""
    golden = load_golden("flatten_full")
    src = golden["input"]
    out = flatten_cell_format(src)
    assert out["padding"] is not src["padding"]
    assert out["textRotation"] is not src["textRotation"]


# --------------------------------------------------------------------------------------------
# Token efficiency: unset keys omitted
# --------------------------------------------------------------------------------------------


def test_empty_dict_yields_empty():
    assert flatten_cell_format({}) == {}


def test_none_yields_empty():
    assert flatten_cell_format(None) == {}


def test_only_present_keys_emitted():
    # A format carrying ONLY a background color flattens to exactly {"bg": ...} — nothing else.
    out = flatten_cell_format(
        {"backgroundColorStyle": {"rgbColor": {"red": 1.0, "green": 1.0, "blue": 1.0}}}
    )
    assert out == {"bg": "#FFFFFF"}


def test_empty_text_format_contributes_nothing():
    assert flatten_cell_format({"textFormat": {}}) == {}


def test_empty_number_format_contributes_nothing():
    assert flatten_cell_format({"numberFormat": {}}) == {}


def test_empty_borders_contributes_nothing():
    assert flatten_cell_format({"borders": {}}) == {}


def test_empty_padding_omitted():
    assert flatten_cell_format({"padding": {}}) == {}


def test_empty_text_rotation_omitted():
    assert flatten_cell_format({"textRotation": {}}) == {}


# --------------------------------------------------------------------------------------------
# Colors: bg / fg
# --------------------------------------------------------------------------------------------


def test_background_color_style_to_bg_hex():
    out = flatten_cell_format(
        {
            "backgroundColorStyle": {
                "rgbColor": {"red": 1.0, "green": 0.8039215686, "blue": 0.8235294117}
            }
        }
    )
    assert out == {"bg": "#FFCDD2"}


def test_foreground_color_style_to_fg_hex():
    out = flatten_cell_format(
        {"textFormat": {"foregroundColorStyle": {"rgbColor": {"red": 0.0, "green": 0.0, "blue": 0.0}}}}
    )
    assert out == {"fg": "#000000"}


def test_theme_background_color():
    out = flatten_cell_format({"backgroundColorStyle": {"themeColor": "ACCENT1"}})
    assert out == {"bg": "theme:ACCENT1"}


def test_legacy_flat_background_color_fallback():
    # Older payloads echo a flat ``backgroundColor`` (no ColorStyle); we still flatten it.
    out = flatten_cell_format({"backgroundColor": {"red": 1.0, "green": 0.0, "blue": 0.0}})
    assert out == {"bg": "#FF0000"}


def test_color_style_preferred_over_legacy_flat():
    out = flatten_cell_format(
        {
            "backgroundColorStyle": {"rgbColor": {"red": 0.0, "green": 1.0, "blue": 0.0}},
            "backgroundColor": {"red": 1.0, "green": 0.0, "blue": 0.0},
        }
    )
    assert out == {"bg": "#00FF00"}


def test_unspecified_color_sentinel_dropped():
    # A ColorStyle with no rgb/theme info isn't a real color -> omit rather than crash.
    out = flatten_cell_format({"backgroundColorStyle": {"themeColorType": "UNSPECIFIED"}})
    assert out == {}


def test_empty_color_style_dropped():
    assert flatten_cell_format({"backgroundColorStyle": {}}) == {}


# --------------------------------------------------------------------------------------------
# Text format: lifted scalar styles
# --------------------------------------------------------------------------------------------


def test_text_styles_lifted_to_top_level():
    out = flatten_cell_format(
        {
            "textFormat": {
                "bold": True,
                "italic": True,
                "underline": True,
                "strikethrough": True,
                "fontSize": 14,
                "fontFamily": "Roboto",
            }
        }
    )
    assert out == {
        "bold": True,
        "italic": True,
        "underline": True,
        "strikethrough": True,
        "fontSize": 14,
        "fontFamily": "Roboto",
    }


def test_falsy_but_present_text_styles_are_kept():
    # bold:false / fontSize:0 are PRESENT values, not absent — keep them (golden-fidelity).
    out = flatten_cell_format({"textFormat": {"bold": False, "fontSize": 0}})
    assert out == {"bold": False, "fontSize": 0}


def test_none_valued_text_style_dropped():
    out = flatten_cell_format({"textFormat": {"bold": None, "italic": True}})
    assert out == {"italic": True}


def test_text_format_link_and_other_unknown_keys_ignored():
    # Only the enumerated scalar styles lift; unrelated textFormat keys (e.g. link) are dropped.
    out = flatten_cell_format({"textFormat": {"bold": True, "link": {"uri": "https://x"}}})
    assert out == {"bold": True}


# --------------------------------------------------------------------------------------------
# Number format
# --------------------------------------------------------------------------------------------


def test_number_format_type_and_pattern():
    out = flatten_cell_format({"numberFormat": {"type": "PERCENT", "pattern": "0.00%"}})
    assert out == {"numberFormatType": "PERCENT", "numberFormat": "0.00%"}


def test_number_format_type_only():
    out = flatten_cell_format({"numberFormat": {"type": "CURRENCY"}})
    assert out == {"numberFormatType": "CURRENCY"}


def test_number_format_pattern_only():
    out = flatten_cell_format({"numberFormat": {"pattern": "yyyy-mm-dd"}})
    assert out == {"numberFormat": "yyyy-mm-dd"}


# --------------------------------------------------------------------------------------------
# Alignment / wrap
# --------------------------------------------------------------------------------------------


def test_alignment_and_wrap_renamed():
    out = flatten_cell_format(
        {
            "horizontalAlignment": "RIGHT",
            "verticalAlignment": "TOP",
            "wrapStrategy": "CLIP",
        }
    )
    assert out == {"halign": "RIGHT", "valign": "TOP", "wrap": "CLIP"}


# --------------------------------------------------------------------------------------------
# Borders
# --------------------------------------------------------------------------------------------


def test_borders_serialized_per_side():
    out = flatten_cell_format(
        {
            "borders": {
                "top": {"style": "SOLID", "colorStyle": {"rgbColor": {"red": 0.0, "green": 0.0, "blue": 0.0}}},
                "bottom": {"style": "DASHED", "colorStyle": {"rgbColor": {"red": 1.0, "green": 0.0, "blue": 0.0}}},
            }
        }
    )
    assert out == {"borders": {"top": "SOLID #000000", "bottom": "DASHED #FF0000"}}


def test_border_style_none_is_dropped():
    out = flatten_cell_format(
        {
            "borders": {
                "top": {"style": "SOLID", "colorStyle": {"rgbColor": {}}},
                "bottom": {"style": "NONE"},
            }
        }
    )
    # NONE-styled side produces no token; only the SOLID side survives (default-black color).
    assert out == {"borders": {"top": "SOLID #000000"}}


def test_border_without_color_defaults_to_black():
    out = flatten_cell_format({"borders": {"left": {"style": "SOLID_MEDIUM", "width": 2}}})
    assert out == {"borders": {"left": "SOLID_MEDIUM #000000"}}


def test_border_with_empty_rgb_color_is_black():
    # Google omits all three channels for pure black -> #000000.
    out = flatten_cell_format({"borders": {"right": {"style": "DOTTED", "colorStyle": {"rgbColor": {}}}}})
    assert out == {"borders": {"right": "DOTTED #000000"}}


def test_borders_canonical_side_order():
    # All four sides present -> emitted top, bottom, left, right (insertion order is canonical).
    rgb_black = {"colorStyle": {"rgbColor": {}}}
    out = flatten_cell_format(
        {
            "borders": {
                "right": {"style": "SOLID", **rgb_black},
                "left": {"style": "SOLID", **rgb_black},
                "bottom": {"style": "SOLID", **rgb_black},
                "top": {"style": "SOLID", **rgb_black},
            }
        }
    )
    assert list(out["borders"].keys()) == ["top", "bottom", "left", "right"]


def test_border_legacy_flat_color_fallback():
    out = flatten_cell_format(
        {"borders": {"top": {"style": "SOLID", "color": {"red": 1.0, "green": 1.0, "blue": 1.0}}}}
    )
    assert out == {"borders": {"top": "SOLID #FFFFFF"}}


# --------------------------------------------------------------------------------------------
# Padding / textRotation preserved verbatim
# --------------------------------------------------------------------------------------------


def test_padding_preserved():
    out = flatten_cell_format({"padding": {"top": 2, "right": 3, "bottom": 2, "left": 3}})
    assert out == {"padding": {"top": 2, "right": 3, "bottom": 2, "left": 3}}


def test_partial_padding_preserved_verbatim():
    out = flatten_cell_format({"padding": {"left": 5}})
    assert out == {"padding": {"left": 5}}


def test_text_rotation_angle_preserved():
    out = flatten_cell_format({"textRotation": {"angle": 90}})
    assert out == {"textRotation": {"angle": 90}}


def test_text_rotation_vertical_preserved():
    out = flatten_cell_format({"textRotation": {"vertical": True}})
    assert out == {"textRotation": {"vertical": True}}


# --------------------------------------------------------------------------------------------
# effectiveFormat parity (read-only)
# --------------------------------------------------------------------------------------------


def test_effective_format_flattens_identically(load_golden):
    """``effectiveFormat`` uses the SAME Google CellFormat schema, so flatten is identical."""
    golden = load_golden("flatten_full")
    # Same input dict, regardless of whether it came from userEnteredFormat or effectiveFormat.
    user_entered = flatten_cell_format(golden["input"])
    effective = flatten_cell_format(golden["input"])
    assert user_entered == effective == golden["expected"]


def test_effective_format_conditional_result_colors():
    # effectiveFormat shows what RENDERS (incl. a conditional-format fill the user didn't set).
    eff = {
        "backgroundColorStyle": {"rgbColor": {"red": 1.0, "green": 0.8039215686, "blue": 0.8235294117}},
        "textFormat": {"bold": True},
    }
    assert flatten_cell_format(eff) == {"bg": "#FFCDD2", "bold": True}


# --------------------------------------------------------------------------------------------
# Robustness: non-dict / malformed sub-objects don't crash
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"textFormat": "nope"},
        {"numberFormat": "nope"},
        {"borders": "nope"},
        {"padding": "nope"},
        {"textRotation": "nope"},
    ],
)
def test_non_dict_subobjects_are_ignored(bad):
    # A malformed sub-object is skipped, not fatal; nothing else present -> empty result.
    assert flatten_cell_format(bad) == {}
