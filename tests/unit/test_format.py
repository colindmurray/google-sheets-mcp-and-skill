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
        # The module advertises its supported data formats (markdown gated to a later phase).
        assert "csv" in fmtmod.SUPPORTED
        assert "tsv" in fmtmod.SUPPORTED
        assert "json" in fmtmod.SUPPORTED
        assert "jsonl" in fmtmod.SUPPORTED


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
