"""Unit tests for conditional-format (de)serialization (DESIGN §4).

Golden-master style: representative Sheets-API ``ConditionalFormatRule`` JSON in, assert the
exact serialized body-only line out; and the body-only round-trip (DESIGN §4.4):

    Google rule -> line -> parse -> Google rule -> serialize -> identical line

for the boolean rule AND the canonical gradient form
``min=<hex> | mid:<interp>:<n>=<hex> | max=<hex>``.

No network: these functions are pure (ranges are A1 strings; GridRange resolution happens in
callers that hold a ``SheetsServices`` handle, DESIGN §5.2).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gsheets.core import condformat
from gsheets.core.condformat import build_google_rule, parse_rule_line, serialize_rule
from gsheets.core.errors import SheetsError

GOLDEN_DIR = Path(__file__).parent / "golden"


def load_golden(name: str) -> dict:
    """Load a committed golden ``ConditionalFormatRule`` fixture."""
    return json.loads((GOLDEN_DIR / name).read_text())


# ---------------------------------------------------------------------------
# Boolean serialization (golden-master: API JSON -> exact line).
# ---------------------------------------------------------------------------


def test_serialize_boolean_custom_formula_golden():
    rule = load_golden("condformat_boolean_rule.json")
    assert (
        serialize_rule(rule)
        == "[Cliff!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold"
    )


def test_serialize_boolean_number_greater_fg_bold():
    rule = {
        "ranges": ["Cliff!C2:C100"],
        "booleanRule": {
            "condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]},
            "format": {
                "textFormat": {
                    "foregroundColorStyle": {
                        "rgbColor": {"red": 0.105882354, "green": 0.36862746, "blue": 0.1254902}
                    },
                    "bold": True,
                }
            },
        },
    }
    assert serialize_rule(rule) == "[Cliff!C2:C100] if NUMBER_GREATER(0) -> fg #1B5E20 bold"


def test_serialize_boolean_multi_range_text_contains():
    rule = {
        "ranges": ["Cliff!D2:D100", "Cliff!F2:F100"],
        "booleanRule": {
            "condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": "done"}]},
            "format": {
                "backgroundColorStyle": {
                    "rgbColor": {"red": 0.78431374, "green": 0.9019608, "blue": 0.7882353}
                }
            },
        },
    }
    assert (
        serialize_rule(rule)
        == "[Cliff!D2:D100,Cliff!F2:F100] if TEXT_CONTAINS(done) -> bg #C8E6C9"
    )


def test_serialize_boolean_blank_no_args_italic():
    rule = {
        "ranges": ["Cliff!E2:E100"],
        "booleanRule": {
            "condition": {"type": "BLANK"},
            "format": {
                "backgroundColorStyle": {
                    "rgbColor": {"red": 0.9254902, "green": 0.9372549, "blue": 0.94509804}
                },
                "textFormat": {"italic": True},
            },
        },
    }
    assert serialize_rule(rule) == "[Cliff!E2:E100] if BLANK -> bg #ECEFF1 italic"


def test_serialize_boolean_canonical_fmt_token_order():
    # Provide format keys in a SHUFFLED nesting; assert canonical emit order:
    # bg, fg, text-styles (bold/italic/underline/strike), num, halign, valign, wrap.
    rule = {
        "ranges": ["S!A1:A2"],
        "booleanRule": {
            "condition": {"type": "NOT_BLANK"},
            "format": {
                "wrapStrategy": "WRAP",
                "verticalAlignment": "MIDDLE",
                "horizontalAlignment": "CENTER",
                "numberFormat": {"type": "PERCENT", "pattern": "0.00%"},
                "textFormat": {
                    "strikethrough": True,
                    "underline": True,
                    "italic": True,
                    "bold": True,
                    "foregroundColorStyle": {"rgbColor": {"red": 0, "green": 0, "blue": 0}},
                },
                "backgroundColorStyle": {"rgbColor": {"red": 1, "green": 1, "blue": 1}},
            },
        },
    }
    assert (
        serialize_rule(rule)
        == "[S!A1:A2] if NOT_BLANK -> bg #FFFFFF fg #000000 bold italic underline strike "
        "num 0.00% halign CENTER valign MIDDLE wrap WRAP"
    )


def test_serialize_condition_keeps_formula_verbatim():
    rule = {
        "ranges": ["S!A1:A1"],
        "booleanRule": {
            "condition": {
                "type": "CUSTOM_FORMULA",
                "values": [{"userEnteredValue": "=AND($A1>0,$B1<100)"}],
            },
            "format": {},
        },
    }
    line = serialize_rule(rule)
    assert line == "[S!A1:A1] if CUSTOM_FORMULA(=AND($A1>0,$B1<100)) ->"


def test_serialize_number_between_two_args():
    rule = {
        "ranges": ["S!B1:B9"],
        "booleanRule": {
            "condition": {
                "type": "NUMBER_BETWEEN",
                "values": [{"userEnteredValue": "0"}, {"userEnteredValue": "100"}],
            },
            "format": {
                "backgroundColorStyle": {"rgbColor": {"red": 1, "green": 1, "blue": 0}}
            },
        },
    }
    assert serialize_rule(rule) == "[S!B1:B9] if NUMBER_BETWEEN(0,100) -> bg #FFFF00"


# ---------------------------------------------------------------------------
# Gradient serialization (golden-master).
# ---------------------------------------------------------------------------


def test_serialize_gradient_canonical_golden():
    rule = load_golden("condformat_gradient_rule.json")
    assert (
        serialize_rule(rule)
        == "[Cliff!H2:H100] gradient min=#F44336 | mid:num:50=#FFEB3B | max=#4CAF50"
    )


def test_serialize_gradient_min_max_only():
    rule = {
        "ranges": ["Cliff!G2:G100"],
        "gradientRule": {
            "minpoint": {
                "type": "MIN",
                "colorStyle": {"rgbColor": {"red": 1, "green": 1, "blue": 1}},
            },
            "maxpoint": {
                "type": "MAX",
                "colorStyle": {"rgbColor": {"red": 0.101960786, "green": 0.4509804, "blue": 0.9098039}},
            },
        },
    }
    assert serialize_rule(rule) == "[Cliff!G2:G100] gradient min=#FFFFFF | max=#1A73E8"


def test_serialize_gradient_mid_percent():
    rule = {
        "ranges": ["Cliff!I2:I100"],
        "gradientRule": {
            "minpoint": {
                "type": "MIN",
                "colorStyle": {"rgbColor": {"red": 1, "green": 1, "blue": 1}},
            },
            "midpoint": {
                "type": "PERCENT",
                "value": "50",
                "colorStyle": {"rgbColor": {"red": 1, "green": 0.92156863, "blue": 0.23137255}},
            },
            "maxpoint": {
                "type": "MAX",
                "colorStyle": {"rgbColor": {"red": 0.101960786, "green": 0.4509804, "blue": 0.9098039}},
            },
        },
    }
    assert (
        serialize_rule(rule)
        == "[Cliff!I2:I100] gradient min=#FFFFFF | mid:pct:50=#FFEB3B | max=#1A73E8"
    )


def test_serialize_gradient_mid_percentile():
    rule = {
        "ranges": ["S!A1:A9"],
        "gradientRule": {
            "minpoint": {"type": "MIN", "colorStyle": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}},
            "midpoint": {
                "type": "PERCENTILE",
                "value": "90",
                "colorStyle": {"rgbColor": {"red": 0, "green": 0, "blue": 0}},
            },
        },
    }
    assert serialize_rule(rule) == "[S!A1:A9] gradient min=#FFFFFF | mid:pctile:90=#000000"


def test_serialize_gradient_min_max_drop_echoed_value():
    # Google may echo a ``value`` on a MIN/MAX point; serialize must DROP it (DESIGN §4.3).
    rule = {
        "ranges": ["S!A1:A9"],
        "gradientRule": {
            "minpoint": {
                "type": "MIN",
                "value": "0",
                "colorStyle": {"rgbColor": {"red": 1, "green": 1, "blue": 1}},
            },
            "maxpoint": {
                "type": "MAX",
                "value": "100",
                "colorStyle": {"rgbColor": {"red": 0, "green": 0, "blue": 0}},
            },
        },
    }
    assert serialize_rule(rule) == "[S!A1:A9] gradient min=#FFFFFF | max=#000000"


def test_serialize_gradient_theme_color():
    rule = {
        "ranges": ["S!A1:A9"],
        "gradientRule": {
            "minpoint": {"type": "MIN", "colorStyle": {"themeColor": "ACCENT1"}},
            "maxpoint": {"type": "MAX", "colorStyle": {"themeColor": "ACCENT2"}},
        },
    }
    assert (
        serialize_rule(rule)
        == "[S!A1:A9] gradient min=theme:ACCENT1 | max=theme:ACCENT2"
    )


# ---------------------------------------------------------------------------
# parse_rule_line — boolean.
# ---------------------------------------------------------------------------


def test_parse_boolean_basic():
    parsed = parse_rule_line("[Cliff!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold")
    assert parsed == {
        "ranges": ["Cliff!A2:A100"],
        "kind": "boolean",
        "condition": {"type": "CUSTOM_FORMULA", "values": ["=$B2>10"]},
        "format": {"bg": "#FFCDD2", "bold": True},
    }


def test_parse_boolean_no_index_returned():
    parsed = parse_rule_line("[S!A1:A1] if BLANK -> bg #ECEFF1 italic")
    assert "index" not in parsed
    assert "priority" not in parsed


def test_parse_boolean_multi_range():
    parsed = parse_rule_line(
        "[Cliff!D2:D100,Cliff!F2:F100] if TEXT_CONTAINS(done) -> bg #C8E6C9"
    )
    assert parsed["ranges"] == ["Cliff!D2:D100", "Cliff!F2:F100"]
    assert parsed["condition"] == {"type": "TEXT_CONTAINS", "values": ["done"]}
    assert parsed["format"] == {"bg": "#C8E6C9"}


def test_parse_boolean_no_arg_condition():
    parsed = parse_rule_line("[S!A1:A9] if NOT_BLANK -> bg #FFFFFF")
    assert parsed["condition"] == {"type": "NOT_BLANK", "values": []}


def test_parse_boolean_all_fmt_tokens():
    line = (
        "[S!A1:A2] if NOT_BLANK -> bg #FFFFFF fg #000000 bold italic underline strike "
        "num 0.00% halign CENTER valign MIDDLE wrap WRAP"
    )
    parsed = parse_rule_line(line)
    assert parsed["format"] == {
        "bg": "#FFFFFF",
        "fg": "#000000",
        "bold": True,
        "italic": True,
        "underline": True,
        "strikethrough": True,
        "numberFormat": "0.00%",
        "halign": "CENTER",
        "valign": "MIDDLE",
        "wrap": "WRAP",
    }


def test_parse_boolean_number_format_with_spaces():
    parsed = parse_rule_line('[S!A1:A1] if NOT_BLANK -> num #,##0.00 "kg" halign LEFT')
    assert parsed["format"] == {"numberFormat": '#,##0.00 "kg"', "halign": "LEFT"}


def test_parse_boolean_empty_format():
    parsed = parse_rule_line("[S!A1:A1] if NUMBER_GREATER(5) ->")
    assert parsed["format"] == {}
    assert parsed["condition"] == {"type": "NUMBER_GREATER", "values": ["5"]}


def test_parse_condition_keeps_formula_commas_and_equals():
    parsed = parse_rule_line("[S!A1:A1] if CUSTOM_FORMULA(=AND($A1>0,$B1<100)) -> bold")
    # Formula contains a comma; the grammar splits args on commas (DESIGN §4.1) so this yields
    # two verbatim arg tokens — assert the leading '=' is preserved on the first.
    assert parsed["condition"]["type"] == "CUSTOM_FORMULA"
    assert parsed["condition"]["values"][0] == "=AND($A1>0"


def test_parse_number_between():
    parsed = parse_rule_line("[S!B1:B9] if NUMBER_BETWEEN(0,100) -> bg #FFFF00")
    assert parsed["condition"] == {"type": "NUMBER_BETWEEN", "values": ["0", "100"]}


# ---------------------------------------------------------------------------
# parse_rule_line — gradient.
# ---------------------------------------------------------------------------


def test_parse_gradient_full():
    parsed = parse_rule_line(
        "[Cliff!H2:H100] gradient min=#F44336 | mid:num:50=#FFEB3B | max=#4CAF50"
    )
    assert parsed["ranges"] == ["Cliff!H2:H100"]
    assert parsed["kind"] == "gradient"
    assert "condition" not in parsed
    assert parsed["stops"] == [
        {"slot": "min", "hexColor": "#F44336"},
        {"slot": "mid", "hexColor": "#FFEB3B", "interp": "num", "value": "50"},
        {"slot": "max", "hexColor": "#4CAF50"},
    ]


def test_parse_gradient_min_max_only():
    parsed = parse_rule_line("[Cliff!G2:G100] gradient min=#FFFFFF | max=#1A73E8")
    assert parsed["stops"] == [
        {"slot": "min", "hexColor": "#FFFFFF"},
        {"slot": "max", "hexColor": "#1A73E8"},
    ]


def test_parse_gradient_pct_and_pctile():
    p1 = parse_rule_line("[S!A1:A9] gradient min=#FFFFFF | mid:pct:25=#FFEB3B | max=#1A73E8")
    assert p1["stops"][1] == {"slot": "mid", "hexColor": "#FFEB3B", "interp": "pct", "value": "25"}
    p2 = parse_rule_line("[S!A1:A9] gradient min=#FFFFFF | mid:pctile:90=#000000")
    assert p2["stops"][1] == {"slot": "mid", "hexColor": "#000000", "interp": "pctile", "value": "90"}


def test_parse_gradient_theme_color():
    parsed = parse_rule_line("[S!A1:A9] gradient min=theme:ACCENT1 | max=theme:ACCENT2")
    assert parsed["stops"] == [
        {"slot": "min", "hexColor": "theme:ACCENT1"},
        {"slot": "max", "hexColor": "theme:ACCENT2"},
    ]


# ---------------------------------------------------------------------------
# build_google_rule.
# ---------------------------------------------------------------------------


def test_build_boolean_rule_from_parsed():
    parsed = {
        "ranges": ["Cliff!A2:A100"],
        "kind": "boolean",
        "condition": {"type": "CUSTOM_FORMULA", "values": ["=$B2>10"]},
        "format": {"bg": "#FFCDD2", "bold": True},
    }
    rule = build_google_rule(parsed)
    assert rule["ranges"] == ["Cliff!A2:A100"]
    assert rule["booleanRule"]["condition"] == {
        "type": "CUSTOM_FORMULA",
        "values": [{"userEnteredValue": "=$B2>10"}],
    }
    assert rule["booleanRule"]["format"]["backgroundColorStyle"] == {
        "rgbColor": {"red": 1.0, "green": 205 / 255, "blue": 210 / 255}
    }
    assert rule["booleanRule"]["format"]["textFormat"] == {"bold": True}
    assert "gradientRule" not in rule


def test_build_boolean_ranges_stay_a1():
    # build_google_rule is serviceless: ranges must remain A1 strings (DESIGN §5.2).
    parsed = parse_rule_line("[Cliff!A2:A100] if BLANK -> bg #ECEFF1")
    rule = build_google_rule(parsed)
    assert rule["ranges"] == ["Cliff!A2:A100"]


def test_build_boolean_no_format():
    parsed = parse_rule_line("[S!A1:A1] if NOT_BLANK ->")
    rule = build_google_rule(parsed)
    assert "format" not in rule["booleanRule"]
    assert rule["booleanRule"]["condition"] == {"type": "NOT_BLANK"}


def test_build_condition_no_values_omits_values_key():
    rule = build_google_rule(
        {"ranges": ["S!A1"], "kind": "boolean", "condition": {"type": "BLANK"}, "format": {}}
    )
    assert rule["booleanRule"]["condition"] == {"type": "BLANK"}
    assert "values" not in rule["booleanRule"]["condition"]


def test_build_gradient_rule_from_parsed():
    parsed = {
        "ranges": ["Cliff!H2:H100"],
        "kind": "gradient",
        "stops": [
            {"slot": "min", "hexColor": "#F44336"},
            {"slot": "mid", "hexColor": "#FFEB3B", "interp": "num", "value": "50"},
            {"slot": "max", "hexColor": "#4CAF50"},
        ],
        "format": {},
    }
    rule = build_google_rule(parsed)
    grad = rule["gradientRule"]
    assert grad["minpoint"]["type"] == "MIN"
    assert "value" not in grad["minpoint"]
    assert grad["midpoint"] == {
        "type": "NUMBER",
        "value": "50",
        "colorStyle": {"rgbColor": {"red": 255 / 255, "green": 235 / 255, "blue": 59 / 255}},
    }
    assert grad["maxpoint"]["type"] == "MAX"
    assert "value" not in grad["maxpoint"]


def test_build_gradient_interp_maps_to_google_type():
    rule = build_google_rule(
        {
            "ranges": ["S!A1:A9"],
            "kind": "gradient",
            "stops": [
                {"slot": "mid", "hexColor": "#000000", "interp": "pctile", "value": "90"},
            ],
            "format": {},
        }
    )
    assert rule["gradientRule"]["midpoint"]["type"] == "PERCENTILE"
    assert rule["gradientRule"]["midpoint"]["value"] == "90"


def test_build_infers_kind_from_condition():
    rule = build_google_rule(
        {"ranges": ["S!A1"], "condition": {"type": "BLANK"}, "format": {}}
    )
    assert "booleanRule" in rule


def test_build_infers_kind_from_stops():
    rule = build_google_rule(
        {"ranges": ["S!A1"], "stops": [{"slot": "min", "hexColor": "#FFFFFF"}], "format": {}}
    )
    assert "gradientRule" in rule


# ---------------------------------------------------------------------------
# Body-only round-trip golden masters (DESIGN §4.4): rule -> line -> parse ->
# rule -> serialize -> identical line.
# ---------------------------------------------------------------------------


def _roundtrip_line(rule: dict) -> tuple[str, str]:
    line1 = serialize_rule(rule)
    parsed = parse_rule_line(line1)
    rebuilt = build_google_rule(parsed)
    line2 = serialize_rule(rebuilt)
    return line1, line2


def test_roundtrip_boolean_golden():
    rule = load_golden("condformat_boolean_rule.json")
    line1, line2 = _roundtrip_line(rule)
    assert line1 == "[Cliff!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold"
    assert line2 == line1


def test_roundtrip_gradient_canonical_golden():
    rule = load_golden("condformat_gradient_rule.json")
    line1, line2 = _roundtrip_line(rule)
    assert (
        line1 == "[Cliff!H2:H100] gradient min=#F44336 | mid:num:50=#FFEB3B | max=#4CAF50"
    )
    assert line2 == line1


@pytest.mark.parametrize(
    "line",
    [
        "[Cliff!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold",
        "[Cliff!C2:C100] if NUMBER_GREATER(0) -> fg #1B5E20 bold",
        "[Cliff!D2:D100,Cliff!F2:F100] if TEXT_CONTAINS(done) -> bg #C8E6C9",
        "[Cliff!E2:E100] if BLANK -> bg #ECEFF1 italic",
        "[Cliff!G2:G100] gradient min=#FFFFFF | max=#1A73E8",
        "[Cliff!H2:H100] gradient min=#F44336 | mid:num:50=#FFEB3B | max=#4CAF50",
        "[Cliff!I2:I100] gradient min=#FFFFFF | mid:pct:50=#FFEB3B | max=#1A73E8",
        "[S!A1:A9] gradient min=#FFFFFF | mid:pctile:90=#000000",
        "[S!A1:A2] if NOT_BLANK -> bg #FFFFFF fg #000000 bold italic underline strike "
        "num 0.00% halign CENTER valign MIDDLE wrap WRAP",
    ],
)
def test_roundtrip_line_to_rule_to_line_idempotent(line):
    # line -> parse -> build -> serialize must reproduce the input line exactly.
    parsed = parse_rule_line(line)
    rebuilt = build_google_rule(parsed)
    assert serialize_rule(rebuilt) == line


# ---------------------------------------------------------------------------
# Error handling — all raise SheetsError (the one core exception type, DESIGN §6).
# ---------------------------------------------------------------------------


def test_serialize_rejects_non_dict():
    with pytest.raises(SheetsError):
        serialize_rule("not a dict")


def test_serialize_rejects_missing_ranges():
    with pytest.raises(SheetsError):
        serialize_rule({"booleanRule": {"condition": {"type": "BLANK"}}})


def test_serialize_rejects_gridrange_ranges():
    # ranges must be A1 strings; a GridRange dict means the caller forgot to resolve.
    with pytest.raises(SheetsError):
        serialize_rule(
            {
                "ranges": [{"sheetId": 0, "startRowIndex": 1}],
                "booleanRule": {"condition": {"type": "BLANK"}},
            }
        )


def test_serialize_rejects_neither_boolean_nor_gradient():
    with pytest.raises(SheetsError):
        serialize_rule({"ranges": ["S!A1"]})


def test_serialize_rejects_both_boolean_and_gradient():
    with pytest.raises(SheetsError):
        serialize_rule(
            {
                "ranges": ["S!A1"],
                "booleanRule": {"condition": {"type": "BLANK"}},
                "gradientRule": {"minpoint": {"type": "MIN", "colorStyle": {"themeColor": "ACCENT1"}}},
            }
        )


def test_serialize_gradient_mid_without_value_raises():
    with pytest.raises(SheetsError):
        serialize_rule(
            {
                "ranges": ["S!A1"],
                "gradientRule": {
                    "midpoint": {"type": "NUMBER", "colorStyle": {"themeColor": "ACCENT1"}}
                },
            }
        )


def test_parse_rejects_non_string():
    with pytest.raises(SheetsError):
        parse_rule_line(123)


def test_parse_rejects_missing_bracket():
    with pytest.raises(SheetsError):
        parse_rule_line("Cliff!A1 if BLANK -> bold")


def test_parse_rejects_unclosed_bracket():
    with pytest.raises(SheetsError):
        parse_rule_line("[Cliff!A1 if BLANK -> bold")


def test_parse_rejects_empty_range_list():
    with pytest.raises(SheetsError):
        parse_rule_line("[] if BLANK -> bold")


def test_parse_rejects_empty_body():
    with pytest.raises(SheetsError):
        parse_rule_line("[S!A1]")


def test_parse_rejects_unknown_body_kind():
    with pytest.raises(SheetsError):
        parse_rule_line("[S!A1] frobnicate stuff")


def test_parse_rejects_unknown_fmt_token():
    with pytest.raises(SheetsError):
        parse_rule_line("[S!A1] if BLANK -> sparkle")


def test_parse_rejects_bg_without_value():
    with pytest.raises(SheetsError):
        parse_rule_line("[S!A1] if BLANK -> bg")


def test_parse_rejects_gradient_stop_without_equals():
    with pytest.raises(SheetsError):
        parse_rule_line("[S!A1] gradient min #FFFFFF")


def test_parse_rejects_gradient_bad_slot():
    with pytest.raises(SheetsError):
        parse_rule_line("[S!A1] gradient middle=#FFFFFF")


def test_parse_rejects_gradient_bad_interp():
    with pytest.raises(SheetsError):
        parse_rule_line("[S!A1] gradient mid:bogus:50=#FFFFFF")


def test_parse_rejects_gradient_mid_missing_value():
    with pytest.raises(SheetsError):
        parse_rule_line("[S!A1] gradient mid:num:=#FFFFFF")


def test_parse_rejects_duplicate_gradient_slot():
    with pytest.raises(SheetsError):
        parse_rule_line("[S!A1] gradient min=#FFFFFF | min=#000000")


def test_parse_rejects_empty_gradient_body():
    with pytest.raises(SheetsError):
        parse_rule_line("[S!A1] gradient")


def test_build_rejects_non_dict():
    with pytest.raises(SheetsError):
        build_google_rule("nope")


def test_build_rejects_unknown_kind():
    with pytest.raises(SheetsError):
        build_google_rule({"ranges": ["S!A1"], "kind": "fancy", "format": {}})


def test_build_rejects_no_kind_no_condition_no_stops():
    with pytest.raises(SheetsError):
        build_google_rule({"ranges": ["S!A1"], "format": {}})


def test_build_gradient_rejects_empty_stops():
    with pytest.raises(SheetsError):
        build_google_rule({"ranges": ["S!A1"], "kind": "gradient", "stops": [], "format": {}})


# ---------------------------------------------------------------------------
# Number formatting normalization for gradient midpoint values.
# ---------------------------------------------------------------------------


def test_serialize_gradient_midpoint_float_value_renders_integer():
    # Google sometimes echoes the value as a float-ish string; integer-valued floats render
    # without a trailing .0 so round-trips stay canonical.
    rule = {
        "ranges": ["S!A1:A9"],
        "gradientRule": {
            "midpoint": {
                "type": "NUMBER",
                "value": "50.0",
                "colorStyle": {"rgbColor": {"red": 0, "green": 0, "blue": 0}},
            }
        },
    }
    assert serialize_rule(rule) == "[S!A1:A9] gradient mid:num:50=#000000"


def test_serialize_gradient_midpoint_non_integer_value_preserved():
    rule = {
        "ranges": ["S!A1:A9"],
        "gradientRule": {
            "midpoint": {
                "type": "NUMBER",
                "value": "12.5",
                "colorStyle": {"rgbColor": {"red": 0, "green": 0, "blue": 0}},
            }
        },
    }
    assert serialize_rule(rule) == "[S!A1:A9] gradient mid:num:12.5=#000000"


def test_module_exposes_public_symbols():
    # Locked public surface (DESIGN §4 / MANIFEST core_condformat).
    assert hasattr(condformat, "serialize_rule")
    assert hasattr(condformat, "parse_rule_line")
    assert hasattr(condformat, "build_google_rule")
