"""Unit tests for ``gsheets.core.paths`` (SPEC §2.3 — the PURE path-safety + file-output helper).

The MCP-only ``out_path`` escape valve (SPEC §2) writes a serialized read to a local file and
returns a small handle instead of the payload. The path-safety helper that backs it is PURE core
(stdlib only): it resolves a path to absolute, refuses to write under a missing parent directory
(never ``mkdir``), and HARD-REFUSES any credential / config path so a read can never clobber
secrets. These tests pin:

* relative paths resolve against the cwd; absolute paths pass through;
* a missing parent directory raises a clean ``SheetsError("bad_out_path")`` (no ``mkdir``);
* credential globs (``*token*.json``, ``gcp-oauth.keys.json``, ``service-account*.json``,
  ``credentials.json``, ``*.pem``, ``.env*``) and the config / secrets dirs are refused
  (``bad_out_path``);
* the on-disk bytes equal ``render(result, fmt)`` encoded utf-8 (ONE shared serializer);
* the returned handle has the documented shape ``{ok, path, format, rows, cols, bytes, preview}``
  with the preview truncated to the first ~5 rows/records.

Pure test scaffolding: stdlib + ``pytest`` only; never imports ``fastmcp``/``mcp``/``argparse``.
"""

from __future__ import annotations

import importlib
import json
import os

import pytest

from gsheets.core.errors import SheetsError

paths = importlib.import_module("gsheets.core.paths")
fmtmod = importlib.import_module("gsheets.core.format")


# --------------------------------------------------------------------------- result builders


def _read_values(ranges, render="plain"):
    return {
        "ok": True,
        "spreadsheetId": "<ID>",
        "render": render,
        "ranges": [{"range": a1, "values": rows} for a1, rows in ranges],
    }


def _structured():
    return {
        "ok": True,
        "spreadsheetId": "<ID>",
        "sheet": "S",
        "range": "A1:B2",
        "rows": 2,
        "cols": 2,
        "cells": [{"a1": "A1", "value": "x"}],
        "merges": [],
    }


# =========================================================================== resolve_out_path


class TestResolveOutPath:
    def test_relative_path_resolves_against_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        resolved = paths.resolve_out_path("out.csv")
        assert resolved == str((tmp_path / "out.csv").resolve())
        assert os.path.isabs(resolved)

    def test_absolute_path_passes_through(self, tmp_path):
        target = tmp_path / "out.json"
        resolved = paths.resolve_out_path(str(target))
        assert resolved == str(target.resolve())

    def test_missing_parent_dir_raises_bad_out_path(self, tmp_path):
        target = tmp_path / "does_not_exist" / "out.csv"
        with pytest.raises(SheetsError) as exc:
            paths.resolve_out_path(str(target))
        assert exc.value.code == "bad_out_path"
        assert exc.value.hint

    def test_existing_parent_with_new_file_is_fine(self, tmp_path):
        # The named file need not exist (it will be created); only the parent dir must exist.
        target = tmp_path / "fresh.csv"
        assert not target.exists()
        resolved = paths.resolve_out_path(str(target))
        assert resolved == str(target.resolve())

    def test_never_mkdir_on_missing_parent(self, tmp_path):
        target = tmp_path / "nope" / "out.csv"
        with pytest.raises(SheetsError):
            paths.resolve_out_path(str(target))
        # The helper must NOT have created the parent directory.
        assert not (tmp_path / "nope").exists()

    @pytest.mark.parametrize(
        "name",
        [
            "token.json",
            "my-token.json",
            "oauth_token.json",
            "gcp-oauth.keys.json",
            "service-account.json",
            "service-account-prod.json",
            "credentials.json",
            "key.pem",
            "server.pem",
            ".env",
            ".env.local",
            ".env.production",
        ],
    )
    def test_credential_filenames_are_refused(self, tmp_path, name):
        target = tmp_path / name
        with pytest.raises(SheetsError) as exc:
            paths.resolve_out_path(str(target))
        assert exc.value.code == "bad_out_path"

    def test_plain_data_filenames_are_allowed(self, tmp_path):
        # A normal data file is fine even if it mentions "data" — only credential shapes refuse.
        for name in ("out.csv", "data.json", "report.tsv", "values.jsonl", "tokens_report.txt"):
            target = tmp_path / name
            assert paths.resolve_out_path(str(target)) == str(target.resolve())

    def test_config_dir_is_refused(self, tmp_path, monkeypatch):
        # ~/.config/google-sheets-mcp/ holds the app's own credentials — never writable.
        fake_home = tmp_path / "home"
        cfg = fake_home / ".config" / "google-sheets-mcp"
        cfg.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fake_home))
        target = cfg / "out.csv"
        with pytest.raises(SheetsError) as exc:
            paths.resolve_out_path(str(target))
        assert exc.value.code == "bad_out_path"

    def test_config_subdir_is_refused(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        sub = fake_home / ".config" / "google-sheets-mcp" / "nested"
        sub.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fake_home))
        target = sub / "out.csv"
        with pytest.raises(SheetsError) as exc:
            paths.resolve_out_path(str(target))
        assert exc.value.code == "bad_out_path"

    def test_secrets_dir_is_refused(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        secrets = fake_home / ".secrets"
        secrets.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fake_home))
        target = secrets / "out.csv"
        with pytest.raises(SheetsError) as exc:
            paths.resolve_out_path(str(target))
        assert exc.value.code == "bad_out_path"


# =========================================================================== write_file_handle


class TestWriteFileHandle:
    def test_bytes_on_disk_equal_render(self, tmp_path):
        # Compare RAW BYTES — render() uses RFC-4180 \r\n for csv, and a text-mode read would
        # silently universal-newline-translate them away (a false mismatch).
        result = _read_values([("S!A1:B2", [["a", "b"], ["c", "d"]])])
        target = tmp_path / "out.csv"
        paths.write_file_handle(result, "csv", str(target))
        on_disk = target.read_bytes()
        assert on_disk == fmtmod.render(result, "csv").encode("utf-8")

    def test_handle_shape(self, tmp_path):
        result = _read_values([("S!A1:B2", [["a", "b"], ["c", "d"]])])
        target = tmp_path / "out.csv"
        handle = paths.write_file_handle(result, "csv", str(target))
        assert handle["ok"] is True
        assert handle["path"] == str(target.resolve())
        assert handle["format"] == "csv"
        assert handle["rows"] == 2
        assert handle["cols"] == 2
        # bytes == the byte length of the file on disk.
        assert handle["bytes"] == target.stat().st_size
        assert handle["bytes"] == len(fmtmod.render(result, "csv").encode("utf-8"))
        assert handle["preview"] == [["a", "b"], ["c", "d"]]

    def test_handle_path_is_absolute_resolved(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _read_values([("S!A1:A1", [["x"]])])
        handle = paths.write_file_handle(result, "csv", "rel.csv")
        assert handle["path"] == str((tmp_path / "rel.csv").resolve())
        assert os.path.isabs(handle["path"])

    def test_preview_truncates_to_first_five_rows(self, tmp_path):
        rows = [[str(i), str(i * 2)] for i in range(20)]
        result = _read_values([("S!A1:B20", rows)])
        target = tmp_path / "big.csv"
        handle = paths.write_file_handle(result, "csv", str(target))
        assert handle["rows"] == 20
        # preview is the first ~5 rows only — never the whole grid.
        assert len(handle["preview"]) == 5
        assert handle["preview"] == rows[:5]
        # The FULL grid still hit disk (compare raw bytes; csv is \r\n-terminated).
        on_disk = target.read_bytes()
        assert on_disk == fmtmod.render(result, "csv").encode("utf-8")

    def test_jsonl_preview_is_first_five_records(self, tmp_path):
        rows = [[str(i)] for i in range(10)]
        result = _read_values([("S!A1:A10", rows)])
        target = tmp_path / "out.jsonl"
        handle = paths.write_file_handle(result, "jsonl", str(target))
        assert handle["format"] == "jsonl"
        # jsonl preview: first ~5 records (one per row).
        assert len(handle["preview"]) == 5
        assert all(isinstance(rec, dict) for rec in handle["preview"])
        assert handle["preview"][0] == {"range": "S!A1:A10", "row": ["0"]}
        on_disk = target.read_text(encoding="utf-8")
        assert on_disk == fmtmod.render(result, "jsonl")

    def test_json_structured_result_writes_and_handles(self, tmp_path):
        # A structured result is fine for json (the format layer handles json on anything).
        result = _structured()
        target = tmp_path / "out.json"
        handle = paths.write_file_handle(result, "json", str(target))
        on_disk = target.read_text(encoding="utf-8")
        assert on_disk == fmtmod.render(result, "json")
        assert json.loads(on_disk) == result
        assert handle["format"] == "json"
        assert handle["bytes"] == target.stat().st_size

    def test_csv_on_structured_result_raises_format_unsupported(self, tmp_path):
        # csv/tsv on a structured result must still raise format_unsupported (no half-written file).
        result = _structured()
        target = tmp_path / "out.csv"
        with pytest.raises(SheetsError) as exc:
            paths.write_file_handle(result, "csv", str(target))
        assert exc.value.code == "format_unsupported"

    def test_missing_parent_raises_before_write(self, tmp_path):
        result = _read_values([("S!A1:A1", [["x"]])])
        target = tmp_path / "missing" / "out.csv"
        with pytest.raises(SheetsError) as exc:
            paths.write_file_handle(result, "csv", str(target))
        assert exc.value.code == "bad_out_path"
        assert not (tmp_path / "missing").exists()

    def test_credential_path_refused_before_write(self, tmp_path):
        result = _read_values([("S!A1:A1", [["x"]])])
        target = tmp_path / "token.json"
        with pytest.raises(SheetsError) as exc:
            paths.write_file_handle(result, "json", str(target))
        assert exc.value.code == "bad_out_path"
        assert not target.exists()


# =========================================================================== purity guard


class TestPurity:
    def test_module_imports_no_transport(self):
        import sys

        import gsheets.core.paths  # noqa: F401

        forbidden = ("fastmcp", "mcp", "argparse", "pydantic", "gsheets.models")
        for name in sys.modules["gsheets.core.paths"].__dict__.values():
            mod = getattr(name, "__module__", "")
            assert not any(
                mod.startswith(f) for f in forbidden if isinstance(mod, str)
            )
