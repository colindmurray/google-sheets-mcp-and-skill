"""Auto fields-mask construction (DESIGN §5.1).

:func:`build_fields_mask` produces the minimal dotted/group mask covering exactly the keys
present in a write payload — never more (so unspecified subfields are never wiped), never
less (so the write is not a no-op).

The LOCKED atomic-leaf set (:data:`ATOMIC_LEAF_KEYS`) names sub-dicts Google treats
atomically: they are masked at the PARENT, never recursed into children. ``textFormat`` is
deliberately NOT atomic (its children mask individually, e.g. ``textFormat.bold``).

Mask grammar (Google ``FieldMask``):
- A leaf (non-dict value, or a key in the atomic-leaf set / any ``*ColorStyle``) emits its
  own key.
- A non-atomic dict node whose subtree yields exactly ONE token emits ``key.<token>``
  (dotted concatenation), e.g. ``userEnteredFormat.textFormat.bold``.
- A non-atomic dict node whose subtree yields MULTIPLE tokens emits the group form
  ``key(tokenA,tokenB)``, e.g. ``userEnteredFormat(backgroundColorStyle,textFormat.bold)``.

This module is PURE core: stdlib only. It must NEVER import ``fastmcp``, ``mcp``,
``argparse``, ``pydantic``, or ``gsheets.models`` (DESIGN §1 boundary).
"""

from __future__ import annotations

from .errors import SheetsError

#: Keys masked at the parent (never recursed) because Google treats them atomically.
#: Any ``*ColorStyle`` key is ALSO atomic; that family is matched by suffix (see
#: :func:`_is_atomic_leaf`), not by membership in this set.
ATOMIC_LEAF_KEYS: frozenset[str] = frozenset(
    {"numberFormat", "padding", "textRotation"}
)


def _is_atomic_leaf(key: str) -> bool:
    """True when ``key`` must be masked at the parent (never recursed into children).

    A key is an atomic leaf if it is in :data:`ATOMIC_LEAF_KEYS` (``numberFormat``,
    ``padding``, ``textRotation``) OR is any color-style key — the ``*ColorStyle`` family
    (``backgroundColorStyle``, ``foregroundColorStyle``, ``tabColorStyle``, …) AND the
    bare ``colorStyle`` key Google uses on a ``Border`` (``Border.colorStyle``). The
    color-style family is matched by suffix (case-insensitive on the leading ``c``) so new
    color-style keys are covered without enumerating them, and so a ``Border``'s lowercase
    ``colorStyle`` is treated atomically too.
    """
    return key in ATOMIC_LEAF_KEYS or key.lower().endswith("colorstyle")


def _mask_tokens(node: dict) -> list[str]:
    """Return the ordered mask tokens for one nested dict ``node``.

    Recurses ``node`` in insertion order. For each ``key``:
    - leaf (non-dict value, or :func:`_is_atomic_leaf`) → token ``key``;
    - non-atomic dict with one sub-token ``t`` → token ``key.t`` (dotted);
    - non-atomic dict with multiple sub-tokens → token ``key(t1,t2,...)`` (group).

    Empty nested dicts contribute nothing (no token), so a payload of only-empty dicts
    yields no tokens and ``build_fields_mask`` treats it as empty.
    """
    tokens: list[str] = []
    for key, value in node.items():
        if isinstance(value, dict) and not _is_atomic_leaf(key):
            sub = _mask_tokens(value)
            if not sub:
                # Empty (or all-empty) nested dict contributes no field path.
                continue
            if len(sub) == 1:
                tokens.append(f"{key}.{sub[0]}")
            else:
                tokens.append(f"{key}({','.join(sub)})")
        else:
            # Non-dict value, or an atomic-leaf / *ColorStyle key: mask at this key.
            tokens.append(key)
    return tokens


def build_fields_mask(payload: dict) -> str:
    """Build the minimal dotted/group ``fields`` mask for a write payload.

    Recurses ``payload``: a node is a leaf if its value is a non-dict OR its key is in the
    atomic-leaf set (any ``*ColorStyle`` key, ``numberFormat``, ``padding``,
    ``textRotation``). Atomic-leaf keys emit the key itself, not their children. Non-atomic
    nested dicts emit dotted (``parent.child``) syntax for a single present subfield and
    ``parent(childA,childB.grandchild)`` group syntax for multiple present subfields. A
    top-level cell ``note`` contributes a sibling ``note`` token (e.g.
    ``userEnteredFormat(...),note``).

    Args:
        payload: The Google request payload dict (e.g. the value under ``repeatCell.cell``
            or ``updateSheetProperties.properties``).

    Returns:
        A dotted/group ``fields`` mask string covering exactly the present keys, tokens
        joined by ``,`` in payload insertion order.

    Raises:
        SheetsError: ``empty_payload`` if ``payload`` is empty or contains only empty
            nested dicts (refuse a no-op write).
    """
    if not isinstance(payload, dict) or not payload:
        raise SheetsError("empty_payload", "refuse a no-op write: payload is empty")

    tokens = _mask_tokens(payload)
    if not tokens:
        # e.g. {"userEnteredFormat": {}} — nothing concrete to write.
        raise SheetsError("empty_payload", "refuse a no-op write: payload is empty")

    return ",".join(tokens)
