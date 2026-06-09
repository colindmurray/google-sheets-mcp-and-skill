"""Conditional-format (de)serialization to/from the LOCKED body-only readable line (DESIGN §4).

The grammar is the parse target for round-tripping in
:func:`gsheets.core.rules.set_conditional_format`. The serialized ``line`` is the rule
**body only** and carries NO index/priority token — write addressing comes solely from the
``set_conditional_format(index=...)`` kwarg.

Gradient stops are slot-keyed with exactly one ``=`` per stop: ``min=<hex>`` / ``max=<hex>``
(no value) and ``mid:<interp>=<hex>`` (explicit value), joined by ``" | "`` in canonical
``min | mid | max`` order.
"""

from __future__ import annotations


def serialize_rule(rule: dict) -> str:
    """Serialize a Google ``ConditionalFormatRule`` to the body-only readable line.

    Takes NO ``index`` argument and emits NO priority token (DESIGN §4.4). Boolean example:
    ``"[Cliff!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold"``. Gradient example:
    ``"[Cliff!H2:H100] gradient min=#F44336 | mid:num:50=#FFEB3B | max=#4CAF50"``. Colors are
    6-digit uppercase hex (or ``theme:NAME``); fmt-token order and gradient-slot order are
    canonical.

    Args:
        rule: A Google ``ConditionalFormatRule`` dict (with ``ranges`` and a
            ``booleanRule`` or ``gradientRule``).

    Returns:
        The terse body-only line.
    """
    raise NotImplementedError


def parse_rule_line(line: str) -> dict:
    """Parse a body-only readable line into a structured rule dict.

    Returns ``{"ranges", "kind", "condition", "format"}`` and does NOT return an index (the
    index is external — supplied by the ``set_conditional_format(index=...)`` kwarg on write,
    DESIGN §4.4). Formula args are kept verbatim including the leading ``"="``.

    Args:
        line: A body-only conditional-format line per the §4 grammar.

    Returns:
        ``{"ranges": [...], "kind": "boolean"|"gradient", "condition": {...},
        "format": {...}}`` (gradient rules carry ``stops`` in place of ``condition``).
    """
    raise NotImplementedError


def build_google_rule(parsed: dict) -> dict:
    """Build a Google ``ConditionalFormatRule`` from a parsed/structured rule dict.

    Inverse of the parse step: maps the structured ``{ranges, kind, condition/stops, format}``
    shape (from :func:`parse_rule_line` or a caller-supplied dict) to a Google
    ``ConditionalFormatRule`` with ``ranges`` resolved to ``GridRange`` and a ``booleanRule``
    or ``gradientRule`` body.

    Args:
        parsed: A structured rule dict.

    Returns:
        A Google ``ConditionalFormatRule`` dict ready for ``add/updateConditionalFormatRule``.
    """
    raise NotImplementedError
