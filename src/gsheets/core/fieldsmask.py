"""Auto fields-mask construction (DESIGN §5.1).

:func:`build_fields_mask` produces the minimal dotted/group mask covering exactly the keys
present in a write payload — never more (so unspecified subfields are never wiped), never
less (so the write is not a no-op).

The LOCKED atomic-leaf set (:data:`ATOMIC_LEAF_KEYS`) names sub-dicts Google treats
atomically: they are masked at the PARENT, never recursed into children. ``textFormat`` is
deliberately NOT atomic (its children mask individually, e.g. ``textFormat.bold``).
"""

from __future__ import annotations

from .errors import SheetsError

#: Keys masked at the parent (never recursed) because Google treats them atomically.
#: Any ``*ColorStyle`` key is also atomic; that family is matched by suffix, not membership.
ATOMIC_LEAF_KEYS: frozenset[str] = frozenset(
    {"numberFormat", "padding", "textRotation"}
)


def build_fields_mask(payload: dict) -> str:
    """Build the minimal dotted/group ``fields`` mask for a write payload.

    Recurses ``payload``: a node is a leaf if its value is a non-dict OR its key is in the
    atomic-leaf set (any ``*ColorStyle`` key, ``numberFormat``, ``padding``,
    ``textRotation``). Atomic-leaf keys emit the key itself, not their children. Non-atomic
    nested dicts with multiple present children emit ``parent(childA,childB.grandchild)``
    group syntax. A top-level cell ``note`` contributes a sibling ``note`` token.

    Args:
        payload: The Google request payload dict (e.g. the value under ``repeatCell.cell``
            or ``updateSheetProperties.properties``).

    Returns:
        A dotted/group ``fields`` mask string covering exactly the present keys.

    Raises:
        SheetsError: ``empty_payload`` if ``payload`` is empty (refuse a no-op write).
    """
    raise NotImplementedError
