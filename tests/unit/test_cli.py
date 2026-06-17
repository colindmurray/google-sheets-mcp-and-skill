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
        "read_conditional_formats",
        _make("read_conditional_formats", {"ok": True, "spreadsheetId": "X", "sheets": []}),
    )
    monkeypatch.setattr(
        cli.core,
        "describe",
        _make("describe", {"ok": True, "spreadsheetId": "X", "regions": []}),
    )
    monkeypatch.setattr(
        cli.core,
        "formula_patterns",
        _make("formula_patterns", {"ok": True, "spreadsheetId": "X", "columns": []}),
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


# ===========================================================================================
# Global --format {text,json,jsonl,csv,tsv} (SPEC §1.3). --json is a permanent alias for
# --format json. text keeps the terse renderer; the data formats go through core.format.render.
# ===========================================================================================


def test_format_json_equals_json_alias(patched, capsys):
    # --format json and --json must produce byte-identical output (the alias contract).
    rc1 = _run(["--format", "json", "overview", "SHEET_ID"])
    out1 = capsys.readouterr().out
    rc2 = _run(["--json", "overview", "SHEET_ID"])
    out2 = capsys.readouterr().out
    assert rc1 == 0 and rc2 == 0
    assert out1 == out2
    assert json.loads(out1)["title"] == "T"


def test_format_csv_on_read_values_pipes_clean_csv(patched, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.core,
        "read_values",
        lambda services, sid, ranges, **kw: {
            "ok": True,
            "spreadsheetId": sid,
            "render": "plain",
            "ranges": [{"range": "S!A1:B2", "values": [["a", "b"], ["c", "d"]]}],
        },
    )
    rc = _run(["--format", "csv", "read-values", "ID", "S!A1:B2"])
    assert rc == 0
    out = capsys.readouterr().out
    # Clean single-range CSV (no "# range:" header for a single range).
    assert "a,b" in out and "c,d" in out
    assert "# range:" not in out


# A read_values result shared by the byte-equality pins below.
_RV_PAYLOAD = {
    "ok": True,
    "spreadsheetId": "ID",
    "render": "plain",
    "ranges": [{"range": "S!A1:B2", "values": [["a", "b"], ["c", "d"]]}],
}


@pytest.mark.parametrize("fmt", ["csv", "tsv", "jsonl", "markdown"])
def test_data_format_cli_bytes_equal_render(fmt, patched, monkeypatch, capsys, tmp_path):
    # ISSUES.md #20/#22: for the data formats the CLI-piped bytes must be byte-identical to the
    # shared render(), and to the bytes the MCP out_path / write_file_handle producer puts on disk.
    # render() is self-terminating (csv/tsv -> \r\n, jsonl -> \n), so the CLI must NOT add a second
    # trailing newline via print(). This is the regression that pins the single-newline convention.
    from gsheets.core.format import render as core_render
    from gsheets.core.paths import write_file_handle

    monkeypatch.setattr(
        cli.core, "read_values", lambda services, sid, ranges, **kw: _RV_PAYLOAD
    )
    rc = _run(["--format", fmt, "read-values", "ID", "S!A1:B2"])
    assert rc == 0
    out = capsys.readouterr().out
    expected = core_render(_RV_PAYLOAD, fmt)
    assert out.encode("utf-8") == expected.encode("utf-8")
    # ... and equal to the bytes the out_path / MCP producer writes to disk.
    target = tmp_path / f"out.{fmt}"
    write_file_handle(_RV_PAYLOAD, fmt, str(target))
    assert out.encode("utf-8") == target.read_bytes()


def test_csv_cli_has_no_extra_trailing_blank_line(patched, monkeypatch, capsys):
    # The specific symptom of #20/#22: piped csv must end in exactly one \r\n, NOT \r\n\n.
    monkeypatch.setattr(
        cli.core, "read_values", lambda services, sid, ranges, **kw: _RV_PAYLOAD
    )
    rc = _run(["--format", "csv", "read-values", "ID", "S!A1:B2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.endswith("\r\n")
    assert not out.endswith("\r\n\n")
    assert out.encode("utf-8")[-3:] != b"\r\n\n"


def test_json_cli_keeps_friendly_trailing_newline(patched, capsys):
    # The intended asymmetry: json/text are human views and KEEP their print() trailing newline,
    # so a future "byte-equality fix" must not strip it. (ISSUES.md #20/#22 fix preserves this.)
    rc = _run(["--format", "json", "overview", "SHEET_ID"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.endswith("}\n")


def test_format_jsonl_on_read_values_one_record_per_row(patched, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.core,
        "read_values",
        lambda services, sid, ranges, **kw: {
            "ok": True,
            "spreadsheetId": sid,
            "render": "plain",
            "ranges": [{"range": "S!A1:B2", "values": [["a", "b"], ["c", "d"]]}],
        },
    )
    rc = _run(["--format", "jsonl", "read-values", "ID", "S!A1:B2"])
    assert rc == 0
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert lines == [
        {"range": "S!A1:B2", "row": ["a", "b"]},
        {"range": "S!A1:B2", "row": ["c", "d"]},
    ]


def test_format_markdown_on_read_values_renders_table(patched, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.core,
        "read_values",
        lambda services, sid, ranges, **kw: {
            "ok": True,
            "spreadsheetId": sid,
            "render": "plain",
            "ranges": [{"range": "S!A1:B2", "values": [["Name", "Note"], ["a|b", "x"]]}],
        },
    )
    rc = _run(["--format", "markdown", "read-values", "ID", "S!A1:B2"])
    assert rc == 0
    out = capsys.readouterr().out
    # GitHub markdown table with a header rule; the embedded pipe is escaped (not a separator).
    assert "| Name | Note |" in out
    assert "| --- | --- |" in out
    assert r"a\|b" in out


def test_format_markdown_on_structured_result_renders_kv(patched, capsys):
    # markdown on a structured read (overview) does NOT error — it renders key/value lines.
    rc = _run(["--format", "markdown", "overview", "SHEET_ID"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "title: T" in out
    assert "ok: True" in out


def test_format_csv_on_structured_result_errors_format_unsupported(patched, capsys):
    # overview is structured; asking for csv is a clean format_unsupported error (exit 1), not a
    # traceback or silent fallback.
    rc = _run(["--format", "csv", "overview", "SHEET_ID"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "format_unsupported" in err


def test_json_alias_conflicts_with_nonjson_format(patched, capsys):
    rc = _run(["--format", "csv", "--json", "read-values", "ID", "S!A1"])
    assert rc == 1
    assert "conflicting_args" in capsys.readouterr().err


def test_export_subcommand_format_is_the_file_format_not_output_format(patched):
    # The export subcommand owns its own --format (the FILE format: pdf/csv/...), distinct from
    # the global output --format. `export ID --format csv` must drive the export file format.
    _run(["export", "ID", "--format", "csv", "--sheet", "S"])
    assert patched["name"] == "export"
    assert patched["kwargs"]["format"] == "csv"


def test_read_values_maps_ranges_and_render(patched):
    _run(["read-values", "ID", "S!A1:B2", "S!C1", "--render", "all"])
    assert patched["name"] == "read_values"
    # ranges is the first positional core arg after spreadsheet_id; render is a kwarg.
    assert patched["args"][0] == ["S!A1:B2", "S!C1"]
    assert patched["kwargs"]["render"] == "all"
    # New flags default off / unlimited (ISSUES.md #12/#13).
    assert patched["kwargs"]["diff_only"] is False
    assert patched["kwargs"]["max_cells"] is None


def test_read_values_diff_only_and_max_cells_flags_map(patched):
    _run(["read-values", "ID", "S!A1:B2", "--render", "all", "--diff-only", "--max-cells", "2000"])
    assert patched["kwargs"]["diff_only"] is True
    assert patched["kwargs"]["max_cells"] == 2000


def test_read_values_major_defaults_to_rows(patched):
    _run(["read-values", "ID", "S!A1:B2"])
    assert patched["kwargs"]["major"] == "rows"
    assert patched["kwargs"]["data_filters"] is None


def test_read_values_major_columns_maps(patched):
    # SPEC §6 P3: --major columns drives core.read_values(major="columns").
    _run(["read-values", "ID", "S!A1:B2", "--major", "columns"])
    assert patched["kwargs"]["major"] == "columns"


def test_read_values_data_filter_json_maps_and_drops_ranges(patched):
    # SPEC §6 P2: --data-filter-json drives data_filters (symbolic addressing); the positional
    # ranges is empty, so core receives ranges=None (it enforces the exactly-one contract).
    _run([
        "read-values",
        "ID",
        "--data-filter-json",
        '[{"a1":"S!A1:B2"},{"developerMetadataLookup":{"metadataKey":"block:totals"}}]',
    ])
    assert patched["name"] == "read_values"
    assert patched["args"][0] is None  # ranges positional empty -> None
    assert patched["kwargs"]["data_filters"] == [
        {"a1": "S!A1:B2"},
        {"developerMetadataLookup": {"metadataKey": "block:totals"}},
    ]


def test_read_conditional_formats_sheet_and_range_map(patched):
    # SPEC §6 P3: --range drives core.read_conditional_formats(range=...); default sheet/range None.
    _run(["read-conditional-formats", "ID"])
    assert patched["name"] == "read_conditional_formats"
    assert patched["kwargs"]["range"] is None

    _run(["read-conditional-formats", "ID", "--range", "Cliff!A1:A50"])
    assert patched["kwargs"]["range"] == "Cliff!A1:A50"
    # sheet is the first positional core arg after spreadsheet_id (None here).
    assert patched["args"][0] is None


def test_inspect_include_flags_map_one_to_one(patched):
    _run(["inspect", "ID", "S!A1:B2", "--compact", "--no-formulas", "--no-validation"])
    kw = patched["kwargs"]
    assert kw["compact"] is True
    assert kw["include_formulas"] is False
    assert kw["include_validation"] is False
    assert kw["include_effective_format"] is True  # untouched default


def test_describe_maps_ranges_and_max_cells(patched):
    _run(["describe", "ID", "Cliff!A1:C2", "Plan!A1", "--max-cells", "500"])
    assert patched["name"] == "describe"
    # ranges is the first positional core arg after spreadsheet_id; max_cells is a kwarg.
    assert patched["args"][0] == ["Cliff!A1:C2", "Plan!A1"]
    assert patched["kwargs"]["max_cells"] == 500
    assert patched["kwargs"]["data_filters"] is None


def test_describe_data_filter_json_maps_and_drops_ranges(patched):
    # SPEC §6 P2: --data-filter-json drives core.describe(data_filters=...); the empty ranges
    # positional becomes None (core enforces the exactly-one contract).
    _run([
        "describe",
        "ID",
        "--data-filter-json",
        '[{"developerMetadataLookup":{"metadataKey":"block:totals"}}]',
    ])
    assert patched["name"] == "describe"
    assert patched["args"][0] is None
    assert patched["kwargs"]["data_filters"] == [
        {"developerMetadataLookup": {"metadataKey": "block:totals"}}
    ]


def test_describe_max_cells_defaults_unlimited(patched):
    _run(["describe", "ID", "Cliff!A1:C2"])
    assert patched["kwargs"]["max_cells"] is None


def test_describe_text_render(patched, monkeypatch, capsys):
    # The text renderer surfaces per-region cells, range-scoped CF (with index), and merges.
    monkeypatch.setattr(
        cli.core,
        "describe",
        lambda *a, **k: {
            "ok": True,
            "spreadsheetId": "X",
            "regions": [
                {
                    "range": "Cliff!A1:C2",
                    "sheet": "Cliff",
                    "cells": [
                        {"a1": "A1", "value": "30", "formula": "=SUM(B2:C2)"},
                    ],
                    "merges": ["Cliff!B1:C1"],
                    "conditionalFormats": [
                        {"index": 0, "line": "[Cliff!A1:A100] if NUMBER_GREATER(0) -> bg #C8E6C9"}
                    ],
                    "tables": [],
                    "bandedRanges": [],
                    "protectedRanges": [],
                    "validationSummary": {"cells": 0, "rules": []},
                }
            ],
        },
    )
    rc = cli.main(["describe", "ID", "Cliff!A1:C2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# Cliff!A1:C2" in out
    assert "=SUM(B2:C2)" in out
    assert "CF [0]: [Cliff!A1:A100] if NUMBER_GREATER(0) -> bg #C8E6C9" in out
    assert "merge: Cliff!B1:C1" in out


def test_formula_patterns_maps_ranges_and_sample_default(patched):
    _run(["formula-patterns", "ID", "Cliff!K3:K52", "Cliff!L3:L52"])
    assert patched["name"] == "formula_patterns"
    assert patched["args"][0] == ["Cliff!K3:K52", "Cliff!L3:L52"]
    assert patched["kwargs"]["sample"] is True  # sample on by default


def test_formula_patterns_no_sample_flag(patched):
    _run(["formula-patterns", "ID", "Cliff!K3:K52", "--no-sample"])
    assert patched["kwargs"]["sample"] is False


def test_formula_patterns_text_render(patched, monkeypatch, capsys):
    # The text renderer follows the SPEC §4.2 shape: header on the first template line, sample as
    # "K3 -> 185"; a non-reducible column appends a verbatim marker.
    monkeypatch.setattr(
        cli.core,
        "formula_patterns",
        lambda *a, **k: {
            "ok": True,
            "spreadsheetId": "X",
            "columns": [
                {
                    "col": "Cliff!K",
                    "reduced": True,
                    "templates": [
                        {
                            "formula": "=SUM(J{r}:R{r})",
                            "rows": "3:52",
                            "cells": 50,
                            "sample": {"a1": "K3", "value": 185},
                        }
                    ],
                },
                {
                    "col": "Cliff!M",
                    "reduced": False,
                    "templates": [
                        {"formula": "=A1+B2", "rows": "3:3", "cells": 1},
                    ],
                },
            ],
        },
    )
    rc = cli.main(["formula-patterns", "ID", "Cliff!K3:M52"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Cliff!K  =SUM(J{r}:R{r})" in out
    assert "rows 3:52" in out
    assert "(50)" in out
    assert "K3 -> 185" in out
    assert "not reduced" in out


def test_read_values_formula_render_is_address_keyed(patched, monkeypatch, capsys):
    # SPEC §4.4: a formula read is SPARSE, so text renders address-keyed ("C5: =SUM(...)") rather
    # than a dense rectangle. The anchor → absolute-A1 expansion lives in core.format.
    monkeypatch.setattr(
        cli.core,
        "read_values",
        lambda services, sid, ranges, **kw: {
            "ok": True,
            "spreadsheetId": sid,
            "render": "formula",
            "ranges": [
                {"range": "Cliff!C5:C6", "values": [["=SUM(A5:B5)"], ["=SUM(A6:B6)"]]}
            ],
        },
    )
    rc = cli.main(["read-values", "ID", "Cliff!C5:C6", "--render", "formula"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["Cliff!C5: =SUM(A5:B5)", "Cliff!C6: =SUM(A6:B6)"]


def test_read_values_plain_render_keeps_rectangle(patched, monkeypatch, capsys):
    # A dense value read keeps the rectangle + range form (only sparse formula reads go addressed).
    monkeypatch.setattr(
        cli.core,
        "read_values",
        lambda services, sid, ranges, **kw: {
            "ok": True,
            "spreadsheetId": sid,
            "render": "plain",
            "ranges": [{"range": "S!A1:B1", "values": [["a", "b"]]}],
        },
    )
    rc = cli.main(["read-values", "ID", "S!A1:B1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "render=plain" in out
    assert "a | b" in out


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
        # v0.3 reads (SPEC §3/§4): the unified region read + formula-pattern collapse.
        "describe", "formula-patterns",
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


# ===========================================================================================
# Terse-renderer contracts pinned after the doc-review pass: the default output must actually
# show what the docs say it shows (locale/tz on overview, the v0.2 structural lines, canonical
# rich-text segments, and a non-duplicated inspect header).


def test_overview_renders_locale_and_timezone(patched, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.core,
        "overview",
        lambda services, sid: {
            "ok": True,
            "spreadsheetId": sid,
            "title": "Tracker",
            "locale": "en_US",
            "timeZone": "America/New_York",
            "sheets": [],
            "namedRanges": [],
        },
    )
    rc = _run(["overview", "ID"])
    assert rc == 0
    assert "(locale=en_US, tz=America/New_York)" in capsys.readouterr().out


def test_structure_read_renders_v02_object_lines(patched, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.core,
        "structure",
        lambda services, sid, **kw: {
            "ok": True,
            "spreadsheetId": sid,
            "namedRanges": [],
            "sheets": [
                {
                    "sheet": "Sales",
                    "sheetId": 12,
                    "merges": [],
                    "protectedRanges": [],
                    "dimensionGroups": [],
                    "tables": [{"line": 'table "Q3" [Sales!A1:F500] cols: Region:TEXT'}],
                    "basicFilter": {"line": "basicFilter [Sales!A1:F500] sort C asc"},
                    "filterViews": [{"line": 'filterView 123 "Open only" [Sales!A1:F500]'}],
                    "bandedRanges": [{"line": "banding 7 [Sales!A1:F500] rows: hdr #4285F4"}],
                    "slicers": [{"line": 'slicer 88 "Region" col 0 [Sales!A1:F500] @ Dash!I1'}],
                }
            ],
        },
    )
    rc = _run(["structure", "ID", "--action", "read", "--sheet", "Sales"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# Sales (id=12)" in out
    assert 'table "Q3" [Sales!A1:F500]' in out
    assert "basicFilter [Sales!A1:F500] sort C asc" in out
    assert 'filterView 123 "Open only"' in out
    assert "banding 7 [Sales!A1:F500]" in out
    assert 'slicer 88 "Region" col 0' in out


def test_inspect_header_not_duplicated_for_qualified_range(patched, monkeypatch, capsys):
    # Core returns ``range`` already sheet-qualified; the renderer must not prepend the sheet
    # again (the old behavior printed "Dash!Dash!A1").
    monkeypatch.setattr(
        cli.core,
        "inspect",
        lambda services, sid, rng, **kw: {
            "ok": True,
            "spreadsheetId": sid,
            "sheet": "Dash",
            "range": "Dash!A1",
            "rows": 1,
            "cols": 1,
            "merges": [],
            "cells": [],
        },
    )
    rc = _run(["inspect", "ID", "Dash!A1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Dash!A1  1x1" in out
    assert "Dash!Dash!" not in out


def test_inspect_runs_render_canonical_segments(patched, monkeypatch, capsys):
    # The runs fragment must come from core's text_runs_line: offsets as start:end, canonical
    # token order (fg before the boolean styles), and per-run links.
    monkeypatch.setattr(
        cli.core,
        "inspect",
        lambda services, sid, rng, **kw: {
            "ok": True,
            "spreadsheetId": sid,
            "sheet": "Dash",
            "range": "Dash!A1",
            "rows": 1,
            "cols": 1,
            "merges": [],
            "cells": [
                {
                    "a1": "A1",
                    "value": "Docs / Sheet",
                    "runs": [
                        {
                            "start": 0,
                            "text": "Docs",
                            "format": {"bold": True, "fg": "#1155CC"},
                            "link": "https://docs.example.com",
                        },
                        {"start": 4, "text": " / Sheet", "link": "https://sheet.example.com"},
                    ],
                }
            ],
        },
    )
    rc = _run(["inspect", "ID", "Dash!A1", "--rich-text"])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'runs: "Docs"[0:4 fg #1155CC bold link https://docs.example.com]' in out
    assert '" / Sheet"[4:12 link https://sheet.example.com]' in out


def test_single_form_cf_result_renders_index_and_rule(patched, monkeypatch, capsys):
    # The single-form set_conditional_format result carries action+index+rule; it must NOT be
    # swallowed by the data_ops action-summary path (which would drop index and rule and print
    # just "add: sheet=Budget") — it falls through to the generic renderer instead.
    monkeypatch.setattr(
        cli.core,
        "set_conditional_format",
        lambda services, sid, **kw: {
            "ok": True,
            "spreadsheetId": sid,
            "action": "add",
            "sheet": "Budget",
            "index": 0,
            "rule": "[Budget!D2:D40] if CUSTOM_FORMULA(=$D2<0) -> bg #F4C7C3 bold",
        },
    )
    rc = _run(
        [
            "set-conditional-format",
            "ID",
            "--action",
            "add",
            "--sheet",
            "Budget",
            "--index",
            "0",
            "--rule",
            "[Budget!D2:D40] if CUSTOM_FORMULA(=$D2<0) -> bg #F4C7C3 bold",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "action: add" in out
    assert "index: 0" in out
    assert "rule: [Budget!D2:D40] if CUSTOM_FORMULA(=$D2<0) -> bg #F4C7C3 bold" in out


# ----------------------------------------------------------- ISSUES.md #9b CLI never tracebacks


def test_cli_network_timeout_emits_structured_error_not_traceback(monkeypatch, capsys):
    import gsheets.cli as cli

    monkeypatch.setattr(cli.auth, "build_services", lambda scopes_mode=None: object())
    monkeypatch.setattr(
        cli.core, "inspect", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("read timed out"))
    )
    rc = cli.main(["--json", "inspect", "<ID>", "Cliff!A1:AF51"])
    assert rc == 1
    err = capsys.readouterr().err
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "network_timeout"
    # No raw traceback leaked to stderr.
    assert "Traceback (most recent call last)" not in err


def test_cli_unexpected_exception_is_structured(monkeypatch, capsys):
    import gsheets.cli as cli

    monkeypatch.setattr(cli.auth, "build_services", lambda scopes_mode=None: object())
    monkeypatch.setattr(
        cli.core, "overview", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    rc = cli.main(["overview", "<ID>"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "internal_error" in err
    assert "Traceback" not in err


# ===========================================================================================
# Retry / backoff global flags (ISSUES.md #25, v0.4.0). Retry is OFF BY DEFAULT; the flags
# resolve to a core.retry.RetryPolicy that is ACTIVATED around the build_services + dispatch +
# render block. The robust assertion captures core.retry.current_policy() from inside a
# monkeypatched core fn — i.e. exactly what the auth-layer request builder would read at
# .execute() time — rather than poking at internals.
# ===========================================================================================


@pytest.fixture
def capture_policy(monkeypatch):
    """Patch ``auth.build_services`` + ``core.overview`` to capture the ACTIVE retry policy.

    The stub reads :func:`gsheets.core.retry.current_policy` from inside the dispatched core call,
    so the captured value is the policy the auth-layer request builder would see at ``.execute()``
    time. Returns a one-key dict the test reads (``captured["policy"]``).
    """
    from gsheets.core import retry as retry_mod

    captured: dict = {}
    monkeypatch.setattr(cli.auth, "build_services", lambda scopes_mode=None: object())

    def _stub(services, spreadsheet_id):
        captured["policy"] = retry_mod.current_policy()
        return {"ok": True, "spreadsheetId": spreadsheet_id, "title": "T", "sheets": [], "namedRanges": []}

    monkeypatch.setattr(cli.core, "overview", _stub)
    return captured


def test_no_retry_flags_default_policy_is_off(capture_policy):
    # The v0.4.0 default: with NO retry flags the activated policy is DISABLED (fail-fast), not
    # the always-on backoff of pre-v0.4.
    rc = _run(["overview", "ID"])
    assert rc == 0
    policy = capture_policy["policy"]
    assert policy.enabled is False


def test_default_backoff_strategy_resolves_to_preset(capture_policy):
    from gsheets.core.retry import RetryPolicy

    rc = _run(["--default-backoff-strategy", "overview", "ID"])
    assert rc == 0
    policy = capture_policy["policy"]
    assert policy == RetryPolicy.default_preset()
    assert policy.enabled is True
    assert policy.strategy == "exponential_jitter"
    assert policy.max_retries == 4
    assert policy.total_deadline == 60.0


def test_no_retry_flag_forces_disabled(capture_policy):
    from gsheets.core.retry import RetryPolicy

    rc = _run(["--no-retry", "overview", "ID"])
    assert rc == 0
    policy = capture_policy["policy"]
    assert policy == RetryPolicy.DISABLED
    assert policy.enabled is False


def test_no_retry_overrides_enabling_env(capture_policy, monkeypatch):
    # --no-retry is an explicit fail-fast that must win over a GSHEETS_BACKOFF_* env var that would
    # otherwise enable retry.
    monkeypatch.setenv("GSHEETS_BACKOFF_STRATEGY", "exponential")
    rc = _run(["--no-retry", "overview", "ID"])
    assert rc == 0
    assert capture_policy["policy"].enabled is False


def test_granular_flags_map_one_to_one_to_policy(capture_policy):
    rc = _run(
        [
            "--retries",
            "7",
            "--backoff",
            "exponential-jitter",
            "--retry-base-delay",
            "0.25",
            "--retry-max-delay",
            "10",
            "--retry-deadline",
            "45",
            "--retry-after-cap",
            "20",
            "--honor-retry-after",
            "overview",
            "ID",
        ]
    )
    assert rc == 0
    policy = capture_policy["policy"]
    # Granular flags explicitly ENABLE retry and map 1:1 onto the policy fields.
    assert policy.enabled is True
    assert policy.max_retries == 7
    # --backoff exponential-jitter maps to the underscore core strategy name.
    assert policy.strategy == "exponential_jitter"
    assert policy.base_delay == 0.25
    assert policy.max_delay == 10.0
    assert policy.total_deadline == 45.0  # --retry-deadline -> total_deadline
    assert policy.retry_after_cap == 20.0
    assert policy.honor_retry_after is True


def test_granular_no_honor_retry_after_maps_false(capture_policy):
    # argparse.BooleanOptionalAction: --no-honor-retry-after drives honor_retry_after=False (and is
    # itself a granular flag that enables retry).
    rc = _run(["--no-honor-retry-after", "overview", "ID"])
    assert rc == 0
    policy = capture_policy["policy"]
    assert policy.enabled is True
    assert policy.honor_retry_after is False


def test_granular_deadline_zero_means_no_overall_cap(capture_policy):
    # --retry-deadline <= 0 clears the overall cap (total_deadline=None).
    rc = _run(["--retry-deadline", "0", "--retries", "2", "overview", "ID"])
    assert rc == 0
    policy = capture_policy["policy"]
    assert policy.enabled is True
    assert policy.total_deadline is None


def test_backoff_flags_conflict_preset_plus_granular(patched, capsys):
    # --default-backoff-strategy + a granular flag (--retries) is a structured ok:false error.
    rc = _run(["--default-backoff-strategy", "--retries", "3", "overview", "ID"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "backoff_flags_conflict" in err
    # The destructive/dispatch path must not have run.
    assert patched.get("name") != "overview"


def test_backoff_flags_conflict_no_retry_plus_backoff(patched, capsys):
    # --no-retry + a granular flag (--backoff) is a structured ok:false error.
    rc = _run(["--no-retry", "--backoff", "fixed", "overview", "ID"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "backoff_flags_conflict" in err
    assert patched.get("name") != "overview"


def test_backoff_flags_conflict_preset_plus_no_retry(patched, capsys):
    rc = _run(["--default-backoff-strategy", "--no-retry", "overview", "ID"])
    assert rc == 1
    assert "backoff_flags_conflict" in capsys.readouterr().err
    assert patched.get("name") != "overview"


def test_backoff_flags_conflict_json_envelope(patched, capsys):
    # Under --json the conflict surfaces as the structured ok:false envelope (exit 1).
    rc = _run(["--json", "--default-backoff-strategy", "--retries", "3", "overview", "ID"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "backoff_flags_conflict"


def test_build_parser_registers_retry_flags():
    # The retry flags live on the TOP-LEVEL parser (global), not on a subparser.
    parser = cli.build_parser()
    dests = {a.dest for a in parser._actions}
    assert {
        "default_backoff_strategy",
        "no_retry",
        "retries",
        "backoff",
        "retry_base_delay",
        "retry_max_delay",
        "retry_deadline",
        "retry_after_cap",
        "honor_retry_after",
    } <= dests
