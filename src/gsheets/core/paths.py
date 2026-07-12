"""PURE path-safety + file-output helper for the MCP-only ``out_path`` escape valve (SPEC §2).

The big-read MCP tools (``sheets_read_values`` / ``sheets_inspect`` / ``sheets_read_many``) accept
an optional ``out_path``. When set, the adapter writes the serialized read to that local file and
returns a small *handle* instead of dumping the whole payload into the agent's context. The path
safety and the handle construction both live HERE, in pure core, so they are reusable and unit
testable, and so MCP file output is byte-identical to the shared :func:`gsheets.core.format.render`
(the CLI's piped output / ``export``'s on-disk bytes).

PURE core module: imports ONLY stdlib (``fnmatch``, ``os``, ``pathlib``). It must NEVER import
``fastmcp``, ``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models`` (SPEC §0.2 boundary). The
sibling ``format``/``errors`` imports are core-internal and stay boundary-clean.

Two responsibilities:

* :func:`resolve_out_path` — resolve a caller path to ABSOLUTE (relative -> cwd); raise
  ``SheetsError("bad_out_path")`` if the parent directory does not exist (NEVER ``mkdir``); and
  HARD-REFUSE any path under ``~/.config/google-sheets-mcp/`` or ``~/.secrets/``, or matching a
  credential glob (``*token*.json``, ``gcp-oauth.keys.json``, ``service-account*.json``,
  ``credentials.json``, ``*.pem``, ``.env*``), so a read can never clobber credentials (SPEC §2.3).
* :func:`write_file_handle` — resolve + safety-check the path, serialize ``result`` with the shared
  :func:`render`, write it utf-8, and return the handle dict ``{ok, path, format, rows, cols, bytes,
  preview}`` (SPEC §2.2). ``preview`` is the first ~5 rows (csv/tsv) or records (jsonl/json), so a
  large read costs a handful of tokens instead of the whole grid.
"""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path

from .errors import SheetsError

# Import the serializer names FROM the ``format`` SUBMODULE directly. ``core/__init__`` re-exports
# the ``format`` FUNCTION (from ``formatting.py``), which shadows the ``gsheets.core.format`` MODULE
# under a bare ``from . import format`` (IMPORT_FROM resolves the name to the function) — but
# ``from .format import render`` reaches into the submodule and is unaffected (the same path
# ``export.py`` uses for ``render_grid``). ``_jsonl_records`` is the shared record extraction so the
# handle preview matches the on-disk jsonl lines exactly.
from .format import _jsonl_records, render

#: Filename globs that are NEVER acceptable as an out_path target — the credential shapes from
#: ``.gitignore`` (SPEC §2.3). Matched case-insensitively against the basename only.
_CREDENTIAL_GLOBS: tuple[str, ...] = (
    "*token*.json",
    "gcp-oauth.keys.json",
    "service-account*.json",
    "credentials.json",
    "*.pem",
    ".env",
    ".env.*",
)

#: Directory subtrees that are NEVER writable — the app's own config and the user's secrets
#: (SPEC §2.3). Resolved under ``$HOME`` (or ``~``) at check time.
_REFUSED_DIRS: tuple[str, ...] = (
    ".config/google-sheets-mcp",
    ".secrets",
)

#: How many leading rows/records the handle preview carries (SPEC §2.2 — "first ~5").
_PREVIEW_LIMIT = 5

#: HARD byte cap on the JSON-serialized handle preview (Problem A / audit #10). The preview exists
#: to cost a handful of tokens instead of the whole grid — so it must NEVER approach the full
#: payload size. When the naive record-split (``records[:5]``) exceeds this cap — e.g. an
#: ``inspect``/``describe`` result whose two top-level lists make record extraction fall back to
#: ``[whole result]`` — the preview is replaced by a compact STRUCTURAL SUMMARY (see
#: :func:`_structural_summary`). A few KB keeps the summary useful while staying a tiny fraction of a
#: multi-KB payload.
_PREVIEW_BYTE_CAP = 4096

#: How many items of the largest top-level list a structural summary samples (each item itself
#: byte-truncated). Kept small so the whole summary stays well under :data:`_PREVIEW_BYTE_CAP`.
_SUMMARY_LIST_ITEMS = 3

#: Max bytes for a single sampled item inside a structural summary before it is replaced by a
#: truncated string marker — one runaway item can't blow the cap.
_SUMMARY_ITEM_BYTES = 512


def resolve_out_path(path: str, overwrite: bool = False) -> str:
    """Resolve ``path`` to a safe absolute file path, or raise ``SheetsError``.

    Resolution + the safety gates of SPEC §2.3 (plus the overwrite guard from audit #10):

    1. Resolve relative paths against the current working directory; the result is absolute and
       symlink-normalized (``Path.resolve``), so the directory and glob checks see the real target.
    2. HARD-REFUSE any path whose basename matches a credential glob, or that lives under
       ``~/.config/google-sheets-mcp/`` or ``~/.secrets/`` — a read must never clobber credentials.
       This runs FIRST so ``overwrite=True`` can never unlock a credential target.
    3. The PARENT directory must already exist — a missing parent is ``bad_out_path`` and we NEVER
       ``mkdir`` (the agent named the file; we don't invent directory trees for it).
    4. If the target already EXISTS and ``overwrite`` is ``False`` (the default), raise
       ``out_path_exists`` rather than clobbering it — the out_path callers are read-only, so an
       accidental overwrite of an unrelated file is a footgun, not a feature.

    Args:
        path: The caller-supplied destination (absolute or relative).
        overwrite: When ``False`` (default), refuse to write over an existing file; when ``True``,
            allow it (the caller has explicitly opted into a truncating overwrite).

    Returns:
        The resolved absolute path as a ``str``.

    Raises:
        SheetsError: ``"bad_out_path"`` when ``path`` is empty/invalid, its parent directory is
            missing, or it targets a credential / config / secrets location; ``"out_path_exists"``
            when the target exists and ``overwrite`` is ``False``.
    """
    if not isinstance(path, str) or not path.strip():
        raise SheetsError(
            "bad_out_path",
            "out_path must be a non-empty file path",
            hint="pass an absolute or cwd-relative path whose parent directory already exists",
        )

    # Resolve to absolute (relative -> cwd). ``strict=False`` so a not-yet-created file resolves;
    # we validate the PARENT's existence separately below.
    resolved = Path(os.path.expanduser(path)).resolve()

    _refuse_credential_target(resolved)

    parent = resolved.parent
    if not parent.is_dir():
        raise SheetsError(
            "bad_out_path",
            f"parent directory does not exist: {parent}",
            hint="create the directory first, or choose a path under an existing directory "
            "(out_path never creates directories)",
        )

    if not overwrite and resolved.exists():
        raise SheetsError(
            "out_path_exists",
            f"refusing to overwrite an existing file: {resolved}",
            hint="choose a new path, or pass overwrite=True to replace it "
            "(out_path never clobbers an existing file by default)",
        )

    return str(resolved)


def _refuse_credential_target(resolved: Path) -> None:
    """Raise ``SheetsError("bad_out_path")`` if ``resolved`` is a credential / config / secrets path.

    Two checks (SPEC §2.3): a basename matching any credential glob (case-insensitive), and any path
    under a refused directory subtree (``~/.config/google-sheets-mcp/`` / ``~/.secrets/``).
    """
    name = resolved.name.lower()
    for pattern in _CREDENTIAL_GLOBS:
        if fnmatch.fnmatch(name, pattern):
            raise SheetsError(
                "bad_out_path",
                f"refusing to write to a credential-shaped path: {resolved.name}",
                hint="choose a non-credential filename — this guard protects tokens / keys / "
                ".env files from being overwritten by a read",
            )

    home = Path(os.path.expanduser("~")).resolve()
    for refused in _REFUSED_DIRS:
        base = (home / refused).resolve()
        if resolved == base or _is_relative_to(resolved, base):
            raise SheetsError(
                "bad_out_path",
                f"refusing to write under a protected directory: {base}",
                hint="this directory holds credentials / secrets and is never a write target; "
                "choose a path outside it",
            )


def _is_relative_to(child: Path, parent: Path) -> bool:
    """True iff ``child`` is at or under ``parent`` (``Path.is_relative_to`` is 3.9+ but explicit)."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def write_file_handle(result: dict, fmt: str, path: str, overwrite: bool = False) -> dict:
    """Serialize ``result`` to ``path`` and return the small file-output handle (SPEC §2.2).

    The ENTIRE body of the MCP ``out_path`` branch. Resolves + safety-checks ``path`` via
    :func:`resolve_out_path` (so a bad path, credential target, or existing-file collision fails
    BEFORE anything is written), serializes ``result`` with the shared
    :func:`gsheets.core.format.render` (the same bytes the CLI pipes / ``export`` writes), writes it
    utf-8, and returns the handle:

    ``{"ok": True, "path": <abs>, "format": <fmt>, "rows": <int>, "cols": <int>, "bytes": <int>,
    "preview": [...]}``

    ``rows`` / ``cols`` describe the underlying value grid (or the record count for jsonl/json);
    ``preview`` is the first ~5 rows (csv/tsv) or records (jsonl/json), BYTE-bounded so it can never
    approach the full payload — a nested read whose record-split degenerates to ``[whole result]``
    gets a compact structural summary instead (Problem A / audit #10).

    Args:
        result: A plain core result dict.
        fmt: A data format (``json`` | ``jsonl`` | ``csv`` | ``tsv``). A csv/tsv request on a
            structured (non-tabular) result raises ``format_unsupported`` BEFORE the path is touched.
        path: The caller-supplied destination (validated by :func:`resolve_out_path`).
        overwrite: When ``False`` (default), refuse to overwrite an existing file
            (``out_path_exists``); when ``True``, allow a truncating overwrite.

    Returns:
        The handle dict (SPEC §2.2).

    Raises:
        SheetsError: ``"bad_out_path"`` (bad/credential/missing-parent path), ``"out_path_exists"``
            (target exists and ``overwrite`` is ``False``), or ``"format_unsupported"`` (csv/tsv on a
            structured result) — all raised before any write.
    """
    # Render FIRST so a format_unsupported error never leaves a half-written file behind. (render
    # is pure and cheap relative to a real API read; doing it before the path resolution is fine.)
    serialized = render(result, fmt)

    resolved = resolve_out_path(path, overwrite=overwrite)

    data = serialized.encode("utf-8")
    with open(resolved, "wb") as fh:
        written = fh.write(data)

    rows, cols, preview = _describe_payload(result, fmt)
    return {
        "ok": True,
        "path": resolved,
        "format": fmt,
        "rows": rows,
        "cols": cols,
        "bytes": written if written is not None else len(data),
        "preview": preview,
    }


def _describe_payload(result: dict, fmt: str) -> tuple[int, int, list]:
    """Compute the handle's ``(rows, cols, preview)`` for ``result`` rendered as ``fmt`` (SPEC §2.2).

    * csv/tsv -> the rectangular value grid: ``rows`` is the total grid-row count across every
      range, ``cols`` the widest row, ``preview`` the first ~5 grid rows.
    * jsonl -> the per-line records (one per value-grid row, or one per list element): ``rows`` is
      the record count, ``cols`` 0 (records aren't a rectangle), ``preview`` the first ~5 records.
    * json -> the same record extraction as jsonl when the result is record-shaped; otherwise a
      single-object payload (``rows=1``, ``preview=[result]``) so the handle still summarizes it.

    Whatever the shape, the preview is passed through :func:`_bounded_preview` so it can NEVER
    approach the full payload size (Problem A / audit #10): if the naive first-``N`` slice exceeds
    :data:`_PREVIEW_BYTE_CAP`, it is replaced by a compact structural summary.
    """
    if fmt in ("csv", "tsv"):
        grid = _value_grid(result)
        cols = max((len(row) for row in grid), default=0)
        return len(grid), cols, _bounded_preview(grid[:_PREVIEW_LIMIT], result)

    # jsonl / json are record-shaped (reuse the format layer's own record extraction so the
    # preview matches the on-disk lines exactly).
    records = _jsonl_records(result)  # shared core-internal extraction (matches the on-disk lines)
    return len(records), 0, _bounded_preview(records[:_PREVIEW_LIMIT], result)


def _bounded_preview(preview: list, result: dict) -> list:
    """Return ``preview`` if it fits under :data:`_PREVIEW_BYTE_CAP`, else a structural summary.

    The naive preview (``rows[:5]`` / ``records[:5]``) is fine for a genuine tabular or list read.
    But a nested read (``inspect``/``describe``) whose record-split degenerates to ``[whole result]``
    — or a list whose leading records are individually enormous — would otherwise carry ~the entire
    payload in the handle, defeating out_path. When the serialized preview breaches the cap we swap
    in a compact structural summary (:func:`_structural_summary`), then a minimal keys-only summary
    if even that is somehow too big — so the returned preview is HARD-bounded.
    """
    if _preview_bytes(preview) <= _PREVIEW_BYTE_CAP:
        return preview
    summary = _structural_summary(result)
    if _preview_bytes(summary) <= _PREVIEW_BYTE_CAP:
        return summary
    return _minimal_summary(result)


def _preview_bytes(preview: object) -> int:
    """The utf-8 byte length of ``preview`` serialized as compact JSON (the cap unit)."""
    return len(json.dumps(preview, ensure_ascii=False, default=str).encode("utf-8"))


def _structural_summary(result: dict) -> list:
    """A compact, byte-bounded summary of an over-large ``result`` (audit #10).

    Carries the shape an agent needs to decide what to fetch next WITHOUT the payload: the top-level
    keys, the item count of every top-level list, the name of the largest list, and the first few of
    its items (each byte-truncated). Sample items are added one at a time and stop as soon as the
    growing summary would breach :data:`_PREVIEW_BYTE_CAP`, so the result stays under the cap by
    construction. Returned as a single-element list so ``preview`` keeps its list type.
    """
    list_counts = {key: len(val) for key, val in result.items() if isinstance(val, list)}
    summary: dict = {
        "_preview": "structural_summary",
        "note": "payload too large to preview inline; full data written to out_path",
        "keys": list(result.keys()),
        "listCounts": list_counts,
    }

    if list_counts:
        largest = max(list_counts, key=lambda key: list_counts[key])
        summary["largestList"] = largest
        sample: list = []
        for item in result[largest][:_SUMMARY_LIST_ITEMS]:
            trial = sample + [_truncate_item(item)]
            if _preview_bytes([{**summary, "sample": trial}]) > _PREVIEW_BYTE_CAP:
                break
            sample = trial
        summary["sample"] = sample

    return [summary]


def _truncate_item(item: object) -> object:
    """Return ``item`` unchanged if it serializes small, else a truncated string marker.

    Bounds a single sampled item so one runaway record (a giant cell, a wide row) can't blow the
    summary's byte budget on its own.
    """
    encoded = json.dumps(item, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) <= _SUMMARY_ITEM_BYTES:
        return item
    return encoded[:_SUMMARY_ITEM_BYTES] + "…(truncated)"


def _minimal_summary(result: dict) -> list:
    """The last-resort preview: top-level keys only (bounded), when even the summary is too big."""
    return [{"_preview": "structural_summary", "keys": list(result.keys())[:50]}]


def _value_grid(result: dict) -> list[list]:
    """Flatten a tabular ``read_values`` result's ranges into one list of rows (for csv/tsv stats).

    Mirrors the shared csv path: every range's ``values`` rows concatenated in order. A non-tabular
    result reaching here means csv/tsv was requested on a structured result — but
    :func:`gsheets.core.format.render` already raised ``format_unsupported`` before we get here, so
    this only ever sees a real value grid.
    """
    grid: list[list] = []
    for entry in result.get("ranges", []) or []:
        for row in entry.get("values", []) or []:
            grid.append(row)
    return grid
