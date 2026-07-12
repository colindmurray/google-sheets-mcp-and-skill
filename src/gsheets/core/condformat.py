"""Conditional-format (de)serialization to/from the LOCKED body-only readable line (DESIGN §4).

The grammar is the parse target for round-tripping in
:func:`gsheets.core.rules.set_conditional_format`. The serialized ``line`` is the rule
**body only** and carries NO index/priority token — write addressing comes solely from the
``set_conditional_format(index=...)`` kwarg.

Gradient stops are slot-keyed with exactly one ``=`` per stop: ``min=<hex>`` / ``max=<hex>``
(no value) and ``mid:<interp>=<hex>`` (explicit value), joined by ``" | "`` in canonical
``min | mid | max`` order.

Range handling (DESIGN §4.4, §5.2 boundary):
    This module is dependency-light (it depends only on ``core_colors`` + ``core_errors``,
    never on ``core_addressing``). It therefore treats a rule's ``ranges`` as **A1 strings**
    and never resolves a ``GridRange``. Callers that have a ``SheetsServices`` handle do the
    A1<->GridRange resolution at the edges:
      - ``reads.read_conditional_formats`` resolves each Google ``GridRange`` to an A1 string
        (``gridrange_to_a1``) BEFORE handing the rule to :func:`serialize_rule`;
      - ``rules.set_conditional_format`` resolves the A1 ranges that :func:`build_google_rule`
        leaves on the rule into ``GridRange`` dicts (``a1_to_gridrange``) before the
        ``batchUpdate``.
    Keeping ranges as A1 here makes the body-only round-trip (rule -> line -> parse -> rule ->
    serialize -> identical line, DESIGN §4.4) self-contained and serviceless.
"""

from __future__ import annotations

from . import colors
from .errors import SheetsError

# ---------------------------------------------------------------------------
# Format-token serialization order + flat-CellFormat key mapping (DESIGN §4.1).
#
#   format    := fmt_token (" " fmt_token)*   # order: bg, fg, text-styles, number, align
#   fmt_token := "bg " hex | "fg " hex | "bold" | "italic" | "underline" | "strike"
#              | "num " pattern | "halign " H | "valign " V | "wrap " W
#
# We round-trip through the FLAT CellFormat shape (DESIGN §3.1): ``bg``/``fg`` hex strings,
# boolean style flags, ``numberFormat`` pattern, ``halign``/``valign``/``wrap`` enums.
# ---------------------------------------------------------------------------

# Boolean style flags, in canonical emit order, mapped flat-key -> token word.
_STYLE_FLAGS: tuple[tuple[str, str], ...] = (
    ("bold", "bold"),
    ("italic", "italic"),
    ("underline", "underline"),
    ("strikethrough", "strike"),
)
# Reverse: token word -> flat key.
_STYLE_TOKEN_TO_KEY = {token: key for key, token in _STYLE_FLAGS}

# Value-bearing tokens, in canonical emit order, mapped token word -> flat key.
_VALUE_TOKENS: tuple[tuple[str, str], ...] = (
    ("num", "numberFormat"),
    ("halign", "halign"),
    ("valign", "valign"),
    ("wrap", "wrap"),
)
_VALUE_TOKEN_TO_KEY = {token: key for token, key in _VALUE_TOKENS}

# Gradient interpolation prefixes (DESIGN §4.1): ``num:`` -> NUMBER, ``pct:`` -> PERCENT,
# ``pctile:`` -> PERCENTILE. ``min``/``max`` carry the implicit MIN/MAX type and no value.
_INTERP_PREFIX_TO_TYPE = {
    "num": "NUMBER",
    "pct": "PERCENT",
    "pctile": "PERCENTILE",
}
_INTERP_TYPE_TO_PREFIX = {v: k for k, v in _INTERP_PREFIX_TO_TYPE.items()}


# ===========================================================================
# serialize: Google ConditionalFormatRule (ranges already A1) -> body line
# ===========================================================================


def serialize_rule(rule: dict) -> str:
    """Serialize a Google ``ConditionalFormatRule`` to the body-only readable line.

    Takes NO ``index`` argument and emits NO priority token (DESIGN §4.4). Boolean example:
    ``"[Cliff!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold"``. Gradient example:
    ``"[Cliff!H2:H100] gradient min=#F44336 | mid:num:50=#FFEB3B | max=#4CAF50"``. Colors are
    6-digit uppercase hex (or ``theme:NAME``); fmt-token order and gradient-slot order are
    canonical.

    Args:
        rule: A Google ``ConditionalFormatRule`` dict whose ``ranges`` are A1 strings
            (callers resolve ``GridRange`` -> A1 first) and which carries a ``booleanRule``
            or ``gradientRule``.

    Returns:
        The terse body-only line.
    """
    if not isinstance(rule, dict):
        raise SheetsError("bad_rule", f"rule must be a dict, got {type(rule).__name__}")

    ranges = _ranges_to_a1_list(rule.get("ranges"))
    rangelist = ",".join(ranges)

    boolean = rule.get("booleanRule")
    gradient = rule.get("gradientRule")
    if boolean is not None and gradient is not None:
        raise SheetsError(
            "bad_rule", "rule has both booleanRule and gradientRule; expected exactly one"
        )
    if boolean is not None:
        body = _serialize_boolean(boolean)
    elif gradient is not None:
        body = _serialize_gradient(gradient)
    else:
        raise SheetsError(
            "bad_rule", "rule has neither booleanRule nor gradientRule"
        )

    return f"[{rangelist}] {body}"


def serialize_rule_structured(rule: dict) -> dict:
    """Serialize a Google ``ConditionalFormatRule`` to its STRUCTURED ``{ranges, kind, ...}`` dict.

    The structured counterpart of :func:`serialize_rule`, built DIRECTLY from the Google rule
    body — NOT by re-parsing the serialized line. That distinction is the whole point: the line
    grammar comma-separates a condition's args, so re-parsing a ``CUSTOM_FORMULA`` whose single
    formula contains commas (``CUSTOM_FORMULA(=AND($A1<>"", $B1=$C1))``) shreds it into several
    bogus values. Reading the condition's ``values[]`` straight off the rule keeps a single
    formula a single value (ISSUES.md #2).

    Boolean returns ``{"ranges", "kind": "boolean", "condition": {"type", "values"}, "format"}``;
    gradient returns ``{"ranges", "kind": "gradient", "stops": [...], "format": {}}`` — the same
    shapes :func:`parse_rule_line` produces, so the read side stays unchanged for every rule
    EXCEPT the comma-in-formula case it was getting wrong.

    Args:
        rule: A Google ``ConditionalFormatRule`` whose ``ranges`` are already A1 strings.

    Returns:
        The structured rule dict (no ``index`` — addressing stays external, DESIGN §4.4).
    """
    if not isinstance(rule, dict):
        raise SheetsError("bad_rule", f"rule must be a dict, got {type(rule).__name__}")

    ranges = _ranges_to_a1_list(rule.get("ranges"))
    boolean = rule.get("booleanRule")
    gradient = rule.get("gradientRule")
    if boolean is not None and gradient is not None:
        raise SheetsError(
            "bad_rule", "rule has both booleanRule and gradientRule; expected exactly one"
        )
    if boolean is not None:
        condition = boolean.get("condition")
        if not isinstance(condition, dict):
            raise SheetsError("bad_rule", "booleanRule has no condition")
        cond_type = condition.get("type")
        if not isinstance(cond_type, str) or not cond_type:
            raise SheetsError("bad_rule", "condition has no type")
        return {
            "ranges": ranges,
            "kind": "boolean",
            "condition": {"type": cond_type, "values": _condition_value_args(condition)},
            "format": _google_format_to_flat(boolean.get("format") or {}),
        }
    if gradient is not None:
        return {
            "ranges": ranges,
            "kind": "gradient",
            "stops": _gradient_stops_structured(gradient),
            "format": {},
        }
    raise SheetsError("bad_rule", "rule has neither booleanRule nor gradientRule")


def _gradient_stops_structured(gradient: dict) -> list[dict]:
    """Build the structured ``stops`` list directly from a Google ``gradientRule``.

    Mirrors :func:`_parse_gradient_body`'s output shape (``{slot, hexColor[, interp, value]}``)
    in canonical ``min | mid | max`` order, so the structured read is identical to the prior
    parse-based path for gradients.
    """
    stops: list[dict] = []
    minpoint = gradient.get("minpoint")
    midpoint = gradient.get("midpoint")
    maxpoint = gradient.get("maxpoint")
    if minpoint is not None:
        stops.append({"slot": "min", "hexColor": _point_color_hex(minpoint, "min")})
    if midpoint is not None:
        hex_color = _point_color_hex(midpoint, "mid")
        prefix = _INTERP_TYPE_TO_PREFIX.get(midpoint.get("type"))
        if prefix is None:
            raise SheetsError(
                "bad_rule",
                f"gradient midpoint type {midpoint.get('type')!r} must be "
                "NUMBER/PERCENT/PERCENTILE",
            )
        value = midpoint.get("value")
        if value is None:
            raise SheetsError("bad_rule", "gradient midpoint requires a value")
        stops.append(
            {
                "slot": "mid",
                "hexColor": hex_color,
                "interp": prefix,
                "value": _format_number(value),
            }
        )
    if maxpoint is not None:
        stops.append({"slot": "max", "hexColor": _point_color_hex(maxpoint, "max")})
    if not stops:
        raise SheetsError("bad_rule", "gradientRule has no interpolation points")
    return stops


def _ranges_to_a1_list(ranges: object) -> list[str]:
    """Coerce a rule's ``ranges`` (expected A1 strings) to a non-empty list of A1 strings."""
    if ranges is None:
        raise SheetsError("bad_rule", "rule has no ranges")
    if isinstance(ranges, str):
        ranges = [ranges]
    if not isinstance(ranges, (list, tuple)) or not ranges:
        raise SheetsError("bad_rule", "rule ranges must be a non-empty list of A1 strings")
    out: list[str] = []
    for r in ranges:
        if not isinstance(r, str) or not r.strip():
            raise SheetsError(
                "bad_rule",
                "rule ranges must be A1 strings; resolve GridRange -> A1 before serializing",
            )
        out.append(r.strip())
    return out


def _serialize_boolean(boolean: dict) -> str:
    """``"if " condition " -> " format`` body for a ``booleanRule``."""
    condition = boolean.get("condition")
    if not isinstance(condition, dict):
        raise SheetsError("bad_rule", "booleanRule has no condition")
    cond_str = _serialize_condition(condition)
    fmt_str = _serialize_format(boolean.get("format") or {})
    if fmt_str:
        return f"if {cond_str} -> {fmt_str}"
    # A rule with no format is degenerate but emit a stable line (arrow with empty format).
    return f"if {cond_str} ->"


def _condition_value_args(condition: dict) -> list[str]:
    """Extract a ``BooleanCondition``'s ``values[]`` as a flat list of verbatim string args.

    Each value is ``{"userEnteredValue": "..."}`` (or ``relativeDate``); formulas keep their
    leading ``=`` exactly. Shared by the line serializer and the structured serializer so both
    read the SAME values straight off the Google rule (no lossy re-parse round-trip).
    """
    values = condition.get("values") or []
    args: list[str] = []
    for v in values:
        if isinstance(v, dict):
            # BooleanCondition.values[] -> {"userEnteredValue": "..."} (or relativeDate).
            if "userEnteredValue" in v:
                args.append(str(v["userEnteredValue"]))
            elif "relativeDate" in v:
                args.append(str(v["relativeDate"]))
            else:
                # Unknown value variant: stringify deterministically (best effort).
                args.append(str(next(iter(v.values()), "")))
        else:
            args.append(str(v))
    return args


def _serialize_condition(condition: dict) -> str:
    """``COND_TYPE`` or ``COND_TYPE(arg,arg)`` — args verbatim, formulas kept exact."""
    cond_type = condition.get("type")
    if not isinstance(cond_type, str) or not cond_type:
        raise SheetsError("bad_rule", "condition has no type")
    args = _condition_value_args(condition)
    if args:
        return f"{cond_type}({','.join(args)})"
    return cond_type


def _serialize_format(fmt: dict) -> str:
    """Serialize a Google ``CellFormat`` (from a booleanRule) to space-joined fmt tokens.

    Canonical order: ``bg``, ``fg``, text-styles (bold/italic/underline/strike), ``num``,
    ``halign``, ``valign``, ``wrap``.
    """
    flat = _google_format_to_flat(fmt)
    return _flat_format_to_tokens(flat)


def _flat_format_to_tokens(flat: dict) -> str:
    """Render a flat CellFormat dict to the canonical space-joined fmt-token string."""
    tokens: list[str] = []
    bg = flat.get("bg")
    if bg:
        tokens.append(f"bg {bg}")
    fg = flat.get("fg")
    if fg:
        tokens.append(f"fg {fg}")
    for key, word in _STYLE_FLAGS:
        if flat.get(key):
            tokens.append(word)
    num = flat.get("numberFormat")
    if num:
        tokens.append(f"num {num}")
    halign = flat.get("halign")
    if halign:
        tokens.append(f"halign {halign}")
    valign = flat.get("valign")
    if valign:
        tokens.append(f"valign {valign}")
    wrap = flat.get("wrap")
    if wrap:
        tokens.append(f"wrap {wrap}")
    return " ".join(tokens)


def _google_format_to_flat(fmt: dict) -> dict:
    """Map the relevant subset of a Google ``CellFormat`` to the flat CellFormat shape.

    Local to condformat (this module must not depend on ``core_flatten``); covers only the
    keys the §4.1 ``fmt_token`` grammar can express.
    """
    if not isinstance(fmt, dict):
        return {}
    flat: dict[str, object] = {}

    bg_style = fmt.get("backgroundColorStyle")
    if bg_style:
        flat["bg"] = colors.color_style_to_hex(bg_style)
    elif fmt.get("backgroundColor"):  # legacy flat Color
        flat["bg"] = colors.color_style_to_hex(fmt["backgroundColor"])

    text = fmt.get("textFormat")
    if isinstance(text, dict):
        fg_style = text.get("foregroundColorStyle")
        if fg_style:
            flat["fg"] = colors.color_style_to_hex(fg_style)
        elif text.get("foregroundColor"):
            flat["fg"] = colors.color_style_to_hex(text["foregroundColor"])
        for key, _word in _STYLE_FLAGS:
            if text.get(key):
                flat[key] = True

    number = fmt.get("numberFormat")
    if isinstance(number, dict) and number.get("pattern"):
        flat["numberFormat"] = number["pattern"]

    halign = fmt.get("horizontalAlignment")
    if halign:
        flat["halign"] = halign
    valign = fmt.get("verticalAlignment")
    if valign:
        flat["valign"] = valign
    wrap = fmt.get("wrapStrategy")
    if wrap:
        flat["wrap"] = wrap

    return flat


def _serialize_gradient(gradient: dict) -> str:
    """``"gradient " gradstop (" | " gradstop)*`` body for a ``gradientRule``.

    Canonical slot order ``min | mid | max`` (omit absent slots). ``min``/``max`` emit
    ``min=<hex>`` / ``max=<hex>`` (no value, regardless of any echoed value); ``mid`` emits
    ``mid:<interp>:<value>=<hex>``.
    """
    stops: list[str] = []
    minpoint = gradient.get("minpoint")
    midpoint = gradient.get("midpoint")
    maxpoint = gradient.get("maxpoint")

    if minpoint is not None:
        stops.append(f"min={_point_color_hex(minpoint, 'min')}")
    if midpoint is not None:
        stops.append(_serialize_midpoint(midpoint))
    if maxpoint is not None:
        stops.append(f"max={_point_color_hex(maxpoint, 'max')}")

    if not stops:
        raise SheetsError("bad_rule", "gradientRule has no interpolation points")
    return "gradient " + " | ".join(stops)


def _point_color_hex(point: dict, slot: str) -> str:
    """Pull the hex/theme color from an ``InterpolationPoint``."""
    if not isinstance(point, dict):
        raise SheetsError("bad_rule", f"gradient {slot}point must be a dict")
    style = point.get("colorStyle") or point.get("color")
    if not style:
        raise SheetsError("bad_rule", f"gradient {slot}point has no color")
    return colors.color_style_to_hex(style)


def _serialize_midpoint(point: dict) -> str:
    """``mid:<interp>:<value>=<hex>`` for the midpoint InterpolationPoint."""
    hex_color = _point_color_hex(point, "mid")
    interp_type = point.get("type")
    prefix = _INTERP_TYPE_TO_PREFIX.get(interp_type)
    if prefix is None:
        raise SheetsError(
            "bad_rule",
            f"gradient midpoint type {interp_type!r} must be NUMBER/PERCENT/PERCENTILE",
        )
    value = point.get("value")
    if value is None:
        raise SheetsError("bad_rule", "gradient midpoint requires a value")
    return f"mid:{prefix}:{_format_number(value)}={hex_color}"


# ===========================================================================
# parse: body line -> structured {ranges, kind, condition|stops, format}
# ===========================================================================


def parse_rule_line(line: str) -> dict:
    """Parse a body-only readable line into a structured rule dict.

    Returns ``{"ranges", "kind", "condition", "format"}`` (boolean) or
    ``{"ranges", "kind", "stops", "format"}`` (gradient) and does NOT return an index (the
    index is external — supplied by the ``set_conditional_format(index=...)`` kwarg on write,
    DESIGN §4.4). Formula args are kept verbatim including the leading ``"="``.

    Args:
        line: A body-only conditional-format line per the §4 grammar.

    Returns:
        ``{"ranges": [...], "kind": "boolean"|"gradient", "condition": {...},
        "format": {...}}`` (gradient rules carry ``stops`` in place of ``condition``).
    """
    if not isinstance(line, str):
        raise SheetsError("bad_rule_line", f"line must be a string, got {type(line).__name__}")
    text = line.strip()
    if not text.startswith("["):
        raise SheetsError("bad_rule_line", f"line must start with '[': {line!r}")
    close = text.find("]")
    if close == -1:
        raise SheetsError("bad_rule_line", f"line missing ']' after range list: {line!r}")

    rangelist = text[1:close].strip()
    if not rangelist:
        raise SheetsError("bad_rule_line", "line has an empty range list")
    ranges = [r.strip() for r in rangelist.split(",")]
    ranges = [r for r in ranges if r]
    if not ranges:
        raise SheetsError("bad_rule_line", "line has an empty range list")

    body = text[close + 1 :].strip()
    if not body:
        raise SheetsError("bad_rule_line", "line has an empty body")

    if body.startswith("if "):
        condition, fmt = _parse_boolean_body(body[len("if ") :])
        return {
            "ranges": ranges,
            "kind": "boolean",
            "condition": condition,
            "format": fmt,
        }
    if body.startswith("gradient ") or body == "gradient":
        stops = _parse_gradient_body(body[len("gradient ") :] if body != "gradient" else "")
        return {
            "ranges": ranges,
            "kind": "gradient",
            "stops": stops,
            "format": {},
        }
    raise SheetsError(
        "bad_rule_line",
        f"body must start with 'if ' (boolean) or 'gradient ' (gradient): {body!r}",
    )


def _parse_boolean_body(body: str) -> tuple[dict, dict]:
    """Split ``condition " -> " format`` and parse each half."""
    # The arrow separator is " -> ". A condition's args are inside parens and never contain
    # a bare " -> " token, so splitting on the FIRST " -> " is safe.
    if " -> " in body:
        cond_part, fmt_part = body.split(" -> ", 1)
    elif body.endswith(" ->"):
        cond_part, fmt_part = body[: -len(" ->")], ""
    else:
        raise SheetsError(
            "bad_rule_line", f"boolean body missing ' -> ' format separator: {body!r}"
        )
    condition = _parse_condition(cond_part.strip())
    fmt = _parse_format_tokens(fmt_part.strip())
    return condition, fmt


# BooleanCondition types whose terse line body is genuinely a comma-separated value LIST. Every
# OTHER condition carries 0 or 1 value, so its parenthesized body is kept VERBATIM — a lone value
# may itself contain a comma (``TEXT_EQ(Smith, John)``) and must not be shredded into bogus values.
# This generalizes the original CUSTOM_FORMULA carve-out (whose formulas routinely contain commas,
# e.g. ``=AND($A1<>"", $B1=$C1)``) to ALL single-value conditions (ISSUES.md #2 + comma-in-value).
_MULTI_VALUE_CONDITIONS: frozenset[str] = frozenset(
    {"NUMBER_BETWEEN", "NUMBER_NOT_BETWEEN", "ONE_OF_LIST"}
)


def _parse_condition(text: str) -> dict:
    """Parse ``COND_TYPE`` or ``COND_TYPE(arg,arg)`` into a structured condition dict."""
    if not text:
        raise SheetsError("bad_rule_line", "empty condition")
    if text.endswith(")") and "(" in text:
        open_paren = text.index("(")
        cond_type = text[:open_paren].strip()
        inner = text[open_paren + 1 : -1]
        if not cond_type:
            raise SheetsError("bad_rule_line", f"condition missing type: {text!r}")
        # Only genuinely multi-value conditions split on commas; args are verbatim (formulas keep
        # their leading "=").
        if cond_type in _MULTI_VALUE_CONDITIONS:
            args = [a.strip() for a in inner.split(",")] if inner != "" else []
            return {"type": cond_type, "values": args}
        # Single-value (or formula) conditions: the WHOLE parenthesized body is ONE value — keep
        # embedded commas intact rather than shredding them (TEXT_EQ, NUMBER_GREATER, CUSTOM_FORMULA, …).
        return {"type": cond_type, "values": [inner.strip()] if inner.strip() else []}
    # No-arg condition (BLANK, NOT_BLANK, ...).
    return {"type": text, "values": []}


def _parse_format_tokens(text: str) -> dict:
    """Parse the space-joined fmt-token string into a flat CellFormat dict."""
    flat: dict[str, object] = {}
    if not text:
        return flat
    tokens = text.split(" ")
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "":
            i += 1
            continue
        if tok == "bg":
            flat["bg"] = _take_value(tokens, i, "bg")
            i += 2
        elif tok == "fg":
            flat["fg"] = _take_value(tokens, i, "fg")
            i += 2
        elif tok in _STYLE_TOKEN_TO_KEY:
            flat[_STYLE_TOKEN_TO_KEY[tok]] = True
            i += 1
        elif tok == "num":
            # A number pattern can contain spaces (e.g. ``#,##0.00 "kg"``). Greedily consume
            # the REST of the token stream as the pattern, since ``num`` is emitted last among
            # value tokens only when no align/wrap follows. To stay robust, consume until a
            # recognized trailing keyword token — but per canonical order (num precedes
            # halign/valign/wrap) we consume up to the next align/wrap keyword.
            j = i + 1
            parts: list[str] = []
            while j < n and tokens[j] not in ("halign", "valign", "wrap"):
                parts.append(tokens[j])
                j += 1
            if not parts:
                raise SheetsError("bad_rule_line", "num token missing a pattern")
            flat["numberFormat"] = " ".join(parts)
            i = j
        elif tok == "halign":
            flat["halign"] = _take_value(tokens, i, "halign")
            i += 2
        elif tok == "valign":
            flat["valign"] = _take_value(tokens, i, "valign")
            i += 2
        elif tok == "wrap":
            flat["wrap"] = _take_value(tokens, i, "wrap")
            i += 2
        else:
            raise SheetsError("bad_rule_line", f"unknown format token: {tok!r}")
    return flat


def _take_value(tokens: list[str], i: int, name: str) -> str:
    """Return the token after position ``i`` or raise if it is missing."""
    if i + 1 >= len(tokens) or tokens[i + 1] == "":
        raise SheetsError("bad_rule_line", f"{name} token missing a value")
    return tokens[i + 1]


def _parse_gradient_body(body: str) -> list[dict]:
    """Parse ``gradstop (" | " gradstop)*`` into a list of structured stops.

    Each stop is ``{"slot": "min"|"mid"|"max", "hexColor": "#...", ...}``; ``mid`` also
    carries ``interp`` (``num``/``pct``/``pctile``) and ``value`` (str). Slots are kept in
    written order; canonical order is enforced at serialize time.
    """
    body = body.strip()
    if not body:
        raise SheetsError("bad_rule_line", "gradient body has no stops")
    raw_stops = [s.strip() for s in body.split(" | ")]
    stops: list[dict] = []
    seen_slots: set[str] = set()
    for raw in raw_stops:
        if not raw:
            raise SheetsError("bad_rule_line", "empty gradient stop")
        if "=" not in raw:
            raise SheetsError("bad_rule_line", f"gradient stop missing '=': {raw!r}")
        keyspec, hex_color = raw.split("=", 1)
        keyspec = keyspec.strip()
        hex_color = hex_color.strip()
        if not hex_color:
            raise SheetsError("bad_rule_line", f"gradient stop missing color: {raw!r}")

        if keyspec in ("min", "max"):
            slot = keyspec
            stop = {"slot": slot, "hexColor": hex_color}
        elif keyspec.startswith("mid:"):
            slot = "mid"
            interp_spec = keyspec[len("mid:") :]
            # ``<interp>:<value>`` (e.g. ``num:50``). Split on the FIRST ':'.
            if ":" not in interp_spec:
                raise SheetsError(
                    "bad_rule_line",
                    f"mid stop must be 'mid:<interp>:<value>=<hex>': {raw!r}",
                )
            interp, value = interp_spec.split(":", 1)
            interp = interp.strip()
            value = value.strip()
            if interp not in _INTERP_PREFIX_TO_TYPE:
                raise SheetsError(
                    "bad_rule_line",
                    f"mid interp must be num/pct/pctile, got {interp!r}",
                )
            if value == "":
                raise SheetsError("bad_rule_line", f"mid stop missing a value: {raw!r}")
            stop = {"slot": "mid", "hexColor": hex_color, "interp": interp, "value": value}
        else:
            raise SheetsError(
                "bad_rule_line",
                f"gradient stop slot must be min/mid/max, got {keyspec!r}",
            )

        if slot in seen_slots:
            raise SheetsError("bad_rule_line", f"duplicate gradient slot: {slot!r}")
        seen_slots.add(slot)
        stops.append(stop)
    return stops


# ===========================================================================
# build: structured/parsed dict -> Google ConditionalFormatRule
# ===========================================================================


def build_google_rule(parsed: dict) -> dict:
    """Build a Google ``ConditionalFormatRule`` from a parsed/structured rule dict.

    Maps the structured ``{ranges, kind, condition/stops, format}`` shape (from
    :func:`parse_rule_line` or a caller-supplied dict) to a Google ``ConditionalFormatRule``
    with a ``booleanRule`` or ``gradientRule`` body. ``ranges`` are left as A1 strings —
    :func:`gsheets.core.rules.set_conditional_format` resolves them to ``GridRange`` dicts
    (it owns the ``SheetsServices`` handle); this module stays serviceless (DESIGN §5.2).

    Args:
        parsed: A structured rule dict (``kind`` ``"boolean"`` or ``"gradient"``).

    Returns:
        A Google ``ConditionalFormatRule`` dict (``ranges`` as A1 strings) ready for
        ``add/updateConditionalFormatRule`` once the caller resolves the ranges.
    """
    if not isinstance(parsed, dict):
        raise SheetsError(
            "bad_rule", f"parsed rule must be a dict, got {type(parsed).__name__}"
        )

    ranges = _ranges_to_a1_list(parsed.get("ranges"))
    kind = parsed.get("kind")
    if kind is None:
        # Infer from presence of condition vs stops (caller-supplied dicts may omit kind).
        if parsed.get("condition") is not None:
            kind = "boolean"
        elif parsed.get("stops") is not None:
            kind = "gradient"
        else:
            raise SheetsError("bad_rule", "parsed rule has no kind and no condition/stops")

    rule: dict[str, object] = {"ranges": list(ranges)}
    if kind == "boolean":
        rule["booleanRule"] = _build_boolean_rule(parsed)
    elif kind == "gradient":
        rule["gradientRule"] = _build_gradient_rule(parsed)
    else:
        raise SheetsError("bad_rule", f"unknown rule kind: {kind!r}")
    return rule


def _build_boolean_rule(parsed: dict) -> dict:
    """Build a Google ``booleanRule`` from ``{condition, format}``."""
    condition = parsed.get("condition")
    if not isinstance(condition, dict):
        raise SheetsError("bad_rule", "boolean rule requires a condition dict")
    out: dict[str, object] = {"condition": _build_condition(condition)}
    fmt = parsed.get("format") or {}
    google_fmt = _flat_format_to_google(fmt)
    if google_fmt:
        out["format"] = google_fmt
    return out


# ISSUES.md #39: Google's numeric-equality condition types reject a NON-numeric userEnteredValue on
# EVERY API write ("Invalid ConditionValue.userEnteredValue: Yes for ConditionType: NUMBER_EQ"), yet
# the UI happily creates "text is exactly Yes" rules that read back as NUMBER_EQ(Yes) — so a faithful
# read -> write round-trip 400s. ``NUMBER_EQ`` has a semantically-identical text equivalent that IS
# valid in a conditional format (verified live: TEXT_EQ(Yes) writes fine and matches identically), so
# on BUILD we coerce a NUMBER_EQ condition whose value is a plain non-numeric literal to TEXT_EQ. We
# do NOT map NUMBER_NOT_EQ -> TEXT_NOT_EQ: TEXT_NOT_EQ exists in the enum but Google rejects it inside
# a conditional format ("ConditionType 'TEXT_NOT_EQ' is not supported in conditional formats" —
# confirmed live), so coercing it would only swap one 400 for another. We also never touch the ORDERED
# comparisons (NUMBER_GREATER/LESS/BETWEEN) — no text equivalent — nor numeric or formula
# (``=``-prefixed) values, which Google accepts as-is.
_NUMERIC_EQ_TO_TEXT = {"NUMBER_EQ": "TEXT_EQ"}


def _is_numeric_literal(value: str) -> bool:
    """True iff ``value`` is a plain finite number literal (what a NUMBER_* condition wants)."""
    text = value.strip()
    if not text:
        return False
    try:
        num = float(text)
    except (TypeError, ValueError):
        return False
    return num == num and num not in (float("inf"), float("-inf"))  # reject nan / inf


def _effective_condition_type(cond_type: str, values: list) -> str:
    """Coerce a numeric-equality type to its text equivalent for non-numeric values (#39)."""
    text_type = _NUMERIC_EQ_TO_TEXT.get(cond_type)
    if text_type is None or not values:
        return cond_type
    # Coerce only when EVERY value is a plain non-numeric literal (not a number, not a ``=`` formula
    # / relative reference that Google evaluates numerically). Equality types carry a single value.
    for v in values:
        s = str(v)
        if s.startswith("=") or _is_numeric_literal(s):
            return cond_type
    return text_type


# ---------------------------------------------------------------------------
# Context-aware ConditionType validation (T1.5 / audit #5b).
#
# Google's ``ConditionType`` enum is shared across three surfaces — data validation,
# conditional formatting, and filters (basic filter / filter view / slicer) — but EACH surface
# accepts only a SUBSET, and Google 400s a type used on the wrong surface (e.g. ``BLANK`` /
# ``NOT_BLANK`` are CF/filter-only, illegal in data validation; ``ONE_OF_RANGE`` / ``BOOLEAN`` /
# email/URL/date-validity checks are data-validation-only). Each type also has a required
# ``ConditionValue`` arg count (``NUMBER_BETWEEN`` needs 2, ``ONE_OF_RANGE`` 1, ``BLANK`` 0, …).
# We validate BEFORE the API call so a misuse surfaces as a clear ``bad_condition`` instead of an
# opaque server 400. Pure data + a pure validator — usable by the CF, filter/slicer, AND
# data-validation (``rules.py``) build paths.
#
# Arg counts are ``(min, max)``; ``max`` of ``None`` means unbounded (``ONE_OF_LIST``).
# ---------------------------------------------------------------------------

_CONDITION_ARITY: dict[str, tuple[int, int | None]] = {
    "NUMBER_GREATER": (1, 1),
    "NUMBER_GREATER_THAN_EQ": (1, 1),
    "NUMBER_LESS": (1, 1),
    "NUMBER_LESS_THAN_EQ": (1, 1),
    "NUMBER_EQ": (1, 1),
    "NUMBER_NOT_EQ": (1, 1),
    "NUMBER_BETWEEN": (2, 2),
    "NUMBER_NOT_BETWEEN": (2, 2),
    "TEXT_CONTAINS": (1, 1),
    "TEXT_NOT_CONTAINS": (1, 1),
    "TEXT_STARTS_WITH": (1, 1),
    "TEXT_ENDS_WITH": (1, 1),
    "TEXT_EQ": (1, 1),
    "TEXT_NOT_EQ": (1, 1),
    "TEXT_IS_EMAIL": (0, 0),
    "TEXT_IS_URL": (0, 0),
    "DATE_EQ": (1, 1),
    "DATE_NOT_EQ": (1, 1),
    "DATE_BEFORE": (1, 1),
    "DATE_AFTER": (1, 1),
    "DATE_ON_OR_BEFORE": (1, 1),
    "DATE_ON_OR_AFTER": (1, 1),
    "DATE_BETWEEN": (2, 2),
    "DATE_NOT_BETWEEN": (2, 2),
    "DATE_IS_VALID": (0, 0),
    "ONE_OF_RANGE": (1, 1),
    "ONE_OF_LIST": (1, None),
    "BLANK": (0, 0),
    "NOT_BLANK": (0, 0),
    "CUSTOM_FORMULA": (1, 1),
    "BOOLEAN": (0, 2),
}

# Legal ConditionType sets per surface, from Google's ConditionType discovery annotations
# ("Supported by data validation / conditional formatting / filters").
_DATA_VALIDATION_CONDITIONS: frozenset[str] = frozenset(
    {
        "NUMBER_GREATER", "NUMBER_GREATER_THAN_EQ", "NUMBER_LESS", "NUMBER_LESS_THAN_EQ",
        "NUMBER_EQ", "NUMBER_NOT_EQ", "NUMBER_BETWEEN", "NUMBER_NOT_BETWEEN",
        "TEXT_CONTAINS", "TEXT_NOT_CONTAINS", "TEXT_EQ", "TEXT_IS_EMAIL", "TEXT_IS_URL",
        "DATE_EQ", "DATE_BEFORE", "DATE_AFTER", "DATE_ON_OR_BEFORE", "DATE_ON_OR_AFTER",
        "DATE_BETWEEN", "DATE_NOT_BETWEEN", "DATE_IS_VALID",
        "ONE_OF_RANGE", "ONE_OF_LIST", "CUSTOM_FORMULA", "BOOLEAN",
    }
)
_CONDITIONAL_FORMAT_CONDITIONS: frozenset[str] = frozenset(
    {
        "NUMBER_GREATER", "NUMBER_GREATER_THAN_EQ", "NUMBER_LESS", "NUMBER_LESS_THAN_EQ",
        "NUMBER_EQ", "NUMBER_NOT_EQ", "NUMBER_BETWEEN", "NUMBER_NOT_BETWEEN",
        "TEXT_CONTAINS", "TEXT_NOT_CONTAINS", "TEXT_STARTS_WITH", "TEXT_ENDS_WITH", "TEXT_EQ",
        "DATE_EQ", "DATE_BEFORE", "DATE_AFTER",
        "BLANK", "NOT_BLANK", "CUSTOM_FORMULA",
    }
)
# Regular filters (basic filter, filter views, slicers) support a strict subset: exactly the
# types whose ConditionType annotation reads "Supported by ... filters", EXCLUDING the ones marked
# only for filters on data-source objects. That authoritative regular-filter set is these 19. In
# particular TEXT_NOT_EQ/DATE_NOT_EQ are data-source-filter-only, and DATE_ON_OR_BEFORE/AFTER,
# DATE_BETWEEN/NOT_BETWEEN and ONE_OF_LIST are data-validation-only — none are valid on a regular
# filter, so they are deliberately absent here (a wrong-surface use is caught pre-flight).
_FILTER_CONDITIONS: frozenset[str] = frozenset(
    {
        "NUMBER_GREATER", "NUMBER_GREATER_THAN_EQ", "NUMBER_LESS", "NUMBER_LESS_THAN_EQ",
        "NUMBER_EQ", "NUMBER_NOT_EQ", "NUMBER_BETWEEN", "NUMBER_NOT_BETWEEN",
        "TEXT_CONTAINS", "TEXT_NOT_CONTAINS", "TEXT_STARTS_WITH", "TEXT_ENDS_WITH", "TEXT_EQ",
        "DATE_EQ", "DATE_BEFORE", "DATE_AFTER",
        "BLANK", "NOT_BLANK", "CUSTOM_FORMULA",
    }
)

_CONTEXT_CONDITIONS: dict[str, frozenset[str]] = {
    "data_validation": _DATA_VALIDATION_CONDITIONS,
    "conditional_format": _CONDITIONAL_FORMAT_CONDITIONS,
    "filter": _FILTER_CONDITIONS,
}


def validate_condition(cond_type: object, values: object, context: str) -> None:
    """Validate a ``BooleanCondition`` type + arg count for a given surface (context).

    ``context`` is one of ``"data_validation"``, ``"conditional_format"``, ``"filter"`` (slicers
    share the ``"filter"`` surface). Raises :class:`SheetsError` ``bad_condition`` when the type
    is illegal for that surface (naming the context + the allowed types) or when the number of
    ``values`` violates the type's required arg count. Pure: no services, no I/O; returns ``None``
    on success. Shared by the CF, filter/slicer, and data-validation build paths so a wrong-surface
    condition is caught pre-flight rather than as an opaque Google 400.
    """
    legal = _CONTEXT_CONDITIONS.get(context)
    if legal is None:
        raise SheetsError(
            "bad_condition",
            f"unknown condition context {context!r}; expected one of "
            f"{', '.join(sorted(_CONTEXT_CONDITIONS))}",
        )
    if not isinstance(cond_type, str) or not cond_type:
        raise SheetsError("bad_condition", "condition requires a non-empty type")
    if cond_type not in legal:
        raise SheetsError(
            "bad_condition",
            f"condition type {cond_type!r} is not valid for {context}; "
            f"allowed types: {', '.join(sorted(legal))}",
        )
    lo, hi = _CONDITION_ARITY[cond_type]
    n = len(values) if isinstance(values, (list, tuple)) else 0
    if n < lo or (hi is not None and n > hi):
        if lo == hi:
            want = f"exactly {lo}"
        elif hi is None:
            want = f"at least {lo}"
        else:
            want = f"between {lo} and {hi}"
        raise SheetsError(
            "bad_condition",
            f"condition type {cond_type} for {context} requires {want} "
            f"value(s), got {n}",
        )


def _build_condition(condition: dict, context: str = "conditional_format") -> dict:
    """Build a Google ``BooleanCondition`` from ``{type, values}``.

    ``context`` selects the legal-type/arg-count table (:func:`validate_condition`);
    defaults to ``"conditional_format"`` (the CF write path). Filter/slicer callers pass
    ``context="filter"``. Validation runs on the caller's type BEFORE the
    ``NUMBER_EQ`` -> ``TEXT_EQ`` coercion (both are legal on every surface that permits either).
    """
    cond_type = condition.get("type")
    if not isinstance(cond_type, str) or not cond_type:
        raise SheetsError("bad_rule", "condition requires a type")
    values = condition.get("values") or []
    validate_condition(cond_type, values, context)
    out: dict[str, object] = {"type": _effective_condition_type(cond_type, values)}
    if values:
        out["values"] = [{"userEnteredValue": str(v)} for v in values]
    return out


def _flat_format_to_google(flat: dict) -> dict:
    """Build a Google ``CellFormat`` (CF-relevant subset) from a flat CellFormat dict."""
    if not isinstance(flat, dict) or not flat:
        return {}
    google: dict[str, object] = {}
    text_format: dict[str, object] = {}

    bg = flat.get("bg")
    if bg:
        google["backgroundColorStyle"] = colors.hex_to_color_style(bg)
    fg = flat.get("fg")
    if fg:
        text_format["foregroundColorStyle"] = colors.hex_to_color_style(fg)
    for key, _word in _STYLE_FLAGS:
        if flat.get(key):
            text_format[key] = True

    if text_format:
        google["textFormat"] = text_format

    num = flat.get("numberFormat")
    if num:
        google["numberFormat"] = {"type": "NUMBER", "pattern": num}
    halign = flat.get("halign")
    if halign:
        google["horizontalAlignment"] = halign
    valign = flat.get("valign")
    if valign:
        google["verticalAlignment"] = valign
    wrap = flat.get("wrap")
    if wrap:
        google["wrapStrategy"] = wrap

    return google


def _build_gradient_rule(parsed: dict) -> dict:
    """Build a Google ``gradientRule`` from ``{stops: [...]}``.

    ``min`` -> ``minpoint`` (type ``MIN``, no value); ``max`` -> ``maxpoint`` (type ``MAX``,
    no value); ``mid`` -> ``midpoint`` (type NUMBER/PERCENT/PERCENTILE + value).
    """
    stops = parsed.get("stops")
    if not isinstance(stops, list) or not stops:
        raise SheetsError("bad_rule", "gradient rule requires a non-empty stops list")
    out: dict[str, object] = {}
    seen: set[str] = set()
    for stop in stops:
        if not isinstance(stop, dict):
            raise SheetsError("bad_rule", "each gradient stop must be a dict")
        slot = stop.get("slot")
        if slot not in ("min", "mid", "max"):
            raise SheetsError("bad_rule", f"gradient stop slot must be min/mid/max: {slot!r}")
        if slot in seen:
            raise SheetsError("bad_rule", f"duplicate gradient slot: {slot!r}")
        seen.add(slot)
        hex_color = stop.get("hexColor")
        if not hex_color:
            raise SheetsError("bad_rule", f"gradient {slot} stop has no hexColor")
        color_style = colors.hex_to_color_style(hex_color)
        if slot == "min":
            out["minpoint"] = {"type": "MIN", "colorStyle": color_style}
        elif slot == "max":
            out["maxpoint"] = {"type": "MAX", "colorStyle": color_style}
        else:  # mid
            interp = stop.get("interp")
            interp_type = _INTERP_PREFIX_TO_TYPE.get(interp)
            if interp_type is None:
                raise SheetsError(
                    "bad_rule",
                    f"mid stop interp must be num/pct/pctile, got {interp!r}",
                )
            value = stop.get("value")
            if value is None or value == "":
                raise SheetsError("bad_rule", "mid stop requires a value")
            out["midpoint"] = {
                "type": interp_type,
                "value": _format_number(value),
                "colorStyle": color_style,
            }
    return out


# ---------------------------------------------------------------------------
# Number formatting for gradient midpoint values.
# ---------------------------------------------------------------------------


def _format_number(value: object) -> str:
    """Render a midpoint value canonically.

    Google's ``InterpolationPoint.value`` is a string (e.g. ``"50"``). We keep integers as
    bare integers (``50`` not ``50.0``) and pass through other strings/numbers verbatim, so
    ``mid:num:50`` round-trips to ``mid:num:50`` rather than ``mid:num:50.0``.
    """
    if isinstance(value, bool):
        # Guard: bool is an int subclass; never treat True/False as a number here.
        raise SheetsError("bad_rule", "gradient midpoint value must not be a boolean")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    text = str(value).strip()
    if not text:
        raise SheetsError("bad_rule", "gradient midpoint value is empty")
    # Normalize a string like "50.0" -> "50" for stable round-trips.
    try:
        f = float(text)
    except ValueError:
        return text
    if f.is_integer():
        return str(int(f))
    return text
