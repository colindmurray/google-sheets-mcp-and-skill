"""Unit tests for ``gsheets.core.format`` (SPEC §1 — the shared output-format layer).

``render(result, fmt)`` is pure stdlib (``csv``, ``json``, ``io``). These tests pin:

* csv/tsv escaping round-trips: an embedded comma / quote / newline / tab in a cell survives a
  full ``csv.reader`` reparse (RFC-4180 quoting via the stdlib ``csv`` module);
* jsonl line framing: one record per line; an embedded newline in a value does NOT break the
  one-record-per-line invariant (the value is JSON-escaped);
* ``format_unsupported`` is raised (a clean ``SheetsError``) when a tabular format is asked for a
  structured/non-tabular result (``inspect``/``structure``/``read_conditional_formats``);
* multi-range csv/tsv emits each range as a block preceded by a ``# range: <A1>`` comment line,
  while a single-range read stays clean (no comment) so a pipe is plain CSV.

Pure test scaffolding: stdlib + ``pytest`` only; never imports ``fastmcp``/``mcp``/``argparse``.
"""

from __future__ import annotations

import csv
import importlib
import io
import json
import re

import pytest

from gsheets.core.errors import SheetsError

# ``core/__init__`` re-exports the ``format`` FUNCTION (from ``formatting.py``), which shadows
# the ``gsheets.core.format`` MODULE attribute (CPython IMPORT_FROM resolves the name to the
# function). Reach the real module object through ``sys.modules`` via ``import_module`` — mirrors
# the same dance in ``test_export.py`` for ``gsheets.core.export``.
fmtmod = importlib.import_module("gsheets.core.format")


# --------------------------------------------------------------------------- result builders


def _read_values(ranges, render="plain"):
    """A read_values-style result with the given ``[(a1, rows), ...]`` ranges."""
    return {
        "ok": True,
        "spreadsheetId": "<ID>",
        "render": render,
        "ranges": [{"range": a1, "values": rows} for a1, rows in ranges],
    }


_ESCAPE_ROWS = [
    ["Name", "Score", "Note"],
    ["Alice", "10", "ok"],
    ["Bob, Jr.", "20", 'has "quotes"'],
    ["multi\nline", "tab\tinside", "plain"],
]


# =========================================================================== csv / tsv


class TestCsvTsv:
    def test_single_range_csv_is_clean_no_comment(self):
        out = fmtmod.render(_read_values([("Data", _ESCAPE_ROWS)]), "csv")
        # A single-range read is plain RFC-4180 CSV — no "# range:" header line.
        assert not out.startswith("#")

    def test_csv_escaping_round_trips_via_csv_reader(self):
        out = fmtmod.render(_read_values([("Data", _ESCAPE_ROWS)]), "csv")
        reparsed = list(csv.reader(io.StringIO(out)))
        # Every cell survives a full reparse, including comma/quote/newline.
        assert reparsed == [[str(c) for c in row] for row in _ESCAPE_ROWS]

    def test_tsv_escaping_round_trips_via_csv_reader(self):
        out = fmtmod.render(_read_values([("Data", _ESCAPE_ROWS)]), "tsv")
        reparsed = list(csv.reader(io.StringIO(out), delimiter="\t"))
        assert reparsed == [[str(c) for c in row] for row in _ESCAPE_ROWS]

    def test_csv_embedded_comma_and_quote_are_quoted(self):
        out = fmtmod.render(
            _read_values([("Data", [["Bob, Jr.", 'has "quotes"']])]), "csv"
        )
        assert out == '"Bob, Jr.","has ""quotes"""\r\n'

    def test_multi_range_csv_emits_range_comment_blocks(self):
        result = _read_values(
            [("Sheet1!A1:B1", [["a", "b"]]), ("Sheet1!D1:E1", [["c", "d"]])]
        )
        out = fmtmod.render(result, "csv")
        assert "# range: Sheet1!A1:B1" in out
        assert "# range: Sheet1!D1:E1" in out
        # The two blocks appear in order.
        assert out.index("Sheet1!A1:B1") < out.index("Sheet1!D1:E1")
        # The data rows survive between the comment markers.
        assert "a,b" in out
        assert "c,d" in out

    def test_multi_range_blocks_are_reparseable_per_block(self):
        result = _read_values(
            [("S!A1:B1", [["a", "b"]]), ("S!D1:E1", [["c", "d"]])]
        )
        out = fmtmod.render(result, "csv")
        # Split on the comment markers; each block reparses cleanly with csv.reader.
        blocks = [b for b in out.split("# range: ") if b.strip()]
        assert len(blocks) == 2
        # Each block: first line is the A1 label, then CSV rows.
        for label, rows in (("S!A1:B1", [["a", "b"]]), ("S!D1:E1", [["c", "d"]])):
            block = next(b for b in blocks if b.startswith(label))
            body = block.split("\n", 1)[1]
            reparsed = [r for r in csv.reader(io.StringIO(body)) if r]
            assert reparsed == rows

    def test_csv_on_structured_result_raises_format_unsupported(self):
        structured = {"ok": True, "spreadsheetId": "<ID>", "cells": [{"a1": "A1"}]}
        with pytest.raises(SheetsError) as exc:
            fmtmod.render(structured, "csv")
        assert exc.value.code == "format_unsupported"
        assert exc.value.hint

    def test_tsv_on_conditional_formats_result_raises(self):
        structured = {"ok": True, "spreadsheetId": "<ID>", "sheets": [{"sheet": "S"}]}
        with pytest.raises(SheetsError) as exc:
            fmtmod.render(structured, "tsv")
        assert exc.value.code == "format_unsupported"

    def test_empty_range_csv_is_empty_string(self):
        out = fmtmod.render(_read_values([("Data", [])]), "csv")
        assert out == ""


# =========================================================================== json


class TestJson:
    def test_json_is_pretty_full_dict(self):
        result = _read_values([("Data", [["a", "b"]])])
        out = fmtmod.render(result, "json")
        assert json.loads(out) == result
        # Pretty-printed (indent=2) → contains newlines and indentation.
        assert "\n" in out
        assert '  "ok": true' in out

    def test_json_preserves_non_ascii(self):
        result = {"ok": True, "spreadsheetId": "<ID>", "title": "café"}
        out = fmtmod.render(result, "json")
        # ensure_ascii=False keeps the literal character (token-efficient).
        assert "café" in out


# =========================================================================== jsonl


class TestJsonl:
    def test_read_values_jsonl_one_record_per_row(self):
        result = _read_values([("S!A1:B2", [["a", "b"], ["c", "d"]])])
        out = fmtmod.render(result, "jsonl")
        lines = [json.loads(line) for line in out.splitlines() if line]
        assert lines == [
            {"range": "S!A1:B2", "row": ["a", "b"]},
            {"range": "S!A1:B2", "row": ["c", "d"]},
        ]

    def test_read_values_jsonl_multi_range_records_carry_range(self):
        result = _read_values(
            [("S!A1:A1", [["x"]]), ("S!C1:C1", [["y"]])]
        )
        out = fmtmod.render(result, "jsonl")
        lines = [json.loads(line) for line in out.splitlines() if line]
        assert lines == [
            {"range": "S!A1:A1", "row": ["x"]},
            {"range": "S!C1:C1", "row": ["y"]},
        ]

    def test_jsonl_embedded_newline_does_not_break_lines(self):
        # A value containing a literal newline must NOT split one record across two physical
        # lines — json escapes it as \n inside the string.
        result = _read_values([("S!A1:A1", [["line1\nline2"]])])
        out = fmtmod.render(result, "jsonl")
        physical = [l for l in out.splitlines() if l]
        assert len(physical) == 1
        assert json.loads(physical[0]) == {"range": "S!A1:A1", "row": ["line1\nline2"]}

    def test_jsonl_list_result_one_element_per_line(self):
        # A list-shaped result (e.g. read_many) emits one top-level element per line.
        result = {
            "ok": True,
            "mode": "summary",
            "count": 2,
            "results": [
                {"ok": True, "spreadsheetId": "A", "title": "Alpha"},
                {"ok": False, "spreadsheetId": "B", "error": {"code": "x"}},
            ],
        }
        out = fmtmod.render(result, "jsonl")
        lines = [json.loads(line) for line in out.splitlines() if line]
        assert lines == result["results"]

    def test_jsonl_comments_list_one_per_line(self):
        result = {
            "ok": True,
            "spreadsheetId": "<ID>",
            "comments": [
                {"id": "A", "content": "one"},
                {"id": "B", "content": "two\nwith newline"},
            ],
        }
        out = fmtmod.render(result, "jsonl")
        lines = [json.loads(line) for line in out.splitlines() if line]
        assert lines == result["comments"]


# =========================================================================== misc / guards


class TestRenderGuards:
    def test_unknown_format_raises(self):
        with pytest.raises(SheetsError) as exc:
            fmtmod.render(_read_values([("D", [["a"]])]), "yaml")
        assert exc.value.code in ("format_unsupported", "bad_format")

    def test_text_is_not_handled_here(self):
        # ``text`` is the adapters' terse renderer, not core.format's job (SPEC §1.5).
        with pytest.raises(SheetsError):
            fmtmod.render(_read_values([("D", [["a"]])]), "text")

    def test_supported_tuple_present(self):
        # The module advertises its supported data formats.
        assert "csv" in fmtmod.SUPPORTED
        assert "tsv" in fmtmod.SUPPORTED
        assert "json" in fmtmod.SUPPORTED
        assert "jsonl" in fmtmod.SUPPORTED
        # markdown is wired in (Phase 5 / SPEC §6, D-MD).
        assert "markdown" in fmtmod.SUPPORTED


# =========================================================================== markdown (§6, D-MD)


def _unescape_md_cell(cell: str) -> str:
    """Reverse the markdown-table cell escaping (``\\\\`` / ``\\n`` / ``\\|``) for round-trip asserts.

    The renderer escapes a backslash as ``\\\\``, an embedded newline as the two-char ``\\n``, and a
    pipe as ``\\|``. Reversing it must honor the backslash-escape so ``"a\\\\nb"`` (literal
    backslash-n) is NOT mistaken for a newline. A small state machine over the characters does that.
    """
    out: list[str] = []
    i = 0
    while i < len(cell):
        ch = cell[i]
        if ch == "\\" and i + 1 < len(cell):
            nxt = cell[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "\\":
                out.append("\\")
            elif nxt == "|":
                out.append("|")
            else:
                out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_md_table(out: str) -> list[list[str]]:
    """Parse a GitHub markdown table back into rows of (unescaped) cells, dropping the rule row.

    Splits each physical line on UNescaped ``|`` (a ``\\|`` is a literal pipe inside a cell, not a
    column separator), strips the leading/trailing empty cells from the pipe-bracketed row, drops the
    ``|---|---|`` separator row, and unescapes each cell. This proves the rendering is unambiguous:
    every embedded ``|`` / newline survives the reparse.
    """
    rows: list[list[str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # Split on a pipe that is NOT preceded by a backslash.
        cells = re.split(r"(?<!\\)\|", line)
        # A markdown row is bracketed by pipes -> leading/trailing empty strings; drop them.
        cells = cells[1:-1]
        # The header-rule row is all dashes -> skip it.
        if cells and all(set(c.strip()) <= {"-"} and c.strip() for c in cells):
            continue
        rows.append([_unescape_md_cell(c.strip()) for c in cells])
    return rows


class TestMarkdownTable:
    """``render(result, "markdown")`` over a rectangular value grid -> a GitHub markdown table.

    The headline escaping test: a literal ``|`` and a literal newline inside a cell must NOT
    corrupt the table — they must be escaped so the rendering is unambiguous and reparses to the
    original cells. (tabulate does NOT escape either, so this is a custom renderer — SPEC §6.)
    """

    def test_markdown_table_basic_shape(self):
        result = _read_values([("Data", [["a", "b"], ["c", "d"]])])
        out = fmtmod.render(result, "markdown")
        lines = out.splitlines()
        # Header row, separator (---) row, then the data rows.
        assert lines[0].startswith("|") and lines[0].endswith("|")
        assert set(lines[1].replace("|", "").replace(" ", "")) == {"-"}
        assert "| a" in lines[0]

    def test_markdown_table_embedded_pipe_is_escaped_and_unambiguous(self):
        # A literal "|" in a cell must NOT read as a column separator.
        result = _read_values([("Data", [["Name", "Note"], ["a|b", "ok"]])])
        out = fmtmod.render(result, "markdown")
        # The raw pipe is escaped in the body.
        assert r"a\|b" in out
        # Reparsing yields exactly two columns on the data row (not three).
        rows = _parse_md_table(out)
        assert rows == [["Name", "Note"], ["a|b", "ok"]]

    def test_markdown_table_embedded_newline_is_escaped_and_unambiguous(self):
        # A literal newline must NOT split one record across two physical lines.
        result = _read_values([("Data", [["Name", "Note"], ["multi\nline", "x"]])])
        out = fmtmod.render(result, "markdown")
        # Header rule + header + one data row = 3 physical lines (the newline did not add a line).
        assert len(out.splitlines()) == 3
        assert r"multi\nline" in out
        rows = _parse_md_table(out)
        assert rows == [["Name", "Note"], ["multi\nline", "x"]]

    def test_markdown_table_pipe_and_newline_together_round_trip(self):
        # The literal "|" and newline in ONE cell (the SPEC's stress case) round-trip cleanly.
        result = _read_values([("Data", [["h1", "h2"], ["a|b\nc", "plain"]])])
        out = fmtmod.render(result, "markdown")
        rows = _parse_md_table(out)
        assert rows == [["h1", "h2"], ["a|b\nc", "plain"]]

    def test_markdown_table_backslash_is_escaped(self):
        # A literal backslash must be escaped so "a\nb" (backslash-n) is not read as a newline.
        result = _read_values([("Data", [["h"], ["a\\nb"]])])
        out = fmtmod.render(result, "markdown")
        rows = _parse_md_table(out)
        assert rows == [["h"], ["a\\nb"]]

    def test_markdown_table_jagged_rows_padded(self):
        # Short rows pad to the widest row's column count (so the table is rectangular).
        result = _read_values([("Data", [["a", "b", "c"], ["x"]])])
        out = fmtmod.render(result, "markdown")
        rows = _parse_md_table(out)
        assert rows == [["a", "b", "c"], ["x", "", ""]]

    def test_markdown_on_structured_result_falls_back_to_kv(self):
        # Unlike csv/tsv (which require a grid), "markdown" works on any read: a structured result
        # has no rectangular grid, so it renders as the markdown KEY/VALUE form instead of erroring.
        structured = {
            "ok": True,
            "spreadsheetId": "<ID>",
            "comments": [{"id": "A", "content": "hi"}],
        }
        out = fmtmod.render(structured, "markdown")
        # KV shape, not a "| ... |" table.
        assert "id: A" in out
        assert "content: hi" in out

    def test_markdown_table_empty_grid_is_empty_string(self):
        out = fmtmod.render(_read_values([("Data", [])]), "markdown")
        assert out == ""

    def test_markdown_table_multi_range_emits_range_headers(self):
        result = _read_values(
            [("S!A1:B1", [["a", "b"]]), ("S!D1:E1", [["c", "d"]])]
        )
        out = fmtmod.render(result, "markdown")
        # Each range gets a markdown heading so the blocks are distinguishable.
        assert "S!A1:B1" in out
        assert "S!D1:E1" in out
        assert out.index("S!A1:B1") < out.index("S!D1:E1")


class TestMarkdownKV:
    """``render_kv(result)`` — markdown key/value lines (one ``field: value`` per cell/record).

    The structured (non-tabular) counterpart to the markdown table: a small custom renderer with
    collision-resistant newline escaping. Used where a record/cell view reads better than a grid.
    """

    def test_kv_renders_field_value_lines(self):
        result = {
            "ok": True,
            "spreadsheetId": "<ID>",
            "comments": [
                {"id": "A", "author": "Jane", "content": "hi"},
            ],
        }
        out = fmtmod.render_kv(result)
        assert "id: A" in out
        assert "author: Jane" in out
        assert "content: hi" in out

    def test_kv_escapes_embedded_newline(self):
        # A newline inside a value must NOT break the one-field-per-line invariant.
        result = {
            "ok": True,
            "spreadsheetId": "<ID>",
            "comments": [{"id": "A", "content": "line1\nline2"}],
        }
        out = fmtmod.render_kv(result)
        # The value's newline is escaped to the two-char \n; no physical line break inside it.
        assert r"content: line1\nline2" in out
        # The content line is a single physical line.
        content_lines = [l for l in out.splitlines() if l.startswith("content:")]
        assert content_lines == [r"content: line1\nline2"]

    def test_kv_records_separated(self):
        # Multiple records render as separate blocks (a blank line between them).
        result = {
            "ok": True,
            "spreadsheetId": "<ID>",
            "comments": [
                {"id": "A", "content": "one"},
                {"id": "B", "content": "two"},
            ],
        }
        out = fmtmod.render_kv(result)
        blocks = [b for b in out.split("\n\n") if b.strip()]
        assert len(blocks) == 2
        assert "id: A" in blocks[0]
        assert "id: B" in blocks[1]

    def test_kv_round_trips_value(self):
        # The escaped value reverses cleanly (collision-resistant: backslash-escaped).
        result = {
            "ok": True,
            "spreadsheetId": "<ID>",
            "rows": [{"note": "a\\nb\nreal-newline"}],
        }
        out = fmtmod.render_kv(result)
        line = next(l for l in out.splitlines() if l.startswith("note:"))
        value = line[len("note: "):]
        assert _unescape_md_cell(value) == "a\\nb\nreal-newline"

    def test_kv_via_render_dispatch(self):
        # "markdown" on a structured result routes to KV (no error), since a table needs a grid.
        result = {
            "ok": True,
            "spreadsheetId": "<ID>",
            "comments": [{"id": "A", "content": "hi"}],
        }
        # render() with markdown on a tabular result -> table; this is the KV helper directly.
        out = fmtmod.render_kv(result)
        assert "id: A" in out


# =========================================================================== address-keyed (§4.4)


class TestRenderAddressed:
    """``render_addressed(cells)`` — the address-keyed rendering for SPARSE data (SPEC §4.4).

    A list of per-cell dicts (each carrying ``a1`` plus optional ``value``/``formula``/``note``/
    ``validation``) becomes one ``"<A1>: <body>"`` line per NON-empty cell — the natural shape for
    a sparse formula/format/note read (an inverted index), versus the dense rectangle+range.
    """

    def test_formula_cell_renders_address_keyed_line(self):
        cells = [{"a1": "C5", "formula": "=SUM(A5:B5)", "value": "12"}]
        out = fmtmod.render_addressed(cells)
        assert out == "C5: =SUM(A5:B5)"

    def test_value_only_cell_renders_value(self):
        cells = [{"a1": "A1", "value": "hello"}]
        assert fmtmod.render_addressed(cells) == "A1: hello"

    def test_empty_cells_are_skipped(self):
        # A padded blank cell (bare {"a1": ...}) contributes no line.
        cells = [
            {"a1": "A1"},
            {"a1": "A2", "value": "x"},
            {"a1": "A3"},
        ]
        assert fmtmod.render_addressed(cells) == "A2: x"

    def test_multiple_cells_one_line_each(self):
        cells = [
            {"a1": "C5", "formula": "=SUM(A5:B5)", "value": "3"},
            {"a1": "C6", "formula": "=SUM(A6:B6)", "value": "7"},
        ]
        out = fmtmod.render_addressed(cells)
        assert out.splitlines() == ["C5: =SUM(A5:B5)", "C6: =SUM(A6:B6)"]

    def test_note_and_validation_appended(self):
        cells = [{"a1": "D7", "value": "Yes", "note": "pick one", "validation": "ONE_OF_LIST(Yes,No)"}]
        out = fmtmod.render_addressed(cells)
        assert out.startswith("D7: Yes")
        assert "[ONE_OF_LIST(Yes,No)]" in out
        assert "note=" in out

    def test_empty_list_is_empty_string(self):
        assert fmtmod.render_addressed([]) == ""

    def test_addressed_records_are_dicts(self):
        # The jsonl-friendly record form keys by a1 (one record per non-empty cell).
        cells = [{"a1": "C5", "formula": "=SUM(A5:B5)"}, {"a1": "C6"}]
        records = fmtmod.addressed_records(cells)
        assert records == [{"a1": "C5", "formula": "=SUM(A5:B5)"}]


class TestAddressedGrid:
    """``cells_from_value_grid`` + the sparse render glue for ``read_values`` (SPEC §4.4).

    A ``read_values`` result is a rectangular grid anchored at the requested range's top-left.
    For a SPARSE read (a formula read, or ``diff_only`` computed holes) the address-keyed form is
    the natural rendering — these helpers compute each cell's absolute A1 from the range anchor so
    the rectangle becomes ``"<A1>: <formula>"`` lines (empties dropped).
    """

    def test_cells_from_value_grid_computes_absolute_a1(self):
        # Anchor at C5 (sheet-qualified). A 2x1 formula grid → C5/C6 with their formulas.
        cells = fmtmod.cells_from_value_grid(
            "Sheet1!C5:C6", [["=SUM(A5:B5)"], ["=SUM(A6:B6)"]]
        )
        assert cells == [
            {"a1": "Sheet1!C5", "value": "=SUM(A5:B5)"},
            {"a1": "Sheet1!C6", "value": "=SUM(A6:B6)"},
        ]

    def test_cells_from_value_grid_skips_blanks_in_keys(self):
        # A blank cell still gets an a1 (so consumers can index), but render_addressed drops it.
        cells = fmtmod.cells_from_value_grid("S!A1:B1", [["=X", ""]])
        rendered = fmtmod.render_addressed(cells)
        assert rendered == "S!A1: =X"

    def test_render_sparse_values_text(self):
        result = {
            "ok": True,
            "spreadsheetId": "<ID>",
            "render": "formula",
            "ranges": [
                {"range": "S!C5:C6", "values": [["=SUM(A5:B5)"], ["=SUM(A6:B6)"]]}
            ],
        }
        out = fmtmod.render_sparse_values(result)
        assert out.splitlines() == ["S!C5: =SUM(A5:B5)", "S!C6: =SUM(A6:B6)"]


# =========================================================================== purity guard


class TestPurity:
    def test_module_imports_no_transport(self):
        import sys

        import gsheets.core.format  # noqa: F401

        forbidden = ("fastmcp", "mcp", "argparse", "pydantic", "gsheets.models")
        src = sys.modules["gsheets.core.format"].__dict__
        for name in src.values():
            mod = getattr(name, "__module__", "")
            assert not any(
                mod.startswith(f) for f in forbidden if isinstance(mod, str)
            )
