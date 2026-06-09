"""Thin smoke tests for the argparse CLI adapter (DESIGN §7.2, §10).

These assert the adapter's THIN contract — it maps flags 1:1 to core args, prints terse/JSON
output, and routes a :class:`~gsheets.core.errors.SheetsError` to stderr with exit 1 — WITHOUT
touching Google. Core is monkeypatched, so no auth/network is involved. The CLI must stay a
boundary-clean adapter: it owns ``argparse`` but contains zero Sheets logic.
"""

from __future__ import annotations

import json

import pytest

from gsheets import cli
from gsheets.core.errors import SheetsError


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
    }
    assert expected <= set(choices)
