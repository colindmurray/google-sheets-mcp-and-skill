"""Whole-workbook and per-sheet export (DESIGN §3.x — Feature: download a spreadsheet to disk).

Two distinct backends, picked by ``format``:

* **pdf / xlsx / ods** — WHOLE-WORKBOOK exports via the **Drive v3** API
  (``services.drive.files().export_media(fileId=..., mimeType=...)``). Google renders the whole
  spreadsheet server-side; the bytes are streamed down with
  ``googleapiclient.http.MediaIoBaseDownload`` into an in-memory ``io.BytesIO`` (loop until
  ``done``). The ``sheet`` arg is IGNORED for these (Drive exports the entire workbook). REQUIRES
  Drive — when ``services.drive`` is ``None`` this raises ``SheetsError("drive_unavailable")``
  exactly as ``core/comments.py`` does (these formats use the Drive API).
* **csv / tsv** — SINGLE-SHEET exports done WITHOUT Drive. Drive's csv export only ever emits the
  first sheet, so instead we read the named sheet's values through the **Sheets** API
  (reusing :func:`gsheets.core.values.read_values` with ``render="plain"``) and serialize the 2D
  rows through the shared output-format layer (:func:`gsheets.core.format.render_grid`, delimiter
  ``","`` for csv, ``"\t"`` for tsv, utf-8) — ONE csv path shared with CLI ``--format``/MCP file
  output. These need only the Sheets scope, so ``sheet`` is REQUIRED — without it this raises
  ``SheetsError("missing_sheet")``.

The bytes are written to ``path`` (or ``f"{spreadsheet_id}.{ext}"`` in the cwd when omitted) and
the written length is reported back so a caller can verify the download.

PURE core module: imports only stdlib (``io``, ``os``) + ``googleapiclient`` + sibling core
modules (``errors``, ``format``, ``paths``, ``service``, ``values``). It must NEVER import ``fastmcp``, ``mcp``,
``argparse``, ``pydantic``, or ``gsheets.models`` (DESIGN §1 boundary). ``MediaIoBaseDownload``
(from ``googleapiclient.http``) is imported LAZILY inside :func:`_export_via_drive` rather than at
module top: ``googleapiclient.http`` transitively pulls in ``httplib2`` -> ``argparse``, which the
boundary guard forbids from a clean ``import gsheets.core`` (this module is re-exported there).
"""

from __future__ import annotations

import io
import os

from googleapiclient.errors import HttpError

from .errors import SheetsError, classify_google_error
from .format import render_grid
from .paths import resolve_out_path
from .service import SheetsServices
from .values import read_values

# ``googleapiclient.http`` (for ``MediaIoBaseDownload``) transitively imports ``httplib2``, which
# imports ``argparse`` — a module the PURE-core boundary guard forbids from a clean
# ``import gsheets.core`` (DESIGN §1). Since this module is re-exported from ``gsheets.core``, a
# TOP-LEVEL import here would leak ``argparse`` into the package import. We therefore bind it
# LAZILY (sentinel below, populated on first Drive export inside ``_export_via_drive``). The
# module-level name is still the monkeypatch seam the tests patch.
MediaIoBaseDownload = None  # lazily bound on first Drive-backed export (see _export_via_drive)

# Normalized format -> Drive ``export_media`` MIME type. Only the Drive-backed (whole-workbook)
# formats appear here; csv/tsv are serialized locally and never hit Drive (Drive csv export only
# emits the first sheet — a verified gotcha).
_DRIVE_MIME: dict[str, str] = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
}

# Normalized format -> the local MIME type reported for the Sheets-backed text exports.
_TEXT_MIME: dict[str, str] = {
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
}

# Normalized format -> the csv-module delimiter for the Sheets-backed text exports.
_TEXT_DELIMITER: dict[str, str] = {
    "csv": ",",
    "tsv": "\t",
}

# Every accepted format (lowercased). The file extension equals the normalized format token.
_FORMATS: tuple[str, ...] = ("pdf", "xlsx", "ods", "csv", "tsv")


def export(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    format: str = "pdf",
    path: str | None = None,
    sheet: str | None = None,
) -> dict:
    """Export a spreadsheet to a local file (DESIGN §3.x).

    ``format`` (case-insensitive) is one of ``"pdf"``, ``"xlsx"``, ``"ods"``, ``"csv"``, ``"tsv"``.

    * ``pdf`` / ``xlsx`` / ``ods`` are WHOLE-WORKBOOK exports via the **Drive v3** API
      (``files().export_media``); the ``sheet`` arg is IGNORED for these. They REQUIRE a Drive
      service — when ``services.drive`` is ``None`` this raises ``SheetsError("drive_unavailable")``.
    * ``csv`` / ``tsv`` export a SINGLE named sheet WITHOUT Drive (Drive's csv export only emits the
      first sheet): the sheet's plain values are read via :func:`gsheets.core.values.read_values`
      and serialized locally with the stdlib ``csv`` module. ``sheet`` is REQUIRED — omitting it
      raises ``SheetsError("missing_sheet")``.

    The bytes are written to ``path``; when ``path`` is ``None`` it defaults to
    ``f"{spreadsheet_id}.{ext}"`` (``ext`` == the normalized format) in the current directory.

    Args:
        services: The authed handle. ``services.drive`` MUST be non-``None`` for pdf/xlsx/ods.
        spreadsheet_id: The spreadsheet's Drive file id.
        format: ``"pdf"`` (default) | ``"xlsx"`` | ``"ods"`` | ``"csv"`` | ``"tsv"`` (case-insensitive).
        path: Destination path; defaults to ``f"{spreadsheet_id}.{format}"`` in the cwd.
        sheet: The sheet name to export — REQUIRED for csv/tsv, IGNORED for pdf/xlsx/ods.

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "format": <normalized>, "mimeType": ...,
        "path": <written path>, "bytes": <int length written>}``.

    Raises:
        SheetsError: ``"bad_format"`` for an unknown format; ``"missing_sheet"`` when csv/tsv is
            requested without a ``sheet``; ``"drive_unavailable"`` when a Drive-backed format is
            requested but no Drive service is available; or ``"google_api_error"`` (via
            :func:`classify_google_error`) on a Drive/Sheets ``HttpError``.
    """
    fmt = _normalize_format(format)
    ext = fmt
    # Route the destination through the SAME path-safety gate as the read-side ``out_path`` valve
    # (``paths.resolve_out_path``): resolve relative->cwd and HARD-REFUSE credential-shaped names
    # and the ``~/.config/google-sheets-mcp`` / ``~/.secrets`` subtrees, so an export can never
    # clobber the very token/secret files a read is forbidden to overwrite (SPEC §2.3). Done BEFORE
    # the fetch so a bad destination fails fast without spending an API call.
    out_path = resolve_out_path(path if path is not None else f"{spreadsheet_id}.{ext}")

    if fmt in _DRIVE_MIME:
        data = _export_via_drive(services, spreadsheet_id, fmt)
        mime_type = _DRIVE_MIME[fmt]
    else:
        data = _export_text_via_sheets(services, spreadsheet_id, fmt, sheet)
        mime_type = _TEXT_MIME[fmt]

    with open(out_path, "wb") as fh:
        written = fh.write(data)

    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "format": fmt,
        "mimeType": mime_type,
        "path": out_path,
        "bytes": written if written is not None else len(data),
    }


def _normalize_format(format: str) -> str:
    """Lowercase/strip ``format`` and validate it against the accepted set, else raise."""
    fmt = format.strip().lower() if isinstance(format, str) else format
    if fmt not in _FORMATS:
        raise SheetsError(
            "bad_format",
            f"unknown export format {format!r}; expected one of "
            "'pdf', 'xlsx', 'ods', 'csv', 'tsv'",
        )
    return fmt


def _export_via_drive(
    services: SheetsServices, spreadsheet_id: str, fmt: str
) -> bytes:
    """Whole-workbook export via Drive ``files().export_media`` -> raw bytes (DESIGN §3.x).

    Streams the rendered export down with ``MediaIoBaseDownload`` into an ``io.BytesIO`` (looping
    until ``done``). REQUIRES Drive — mirrors ``core/comments.py``: a ``None`` Drive service raises
    ``SheetsError("drive_unavailable")`` with the broad-scope hint.
    """
    if services.drive is None:
        raise SheetsError(
            "drive_unavailable",
            f"{fmt} export requires a Drive API service, but none is available",
            hint="enable a Drive scope (GSHEETS_SCOPES=broad)",
        )

    # LAZY import of the heavy/argparse-leaking ``googleapiclient.http`` (DESIGN §1 boundary): bind
    # the module-level ``MediaIoBaseDownload`` on first use unless a test (or caller) has already
    # patched it. Reading it back off this module keeps the monkeypatch seam intact.
    global MediaIoBaseDownload
    if MediaIoBaseDownload is None:
        from googleapiclient.http import MediaIoBaseDownload as _MediaIoBaseDownload

        MediaIoBaseDownload = _MediaIoBaseDownload

    buffer = io.BytesIO()
    try:
        request = services.drive.files().export_media(
            fileId=spreadsheet_id, mimeType=_DRIVE_MIME[fmt]
        )
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc

    return buffer.getvalue()


def _export_text_via_sheets(
    services: SheetsServices, spreadsheet_id: str, fmt: str, sheet: str | None
) -> bytes:
    """Single-sheet csv/tsv export WITHOUT Drive: read plain values + serialize locally.

    Reads the named sheet through :func:`gsheets.core.values.read_values` (``render="plain"``) and
    serializes the single range's 2D rows through :func:`gsheets.core.format.render_grid` (utf-8) —
    the shared csv path. REQUIRES a ``sheet`` (Drive's csv export only does the first sheet, so we
    always go through Sheets) — omitting it raises ``SheetsError("missing_sheet")``.
    """
    if sheet is None:
        raise SheetsError(
            "missing_sheet",
            "csv/tsv export requires a sheet name",
            hint="pass the sheet name to export (Drive csv export only does the first sheet)",
        )

    # read_values already classifies any Sheets HttpError through the single error envelope.
    result = read_values(services, spreadsheet_id, [sheet], render="plain")
    ranges = result.get("ranges") or []
    rows = ranges[0].get("values", []) if ranges else []

    # Delegate the csv/tsv serialization to the shared output-format layer (SPEC §1.2) so there
    # is ONE csv path — the bytes are byte-identical to what this used to build inline (and to
    # what CLI ``--format csv`` / MCP file output now produce).
    return render_grid(rows, _TEXT_DELIMITER[fmt]).encode("utf-8")
