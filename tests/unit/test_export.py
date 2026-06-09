"""Unit tests for ``gsheets.core.export`` (DESIGN §3.x — export a spreadsheet to disk).

All tests run against a MOCKED service (NO network). Two backends are exercised:

* **pdf / xlsx / ods** — Drive-backed whole-workbook exports. ``services.drive.files()
  .export_media(...)`` is mocked and ``MediaIoBaseDownload`` is monkeypatched with a fake that
  streams pre-seeded bytes through the ``while not done`` chunk loop, so no network runs. Tests
  assert the MIME type, the bytes written to ``tmp_path``, and the default-path derivation
  (``f"{id}.{ext}"`` in the cwd).
* **csv / tsv** — Sheets-backed single-sheet exports. ``read_values`` is monkeypatched to return a
  known 2D array and tests assert the EXACT csv/tsv text written (a golden-ish assertion on the
  serialized bytes).

Error paths covered: ``bad_format`` (unknown format), ``missing_sheet`` (csv without a sheet),
and ``drive_unavailable`` (pdf with ``services.drive=None``).

Pure test scaffolding: stdlib + ``pytest`` only; never imports ``fastmcp``/``mcp``/``argparse``.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest

from gsheets.core.errors import SheetsError
from gsheets.core.export import export
from gsheets.core.service import SheetsServices

# ``core/__init__`` re-exports ``export`` (the function) under the same name, which shadows the
# ``gsheets.core.export`` package *attribute* (per CPython's IMPORT_FROM, even
# ``import gsheets.core.export as x`` then resolves to the function). Reach the real MODULE object
# through ``sys.modules`` via ``import_module`` so monkeypatching its globals (read_values /
# MediaIoBaseDownload) hits the module, not the function. Mirrors test_dimensions.py.
export_mod = importlib.import_module("gsheets.core.export")

SHEET_ID = "<TEST_SPREADSHEET_ID>"


# --------------------------------------------------------------------------- service builders


def _make_service(*, with_drive=True, account_email=None):
    """Build a ``SheetsServices`` with a mocked Sheets handle and optional Drive handle.

    With Drive, ``drive.files().export_media`` returns a sentinel request object whose identity is
    captured so a test can assert it was passed to ``MediaIoBaseDownload``.
    """
    sheets = MagicMock(name="sheets_v4")
    if not with_drive:
        return SheetsServices(sheets=sheets, drive=None, account_email=account_email)
    drive = MagicMock(name="drive_v3")
    export_request = MagicMock(name="export_media_request")
    drive.files.return_value.export_media.return_value = export_request
    return SheetsServices(sheets=sheets, drive=drive, account_email=account_email)


def _fake_downloader_factory(payload: bytes, *, chunks: int = 1):
    """Build a ``MediaIoBaseDownload`` stand-in that streams ``payload`` over ``chunks`` calls.

    The fake writes ``payload`` into the supplied ``fd`` (the ``io.BytesIO`` buffer) and returns
    ``(status, done)`` from ``next_chunk()`` — ``done`` flips ``True`` on the final chunk so the
    core's ``while not done`` loop terminates. Records the ``request`` it was constructed with so a
    test can assert ``export_media``'s request flowed through unchanged.
    """
    constructed: dict = {}

    class _FakeDownloader:
        def __init__(self, fd, request, chunksize=None):
            self._fd = fd
            self._remaining = max(chunks, 1)
            constructed["fd"] = fd
            constructed["request"] = request
            # Split the payload across the requested number of chunks.
            n = self._remaining
            size = (len(payload) + n - 1) // n if payload else 0
            self._pieces = (
                [payload[i : i + size] for i in range(0, len(payload), size)]
                if payload
                else [b""]
            )
            # Ensure exactly ``n`` next_chunk() iterations even for tiny/empty payloads.
            while len(self._pieces) < n:
                self._pieces.append(b"")

        def next_chunk(self, num_retries=0):
            piece = self._pieces.pop(0)
            self._fd.write(piece)
            done = not self._pieces
            status = MagicMock(name="download_status")
            return status, done

    return _FakeDownloader, constructed


def _make_http_error(status: int = 404):
    """A minimal stand-in for a Drive ``googleapiclient.errors.HttpError``."""
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = status
    resp.reason = "Not Found"
    content = (
        b'{"error": {"code": %d, "status": "NOT_FOUND", "message": "nope"}}' % status
    )
    return HttpError(resp=resp, content=content)


# =========================================================================== Drive-backed exports


class TestDriveExports:
    @pytest.mark.parametrize(
        "fmt, mime",
        [
            ("pdf", "application/pdf"),
            (
                "xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
            ("ods", "application/vnd.oasis.opendocument.spreadsheet"),
        ],
    )
    def test_export_writes_bytes_and_reports_mime(self, fmt, mime, tmp_path, monkeypatch):
        payload = b"%PDF-fake-bytes-or-zip-bytes\x00\x01\x02"
        fake_dl, _constructed = _fake_downloader_factory(payload)
        monkeypatch.setattr(export_mod, "MediaIoBaseDownload", fake_dl)

        services = _make_service()
        dest = tmp_path / f"out.{fmt}"
        out = export(services, SHEET_ID, format=fmt, path=str(dest))

        assert out == {
            "ok": True,
            "spreadsheetId": SHEET_ID,
            "format": fmt,
            "mimeType": mime,
            "path": str(dest),
            "bytes": len(payload),
        }
        # The bytes actually landed on disk, byte-for-byte.
        assert dest.read_bytes() == payload
        # ``export_media`` was called with the right fileId + MIME.
        services.drive.files.return_value.export_media.assert_called_once_with(
            fileId=SHEET_ID, mimeType=mime
        )

    def test_format_is_case_insensitive(self, tmp_path, monkeypatch):
        payload = b"PDFDATA"
        fake_dl, _ = _fake_downloader_factory(payload)
        monkeypatch.setattr(export_mod, "MediaIoBaseDownload", fake_dl)
        services = _make_service()
        dest = tmp_path / "x.pdf"
        out = export(services, SHEET_ID, format="  PDF ", path=str(dest))
        assert out["format"] == "pdf"
        assert out["mimeType"] == "application/pdf"

    def test_multi_chunk_download_loops_until_done(self, tmp_path, monkeypatch):
        payload = b"0123456789abcdef"
        fake_dl, constructed = _fake_downloader_factory(payload, chunks=4)
        monkeypatch.setattr(export_mod, "MediaIoBaseDownload", fake_dl)
        services = _make_service()
        dest = tmp_path / "multi.xlsx"
        out = export(services, SHEET_ID, format="xlsx", path=str(dest))
        # All four chunks were assembled into the full payload.
        assert dest.read_bytes() == payload
        assert out["bytes"] == len(payload)
        # The exact request object from export_media flowed into MediaIoBaseDownload unchanged.
        assert (
            constructed["request"]
            is services.drive.files.return_value.export_media.return_value
        )

    def test_default_path_derived_from_id_and_ext(self, tmp_path, monkeypatch):
        payload = b"odsbytes"
        fake_dl, _ = _fake_downloader_factory(payload)
        monkeypatch.setattr(export_mod, "MediaIoBaseDownload", fake_dl)
        # cwd-relative default path -> chdir into tmp_path so we don't pollute the repo.
        monkeypatch.chdir(tmp_path)
        services = _make_service()
        out = export(services, SHEET_ID, format="ods")
        expected = f"{SHEET_ID}.ods"
        assert out["path"] == expected
        assert (tmp_path / expected).read_bytes() == payload

    def test_sheet_arg_ignored_for_whole_workbook(self, tmp_path, monkeypatch):
        payload = b"PDF"
        fake_dl, _ = _fake_downloader_factory(payload)
        monkeypatch.setattr(export_mod, "MediaIoBaseDownload", fake_dl)
        services = _make_service()
        dest = tmp_path / "ignored.pdf"
        # Passing a sheet name must not change the whole-workbook export behavior.
        out = export(services, SHEET_ID, format="pdf", path=str(dest), sheet="Sheet1")
        assert out["bytes"] == len(payload)
        # No Sheets values read happened for a Drive-backed export.
        services.sheets.spreadsheets.assert_not_called()

    def test_lazy_binds_media_download_on_first_drive_export(self, tmp_path, monkeypatch):
        # The module-level ``MediaIoBaseDownload`` is bound LAZILY from googleapiclient.http on the
        # FIRST Drive export (the argparse-boundary deferral, export.py:170-174). Force the unbound
        # state (None) and intercept the real ``from googleapiclient.http import ...`` so the lazy
        # branch runs without touching the network, then assert the global got populated.
        import googleapiclient.http as ghttp

        payload = b"lazy-bound-bytes"
        fake_dl, constructed = _fake_downloader_factory(payload)
        # Patch the SOURCE module attribute so the in-function ``from googleapiclient.http import
        # MediaIoBaseDownload`` resolves to our fake; reset the export module's seam to None so the
        # ``if MediaIoBaseDownload is None:`` branch is taken.
        monkeypatch.setattr(ghttp, "MediaIoBaseDownload", fake_dl)
        monkeypatch.setattr(export_mod, "MediaIoBaseDownload", None)

        services = _make_service()
        dest = tmp_path / "lazy.pdf"
        out = export(services, SHEET_ID, format="pdf", path=str(dest))

        # The bytes streamed through the lazily-bound downloader.
        assert dest.read_bytes() == payload
        assert out["bytes"] == len(payload)
        # The lazy branch populated the module-level seam from googleapiclient.http (no longer None).
        assert export_mod.MediaIoBaseDownload is fake_dl
        # And the real export_media request flowed into the freshly-bound downloader.
        assert (
            constructed["request"]
            is services.drive.files.return_value.export_media.return_value
        )

    def test_already_bound_media_download_not_reimported(self, tmp_path, monkeypatch):
        # When the seam is ALREADY bound (a prior export or a test patch), the lazy import is
        # skipped: a poisoned googleapiclient.http import would raise if it were re-entered.
        import googleapiclient.http as ghttp

        payload = b"already-bound"
        fake_dl, _ = _fake_downloader_factory(payload)
        monkeypatch.setattr(export_mod, "MediaIoBaseDownload", fake_dl)

        def _boom():  # pragma: no cover - must never be evaluated
            raise AssertionError("lazy import must be skipped when already bound")

        # Make the source attribute a property that explodes if read via re-import.
        monkeypatch.delattr(ghttp, "MediaIoBaseDownload", raising=False)

        services = _make_service()
        dest = tmp_path / "bound.xlsx"
        out = export(services, SHEET_ID, format="xlsx", path=str(dest))
        assert out["bytes"] == len(payload)
        # Still the same patched object — never replaced by a re-import.
        assert export_mod.MediaIoBaseDownload is fake_dl

    def test_drive_http_error_classified(self, tmp_path, monkeypatch):
        # export_media raises an HttpError -> classified through the single envelope.
        services = _make_service()
        services.drive.files.return_value.export_media.side_effect = _make_http_error(404)
        # Even though MediaIoBaseDownload is monkeypatched, export_media raises first.
        fake_dl, _ = _fake_downloader_factory(b"x")
        monkeypatch.setattr(export_mod, "MediaIoBaseDownload", fake_dl)
        with pytest.raises(SheetsError) as exc:
            export(services, SHEET_ID, format="pdf", path=str(tmp_path / "e.pdf"))
        assert exc.value.code == "google_api_error"
        assert exc.value.status == 404


# =========================================================================== Sheets-backed text


class TestTextExports:
    _ROWS = [
        ["Name", "Score", "Note"],
        ["Alice", "10", "ok"],
        ["Bob, Jr.", "20", 'has "quotes"'],
        ["", "", ""],
    ]

    def _patch_read_values(self, monkeypatch, rows):
        """Monkeypatch ``read_values`` to return a known single-range payload."""
        captured: dict = {}

        def _fake_read_values(services, spreadsheet_id, ranges, *, render="plain"):
            captured["services"] = services
            captured["spreadsheet_id"] = spreadsheet_id
            captured["ranges"] = ranges
            captured["render"] = render
            return {
                "ok": True,
                "spreadsheetId": spreadsheet_id,
                "render": render,
                "ranges": [{"range": ranges[0], "values": rows}],
            }

        monkeypatch.setattr(export_mod, "read_values", _fake_read_values)
        return captured

    def test_csv_exact_text_written(self, tmp_path, monkeypatch):
        captured = self._patch_read_values(monkeypatch, self._ROWS)
        services = _make_service()
        dest = tmp_path / "data.csv"
        out = export(services, SHEET_ID, format="csv", path=str(dest), sheet="Data")

        expected = (
            "Name,Score,Note\r\n"
            "Alice,10,ok\r\n"
            '"Bob, Jr.",20,"has ""quotes"""\r\n'
            ",,\r\n"
        )
        # Read raw bytes so RFC-4180 CRLF line terminators survive (text-mode read would
        # universal-newline-translate \r\n -> \n and hide the real on-disk content).
        assert dest.read_bytes() == expected.encode("utf-8")
        assert out["mimeType"] == "text/csv"
        assert out["format"] == "csv"
        assert out["bytes"] == len(expected.encode("utf-8"))
        # read_values was called for the single named sheet, plain render, no Drive needed.
        assert captured["ranges"] == ["Data"]
        assert captured["render"] == "plain"

    def test_tsv_exact_text_written(self, tmp_path, monkeypatch):
        self._patch_read_values(monkeypatch, self._ROWS)
        services = _make_service()
        dest = tmp_path / "data.tsv"
        out = export(services, SHEET_ID, format="tsv", path=str(dest), sheet="Data")

        # Tab delimiter; the comma/quote cells need no csv-quoting under TAB delimiter, but the
        # embedded double-quote cell still triggers quoting per the csv dialect.
        expected = (
            "Name\tScore\tNote\r\n"
            "Alice\t10\tok\r\n"
            'Bob, Jr.\t20\t"has ""quotes"""\r\n'
            "\t\t\r\n"
        )
        assert dest.read_bytes() == expected.encode("utf-8")
        assert out["mimeType"] == "text/tab-separated-values"
        assert out["format"] == "tsv"

    def test_csv_does_not_use_drive(self, tmp_path, monkeypatch):
        self._patch_read_values(monkeypatch, self._ROWS)
        # Even with Drive available, csv export goes through Sheets, never Drive.
        services = _make_service()
        export(services, SHEET_ID, format="csv", path=str(tmp_path / "x.csv"), sheet="S")
        services.drive.files.return_value.export_media.assert_not_called()

    def test_csv_works_without_drive_scope(self, tmp_path, monkeypatch):
        self._patch_read_values(monkeypatch, self._ROWS)
        # csv/tsv need only the Sheets scope -> a None Drive service is fine.
        services = _make_service(with_drive=False)
        out = export(services, SHEET_ID, format="csv", path=str(tmp_path / "y.csv"), sheet="S")
        assert out["ok"] is True

    def test_csv_default_path_uses_csv_ext(self, tmp_path, monkeypatch):
        self._patch_read_values(monkeypatch, [["a", "b"]])
        monkeypatch.chdir(tmp_path)
        services = _make_service()
        out = export(services, SHEET_ID, format="csv", sheet="S")
        assert out["path"] == f"{SHEET_ID}.csv"
        assert (tmp_path / f"{SHEET_ID}.csv").read_bytes() == b"a,b\r\n"

    def test_empty_sheet_writes_empty_file(self, tmp_path, monkeypatch):
        self._patch_read_values(monkeypatch, [])
        services = _make_service()
        dest = tmp_path / "empty.csv"
        out = export(services, SHEET_ID, format="csv", path=str(dest), sheet="S")
        assert dest.read_bytes() == b""
        assert out["bytes"] == 0


# =========================================================================== error paths


class TestExportErrors:
    def test_bad_format_raises(self, tmp_path):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            export(services, SHEET_ID, format="docx", path=str(tmp_path / "x.docx"))
        assert exc.value.code == "bad_format"
        # No file should be created for a rejected format.
        assert not (tmp_path / "x.docx").exists()

    def test_missing_sheet_for_csv_raises(self, tmp_path):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            export(services, SHEET_ID, format="csv", path=str(tmp_path / "x.csv"))
        assert exc.value.code == "missing_sheet"

    def test_missing_sheet_for_tsv_raises(self, tmp_path):
        services = _make_service()
        with pytest.raises(SheetsError) as exc:
            export(services, SHEET_ID, format="tsv", path=str(tmp_path / "x.tsv"))
        assert exc.value.code == "missing_sheet"

    def test_pdf_without_drive_raises_drive_unavailable(self, tmp_path):
        services = _make_service(with_drive=False)
        with pytest.raises(SheetsError) as exc:
            export(services, SHEET_ID, format="pdf", path=str(tmp_path / "x.pdf"))
        assert exc.value.code == "drive_unavailable"
        assert "Drive" in (exc.value.hint or "") or "broad" in (exc.value.hint or "")

    def test_xlsx_without_drive_raises_drive_unavailable(self, tmp_path):
        services = _make_service(with_drive=False)
        with pytest.raises(SheetsError) as exc:
            export(services, SHEET_ID, format="xlsx", path=str(tmp_path / "x.xlsx"))
        assert exc.value.code == "drive_unavailable"


# =========================================================================== purity guard


class TestPurity:
    def test_module_imports_no_transport(self):
        import sys

        import gsheets.core.export  # noqa: F401

        forbidden = ("fastmcp", "mcp", "argparse", "pydantic", "gsheets.models")
        src = sys.modules["gsheets.core.export"].__dict__
        for name in src.values():
            mod = getattr(name, "__module__", "")
            assert not any(mod.startswith(f) for f in forbidden if isinstance(mod, str))
