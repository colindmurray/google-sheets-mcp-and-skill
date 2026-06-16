"""Tests for the MCP-only ``out_path`` file-output escape valve (SPEC §2).

The big-read tools (``sheets_read_values`` / ``sheets_inspect`` / ``sheets_describe`` /
``sheets_formula_patterns`` / ``sheets_read_many``) gain
an optional ``out_path``. When set, the adapter writes ``render(result, output_format)`` to that
local file (utf-8) and returns a small HANDLE ``{ok, path, format, rows, cols, bytes, preview}``
INSTEAD of the payload — so a large read costs a handful of tokens, not 400 KB of grid in context.
These tests pin (via the adapter's ``_call_formatted`` body and the tool input schemas):

* the handle shape + that the bytes on disk equal ``render(result, fmt)``;
* ``bad_out_path`` on a missing parent directory (surfaced as a clean ``ToolError``);
* credential-path refusal (surfaced as ``ToolError``);
* preview truncation (first ~5 rows/records, never the whole grid);
* the tools advertise ``out_path`` (optional, default null) and stay ``readOnlyHint=True`` (the
  local write is a caller-named opt-in side effect — SPEC §2.4 / D-ANNOT).

This is an ADAPTER test (it imports ``fastmcp``/``pydantic`` by design); the subprocess boundary
guard in ``test_boundary_guard.py`` keeps the pure-core import clean independently.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult

import gsheets.mcp_server as srv


def _tools() -> dict:
    return asyncio.run(srv.mcp.get_tools())


def _read_values_payload(rows):
    return {
        "ok": True,
        "spreadsheetId": "<ID>",
        "render": "plain",
        "ranges": [{"range": "S!A1:B2", "values": rows}],
    }


def _handle(tool_result: ToolResult) -> dict:
    """Parse the file-output handle out of a ToolResult's JSON-string body.

    The out_path branch returns the handle as a JSON string body (the same ToolResult(content=...)
    shape the data-format branch uses) so it never collides with the tool's declared output_schema.
    """
    return json.loads(tool_result.content[0].text)


# ----------------------------------------------------------- input-schema surface (SPEC §2.2/§2.4)


@pytest.mark.parametrize(
    "name",
    [
        "sheets_read_values",
        "sheets_inspect",
        "sheets_describe",
        "sheets_formula_patterns",
        "sheets_read_many",
    ],
)
def test_out_path_exposed_optional_default_null(name):
    props = _tools()[name].parameters.get("properties", {})
    required = _tools()[name].parameters.get("required", [])
    assert "out_path" in props
    assert "out_path" not in required


@pytest.mark.parametrize(
    "name",
    [
        "sheets_read_values",
        "sheets_inspect",
        "sheets_describe",
        "sheets_formula_patterns",
        "sheets_read_many",
    ],
)
def test_out_path_tools_stay_read_only(name):
    # D-ANNOT: the local write is a caller-named opt-in side effect; these tools modify no remote
    # state, so they keep readOnlyHint=True (the side effect is documented in the docstring).
    ann = _tools()[name].annotations
    assert ann.readOnlyHint is True


@pytest.mark.parametrize(
    "name",
    [
        "sheets_read_values",
        "sheets_inspect",
        "sheets_describe",
        "sheets_formula_patterns",
        "sheets_read_many",
    ],
)
def test_out_path_documented_in_docstring(name):
    desc = _tools()[name].description or ""
    assert "out_path" in desc


def test_export_does_not_gain_out_path():
    # export already writes files via its own ``path`` arg; it must NOT also grow ``out_path``.
    props = _tools()["sheets_export"].parameters.get("properties", {})
    assert "out_path" not in props


def test_overview_and_cf_have_no_out_path():
    # SPEC §2.2: out_path is on the BIG reads only — not overview / read_conditional_formats.
    for name in ("sheets_overview", "sheets_read_conditional_formats"):
        props = _tools()[name].parameters.get("properties", {})
        assert "out_path" not in props


# ----------------------------------------------------------- _call_formatted out_path behavior


def test_call_formatted_out_path_writes_file_and_returns_handle(tmp_path):
    rows = [["a", "b"], ["c", "d"]]
    payload = _read_values_payload(rows)
    target = tmp_path / "out.csv"
    out = srv._call_formatted(
        srv.models.ReadValuesResult,
        lambda s, sid, rngs, **kw: payload,
        "csv",
        object(),
        "<ID>",
        ["S!A1:B2"],
        out_path=str(target),
    )
    # The tool returns a HANDLE (a ToolResult carrying the small handle), NOT the full payload.
    assert isinstance(out, ToolResult)
    handle = _handle(out)
    assert handle["ok"] is True
    assert handle["path"] == str(target.resolve())
    assert handle["format"] == "csv"
    assert handle["rows"] == 2
    assert handle["cols"] == 2
    assert handle["preview"] == rows
    # Bytes on disk == render(result, fmt) (compare raw bytes; csv is \r\n-terminated).
    from gsheets.core.format import render

    assert target.read_bytes() == render(payload, "csv").encode("utf-8")
    assert handle["bytes"] == target.stat().st_size


def test_call_formatted_out_path_preview_truncates(tmp_path):
    rows = [[str(i), str(i * 2)] for i in range(50)]
    payload = _read_values_payload(rows)
    target = tmp_path / "big.csv"
    out = srv._call_formatted(
        srv.models.ReadValuesResult,
        lambda s, sid, rngs, **kw: payload,
        "csv",
        object(),
        "<ID>",
        ["S!A1:B2"],
        out_path=str(target),
    )
    handle = _handle(out)
    assert handle["rows"] == 50
    assert len(handle["preview"]) == 5
    assert handle["preview"] == rows[:5]


def test_call_formatted_out_path_text_format_still_writes(tmp_path):
    # out_path with the DEFAULT output_format (text) still produces a file — text falls back to
    # a serializable data format under file output (it must not silently no-op). We write json so
    # the on-disk content is well-formed and round-trips.
    payload = _read_values_payload([["a", "b"]])
    target = tmp_path / "out.txt"
    out = srv._call_formatted(
        srv.models.ReadValuesResult,
        lambda s, sid, rngs, **kw: payload,
        "text",
        object(),
        "<ID>",
        ["S!A1:B2"],
        out_path=str(target),
    )
    assert isinstance(out, ToolResult)
    handle = _handle(out)
    # The on-disk file is valid json (text under file-output resolves to json — see impl note).
    assert json.loads(target.read_text(encoding="utf-8")) == payload
    assert handle["format"] == "json"


def test_call_formatted_out_path_jsonl_handle(tmp_path):
    rows = [["x"], ["y"], ["z"]]
    payload = _read_values_payload(rows)
    target = tmp_path / "out.jsonl"
    out = srv._call_formatted(
        srv.models.ReadValuesResult,
        lambda s, sid, rngs, **kw: payload,
        "jsonl",
        object(),
        "<ID>",
        ["S!A1:B2"],
        out_path=str(target),
    )
    handle = _handle(out)
    assert handle["format"] == "jsonl"
    lines = [l for l in target.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 3
    assert json.loads(lines[0]) == {"range": "S!A1:B2", "row": ["x"]}


def test_call_formatted_out_path_missing_parent_raises_tool_error(tmp_path):
    payload = _read_values_payload([["a"]])
    target = tmp_path / "missing_dir" / "out.csv"
    with pytest.raises(ToolError) as ei:
        srv._call_formatted(
            srv.models.ReadValuesResult,
            lambda s, sid, rngs, **kw: payload,
            "csv",
            object(),
            "<ID>",
            ["S!A1:B2"],
            out_path=str(target),
        )
    assert "bad_out_path" in str(ei.value)
    assert not (tmp_path / "missing_dir").exists()


def test_call_formatted_out_path_credential_target_raises_tool_error(tmp_path):
    payload = _read_values_payload([["a"]])
    target = tmp_path / "token.json"
    with pytest.raises(ToolError) as ei:
        srv._call_formatted(
            srv.models.ReadValuesResult,
            lambda s, sid, rngs, **kw: payload,
            "json",
            object(),
            "<ID>",
            ["S!A1:B2"],
            out_path=str(target),
        )
    assert "bad_out_path" in str(ei.value)
    assert not target.exists()


def test_call_formatted_out_path_csv_on_structured_raises_before_write(tmp_path):
    # csv on a structured (inspect-shaped) result -> format_unsupported, no half-written file.
    structured = {
        "ok": True, "spreadsheetId": "<ID>", "sheet": "S", "range": "A1", "rows": 1,
        "cols": 1, "cells": [{"a1": "A1"}], "merges": [],
    }
    target = tmp_path / "out.csv"
    with pytest.raises(ToolError) as ei:
        srv._call_formatted(
            srv.models.InspectResult,
            lambda s, sid, rng, **kw: structured,
            "csv",
            object(),
            "<ID>",
            "A1",
            out_path=str(target),
        )
    assert "format_unsupported" in str(ei.value)
    assert not target.exists()


def test_call_formatted_no_out_path_is_unchanged(tmp_path):
    # Sanity: with out_path=None the existing behavior is intact (model for text/json).
    payload = _read_values_payload([["a", "b"]])
    out = srv._call_formatted(
        srv.models.ReadValuesResult,
        lambda s, sid, rngs, **kw: payload,
        "text",
        object(),
        "<ID>",
        ["S!A1:B2"],
        out_path=None,
    )
    assert isinstance(out, srv.models.ReadValuesResult)
