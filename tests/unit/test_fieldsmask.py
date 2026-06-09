"""Unit tests for :mod:`gsheets.core.fieldsmask` (DESIGN §5.1).

Golden-master style: representative Google write payloads in, assert the EXACT minimal
dotted/group ``fields`` mask out. The golden cases under
``tests/unit/golden/fieldsmask_cases.json`` pin the LOCKED atomic-leaf behavior
(``*ColorStyle``, ``numberFormat``, ``padding``, ``textRotation`` -> parent not children;
``textFormat`` children mask individually). Pure core: no network, no mocks needed (the
function is a pure transform), no transport imports.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gsheets.core.errors import SheetsError
from gsheets.core.fieldsmask import ATOMIC_LEAF_KEYS, build_fields_mask

GOLDEN = Path(__file__).parent / "golden" / "fieldsmask_cases.json"


def _load_cases() -> list[dict]:
    with GOLDEN.open(encoding="utf-8") as fh:
        return json.load(fh)["cases"]


_CASES = _load_cases()


# --------------------------------------------------------------------------- #
# Golden-master: payload -> exact mask
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_golden_mask(case: dict) -> None:
    assert build_fields_mask(case["payload"]) == case["mask"]


# --------------------------------------------------------------------------- #
# Atomic-leaf set (LOCKED)
# --------------------------------------------------------------------------- #


def test_atomic_leaf_keys_locked_set() -> None:
    # The membership set is exactly these three; *ColorStyle is matched by suffix, not here.
    assert ATOMIC_LEAF_KEYS == frozenset({"numberFormat", "padding", "textRotation"})


def test_color_style_is_not_a_set_member() -> None:
    # The *ColorStyle family is matched by suffix at runtime, NOT enumerated in the set.
    assert "backgroundColorStyle" not in ATOMIC_LEAF_KEYS
    assert "foregroundColorStyle" not in ATOMIC_LEAF_KEYS


def test_atomic_leaf_keys_is_frozenset() -> None:
    assert isinstance(ATOMIC_LEAF_KEYS, frozenset)


@pytest.mark.parametrize(
    "color_key",
    ["backgroundColorStyle", "foregroundColorStyle", "borderColorStyle", "tabColorStyle"],
)
def test_any_color_style_is_atomic_parent_only(color_key: str) -> None:
    # Even with nested rgbColor present, the mask stops at the *ColorStyle parent — never
    # ...colorStyle.rgbColor (which Google rejects / partially-wipes).
    payload = {color_key: {"rgbColor": {"red": 1.0, "green": 0.0, "blue": 0.0}}}
    assert build_fields_mask(payload) == color_key


@pytest.mark.parametrize("atomic_key", ["numberFormat", "padding", "textRotation"])
def test_atomic_leaf_emits_parent_not_children(atomic_key: str) -> None:
    payload = {atomic_key: {"a": 1, "b": 2}}
    assert build_fields_mask(payload) == atomic_key


# --------------------------------------------------------------------------- #
# textFormat is NOT atomic — children mask individually
# --------------------------------------------------------------------------- #


def test_text_format_single_child_is_dotted() -> None:
    assert (
        build_fields_mask({"userEnteredFormat": {"textFormat": {"bold": True}}})
        == "userEnteredFormat.textFormat.bold"
    )


def test_text_format_multiple_children_is_group() -> None:
    payload = {"textFormat": {"bold": True, "italic": False, "fontSize": 10}}
    assert build_fields_mask(payload) == "textFormat(bold,italic,fontSize)"


def test_text_format_child_color_style_stays_atomic() -> None:
    # A *ColorStyle nested inside textFormat is still atomic (parent only).
    payload = {
        "textFormat": {
            "bold": True,
            "foregroundColorStyle": {"rgbColor": {"red": 0, "green": 0, "blue": 0}},
        }
    }
    assert build_fields_mask(payload) == "textFormat(bold,foregroundColorStyle)"


# --------------------------------------------------------------------------- #
# Dotted vs group selection
# --------------------------------------------------------------------------- #


def test_single_subfield_uses_dotted_concatenation() -> None:
    assert build_fields_mask({"a": {"b": {"c": 1}}}) == "a.b.c"


def test_multiple_subfields_use_group_syntax() -> None:
    assert build_fields_mask({"a": {"x": 1, "y": 2}}) == "a(x,y)"


def test_mixed_dotted_and_group_inside_group() -> None:
    # one child is a single-path (dotted), the other multi (group) -> commas at top level.
    payload = {"root": {"single": {"leaf": 1}, "multi": {"p": 1, "q": 2}}}
    assert build_fields_mask(payload) == "root(single.leaf,multi(p,q))"


def test_top_level_multiple_scalars() -> None:
    assert build_fields_mask({"a": 1, "b": 2, "c": 3}) == "a,b,c"


# --------------------------------------------------------------------------- #
# Insertion-order preservation (mask order mirrors payload order)
# --------------------------------------------------------------------------- #


def test_mask_preserves_insertion_order() -> None:
    payload = {
        "userEnteredFormat": {
            "numberFormat": {"type": "PERCENT"},
            "backgroundColorStyle": {"rgbColor": {"red": 1, "green": 1, "blue": 1}},
            "textFormat": {"bold": True},
        }
    }
    # Order must follow the payload, not be re-sorted alphabetically.
    assert (
        build_fields_mask(payload)
        == "userEnteredFormat(numberFormat,backgroundColorStyle,textFormat.bold)"
    )


# --------------------------------------------------------------------------- #
# note sibling token (cell-level field alongside userEnteredFormat)
# --------------------------------------------------------------------------- #


def test_note_sibling_alongside_format() -> None:
    payload = {
        "userEnteredFormat": {"textFormat": {"bold": True}},
        "note": "reviewed",
    }
    assert build_fields_mask(payload) == "userEnteredFormat.textFormat.bold,note"


def test_note_only() -> None:
    assert build_fields_mask({"note": "anything"}) == "note"


def test_empty_string_note_still_masked() -> None:
    # Clearing a note writes "" — a present (non-dict) leaf, so it must appear in the mask.
    assert build_fields_mask({"note": ""}) == "note"


# --------------------------------------------------------------------------- #
# Scalar / falsy / None leaf values are still present keys
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", [True, False, 0, "", None, [], 1.5, "CENTER"])
def test_non_dict_value_is_a_leaf_regardless_of_truthiness(value: object) -> None:
    # Presence — not truthiness — decides membership; False/0/""/None are explicit writes.
    assert build_fields_mask({"k": value}) == "k"


def test_list_value_is_a_leaf() -> None:
    payload = {"editors": {"users": ["a@example.com"]}}
    assert build_fields_mask(payload) == "editors.users"


# --------------------------------------------------------------------------- #
# Empty payload -> SheetsError("empty_payload")
# --------------------------------------------------------------------------- #


def test_empty_dict_raises_empty_payload() -> None:
    with pytest.raises(SheetsError) as exc_info:
        build_fields_mask({})
    assert exc_info.value.code == "empty_payload"


def test_empty_payload_error_str_format() -> None:
    with pytest.raises(SheetsError) as exc_info:
        build_fields_mask({})
    # str(SheetsError) == "<code>: <message>"
    assert str(exc_info.value).startswith("empty_payload: ")


def test_payload_of_only_empty_nested_dicts_raises() -> None:
    # {"userEnteredFormat": {}} has no concrete subfield to write -> refuse the no-op.
    with pytest.raises(SheetsError) as exc_info:
        build_fields_mask({"userEnteredFormat": {}})
    assert exc_info.value.code == "empty_payload"


def test_payload_of_nested_only_empty_dicts_raises() -> None:
    with pytest.raises(SheetsError) as exc_info:
        build_fields_mask({"a": {"b": {}}})
    assert exc_info.value.code == "empty_payload"


def test_non_dict_payload_raises_empty_payload() -> None:
    with pytest.raises(SheetsError) as exc_info:
        build_fields_mask(None)  # type: ignore[arg-type]
    assert exc_info.value.code == "empty_payload"


# --------------------------------------------------------------------------- #
# Empty nested dict alongside real fields is skipped, not emitted
# --------------------------------------------------------------------------- #


def test_empty_nested_dict_dropped_among_real_fields() -> None:
    payload = {
        "userEnteredFormat": {"textFormat": {"bold": True}, "padding": {}},  # padding empty
    }
    # padding is atomic -> but with empty dict it's treated as a leaf and emitted? No:
    # padding={} is an atomic key whose presence still means "set padding". It IS a leaf.
    # The atomic-leaf rule emits the key regardless of children, so padding appears.
    assert build_fields_mask(payload) == "userEnteredFormat(textFormat.bold,padding)"


def test_empty_non_atomic_nested_dict_dropped() -> None:
    payload = {"userEnteredFormat": {"textFormat": {"bold": True}, "gridProperties": {}}}
    # gridProperties is NOT atomic and is empty -> contributes nothing.
    assert build_fields_mask(payload) == "userEnteredFormat.textFormat.bold"


# --------------------------------------------------------------------------- #
# Purity: no transport imports pulled in by this module
# --------------------------------------------------------------------------- #


def test_module_source_has_no_transport_imports() -> None:
    """The module must not contain transport/CLI/pydantic/model IMPORT statements.

    We scan parsed import nodes (not raw text) so a docstring that merely names
    ``gsheets.models`` as a boundary note is not a false positive.
    """
    import ast

    import gsheets.core.fieldsmask as fm

    tree = ast.parse(Path(fm.__file__).read_text(encoding="utf-8"))
    forbidden_roots = {"fastmcp", "mcp", "argparse", "pydantic"}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            imported.add(node.module)
    assert not (forbidden_roots & imported), sorted(forbidden_roots & imported)
    assert "gsheets.models" not in imported
