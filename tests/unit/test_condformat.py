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
    # A CUSTOM_FORMULA has exactly ONE value (its formula); commas inside it must NOT split it
    # into bogus args (ISSUES.md #2). The whole formula stays one verbatim value, '=' preserved.
    assert parsed["condition"]["type"] == "CUSTOM_FORMULA"
    assert parsed["condition"]["values"] == ["=AND($A1>0,$B1<100)"]


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


# ===========================================================================
# Below: branch-pinning tests for uncovered paths. Each asserts a concrete
# behavior (exact line out, exact error CODE, or boundary result), not just
# "an exception was raised". Error codes are split: serialize/build raise
# ``bad_rule`` (they consume Google/structured dicts); parse raises
# ``bad_rule_line`` (it consumes the terse line) — pinning the code proves the
# failure happened on the intended side of the grammar.
# ===========================================================================


# ---------------------------------------------------------------------------
# serialize_rule — ranges coercion (single string + malformed list). DESIGN §4.4
# keeps ranges as A1 here (serviceless); these pin the coercion contract.
# ---------------------------------------------------------------------------


def test_serialize_accepts_single_string_range():
    # A bare A1 string (not a list) is coerced to a one-element range list (line 122).
    rule = {
        "ranges": "Cliff!A2:A9",
        "booleanRule": {"condition": {"type": "BLANK"}, "format": {}},
    }
    assert serialize_rule(rule) == "[Cliff!A2:A9] if BLANK ->"


def test_serialize_rejects_empty_range_list():
    # An empty list is not "non-empty" -> bad_rule (line 124), distinct from None ranges.
    with pytest.raises(SheetsError) as exc:
        serialize_rule(
            {"ranges": [], "booleanRule": {"condition": {"type": "BLANK"}}}
        )
    assert exc.value.code == "bad_rule"


def test_serialize_rejects_non_sequence_ranges():
    # A non-str / non-list/tuple ranges value (e.g. an int) hits the same guard (line 124).
    with pytest.raises(SheetsError) as exc:
        serialize_rule(
            {"ranges": 7, "booleanRule": {"condition": {"type": "BLANK"}}}
        )
    assert exc.value.code == "bad_rule"


def test_serialize_rejects_blank_range_string_in_list():
    # A whitespace-only range string is not resolvable A1 -> bad_rule.
    with pytest.raises(SheetsError) as exc:
        serialize_rule(
            {"ranges": ["   "], "booleanRule": {"condition": {"type": "BLANK"}}}
        )
    assert exc.value.code == "bad_rule"


# ---------------------------------------------------------------------------
# serialize_rule — booleanRule / condition structural guards.
# ---------------------------------------------------------------------------


def test_serialize_rejects_boolean_without_condition():
    # booleanRule present but no condition dict -> bad_rule (line 140).
    with pytest.raises(SheetsError) as exc:
        serialize_rule({"ranges": ["S!A1"], "booleanRule": {"format": {}}})
    assert exc.value.code == "bad_rule"


def test_serialize_rejects_condition_without_type():
    # condition dict present but no ``type`` -> bad_rule (line 153).
    with pytest.raises(SheetsError) as exc:
        serialize_rule(
            {
                "ranges": ["S!A1"],
                "booleanRule": {"condition": {"values": [{"userEnteredValue": "x"}]}},
            }
        )
    assert exc.value.code == "bad_rule"


def test_serialize_rejects_condition_with_empty_string_type():
    # An empty-string ``type`` is falsy and must also raise (line 152-153 guard).
    with pytest.raises(SheetsError) as exc:
        serialize_rule(
            {"ranges": ["S!A1"], "booleanRule": {"condition": {"type": ""}}}
        )
    assert exc.value.code == "bad_rule"


# ---------------------------------------------------------------------------
# _serialize_condition — value variant handling (lines 161-167).
# ---------------------------------------------------------------------------


def test_serialize_condition_relative_date_value():
    # A relativeDate-bearing value uses the relativeDate string verbatim (line 161-162).
    rule = {
        "ranges": ["S!A1"],
        "booleanRule": {
            "condition": {
                "type": "DATE_BEFORE",
                "values": [{"relativeDate": "PAST_WEEK"}],
            },
            "format": {},
        },
    }
    assert serialize_rule(rule) == "[S!A1] if DATE_BEFORE(PAST_WEEK) ->"


def test_serialize_condition_unknown_value_variant_best_effort():
    # An unrecognized value dict variant is stringified best-effort from its first
    # value (line 164-165); a single-key dict is deterministic.
    rule = {
        "ranges": ["S!A1"],
        "booleanRule": {
            "condition": {"type": "WEIRD", "values": [{"futureField": "zzz"}]},
            "format": {},
        },
    }
    assert serialize_rule(rule) == "[S!A1] if WEIRD(zzz) ->"


def test_serialize_condition_scalar_values_stringified():
    # Non-dict scalar values (already-flattened ints/strings) are str()'d (line 167).
    rule = {
        "ranges": ["S!A1"],
        "booleanRule": {
            "condition": {"type": "NUMBER_BETWEEN", "values": ["lo", 42]},
            "format": {},
        },
    }
    assert serialize_rule(rule) == "[S!A1] if NUMBER_BETWEEN(lo,42) ->"


# ---------------------------------------------------------------------------
# _google_format_to_flat — non-dict + legacy flat Color fallbacks.
# ---------------------------------------------------------------------------


def test_serialize_format_non_dict_is_ignored():
    # A non-dict ``format`` yields no fmt tokens (line 217) -> degenerate arrow line.
    rule = {
        "ranges": ["S!A1"],
        "booleanRule": {"condition": {"type": "BLANK"}, "format": "oops"},
    }
    assert serialize_rule(rule) == "[S!A1] if BLANK ->"


def test_serialize_legacy_flat_background_color():
    # No backgroundColorStyle, only a deprecated flat ``backgroundColor`` -> bg token
    # via the legacy fallback (line 224). ColorStyle-vs-flat Color gotcha.
    rule = {
        "ranges": ["S!A1"],
        "booleanRule": {
            "condition": {"type": "BLANK"},
            "format": {"backgroundColor": {"red": 1, "green": 0, "blue": 0}},
        },
    }
    assert serialize_rule(rule) == "[S!A1] if BLANK -> bg #FF0000"


def test_serialize_legacy_flat_foreground_color():
    # No foregroundColorStyle, only a deprecated flat ``foregroundColor`` -> fg token
    # via the legacy fallback (line 232).
    rule = {
        "ranges": ["S!A1"],
        "booleanRule": {
            "condition": {"type": "BLANK"},
            "format": {"textFormat": {"foregroundColor": {"red": 0, "green": 1, "blue": 0}}},
        },
    }
    assert serialize_rule(rule) == "[S!A1] if BLANK -> fg #00FF00"


def test_serialize_color_style_preferred_over_legacy_flat():
    # When BOTH the ColorStyle and the legacy flat Color are present, ColorStyle wins
    # (the ``elif`` never runs) — prove the preferred branch dominates.
    rule = {
        "ranges": ["S!A1"],
        "booleanRule": {
            "condition": {"type": "BLANK"},
            "format": {
                "backgroundColorStyle": {"rgbColor": {"red": 0, "green": 0, "blue": 1}},
                "backgroundColor": {"red": 1, "green": 0, "blue": 0},
            },
        },
    }
    assert serialize_rule(rule) == "[S!A1] if BLANK -> bg #0000FF"


# ---------------------------------------------------------------------------
# _serialize_gradient / _point_color_hex / _serialize_midpoint guards.
# ---------------------------------------------------------------------------


def test_serialize_gradient_no_points_raises():
    # gradientRule present but with no min/mid/max points -> bad_rule (line 274).
    with pytest.raises(SheetsError) as exc:
        serialize_rule({"ranges": ["S!A1"], "gradientRule": {}})
    assert exc.value.code == "bad_rule"


def test_serialize_gradient_non_dict_point_raises():
    # A minpoint that is not a dict -> bad_rule (line 281).
    with pytest.raises(SheetsError) as exc:
        serialize_rule(
            {"ranges": ["S!A1"], "gradientRule": {"minpoint": "not-a-dict"}}
        )
    assert exc.value.code == "bad_rule"


def test_serialize_gradient_point_without_color_raises():
    # An interpolation point with no colorStyle/color -> bad_rule (line 284).
    with pytest.raises(SheetsError) as exc:
        serialize_rule(
            {"ranges": ["S!A1"], "gradientRule": {"minpoint": {"type": "MIN"}}}
        )
    assert exc.value.code == "bad_rule"


def test_serialize_gradient_point_legacy_color_field():
    # _point_color_hex falls back to the legacy ``color`` field when ``colorStyle`` absent.
    rule = {
        "ranges": ["S!A1"],
        "gradientRule": {
            "minpoint": {"type": "MIN", "color": {"red": 1, "green": 1, "blue": 1}},
            "maxpoint": {"type": "MAX", "color": {"red": 0, "green": 0, "blue": 0}},
        },
    }
    assert serialize_rule(rule) == "[S!A1] gradient min=#FFFFFF | max=#000000"


def test_serialize_gradient_midpoint_bad_interp_type_raises():
    # Midpoint with a type Google would never use (not NUMBER/PERCENT/PERCENTILE) and that
    # is therefore unmappable to an interp prefix -> bad_rule (line 294).
    with pytest.raises(SheetsError) as exc:
        serialize_rule(
            {
                "ranges": ["S!A1"],
                "gradientRule": {
                    "midpoint": {
                        "type": "MIN",  # MIN/MAX are not valid midpoint interp types
                        "value": "5",
                        "colorStyle": {"themeColor": "ACCENT1"},
                    }
                },
            }
        )
    assert exc.value.code == "bad_rule"


# ---------------------------------------------------------------------------
# parse_rule_line — range-list and body-shape edge cases.
# ---------------------------------------------------------------------------


def test_parse_rejects_range_list_of_only_commas():
    # "[ , ]" survives the empty-string check (non-empty rangelist) but every split
    # token is blank, so the post-filter list is empty -> bad_rule_line (line 339).
    with pytest.raises(SheetsError) as exc:
        parse_rule_line("[ , ] if BLANK -> bold")
    assert exc.value.code == "bad_rule_line"


def test_parse_boolean_body_missing_arrow_raises():
    # An ``if`` body with neither " -> " nor a trailing " ->" -> bad_rule_line (line 376).
    with pytest.raises(SheetsError) as exc:
        parse_rule_line("[S!A1] if BLANK bg #FFFFFF")
    assert exc.value.code == "bad_rule_line"


def test_parse_rejects_empty_condition():
    # "if  -> bold" leaves an empty condition before the arrow -> bad_rule_line (line 387).
    with pytest.raises(SheetsError) as exc:
        parse_rule_line("[S!A1] if  -> bold")
    assert exc.value.code == "bad_rule_line"


def test_parse_rejects_condition_with_empty_type_before_paren():
    # "(arg)" with nothing before the paren -> condition missing type (line 393).
    with pytest.raises(SheetsError) as exc:
        parse_rule_line("[S!A1] if (5) -> bold")
    assert exc.value.code == "bad_rule_line"


# ---------------------------------------------------------------------------
# _parse_format_tokens — interior empty tokens + num-pattern edge.
# ---------------------------------------------------------------------------


def test_parse_format_tolerates_double_spaces_between_tokens():
    # Collapsed/duplicated spaces produce empty tokens that are skipped (lines 414-415),
    # not treated as unknown tokens. The format still parses cleanly.
    parsed = parse_rule_line("[S!A1] if BLANK ->  bold   italic")
    assert parsed["format"] == {"bold": True, "italic": True}


def test_parse_num_token_with_trailing_align_keeps_pattern_tight():
    # ``num`` greedily consumes up to the next align/wrap keyword; the pattern stops
    # exactly at ``halign`` even though a number pattern may itself contain a space.
    parsed = parse_rule_line('[S!A1] if BLANK -> num #,##0.00 "kg" halign RIGHT')
    assert parsed["format"] == {"numberFormat": '#,##0.00 "kg"', "halign": "RIGHT"}


def test_parse_rejects_num_token_without_pattern():
    # ``num`` immediately followed by an align keyword leaves no pattern parts (line 437).
    with pytest.raises(SheetsError) as exc:
        parse_rule_line("[S!A1] if BLANK -> num halign LEFT")
    assert exc.value.code == "bad_rule_line"


# ---------------------------------------------------------------------------
# _parse_gradient_body — stop-level edge cases.
# ---------------------------------------------------------------------------


def test_parse_rejects_empty_gradient_stop_between_pipes():
    # A blank stop between two " | " separators -> bad_rule_line (line 476).
    with pytest.raises(SheetsError) as exc:
        parse_rule_line("[S!A1] gradient min=#FFFFFF |  | max=#000000")
    assert exc.value.code == "bad_rule_line"


def test_parse_rejects_gradient_stop_missing_color():
    # ``min=`` has a slot but no hex after the '=' -> bad_rule_line (line 483).
    with pytest.raises(SheetsError) as exc:
        parse_rule_line("[S!A1] gradient min=")
    assert exc.value.code == "bad_rule_line"


def test_parse_rejects_mid_stop_without_interp_value_colon():
    # ``mid:num`` has no inner ':' separating interp from value -> bad_rule_line (line 493).
    with pytest.raises(SheetsError) as exc:
        parse_rule_line("[S!A1] gradient mid:num=#FFFFFF")
    assert exc.value.code == "bad_rule_line"


# ---------------------------------------------------------------------------
# build_google_rule — boolean/condition structural guards.
# ---------------------------------------------------------------------------


def test_build_rejects_boolean_with_non_dict_condition():
    # kind=boolean but condition is not a dict -> bad_rule (line 572).
    with pytest.raises(SheetsError) as exc:
        build_google_rule(
            {"ranges": ["S!A1"], "kind": "boolean", "condition": "BLANK", "format": {}}
        )
    assert exc.value.code == "bad_rule"


def test_build_rejects_condition_missing_type():
    # A condition dict with no ``type`` -> bad_rule (line 585).
    with pytest.raises(SheetsError) as exc:
        build_google_rule(
            {"ranges": ["S!A1"], "kind": "boolean", "condition": {"values": ["x"]}}
        )
    assert exc.value.code == "bad_rule"


def test_build_rejects_condition_empty_string_type():
    # An empty-string type is falsy and rejected (line 584-585 guard).
    with pytest.raises(SheetsError) as exc:
        build_google_rule(
            {"ranges": ["S!A1"], "kind": "boolean", "condition": {"type": ""}}
        )
    assert exc.value.code == "bad_rule"


# ---------------------------------------------------------------------------
# _build_gradient_rule — stop-level structural guards.
# ---------------------------------------------------------------------------


def test_build_rejects_non_dict_gradient_stop():
    # A non-dict stop entry -> bad_rule (line 642).
    with pytest.raises(SheetsError) as exc:
        build_google_rule(
            {"ranges": ["S!A1"], "kind": "gradient", "stops": ["min=#FFFFFF"], "format": {}}
        )
    assert exc.value.code == "bad_rule"


def test_build_rejects_gradient_stop_bad_slot():
    # A stop slot outside min/mid/max -> bad_rule (line 645).
    with pytest.raises(SheetsError) as exc:
        build_google_rule(
            {
                "ranges": ["S!A1"],
                "kind": "gradient",
                "stops": [{"slot": "middle", "hexColor": "#FFFFFF"}],
                "format": {},
            }
        )
    assert exc.value.code == "bad_rule"


def test_build_rejects_duplicate_gradient_slot():
    # Two ``min`` stops -> bad_rule (line 647). (parse dedups too, but a caller-built dict
    # can reach build directly.)
    with pytest.raises(SheetsError) as exc:
        build_google_rule(
            {
                "ranges": ["S!A1"],
                "kind": "gradient",
                "stops": [
                    {"slot": "min", "hexColor": "#FFFFFF"},
                    {"slot": "min", "hexColor": "#000000"},
                ],
                "format": {},
            }
        )
    assert exc.value.code == "bad_rule"


def test_build_rejects_gradient_stop_without_hexcolor():
    # A stop with no hexColor -> bad_rule (line 651).
    with pytest.raises(SheetsError) as exc:
        build_google_rule(
            {
                "ranges": ["S!A1"],
                "kind": "gradient",
                "stops": [{"slot": "min"}],
                "format": {},
            }
        )
    assert exc.value.code == "bad_rule"


def test_build_rejects_mid_stop_bad_interp():
    # A mid stop with an interp not in num/pct/pctile -> bad_rule (line 661).
    with pytest.raises(SheetsError) as exc:
        build_google_rule(
            {
                "ranges": ["S!A1"],
                "kind": "gradient",
                "stops": [{"slot": "mid", "hexColor": "#000000", "interp": "bogus", "value": "5"}],
                "format": {},
            }
        )
    assert exc.value.code == "bad_rule"


def test_build_rejects_mid_stop_missing_value():
    # A mid stop with interp but no value -> bad_rule (line 667).
    with pytest.raises(SheetsError) as exc:
        build_google_rule(
            {
                "ranges": ["S!A1"],
                "kind": "gradient",
                "stops": [{"slot": "mid", "hexColor": "#000000", "interp": "num"}],
                "format": {},
            }
        )
    assert exc.value.code == "bad_rule"


def test_build_rejects_mid_stop_empty_string_value():
    # An empty-string value is also rejected (line 666-667 ``value == ""`` guard).
    with pytest.raises(SheetsError) as exc:
        build_google_rule(
            {
                "ranges": ["S!A1"],
                "kind": "gradient",
                "stops": [{"slot": "mid", "hexColor": "#000000", "interp": "num", "value": ""}],
                "format": {},
            }
        )
    assert exc.value.code == "bad_rule"


def test_build_gradient_min_max_emit_no_value_and_correct_types():
    # min -> MIN/no value; max -> MAX/no value; mid -> mapped interp type + formatted value.
    rule = build_google_rule(
        {
            "ranges": ["S!A1:A9"],
            "kind": "gradient",
            "stops": [
                {"slot": "min", "hexColor": "#FFFFFF"},
                {"slot": "mid", "hexColor": "#FFEB3B", "interp": "pct", "value": "50"},
                {"slot": "max", "hexColor": "#000000"},
            ],
            "format": {},
        }
    )
    grad = rule["gradientRule"]
    assert grad["minpoint"]["type"] == "MIN" and "value" not in grad["minpoint"]
    assert grad["maxpoint"]["type"] == "MAX" and "value" not in grad["maxpoint"]
    assert grad["midpoint"]["type"] == "PERCENT"
    assert grad["midpoint"]["value"] == "50"


# ---------------------------------------------------------------------------
# _format_number — every render branch (via build_google_rule, the real caller).
# ---------------------------------------------------------------------------


def _build_mid_value(value):
    """Build a gradient mid stop with ``value`` and return the emitted Google value string."""
    rule = build_google_rule(
        {
            "ranges": ["S!A1"],
            "kind": "gradient",
            "stops": [{"slot": "mid", "hexColor": "#000000", "interp": "num", "value": value}],
            "format": {},
        }
    )
    return rule["gradientRule"]["midpoint"]["value"]


def test_format_number_int_value_renders_bare():
    # A bare int value renders without a decimal point (line 692): 50 -> "50".
    assert _build_mid_value(50) == "50"


def test_format_number_integer_valued_float_drops_point():
    # An integer-valued float renders as a bare int (lines 694-695): 50.0 -> "50".
    assert _build_mid_value(50.0) == "50"


def test_format_number_non_integer_float_uses_repr():
    # A non-integer float keeps its fractional part (line 696): 12.5 -> "12.5".
    assert _build_mid_value(12.5) == "12.5"


def test_format_number_non_numeric_string_passthrough():
    # A non-numeric string can't be float()'d, so it passes through verbatim (lines 703-704).
    assert _build_mid_value("abc") == "abc"


def test_format_number_rejects_boolean_value():
    # bool is an int subclass; the explicit guard rejects True/False (line 690).
    with pytest.raises(SheetsError) as exc:
        _build_mid_value(True)
    assert exc.value.code == "bad_rule"


def test_format_number_rejects_whitespace_only_string():
    # A whitespace-only string is empty after strip -> bad_rule (line 699). (Reached via
    # _format_number directly because build's ``value == ""`` guard precedes it for "" but
    # not for "   ".)
    with pytest.raises(SheetsError) as exc:
        _build_mid_value("   ")
    assert exc.value.code == "bad_rule"


def test_format_number_string_50_0_normalizes_to_50():
    # A float-ish string normalizes through float() back to a bare int (line 705-707).
    assert _build_mid_value("50.0") == "50"


# --------------------------------------------------------------------- ISSUES.md #2 regression


def test_serialize_rule_structured_keeps_custom_formula_single_value():
    from gsheets.core.condformat import serialize_rule_structured

    rule = {
        "ranges": ["Cliff!A3:A345"],
        "booleanRule": {
            "condition": {
                "type": "CUSTOM_FORMULA",
                "values": [{"userEnteredValue": '=AND($A3<>"", $B3=$C3)'}],
            },
            "format": {"backgroundColorStyle": {"rgbColor": {"red": 1.0}}},
        },
    }
    out = serialize_rule_structured(rule)
    assert out["kind"] == "boolean"
    assert out["condition"] == {
        "type": "CUSTOM_FORMULA",
        "values": ['=AND($A3<>"", $B3=$C3)'],
    }


def test_serialize_rule_structured_gradient_matches_parse_shape():
    from gsheets.core.condformat import parse_rule_line, serialize_rule, serialize_rule_structured

    rule = {
        "ranges": ["Cliff!H2:H100"],
        "gradientRule": {
            "minpoint": {"type": "MIN", "colorStyle": {"rgbColor": {"red": 0.96}}},
            "midpoint": {"type": "NUMBER", "value": "50", "colorStyle": {"rgbColor": {"green": 1}}},
            "maxpoint": {"type": "MAX", "colorStyle": {"rgbColor": {"green": 0.69}}},
        },
    }
    structured = serialize_rule_structured(rule)
    via_parse = parse_rule_line(serialize_rule(rule))
    assert structured["stops"] == via_parse["stops"]


def test_parse_condition_does_not_split_custom_formula_commas():
    from gsheets.core.condformat import parse_rule_line

    line = '[Cliff!A3:A345] if CUSTOM_FORMULA(=AND($A3<>"", $D3=$G3)) -> bg #FFCDD2'
    parsed = parse_rule_line(line)
    assert parsed["condition"] == {
        "type": "CUSTOM_FORMULA",
        "values": ['=AND($A3<>"", $D3=$G3)'],
    }


def test_number_between_still_comma_splits():
    from gsheets.core.condformat import parse_rule_line

    parsed = parse_rule_line("[Cliff!C2:C9] if NUMBER_BETWEEN(0,100) -> bg #C8E6C9")
    assert parsed["condition"] == {"type": "NUMBER_BETWEEN", "values": ["0", "100"]}


# ----------------------------------------------- ISSUES.md #2 serialize_rule_structured guards


def test_serialize_rule_structured_error_guards():
    import pytest

    from gsheets.core.condformat import serialize_rule_structured
    from gsheets.core.errors import SheetsError

    with pytest.raises(SheetsError, match="rule must be a dict"):
        serialize_rule_structured("nope")
    with pytest.raises(SheetsError, match="expected exactly one"):
        serialize_rule_structured(
            {"ranges": ["S!A1"], "booleanRule": {"condition": {"type": "BLANK"}},
             "gradientRule": {"minpoint": {}}}
        )
    with pytest.raises(SheetsError, match="booleanRule has no condition"):
        serialize_rule_structured({"ranges": ["S!A1"], "booleanRule": {}})
    with pytest.raises(SheetsError, match="condition has no type"):
        serialize_rule_structured(
            {"ranges": ["S!A1"], "booleanRule": {"condition": {"values": []}}}
        )
    with pytest.raises(SheetsError, match="neither booleanRule nor gradientRule"):
        serialize_rule_structured({"ranges": ["S!A1"]})


def test_gradient_stops_structured_error_guards():
    import pytest

    from gsheets.core.condformat import serialize_rule_structured
    from gsheets.core.errors import SheetsError

    with pytest.raises(SheetsError, match="must be NUMBER/PERCENT/PERCENTILE"):
        serialize_rule_structured(
            {"ranges": ["S!A1"], "gradientRule": {
                "minpoint": {"type": "MIN", "colorStyle": {"rgbColor": {"red": 1}}},
                "midpoint": {"type": "BOGUS", "colorStyle": {"rgbColor": {"red": 1}}},
            }}
        )
    with pytest.raises(SheetsError, match="midpoint requires a value"):
        serialize_rule_structured(
            {"ranges": ["S!A1"], "gradientRule": {
                "midpoint": {"type": "NUMBER", "colorStyle": {"rgbColor": {"red": 1}}},
            }}
        )
    with pytest.raises(SheetsError, match="no interpolation points"):
        serialize_rule_structured({"ranges": ["S!A1"], "gradientRule": {}})


# --------------------------------------------------------------------------------------
# Comma-in-value safety: a single text value containing a comma must NOT be shredded on the
# terse line round-trip (only genuinely multi-value conditions split on commas).
# --------------------------------------------------------------------------------------


def test_single_text_value_with_comma_not_shredded():
    parsed = parse_rule_line("[S!A1:A1] if TEXT_EQ(Smith, John) -> bold")
    assert parsed["condition"] == {"type": "TEXT_EQ", "values": ["Smith, John"]}


def test_single_text_contains_value_with_comma_preserved():
    parsed = parse_rule_line("[S!A1:A9] if TEXT_CONTAINS(1,000) -> bg #FFFFFF")
    assert parsed["condition"] == {"type": "TEXT_CONTAINS", "values": ["1,000"]}


def test_multi_value_one_of_list_still_splits_on_comma():
    parsed = parse_rule_line("[S!A1:A9] if ONE_OF_LIST(a,b,c) -> bg #FFFFFF")
    assert parsed["condition"] == {"type": "ONE_OF_LIST", "values": ["a", "b", "c"]}


def test_multi_value_number_between_still_splits_on_comma():
    parsed = parse_rule_line("[S!B1:B9] if NUMBER_BETWEEN(0,100) -> bg #FFFF00")
    assert parsed["condition"] == {"type": "NUMBER_BETWEEN", "values": ["0", "100"]}


def test_text_eq_comma_value_round_trips_through_line():
    # Google rule -> terse line -> parsed: the comma-bearing value survives intact.
    rule = {
        "ranges": ["S!A1:A1"],
        "booleanRule": {
            "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": "Smith, John"}]},
            "format": {"textFormat": {"bold": True}},
        },
    }
    line = serialize_rule(rule)
    assert line == "[S!A1:A1] if TEXT_EQ(Smith, John) -> bold"
    assert parse_rule_line(line)["condition"]["values"] == ["Smith, John"]
