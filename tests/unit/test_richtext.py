"""Unit tests for rich-text run + cell-hyperlink serialization (DESIGN §X.0a / §X.1).

Golden-master style: representative Sheets-API ``textFormatRuns`` JSON in, assert the exact
flat per-run struct list AND the exact terse ``runs`` line out. Plus targeted cases for the
omit-when-absent rules, the run-level-link-takes-precedence contract, defensive bounds, and
input validation.

No network: :func:`serialize_text_runs` / :func:`text_runs_line` are pure and serviceless —
they take already-resolved Google JSON (runs + the cell's plain text) and return plain
dicts/lists. The owning read fn (``reads.inspect``) holds the ``SheetsServices`` handle and the
GridRange->A1 resolution happens there, not here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gsheets.core import richtext
from gsheets.core.errors import SheetsError
from gsheets.core.richtext import serialize_text_runs, text_runs_line

GOLDEN_DIR = Path(__file__).parent / "golden"


def load_golden(name: str) -> dict:
    """Load a committed golden rich-text fixture."""
    return json.loads((GOLDEN_DIR / name).read_text())


# ---------------------------------------------------------------------------
# Golden-master: representative multi-run cell JSON -> exact structs + line.
# ---------------------------------------------------------------------------


def test_serialize_runs_golden():
    fix = load_golden("richtext_runs.json")
    runs = serialize_text_runs(fix["textFormatRuns"], fix["full_text"])
    assert runs == fix["expected_runs"]


def test_runs_line_golden():
    fix = load_golden("richtext_runs.json")
    runs = serialize_text_runs(fix["textFormatRuns"], fix["full_text"])
    assert text_runs_line("A1", runs) == fix["expected_line"]


# ---------------------------------------------------------------------------
# Per-run struct shape: start, substring slicing, format flatten, link.
# ---------------------------------------------------------------------------


def test_first_run_start_defaults_to_zero_when_omitted():
    # Google omits ``startIndex`` on a run that begins at offset 0.
    runs = serialize_text_runs([{"format": {"bold": True}}], "hello")
    assert runs == [{"start": 0, "text": "hello", "format": {"bold": True}}]


def test_substring_is_sliced_between_consecutive_starts():
    runs = serialize_text_runs(
        [
            {"startIndex": 0, "format": {"bold": True}},
            {"startIndex": 5, "format": {"italic": True}},
            {"startIndex": 8, "format": {}},
        ],
        "ABCDEFGHIJ",
    )
    assert [r["text"] for r in runs] == ["ABCDE", "FGH", "IJ"]
    assert [r["start"] for r in runs] == [0, 5, 8]


def test_run_format_is_flattened_textformat_subset():
    # fontSize/fontFamily/underline/strikethrough/fg all lift to the flat subset; the run's
    # ``link`` inside the TextFormat is NOT surfaced as a format key (it becomes ``link``).
    runs = serialize_text_runs(
        [
            {
                "format": {
                    "underline": True,
                    "strikethrough": True,
                    "fontSize": 14,
                    "fontFamily": "Arial",
                    "foregroundColorStyle": {
                        "rgbColor": {"red": 0.0, "green": 0.0, "blue": 0.0}
                    },
                    "link": {"uri": "https://link"},
                }
            }
        ],
        "styled",
    )
    assert runs == [
        {
            "start": 0,
            "text": "styled",
            "format": {
                "underline": True,
                "strikethrough": True,
                "fontSize": 14,
                "fontFamily": "Arial",
                "fg": "#000000",
            },
            "link": "https://link",
        }
    ]


def test_run_with_no_style_omits_format_key():
    # A run whose TextFormat carries no displayable styling omits ``format`` entirely.
    runs = serialize_text_runs([{"format": {}}], "plain")
    assert runs == [{"start": 0, "text": "plain"}]
    assert "format" not in runs[0]


def test_run_with_no_link_omits_link_key():
    runs = serialize_text_runs([{"format": {"bold": True}}], "x")
    assert "link" not in runs[0]


def test_empty_link_dict_or_blank_uri_omits_link():
    runs = serialize_text_runs(
        [
            {"startIndex": 0, "format": {"link": {}}},
            {"startIndex": 1, "format": {"link": {"uri": ""}}},
        ],
        "ab",
    )
    assert all("link" not in r for r in runs)


def test_run_link_non_dict_format_returns_none():
    # ``_run_link`` is the defensive link extractor: handed a non-dict (a degenerate run whose
    # ``format`` was a scalar/list), it must return ``None`` rather than raise, so a malformed
    # run never crashes the surrounding read.
    assert richtext._run_link("not-a-dict") is None
    assert richtext._run_link(None) is None


def test_run_link_non_dict_link_value_returns_none():
    # ``format.link`` present but not a dict (e.g. Google handed back a bare string) -> None.
    assert richtext._run_link({"link": "https://x"}) is None


# ---------------------------------------------------------------------------
# Run-level link takes precedence over cell hyperlink (the headline contract).
# ---------------------------------------------------------------------------


def test_run_level_links_recover_multi_link_cell():
    # A multi-link cell: each run carries its own link; the cell-level hyperlink (handled by the
    # caller) would be EMPTY for this cell, so these per-run links are the only recovery path.
    runs = serialize_text_runs(
        [
            {"startIndex": 0, "format": {"link": {"uri": "https://a"}}},
            {"startIndex": 6, "format": {"link": {"uri": "https://b"}}},
        ],
        "alpha beta",
    )
    assert [r.get("link") for r in runs] == ["https://a", "https://b"]
    assert runs[0]["text"] == "alpha "
    assert runs[1]["text"] == "beta"


# ---------------------------------------------------------------------------
# Defensive bounds + ordering.
# ---------------------------------------------------------------------------


def test_out_of_order_runs_are_sorted_by_start():
    runs = serialize_text_runs(
        [
            {"startIndex": 5, "format": {"italic": True}},
            {"startIndex": 0, "format": {"bold": True}},
        ],
        "ABCDEFGH",
    )
    assert [r["start"] for r in runs] == [0, 5]
    assert [r["text"] for r in runs] == ["ABCDE", "FGH"]


def test_start_beyond_text_length_yields_empty_substring():
    # A run whose start runs past the (shortened) text never raises and yields empty text.
    runs = serialize_text_runs([{"startIndex": 0}, {"startIndex": 50}], "short")
    assert runs[0]["text"] == "short"
    assert runs[1]["text"] == ""


def test_none_full_text_yields_empty_substrings():
    runs = serialize_text_runs([{"startIndex": 0, "format": {"bold": True}}], None)
    assert runs == [{"start": 0, "text": "", "format": {"bold": True}}]


def test_explicit_null_start_index_coerced_to_zero():
    # Google may emit ``"startIndex": null`` on the first run (the field is present-but-null,
    # not absent). The serializer must coerce an explicit ``None`` to 0 (not crash, not treat
    # it as a negative/invalid offset) so run 0 still starts at the head of the text.
    runs = serialize_text_runs([{"startIndex": None, "format": {"italic": True}}], "hello")
    assert runs == [{"start": 0, "text": "hello", "format": {"italic": True}}]


def test_explicit_null_start_index_only_first_run_slices_whole_text():
    # Two runs where the leading run carries an explicit null startIndex: it must behave exactly
    # like an omitted startIndex (start 0), so the slice boundary to the next run is unchanged.
    runs = serialize_text_runs(
        [
            {"startIndex": None, "format": {"bold": True}},
            {"startIndex": 5, "format": {"italic": True}},
        ],
        "ABCDEFGH",
    )
    assert [r["start"] for r in runs] == [0, 5]
    assert [r["text"] for r in runs] == ["ABCDE", "FGH"]


# ---------------------------------------------------------------------------
# Empty / None input -> [] (caller then omits the cell ``runs`` key).
# ---------------------------------------------------------------------------


def test_none_runs_returns_empty_list():
    assert serialize_text_runs(None, "anything") == []


def test_empty_runs_returns_empty_list():
    assert serialize_text_runs([], "anything") == []


# ---------------------------------------------------------------------------
# Input validation -> SheetsError (never returns an error dict).
# ---------------------------------------------------------------------------


def test_non_list_runs_raises():
    with pytest.raises(SheetsError) as exc:
        serialize_text_runs({"startIndex": 0}, "x")
    assert exc.value.code == "bad_text_runs"


def test_non_dict_run_raises():
    with pytest.raises(SheetsError) as exc:
        serialize_text_runs(["not a dict"], "x")
    assert exc.value.code == "bad_text_runs"


def test_negative_start_index_raises():
    with pytest.raises(SheetsError) as exc:
        serialize_text_runs([{"startIndex": -1}], "x")
    assert exc.value.code == "bad_text_runs"


def test_bool_start_index_raises():
    # bool is an int subclass; True/False must not be accepted as an offset.
    with pytest.raises(SheetsError) as exc:
        serialize_text_runs([{"startIndex": True}], "x")
    assert exc.value.code == "bad_text_runs"


def test_non_dict_format_raises():
    with pytest.raises(SheetsError) as exc:
        serialize_text_runs([{"startIndex": 0, "format": "bad"}], "x")
    assert exc.value.code == "bad_text_runs"


# ---------------------------------------------------------------------------
# Terse line renderer details.
# ---------------------------------------------------------------------------


def test_text_runs_line_empty_runs_is_empty_string():
    assert text_runs_line("A1", []) == ""


def test_text_runs_line_single_plain_run():
    runs = serialize_text_runs([{"format": {}}], "hello")
    assert text_runs_line("B2", runs) == 'runs B2: "hello"[0:5]'


def test_text_runs_line_bg_fg_and_styles_order():
    # bg before fg before the boolean style flags (canonical condformat-style order).
    runs = [
        {
            "start": 0,
            "text": "x",
            "format": {
                "bold": True,
                "italic": True,
                "underline": True,
                "strikethrough": True,
                "fg": "#1155CC",
                "bg": "#FFFF00",
            },
            "link": "https://x",
        }
    ]
    assert (
        text_runs_line("A1", runs)
        == 'runs A1: "x"[0:1 bg #FFFF00 fg #1155CC bold italic underline strike link https://x]'
    )


# ---------------------------------------------------------------------------
# Boundary purity: the serializer must not pull a transport/pydantic module.
# (A focused in-module check; the repo-wide subprocess guard is the source of truth.)
# ---------------------------------------------------------------------------


def test_module_imports_no_transport_symbols():
    import ast

    import gsheets.core.richtext as mod

    forbidden_roots = {"fastmcp", "mcp", "argparse", "pydantic"}
    tree = ast.parse(Path(mod.__file__).read_text())
    imported_roots: set[str] = set()
    imported_from: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_from.add(node.module)
            imported_roots.add(node.module.split(".")[0])

    assert forbidden_roots.isdisjoint(imported_roots)
    # Never import the adapter-side pydantic mirror models from pure core.
    assert not any(m == "gsheets.models" or m.endswith(".models") for m in imported_from)
