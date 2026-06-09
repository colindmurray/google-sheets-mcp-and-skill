"""Thin smoke tests for the argparse CLI adapter (DESIGN §7.2, §10).

These assert the adapter's THIN contract — it maps flags 1:1 to core args, prints terse/JSON
output, and routes a :class:`~gsheets.core.errors.SheetsError` to stderr with exit 1 — WITHOUT
touching Google. Core is monkeypatched, so no auth/network is involved. The CLI must stay a
boundary-clean adapter: it owns ``argparse`` but contains zero Sheets logic.
"""

from __future__ import annotations

import json

import pytest

from gsheets import __version__, cli
from gsheets.core.errors import SheetsError


def test_version_flag_prints_and_exits_zero(capsys):
    """``gsheets --version`` prints ``gsheets <version>`` and exits 0 (argparse version action)."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"gsheets {__version__}"


@pytest.fixture
def patched(monkeypatch):
    """Patch ``auth.build_services`` + every core fn the CLI calls; record the last call.

    Returns a dict the test reads to assert which core fn ran and with what args/kwargs. Each
    stub returns a minimal ``ok:True`` dict so the renderer has something to print.
    """
    calls: dict = {}

    sentinel_services = object()
    monkeypatch.setattr(cli.auth, "build_services", lambda scopes_mode=None: sentinel_services)

    def _make(name, ret):
        def _stub(services, spreadsheet_id, *args, **kwargs):
            calls["name"] = name
            calls["services"] = services
            calls["spreadsheet_id"] = spreadsheet_id
            calls["args"] = args
            calls["kwargs"] = kwargs
            return ret
        return _stub

    monkeypatch.setattr(
        cli.core,
        "overview",
        _make("overview", {"ok": True, "spreadsheetId": "X", "title": "T", "sheets": [], "namedRanges": []}),
    )
    monkeypatch.setattr(
        cli.core,
        "read_values",
        _make("read_values", {"ok": True, "spreadsheetId": "X", "render": "plain", "ranges": []}),
    )
    monkeypatch.setattr(
        cli.core,
        "inspect",
        _make("inspect", {"ok": True, "spreadsheetId": "X", "sheet": "S", "range": "A1:B2", "rows": 0, "cols": 0, "cells": [], "merges": []}),
    )
    monkeypatch.setattr(
        cli.core,
        "write_values",
        _make("write_values", {"ok": True, "spreadsheetId": "X", "updatedRanges": ["S!A1"], "updatedCells": 1}),
    )
    monkeypatch.setattr(
        cli.core,
        "format",
        _make("format", {"ok": True, "spreadsheetId": "X", "range": "S!A1", "appliedFields": "userEnteredFormat(...)"}),
    )
    monkeypatch.setattr(
        cli.core,
        "set_conditional_format",
        _make("set_conditional_format", {"ok": True, "spreadsheetId": "X", "action": "add", "index": 0}),
    )
    # v0.2 extensions (DESIGN §Extensions): the three NEW core fns the CLI now dispatches to.
    monkeypatch.setattr(
        cli.core,
        "data_ops",
        _make(
            "data_ops",
            {
                "ok": True,
                "spreadsheetId": "X",
                "action": "find_replace",
                "allSheets": True,
                "occurrencesChanged": 3,
                "valuesChanged": 3,
            },
        ),
    )
    monkeypatch.setattr(
        cli.core,
        "dimensions",
        _make(
            "dimensions",
            {
                "ok": True,
                "spreadsheetId": "X",
                "action": "insert",
                "sheet": "S",
                "dimension": "ROWS",
                "start": 10,
                "end": 12,
            },
        ),
    )
    monkeypatch.setattr(
        cli.core,
        "comments",
        _make(
            "comments",
            {
                "ok": True,
                "spreadsheetId": "X",
                "comments": [
                    {
                        "id": "AAAA",
                        "author": "Jane Doe",
                        "content": "please verify Q3",
                        "resolved": False,
                        "line": 'comment AAAA by Jane Doe: "please verify Q3" (open)',
                    }
                ],
            },
        ),
    )
    # v0.2 cross-file + export extensions (DESIGN §3.x / §3.3): two NEW core fns the CLI dispatches.
    monkeypatch.setattr(
        cli.core,
        "export",
        _make(
            "export",
            {
                "ok": True,
                "spreadsheetId": "X",
                "format": "csv",
                "mimeType": "text/csv",
                "path": "X.csv",
                "bytes": 128,
            },
        ),
    )
    monkeypatch.setattr(
        cli.core,
        "read_many",
        _make(
            "read_many",
            {
                "ok": True,
                "mode": "values",
                "count": 1,
                "results": [
                    {
                        "ok": True,
                        "spreadsheetId": "A",
                        "render": "plain",
                        "ranges": [{"range": "A!A1:B2", "values": [[1, 2], [3, 4]]}],
                    }
                ],
            },
        ),
    )
    return calls


def _run(argv):
    return cli.main(argv)


def test_overview_dispatches_to_core(patched, capsys):
    rc = _run(["overview", "SHEET_ID"])
    assert rc == 0
    assert patched["name"] == "overview"
    assert patched["spreadsheet_id"] == "SHEET_ID"
    assert "T" in capsys.readouterr().out


def test_global_json_prints_raw_dict(patched, capsys):
    rc = _run(["--json", "overview", "SHEET_ID"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["title"] == "T"


def test_read_values_maps_ranges_and_render(patched):
    _run(["read-values", "ID", "S!A1:B2", "S!C1", "--render", "all"])
    assert patched["name"] == "read_values"
    # ranges is the first positional core arg after spreadsheet_id; render is a kwarg.
    assert patched["args"][0] == ["S!A1:B2", "S!C1"]
    assert patched["kwargs"]["render"] == "all"


def test_inspect_include_flags_map_one_to_one(patched):
    _run(["inspect", "ID", "S!A1:B2", "--compact", "--no-formulas", "--no-validation"])
    kw = patched["kwargs"]
    assert kw["compact"] is True
    assert kw["include_formulas"] is False
    assert kw["include_validation"] is False
    assert kw["include_effective_format"] is True  # untouched default


def test_write_values_single_range_form_builds_data(patched):
    _run(["write-values", "ID", "S!A1", "--values-json", "[[1,2]]"])
    assert patched["name"] == "write_values"
    assert patched["args"][0] == [{"range": "S!A1", "values": [[1, 2]]}]
    assert patched["kwargs"]["input"] == "user_entered"


def test_write_values_conflicting_forms_error(patched, capsys):
    rc = _run(["write-values", "ID", "S!A1", "--values-json", "[[1]]", "--data-json", "[]"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "conflicting_args" in err


def test_format_border_flag_parsed(patched):
    _run(["format", "ID", "S!A1", "--bg", "#FFCDD2", "--bold", "--border", "top=SOLID:#000000"])
    # core.format(services, spreadsheet_id, range, fmt): range is args[0], fmt is args[1].
    assert patched["args"][0] == "S!A1"
    fmt = patched["args"][1]
    assert fmt["bg"] == "#FFCDD2"
    assert fmt["bold"] is True
    assert fmt["borders"] == {"top": "SOLID #000000"}


def test_set_conditional_format_rule_line_passthrough(patched):
    _run(
        [
            "set-conditional-format",
            "ID",
            "--action",
            "add",
            "--index",
            "0",
            "--rule",
            "[S!A2:A100] if NUMBER_GREATER(0) -> bg #C8E6C9",
        ]
    )
    kw = patched["kwargs"]
    assert kw["action"] == "add"
    assert kw["index"] == 0
    assert kw["rule"] == "[S!A2:A100] if NUMBER_GREATER(0) -> bg #C8E6C9"
    assert kw["rules"] is None


def test_sheets_error_routes_to_stderr_exit_1(patched, monkeypatch, capsys):
    def _boom(services, spreadsheet_id):
        raise SheetsError("bad_range", "nope", status=400, hint="fix it")

    monkeypatch.setattr(cli.core, "overview", _boom)
    rc = _run(["overview", "ID"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing on stdout
    assert "bad_range: nope" in captured.err
    assert "hint: fix it" in captured.err


def test_sheets_error_json_envelope(patched, monkeypatch, capsys):
    def _boom(services, spreadsheet_id):
        raise SheetsError(
            "google_api_error", "denied", status=403, reason="PERMISSION_DENIED", hint="share it"
        )

    monkeypatch.setattr(cli.core, "overview", _boom)
    rc = _run(["--json", "overview", "ID"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "google_api_error"
    assert payload["error"]["status"] == 403


def test_auth_status_ok_false_exits_nonzero(monkeypatch):
    monkeypatch.setattr(
        cli.auth,
        "status",
        lambda scopes_mode=None: {"ok": False, "error": {"code": "no_creds", "message": "none"}},
    )
    rc = _run(["auth", "status"])
    assert rc == 1


def test_auth_login_does_not_build_services(monkeypatch):
    called = {"build": False, "bootstrap": False}

    def _no_build(scopes_mode=None):
        called["build"] = True
        raise AssertionError("auth login must not build Sheets services")

    monkeypatch.setattr(cli.auth, "build_services", _no_build)
    monkeypatch.setattr(
        cli.auth,
        "bootstrap",
        lambda scopes_mode=None: (
            called.__setitem__("bootstrap", True) or {"ok": True, "tokenPath": "/tmp/t"}
        ),
    )
    rc = _run(["auth", "login"])
    assert rc == 0
    assert called["bootstrap"] is True
    assert called["build"] is False


def test_build_parser_registers_every_subcommand():
    parser = cli.build_parser()
    choices: dict = {}
    for action in parser._actions:
        if isinstance(action, cli.argparse._SubParsersAction):
            choices = action.choices
            break
    expected = {
        "overview", "inspect", "read-values", "read-conditional-formats",
        "write-values", "append-rows", "clear", "format", "set-conditional-format",
        "set-validation", "structure", "manage-sheets", "metadata", "charts",
        "batch", "auth",
        # v0.2 extensions (DESIGN §Extensions): three NEW subcommands.
        "data-ops", "dimensions", "comments",
        # v0.2 cross-file + export extensions (DESIGN §3.x / §3.3): two MORE NEW subcommands.
        "export", "read-many",
    }
    assert expected <= set(choices)


def test_structure_action_choices_include_slicer_writes():
    # The CLI action enum must surface the v0.2 §X.16 slicer write actions.
    parser = cli.build_parser()
    structure_choices: set = set()
    for action in parser._actions:
        if isinstance(action, cli.argparse._SubParsersAction):
            structure_parser = action.choices["structure"]
            for sub_action in structure_parser._actions:
                if getattr(sub_action, "dest", None) == "action":
                    structure_choices = set(sub_action.choices or ())
            break
    assert {"add_slicer", "update_slicer", "delete_slicer"} <= structure_choices


# ===========================================================================================
# v0.2 extension subcommands (DESIGN §Extensions): data-ops / dimensions / comments + the
# inspect --rich-text/--pivot flags. Each asserts the THIN 1:1 mapping into core.
# ===========================================================================================


def test_inspect_rich_text_and_pivot_flags_map_one_to_one(patched):
    _run(["inspect", "ID", "S!A1:B2", "--rich-text", "--pivot"])
    kw = patched["kwargs"]
    assert kw["include_rich_text"] is True
    assert kw["include_pivot"] is True
    # Base include_* defaults stay untouched.
    assert kw["include_effective_format"] is True
    assert kw["include_formulas"] is True


def test_inspect_rich_flags_default_false(patched):
    _run(["inspect", "ID", "S!A1:B2"])
    kw = patched["kwargs"]
    assert kw["include_rich_text"] is False
    assert kw["include_pivot"] is False


def test_data_ops_dispatches_with_action_and_params(patched):
    _run(
        [
            "data-ops",
            "ID",
            "--action",
            "find_replace",
            "--params-json",
            '{"find":"foo","replacement":"bar","allSheets":true}',
        ]
    )
    assert patched["name"] == "data_ops"
    kw = patched["kwargs"]
    assert kw["action"] == "find_replace"
    assert kw["params"] == {"find": "foo", "replacement": "bar", "allSheets": True}


def test_data_ops_requires_action(patched, capsys):
    with pytest.raises(SystemExit):
        _run(["data-ops", "ID", "--params-json", "{}"])
    assert "--action" in capsys.readouterr().err


def test_data_ops_terse_summary_render(patched, capsys):
    rc = _run(["data-ops", "ID", "--action", "find_replace", "--params-json", '{"find":"x","replacement":"y","allSheets":true}'])
    assert rc == 0
    out = capsys.readouterr().out
    assert "find_replace" in out
    assert "occurrencesChanged=3" in out


def test_dimensions_dispatches_with_sheet_and_params(patched):
    _run(
        [
            "dimensions",
            "ID",
            "--action",
            "insert",
            "--sheet",
            "S",
            "--params-json",
            '{"dimension":"ROWS","start":10,"end":12}',
        ]
    )
    assert patched["name"] == "dimensions"
    kw = patched["kwargs"]
    assert kw["action"] == "insert"
    assert kw["sheet"] == "S"
    assert kw["params"] == {"dimension": "ROWS", "start": 10, "end": 12}


def test_dimensions_read_renders_hidden(patched, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.core,
        "dimensions",
        lambda services, sid, *, action, sheet=None, params=None: {
            "ok": True,
            "spreadsheetId": sid,
            "action": "read",
            "sheet": sheet,
            "hiddenRows": [3, 4],
            "hiddenCols": [],
        },
    )
    rc = _run(["dimensions", "ID", "--action", "read", "--sheet", "S"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "rows: [3, 4]" in out
    assert "cols: (none)" in out


def test_dimensions_write_summary_render(patched, capsys):
    rc = _run(["dimensions", "ID", "--action", "insert", "--sheet", "S", "--params-json", '{"dimension":"ROWS","start":10,"end":12}'])
    assert rc == 0
    out = capsys.readouterr().out
    assert "insert" in out
    assert "dimension=ROWS" in out and "start=10" in out and "end=12" in out


def test_comments_flags_map_one_to_one(patched):
    _run(["comments", "ID", "--no-resolved", "--include-deleted"])
    assert patched["name"] == "comments"
    kw = patched["kwargs"]
    assert kw["include_resolved"] is False
    assert kw["include_deleted"] is True


def test_comments_defaults(patched):
    _run(["comments", "ID"])
    kw = patched["kwargs"]
    assert kw["action"] == "read"
    assert kw["comment_id"] is None
    assert kw["content"] is None
    assert kw["anchor"] is None
    assert kw["include_resolved"] is True
    assert kw["include_deleted"] is False


def test_comments_create_maps_action_and_content(patched):
    _run(["comments", "ID", "--action", "create", "--content", "please verify Q3"])
    assert patched["name"] == "comments"
    kw = patched["kwargs"]
    assert kw["action"] == "create"
    assert kw["content"] == "please verify Q3"


def test_comments_reply_maps_action_comment_id_content(patched):
    _run(
        [
            "comments",
            "ID",
            "--action",
            "reply",
            "--comment-id",
            "C1",
            "--content",
            "ack",
        ]
    )
    kw = patched["kwargs"]
    assert kw["action"] == "reply"
    assert kw["comment_id"] == "C1"
    assert kw["content"] == "ack"


def test_comments_delete_requires_confirm(patched, capsys):
    rc = _run(["comments", "ID", "--action", "delete", "--comment-id", "C1"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "confirmation_required" in err
    # The destructive core fn must NOT have been reached.
    assert patched.get("name") != "comments"


def test_comments_delete_with_confirm_dispatches(patched):
    _run(["comments", "ID", "--action", "delete", "--comment-id", "C1", "--confirm"])
    assert patched["name"] == "comments"
    kw = patched["kwargs"]
    assert kw["action"] == "delete"
    assert kw["comment_id"] == "C1"


def test_comments_terse_render_uses_line(patched, capsys):
    rc = _run(["comments", "ID"])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'comment AAAA by Jane Doe: "please verify Q3" (open)' in out


def test_comments_empty_render(patched, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.core,
        "comments",
        lambda services, sid, *, action="read", comment_id=None, content=None, anchor=None,
        include_resolved=True, include_deleted=False: {
            "ok": True,
            "spreadsheetId": sid,
            "comments": [],
        },
    )
    rc = _run(["comments", "ID"])
    assert rc == 0
    assert "(no comments)" in capsys.readouterr().out


def test_inspect_runs_hyperlink_pivot_render(patched, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.core,
        "inspect",
        lambda services, sid, rng, **kw: {
            "ok": True,
            "spreadsheetId": sid,
            "sheet": "S",
            "range": "A1:A1",
            "rows": 1,
            "cols": 1,
            "merges": [],
            "cells": [
                {
                    "a1": "A1",
                    "value": "Click here",
                    "runs": [
                        {"start": 0, "text": "Click here", "link": "https://x"},
                    ],
                    "hyperlink": "https://x",
                    "pivot": {"line": "pivot A1 <- Data!A1:F500 | rows: Region"},
                }
            ],
        },
    )
    rc = _run(["inspect", "ID", "S!A1:A1", "--rich-text", "--pivot"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "runs:" in out and "link https://x" in out
    assert "link=https://x" in out
    assert "pivot A1 <- Data!A1:F500" in out


# ===========================================================================================
# v0.2 cross-file + export subcommands (DESIGN §3.x / §3.3): export / read-many. Each asserts
# the THIN 1:1 mapping into core plus the terse rendering.
# ===========================================================================================


def test_export_dispatches_with_format_path_sheet(patched):
    _run(["export", "ID", "--format", "csv", "--path", "out.csv", "--sheet", "S"])
    assert patched["name"] == "export"
    assert patched["spreadsheet_id"] == "ID"
    kw = patched["kwargs"]
    assert kw["format"] == "csv"
    assert kw["path"] == "out.csv"
    assert kw["sheet"] == "S"


def test_export_defaults_to_pdf(patched):
    _run(["export", "ID"])
    kw = patched["kwargs"]
    assert kw["format"] == "pdf"
    assert kw["path"] is None
    assert kw["sheet"] is None


def test_export_terse_render(patched, capsys):
    rc = _run(["export", "ID", "--format", "csv", "--sheet", "S"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "exported csv -> X.csv" in out
    assert "128 bytes" in out


def test_read_many_dispatches_requests_and_mode(patched):
    _run(
        [
            "read-many",
            "--requests-json",
            '[{"spreadsheetId":"A","ranges":["A!A1:B2"]}]',
            "--mode",
            "values",
        ]
    )
    assert patched["name"] == "read_many"
    # core.read_many(services, requests, *, mode): requests is the 2nd positional (recorded by the
    # generic stub as spreadsheet_id); mode is a kwarg.
    assert patched["spreadsheet_id"] == [{"spreadsheetId": "A", "ranges": ["A!A1:B2"]}]
    assert patched["kwargs"]["mode"] == "values"


def test_read_many_requires_requests_json(patched, capsys):
    with pytest.raises(SystemExit):
        _run(["read-many", "--mode", "summary"])
    assert "--requests-json" in capsys.readouterr().err


def test_read_many_terse_render(patched, capsys):
    rc = _run(
        ["read-many", "--requests-json", '[{"spreadsheetId":"A","ranges":["A!A1:B2"]}]']
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "read-many mode=values: 1 result(s)" in out
    assert "A: 2 row(s) across 1 range(s)" in out


def test_read_many_render_surfaces_captured_failures(patched, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.core,
        "read_many",
        lambda services, requests, *, mode="values": {
            "ok": True,
            "mode": "summary",
            "count": 2,
            "results": [
                {"ok": True, "spreadsheetId": "A", "title": "Alpha", "sheets": [{}, {}]},
                {
                    "ok": False,
                    "spreadsheetId": "B",
                    "error": {"code": "google_api_error", "message": "denied"},
                },
            ],
        },
    )
    rc = _run(["read-many", "--requests-json", '[{"spreadsheetId":"A"}]', "--mode", "summary"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "A: Alpha (2 sheet(s))" in out
    assert "B: ERROR google_api_error: denied" in out
