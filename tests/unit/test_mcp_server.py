"""Thin smoke tests for the FastMCP adapter (DESIGN §7.1, §10).

The adapter is intentionally thin: validate args, pull the shared ``services`` handle out of
the lifespan context, call the matching PURE core fn, and shape the result. These tests pin the
adapter contract WITHOUT touching Google or the core's Sheets logic:

- all tools register (15 base + the 5 v0.2 extension tools: data_ops/dimensions/comments/
  read_many/export), with the §7.1 annotation table (read-only/destructive hints, tags);
- ``Context`` is excluded from every tool's input schema;
- read tools advertise a structured ``output_schema``;
- ``ENABLED_TOOLS`` restricts which tools register (checked in a subprocess so the env var is
  read at import time);
- ``to_tool_error`` produces the two canonical envelope shapes (API vs validation);
- ``_call`` wraps a core dict in its mirror model and maps ``SheetsError`` -> ``ToolError``;
- the lifespan catches a ``build_services`` failure -> stderr message + non-zero exit, never
  touching stdout (the JSON-RPC channel).

No live API, no real ids. The module imports ``fastmcp``/``pydantic`` by design (it is the
adapter test); the boundary guard in ``test_boundary_guard.py`` runs in a subprocess so this
import never gives it a false pass.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult

import gsheets.mcp_server as srv
from gsheets.core import addressing as _addressing
from gsheets.core.errors import SheetsError


# Expected (tool name -> (readOnlyHint, destructiveHint, tag)) from the DESIGN §7.1 table.
EXPECTED = {
    "sheets_overview": (True, None, "read"),
    "sheets_inspect": (True, None, "read"),
    "sheets_describe": (True, None, "read"),
    "sheets_formula_patterns": (True, None, "read"),
    "sheets_read_values": (True, None, "read"),
    "sheets_read_conditional_formats": (True, None, "read"),
    "sheets_read_many": (True, None, "read"),
    # export reads the spreadsheet but WRITES a local file (silently overwriting any existing
    # one at ``path``), so it must not advertise read-only.
    "sheets_export": (False, True, "write"),
    "sheets_comments": (False, True, "write"),
    "sheets_write_values": (False, False, "write"),
    "sheets_append_rows": (False, False, "write"),
    "sheets_clear": (False, True, "write"),
    "sheets_format": (False, False, "write"),
    "sheets_set_conditional_format": (False, True, "write"),
    # rule=None clears all validation on the range — a destructive path, like clear's.
    "sheets_set_validation": (False, True, "write"),
    "sheets_structure": (False, True, "write"),
    "sheets_manage_sheets": (False, True, "write"),
    "sheets_metadata": (False, True, "write"),
    "sheets_data_ops": (False, True, "write"),
    "sheets_dimensions": (False, True, "write"),
    "sheets_charts": (False, True, "write"),
    "sheets_batch": (False, True, "write"),
}


def _tools() -> dict:
    return asyncio.run(srv.mcp.get_tools())


def test_all_tools_register():
    tools = _tools()
    assert set(tools) == set(EXPECTED)
    assert len(tools) == 22


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_annotations_and_tags_match_design_table(name):
    tool = _tools()[name]
    expected_ro, expected_destr, expected_tag = EXPECTED[name]
    ann = tool.annotations
    assert ann is not None
    assert ann.readOnlyHint is expected_ro
    assert ann.destructiveHint is expected_destr
    assert ann.openWorldHint is True  # every tool hits Google's servers
    assert tool.tags == {expected_tag}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_context_excluded_from_input_schema(name):
    tool = _tools()[name]
    props = tool.parameters.get("properties", {})
    assert "ctx" not in props
    assert "context" not in props
    if name == "sheets_read_many":
        # The cross-file tool deliberately has NO top-level id — each request carries its own.
        assert "spreadsheet_id" not in props
    else:
        # spreadsheet_id is always the first required arg.
        assert "spreadsheet_id" in props
        assert "spreadsheet_id" in tool.parameters.get("required", [])


def test_read_tools_have_output_schema():
    # sheets_overview has no out_path; it keeps its derived outputSchema (mirror model).
    assert _tools()["sheets_overview"].output_schema is not None


@pytest.mark.parametrize(
    "name",
    [
        "sheets_inspect",
        "sheets_describe",
        "sheets_formula_patterns",
        "sheets_read_values",
        "sheets_read_many",
    ],
)
def test_out_path_tools_have_no_output_schema(name):
    # ISSUES.md #19/#21: the five out_path-capable read tools are registered with
    # output_schema=None so that a content-only ToolResult (the file-output handle, or a rendered
    # csv/tsv/jsonl/markdown string with no out_path) is NOT rejected by the MCP lowlevel server's
    # structured-output validation. Pin that intent explicitly.
    assert _tools()[name].output_schema is None


def test_read_values_exposes_diff_only_and_max_cells_optional():
    # ISSUES.md #12/#13: the optional payload-shaping knobs are exposed and NOT required.
    tool = _tools()["sheets_read_values"]
    props = tool.parameters.get("properties", {})
    required = tool.parameters.get("required", [])
    assert "diff_only" in props
    assert "max_cells" in props
    assert "diff_only" not in required
    assert "max_cells" not in required


def test_read_values_exposes_major_dimension_default_rows():
    # SPEC §6 P3: major_dimension is exposed, optional, defaults to rows, enum rows|columns.
    tool = _tools()["sheets_read_values"]
    props = tool.parameters.get("properties", {})
    assert "major_dimension" in props
    assert "major_dimension" not in tool.parameters.get("required", [])
    prop = props["major_dimension"]
    assert prop.get("default") == "rows"
    assert set(prop.get("enum") or []) == {"rows", "columns"}


def test_read_values_exposes_data_filters_optional_and_ranges_not_required():
    # SPEC §6 P2: data_filters is exposed and optional; ranges is now optional too (exactly one
    # of ranges/data_filters, enforced in core).
    tool = _tools()["sheets_read_values"]
    props = tool.parameters.get("properties", {})
    required = tool.parameters.get("required", [])
    assert "data_filters" in props
    assert "data_filters" not in required
    assert "ranges" not in required


def test_describe_exposes_data_filters_optional_and_ranges_not_required():
    # SPEC §6 P2: describe also accepts data_filters (symbolic addressing); ranges optional.
    tool = _tools()["sheets_describe"]
    props = tool.parameters.get("properties", {})
    required = tool.parameters.get("required", [])
    assert "data_filters" in props
    assert "data_filters" not in required
    assert "ranges" not in required


def test_read_conditional_formats_exposes_range_optional():
    # SPEC §6 P3: range-scoped CF read — range is exposed and optional alongside sheet.
    tool = _tools()["sheets_read_conditional_formats"]
    props = tool.parameters.get("properties", {})
    required = tool.parameters.get("required", [])
    assert "range" in props
    assert "sheet" in props
    assert "range" not in required


# ----------------------------------------------------------- SPEC §1.3 output_format on reads


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
def test_output_format_exposed_default_text(name):
    # The shared output-format knob is exposed on every read tool and defaults to text.
    props = _tools()[name].parameters.get("properties", {})
    assert "output_format" in props
    assert props["output_format"].get("default") == "text"
    assert name not in _tools()[name].parameters.get("required", [])


def test_read_values_output_format_offers_all_data_formats():
    # The rectangular-values read accepts every data format (text/json/jsonl/csv/tsv/markdown).
    prop = _tools()["sheets_read_values"].parameters["properties"]["output_format"]
    enum = prop.get("enum") or (prop.get("anyOf") and None)
    assert enum is not None
    assert set(enum) == {"text", "json", "jsonl", "csv", "tsv", "markdown"}


@pytest.mark.parametrize(
    "name",
    ["sheets_inspect", "sheets_describe", "sheets_formula_patterns", "sheets_read_many"],
)
def test_structured_reads_restrict_to_text_json_jsonl_markdown(name):
    # Structured reads (no rectangular grid) advertise text/json/jsonl + markdown (KV) — csv/tsv
    # are absent (those need a value grid; SPEC §6, D-MD).
    prop = _tools()[name].parameters["properties"]["output_format"]
    enum = prop.get("enum")
    assert enum is not None
    assert set(enum) == {"text", "json", "jsonl", "markdown"}


def test_call_formatted_text_returns_model():
    payload = {
        "ok": True,
        "spreadsheetId": "<ID>",
        "render": "plain",
        "ranges": [{"range": "S!A1:B1", "values": [["a", "b"]]}],
    }
    out = srv._call_formatted(
        srv.models.ReadValuesResult, lambda s, sid, rngs, **kw: payload, "text", object(), "<ID>", ["S!A1:B1"]
    )
    assert isinstance(out, srv.models.ReadValuesResult)


def test_call_formatted_json_returns_model():
    payload = {
        "ok": True,
        "spreadsheetId": "<ID>",
        "render": "plain",
        "ranges": [{"range": "S!A1:B1", "values": [["a", "b"]]}],
    }
    out = srv._call_formatted(
        srv.models.ReadValuesResult, lambda s, sid, rngs, **kw: payload, "json", object(), "<ID>", ["S!A1:B1"]
    )
    assert isinstance(out, srv.models.ReadValuesResult)


def test_call_formatted_csv_returns_string_via_toolresult():
    # A data format returns the rendered STRING (wrapped in a ToolResult so FastMCP keeps the
    # tool's structured output_schema while still emitting a plain string body).
    payload = {
        "ok": True,
        "spreadsheetId": "<ID>",
        "render": "plain",
        "ranges": [{"range": "S!A1:B2", "values": [["a", "b"], ["c", "d"]]}],
    }
    out = srv._call_formatted(
        srv.models.ReadValuesResult, lambda s, sid, rngs, **kw: payload, "csv", object(), "<ID>", ["S!A1:B2"]
    )
    assert isinstance(out, ToolResult)
    text = out.content[0].text
    assert "a,b" in text and "c,d" in text
    # No structured content for the data-format path (it's a plain string body).
    assert out.structured_content is None


def test_call_formatted_jsonl_returns_string():
    payload = {
        "ok": True,
        "spreadsheetId": "<ID>",
        "render": "plain",
        "ranges": [{"range": "S!A1:A2", "values": [["x"], ["y"]]}],
    }
    out = srv._call_formatted(
        srv.models.ReadValuesResult, lambda s, sid, rngs, **kw: payload, "jsonl", object(), "<ID>", ["S!A1:A2"]
    )
    assert isinstance(out, ToolResult)
    lines = [l for l in out.content[0].text.splitlines() if l.strip()]
    assert len(lines) == 2


def test_call_formatted_markdown_returns_table_string():
    # markdown over a value grid returns a GitHub table string (embedded pipe escaped).
    payload = {
        "ok": True,
        "spreadsheetId": "<ID>",
        "render": "plain",
        "ranges": [{"range": "S!A1:B2", "values": [["Name", "Note"], ["a|b", "x"]]}],
    }
    out = srv._call_formatted(
        srv.models.ReadValuesResult, lambda s, sid, rngs, **kw: payload, "markdown", object(), "<ID>", ["S!A1:B2"]
    )
    assert isinstance(out, ToolResult)
    text = out.content[0].text
    assert "| Name | Note |" in text
    assert "| --- | --- |" in text
    assert r"a\|b" in text  # the embedded pipe is escaped, not a column separator


def test_call_formatted_markdown_on_structured_result_returns_kv():
    # markdown on a structured (inspect-shaped) result does NOT error — it renders KV lines.
    structured = {"ok": True, "spreadsheetId": "<ID>", "sheet": "S", "range": "A1", "rows": 1,
                  "cols": 1, "cells": [{"a1": "A1"}], "merges": []}
    out = srv._call_formatted(
        srv.models.InspectResult, lambda s, sid, rng, **kw: structured, "markdown", object(), "<ID>", "A1"
    )
    assert isinstance(out, ToolResult)
    text = out.content[0].text
    assert "sheet: S" in text
    assert "ok: True" in text


# ===================================================================== #27 sheet-index cache scope
# The adapter MUST open an ``addressing.sheet_index_cache()`` scope around the core call (the same
# chokepoint where it activates the retry policy), so every tool that resolves many GridRanges does
# ONE sheet-index get instead of one per element. These probe fns record whether a scope was active
# in the thread that ran core — the systemic #27 fix wired into _call / _call_formatted.


def _probe_payload(sid):
    return {
        "ok": True,
        "spreadsheetId": sid,
        "render": "plain",
        "ranges": [{"range": "S!A1:A1", "values": [["x"]]}],
    }


def _scope_probe(seen):
    def fn(services, sid, *args, **kwargs):
        seen["cache"] = _addressing._SHEET_INDEX_CACHE.get()
        return _probe_payload(sid)
    return fn


def test_call_opens_sheet_index_cache_scope():
    seen: dict = {}
    assert _addressing._SHEET_INDEX_CACHE.get() is None  # no scope before the call
    srv._call(srv.models.ReadValuesResult, _scope_probe(seen), object(), "<ID>", [])
    assert seen["cache"] is not None  # core ran INSIDE an active sheet-index cache scope
    assert _addressing._SHEET_INDEX_CACHE.get() is None  # scope torn down after (per-operation)


def test_call_formatted_text_branch_opens_scope():
    # text/json delegates to _call — proves the transitive path is covered (no double-wrap).
    seen: dict = {}
    srv._call_formatted(srv.models.ReadValuesResult, _scope_probe(seen), "text", object(), "<ID>", [])
    assert seen["cache"] is not None


def test_call_formatted_data_format_branch_opens_scope():
    # The csv/jsonl/tsv branch wraps fn directly (it does NOT route through _call).
    seen: dict = {}
    srv._call_formatted(srv.models.ReadValuesResult, _scope_probe(seen), "csv", object(), "<ID>", [])
    assert seen["cache"] is not None


def test_call_formatted_out_path_branch_opens_scope(tmp_path):
    # The file-output (out_path) branch — the path big inspect/describe reads take — wraps fn too.
    seen: dict = {}
    out = srv._call_formatted(
        srv.models.ReadValuesResult,
        _scope_probe(seen),
        "text",
        object(),
        "<ID>",
        [],
        out_path=str(tmp_path / "out.json"),
    )
    assert isinstance(out, ToolResult)
    assert seen["cache"] is not None


def test_call_formatted_csv_on_structured_result_raises_tool_error():
    # csv on a structured (inspect-shaped) result -> format_unsupported -> clean ToolError.
    structured = {"ok": True, "spreadsheetId": "<ID>", "sheet": "S", "range": "A1", "rows": 1,
                  "cols": 1, "cells": [{"a1": "A1"}], "merges": []}
    with pytest.raises(ToolError) as ei:
        srv._call_formatted(
            srv.models.InspectResult, lambda s, sid, rng, **kw: structured, "csv", object(), "<ID>", "A1"
        )
    assert "format_unsupported" in str(ei.value)


def test_to_tool_error_api_shape():
    err = SheetsError(
        "google_api_error",
        "The caller does not have permission",
        status=403,
        reason="PERMISSION_DENIED",
        hint="share the sheet with the authenticated account",
    )
    msg = str(srv.to_tool_error(err))
    assert msg.startswith("google_api_error: 403 PERMISSION_DENIED")
    assert "share the sheet" in msg
    # Privacy: no operator email leaks into the pass-through envelope.
    assert "@" not in msg


def test_to_tool_error_validation_shape():
    msg = str(srv.to_tool_error(SheetsError("empty_payload", "refusing a no-op write")))
    assert msg == "empty_payload: refusing a no-op write"

    msg2 = str(srv.to_tool_error(SheetsError("unknown_param", "bad key", hint="see the table")))
    assert msg2 == "unknown_param: bad key — see the table"


def test_call_wraps_core_dict_in_mirror_model():
    payload = {
        "ok": True,
        "spreadsheetId": "<ID>",
        "title": "Demo",
        "locale": "en_US",
        "timeZone": "America/New_York",
        "sheets": [],
        "namedRanges": [],
    }
    out = srv._call(srv.models.OverviewResult, lambda s, sid: payload, object(), "<ID>")
    assert isinstance(out, srv.models.OverviewResult)
    # The model may carry additional optional fields (e.g. locale/timeZone, §X.12) that default to
    # None; the contract is that every core key round-trips faithfully.
    dumped = out.model_dump()
    for key, value in payload.items():
        assert dumped[key] == value


def test_call_maps_sheets_error_to_tool_error():
    def boom(_s, _sid):
        raise SheetsError("bad_range", "not an A1 range")

    with pytest.raises(ToolError) as ei:
        srv._call(srv.models.OverviewResult, boom, object(), "x")
    assert str(ei.value) == "bad_range: not an A1 range"


def test_lifespan_catches_build_failure_to_stderr(monkeypatch):
    def fake_build():
        raise SheetsError("no_credentials", "no usable credentials", hint="run gsheets auth login")

    monkeypatch.setattr(srv.auth, "build_services", fake_build)

    async def run():
        cm = srv.lifespan(srv.mcp)
        with pytest.raises(SystemExit) as ei:
            await cm.__aenter__()
        assert ei.value.code == 1

    asyncio.run(run())


def test_lifespan_failure_writes_to_stderr_not_stdout():
    # Run in a fresh subprocess so the stdout/stderr split is observed cleanly: the JSON-RPC
    # channel (stdout) MUST stay silent on a startup credential failure (DESIGN §7.1).
    code = (
        "import asyncio, gsheets.mcp_server as s\n"
        "from gsheets.core.errors import SheetsError\n"
        "s.auth.build_services = lambda: (_ for _ in ()).throw("
        "SheetsError('no_credentials','none'))\n"
        "async def go():\n"
        "    cm = s.lifespan(s.mcp)\n"
        "    try:\n"
        "        await cm.__aenter__()\n"
        "    except SystemExit:\n"
        "        pass\n"
        "asyncio.run(go())\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert proc.stdout == ""  # JSON-RPC channel untouched
    assert "google-sheets-mcp" in proc.stderr
    assert "gsheets auth login" in proc.stderr


def test_inspect_exposes_rich_text_and_pivot_flags():
    # The v0.2 additive kwargs must surface in the tool input schema (default off).
    props = _tools()["sheets_inspect"].parameters.get("properties", {})
    assert "include_rich_text" in props
    assert "include_pivot" in props
    assert props["include_rich_text"].get("default") is False
    assert props["include_pivot"].get("default") is False


def test_new_extension_tools_have_expected_inputs():
    tools = _tools()
    # data_ops: action required, params optional.
    data_ops = tools["sheets_data_ops"]
    assert "action" in data_ops.parameters.get("required", [])
    assert "params" in data_ops.parameters.get("properties", {})
    # dimensions: action AND sheet required (every op targets one tab).
    dims = tools["sheets_dimensions"]
    dims_required = dims.parameters.get("required", [])
    assert "action" in dims_required
    assert "sheet" in dims_required
    # comments: full CRUD — action + write args, plus the read-only resolved/deleted filters.
    comments = tools["sheets_comments"]
    comments_props = comments.parameters.get("properties", {})
    assert "action" in comments_props
    assert comments_props["action"].get("default") == "read"
    assert "comment_id" in comments_props
    assert "content" in comments_props
    assert "anchor" in comments_props
    assert "include_resolved" in comments_props
    assert "include_deleted" in comments_props
    # read_many: requests required, mode optional (defaults to values).
    read_many = tools["sheets_read_many"]
    assert "requests" in read_many.parameters.get("required", [])
    assert read_many.parameters.get("properties", {}).get("mode", {}).get("default") == "values"
    # export: format defaults to pdf; path/sheet optional.
    export = tools["sheets_export"]
    export_props = export.parameters.get("properties", {})
    assert export_props.get("format", {}).get("default") == "pdf"
    assert "path" in export_props
    assert "sheet" in export_props


def test_comments_tool_wraps_core_via_call():
    # Thin-adapter contract: the tool body just wraps the core dict in its mirror model.
    payload = {"ok": True, "spreadsheetId": "<ID>", "comments": []}
    out = srv._call(srv.models.CommentsResult, lambda s, sid, **kw: payload, object(), "<ID>")
    assert isinstance(out, srv.models.CommentsResult)
    assert out.comments == []


def test_comments_create_wraps_write_return():
    # The full-CRUD return shapes (create -> comment) round-trip through the mirror model.
    payload = {
        "ok": True,
        "spreadsheetId": "<ID>",
        "comment": {"id": "C1", "author": "Jane", "content": "hi", "resolved": False},
    }
    out = srv._call(srv.models.CommentsResult, lambda s, sid, **kw: payload, object(), "<ID>")
    assert isinstance(out, srv.models.CommentsResult)
    assert out.comment is not None
    assert out.comment.id == "C1"


def test_export_call_maps_core_dict():
    payload = {
        "ok": True,
        "spreadsheetId": "<ID>",
        "format": "csv",
        "mimeType": "text/csv",
        "path": "<ID>.csv",
        "bytes": 42,
    }
    out = srv._call(
        srv.models.ExportResult, lambda s, sid, **kw: payload, object(), "<ID>", format="csv"
    )
    assert isinstance(out, srv.models.ExportResult)
    assert out.format == "csv"
    assert out.bytes == 42
    assert out.path == "<ID>.csv"


def test_read_many_call_maps_core_dict_with_mixed_results():
    # A top-level ok:True envelope can carry per-file failures inside results[].
    payload = {
        "ok": True,
        "mode": "summary",
        "count": 2,
        "results": [
            {"ok": True, "spreadsheetId": "A", "title": "Alpha", "sheets": []},
            {"ok": False, "spreadsheetId": "B", "error": {"code": "google_api_error"}},
        ],
    }
    out = srv._call(srv.models.ReadManyResult, lambda s, reqs, **kw: payload, object(), [], mode="summary")
    assert isinstance(out, srv.models.ReadManyResult)
    assert out.mode == "summary"
    assert out.count == 2
    assert out.results[1]["ok"] is False


def test_data_ops_call_maps_unknown_action_error():
    def boom(_s, _sid, *, action, params=None):
        raise SheetsError("unknown_action", f"unknown data_ops action {action!r}")

    with pytest.raises(ToolError) as ei:
        srv._call(srv.models.DataOpsResult, boom, object(), "x", action="nope")
    assert str(ei.value) == "unknown_action: unknown data_ops action 'nope'"


def test_enabled_tools_allowlist_restricts_registration():
    # ENABLED_TOOLS is read at module import time, so exercise it in a subprocess.
    code = (
        "import asyncio, gsheets.mcp_server as s\n"
        "tools = asyncio.run(s.mcp.get_tools())\n"
        "assert set(tools) == {'sheets_overview', 'sheets_inspect'}, sorted(tools)\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "ENABLED_TOOLS": "sheets_overview,sheets_inspect"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


# ----------------------------------------------------------- ISSUES.md #4/#10 exception-proofing


def test_call_maps_timeout_to_structured_tool_error():
    # A transport timeout (NOT a SheetsError) must surface as a structured ToolError, never a
    # bare masked "Error calling tool" (ISSUES.md #4) — and must not propagate raw (ISSUES.md #10).
    def boom(_s, _sid):
        raise TimeoutError("The read operation timed out")

    with pytest.raises(ToolError) as ei:
        srv._call(srv.models.InspectResult, boom, object(), "x")
    msg = str(ei.value)
    assert msg.startswith("network_timeout:")
    assert "timed out" in msg


def test_call_maps_unexpected_exception_to_internal_error():
    def boom(_s, _sid):
        raise KeyError("surprise")

    with pytest.raises(ToolError) as ei:
        srv._call(srv.models.InspectResult, boom, object(), "x")
    assert str(ei.value).startswith("internal_error:")


def test_call_model_validation_error_fails_small():
    # A mirror-model construction error must surface as ONE structured ToolError, not a masked
    # bare error or a 1000-line validation wall (ISSUES.md #1 "fail small", #10).
    def returns_bad_shape(_s, _sid):
        # rows must be an int; a dict triggers a pydantic validation error in model_cls(**result).
        return {"ok": True, "rows": {"not": "an int"}}

    with pytest.raises(ToolError) as ei:
        srv._call(srv.models.InspectResult, returns_bad_shape, object(), "x")
    assert str(ei.value).startswith("internal_error:")


# ------------------------------------------- ISSUES.md #19/#21 end-to-end structuredContent guard


def test_read_values_json_no_out_path_still_emits_structured_content(monkeypatch, stub_mcp_credentials):
    # NO-REGRESSION (ISSUES.md #19/#21): even with output_schema=None on the tool, the normal
    # text/json (no out_path) path returns the mirror model and FastMCP still serializes it into
    # structuredContent. Driven through the REAL FastMCP invocation (in-memory Client), not the
    # direct _call_formatted call the older tests use. Also pins that the @model_serializer
    # null-pruning (ISSUES.md #8) still runs: ``major`` is absent from the payload, so it must NOT
    # appear in structuredContent.
    from fastmcp import Client

    payload = {
        "ok": True,
        "spreadsheetId": "FAKEID",
        "render": "plain",
        "ranges": [{"range": "S!A1:B2", "values": [["a", "b"], ["c", "d"]]}],
    }
    monkeypatch.setattr(
        srv, "_read_values", lambda services, spreadsheet_id, ranges, **kw: payload
    )
    monkeypatch.setattr(srv, "_services", lambda ctx: object())

    async def go():
        async with Client(srv.mcp) as client:
            return await client.call_tool(
                "sheets_read_values",
                {"spreadsheet_id": "FAKEID", "ranges": ["S!A1:B2"], "output_format": "json"},
            )

    r = asyncio.run(go())
    assert r.structured_content is not None
    sc = r.structured_content
    assert sc["ok"] is True
    assert sc["spreadsheetId"] == "FAKEID"
    assert sc["render"] == "plain"
    assert sc["ranges"] == payload["ranges"]
    # Null-pruning survives output_schema=None: ``major`` was never in the payload.
    assert "major" not in sc


# ----------------------------------------------------------- ISSUES.md #25 per-call retry param


@pytest.fixture
def _clear_backoff_env(monkeypatch):
    """Strip every GSHEETS_BACKOFF_* / legacy var so from_env() resolves to the field defaults.

    The _resolve_retry tests assert against RetryPolicy.from_env(); a developer machine with a
    GSHEETS_BACKOFF_* var set would otherwise skew them. CI is clean, but pin it either way.
    """
    for key in list(os.environ):
        if key.startswith("GSHEETS_BACKOFF_") or key == "GSHEETS_MAX_RETRIES":
            monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_tool_exposes_optional_retry_param(name):
    # ISSUES.md #25: EVERY tool (read AND write) grows an optional ``retry`` param defaulting to
    # None — retry is OFF by default, so it must never be required, and never break an existing arg.
    tool = _tools()[name]
    props = tool.parameters.get("properties", {})
    required = tool.parameters.get("required", [])
    assert "retry" in props, name
    assert "retry" not in required, name


def test_retry_param_references_retry_params_model():
    # The retry param is the RetryParams mirror model (an object schema), not a bare scalar — so a
    # client sees {preset, strategy, max_retries, ...}. It is nullable (Optional, default None).
    tool = _tools()["sheets_read_values"]
    prop = tool.parameters["properties"]["retry"]
    # Optional[...] renders as anyOf [<model or $ref>, {"type": "null"}].
    assert "anyOf" in prop
    assert any(sub.get("type") == "null" for sub in prop["anyOf"])
    # The non-null branch is an object (inline or via $ref to the RetryParams def).
    non_null = [s for s in prop["anyOf"] if s.get("type") != "null"]
    assert non_null
    branch = non_null[0]
    is_object = branch.get("type") == "object" or "$ref" in branch or "properties" in branch
    assert is_object


def test_resolve_retry_none_is_from_env(_clear_backoff_env):
    # An omitted retry param resolves from the env — which, creds-free and var-free, is OFF.
    policy = srv._resolve_retry(None)
    assert policy == srv.retry_mod.RetryPolicy.from_env()
    assert policy.enabled is False


def test_resolve_retry_preset_off_is_disabled():
    policy = srv._resolve_retry(srv.models.RetryParams(preset="off"))
    assert policy is srv.retry_mod.RetryPolicy.DISABLED
    assert policy.enabled is False


def test_resolve_retry_preset_default_is_default_preset():
    policy = srv._resolve_retry(srv.models.RetryParams(preset="default"))
    assert policy == srv.retry_mod.RetryPolicy.default_preset()
    assert policy.enabled is True
    assert policy.strategy == "exponential_jitter"


def test_resolve_retry_granular_enables_and_maps_deadline(_clear_backoff_env):
    # Any granular field (no preset) -> enabled, with the granular knobs overriding env; ``deadline``
    # maps onto the core ``total_deadline`` field.
    policy = srv._resolve_retry(
        srv.models.RetryParams(strategy="fixed", max_retries=2, deadline=12.5)
    )
    assert policy.enabled is True
    assert policy.strategy == "fixed"
    assert policy.max_retries == 2
    assert policy.total_deadline == 12.5


def test_resolve_retry_preset_plus_granular_conflicts():
    # preset and granular fields are mutually exclusive -> a clean backoff_params_conflict.
    with pytest.raises(SheetsError) as ei:
        srv._resolve_retry(srv.models.RetryParams(preset="default", max_retries=3))
    assert ei.value.code == "backoff_params_conflict"


def test_resolve_retry_all_none_acts_like_omitted(_clear_backoff_env):
    # An all-None RetryParams (no preset, no granular field) behaves like an omitted param.
    policy = srv._resolve_retry(srv.models.RetryParams())
    assert policy == srv.retry_mod.RetryPolicy.from_env()
    assert policy.enabled is False


def test_resolve_retry_conflict_surfaces_as_tool_error_through_call():
    # The conflict raised inside _resolve_retry is coerced to a clean ToolError by _call (it never
    # leaks as a bare masked error), so a tool body passing a bad retry param fails gracefully.
    with pytest.raises(ToolError) as ei:
        srv._call(
            srv.models.OverviewResult,
            lambda _s, _sid: {"ok": True},
            object(),
            "x",
            retry=srv.models.RetryParams(preset="off", strategy="fixed"),
        )
    assert str(ei.value).startswith("backoff_params_conflict:")
