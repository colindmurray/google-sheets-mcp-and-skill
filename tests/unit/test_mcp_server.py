"""Thin smoke tests for the FastMCP adapter (DESIGN §7.1, §10).

The adapter is intentionally thin: validate args, pull the shared ``services`` handle out of
the lifespan context, call the matching PURE core fn, and shape the result. These tests pin the
adapter contract WITHOUT touching Google or the core's Sheets logic:

- all tools register (15 base + the 3 v0.2 extension tools), with the §7.1 annotation table
  (read-only/destructive hints, tags);
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

import gsheets.mcp_server as srv
from gsheets.core.errors import SheetsError


# Expected (tool name -> (readOnlyHint, destructiveHint, tag)) from the DESIGN §7.1 table.
EXPECTED = {
    "sheets_overview": (True, None, "read"),
    "sheets_inspect": (True, None, "read"),
    "sheets_read_values": (True, None, "read"),
    "sheets_read_conditional_formats": (True, None, "read"),
    "sheets_comments": (True, None, "read"),
    "sheets_write_values": (False, False, "write"),
    "sheets_append_rows": (False, False, "write"),
    "sheets_clear": (False, True, "write"),
    "sheets_format": (False, False, "write"),
    "sheets_set_conditional_format": (False, True, "write"),
    "sheets_set_validation": (False, False, "write"),
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
    assert len(tools) == 18


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
    # spreadsheet_id is always the first required arg.
    assert "spreadsheet_id" in props
    assert "spreadsheet_id" in tool.parameters.get("required", [])


@pytest.mark.parametrize("name", ["sheets_overview", "sheets_inspect", "sheets_read_values"])
def test_read_tools_have_output_schema(name):
    assert _tools()[name].output_schema is not None


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
    # comments: read-only, optional resolved/deleted filters.
    comments = tools["sheets_comments"]
    comments_props = comments.parameters.get("properties", {})
    assert "include_resolved" in comments_props
    assert "include_deleted" in comments_props


def test_comments_tool_wraps_core_via_call():
    # Thin-adapter contract: the tool body just wraps the core dict in its mirror model.
    payload = {"ok": True, "spreadsheetId": "<ID>", "comments": []}
    out = srv._call(srv.models.CommentsResult, lambda s, sid, **kw: payload, object(), "<ID>")
    assert isinstance(out, srv.models.CommentsResult)
    assert out.comments == []


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
