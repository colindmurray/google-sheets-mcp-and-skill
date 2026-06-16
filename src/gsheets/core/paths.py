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


def resolve_out_path(path: str) -> str:
    """Resolve ``path`` to a safe absolute file path, or raise ``SheetsError("bad_out_path")``.

    Resolution + the three safety gates of SPEC §2.3:

    1. Resolve relative paths against the current working directory; the result is absolute and
       symlink-normalized (``Path.resolve``), so the directory and glob checks see the real target.
    2. The PARENT directory must already exist — a missing parent is ``bad_out_path`` and we NEVER
       ``mkdir`` (the agent named the file; we don't invent directory trees for it).
    3. HARD-REFUSE any path whose basename matches a credential glob, or that lives under
       ``~/.config/google-sheets-mcp/`` or ``~/.secrets/`` — a read must never clobber credentials.

    Args:
        path: The caller-supplied destination (absolute or relative).

    Returns:
        The resolved absolute path as a ``str``.

    Raises:
        SheetsError: ``"bad_out_path"`` when ``path`` is empty/invalid, its parent directory is
            missing, or it targets a credential / config / secrets location.
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


def write_file_handle(result: dict, fmt: str, path: str) -> dict:
    """Serialize ``result`` to ``path`` and return the small file-output handle (SPEC §2.2).

    The ENTIRE body of the MCP ``out_path`` branch. Resolves + safety-checks ``path`` via
    :func:`resolve_out_path` (so a bad path or credential target fails BEFORE anything is written),
    serializes ``result`` with the shared :func:`gsheets.core.format.render` (the same bytes the CLI
    pipes / ``export`` writes), writes it utf-8, and returns the handle:

    ``{"ok": True, "path": <abs>, "format": <fmt>, "rows": <int>, "cols": <int>, "bytes": <int>,
    "preview": [...]}``

    ``rows`` / ``cols`` describe the underlying value grid (or the record count for jsonl/json);
    ``preview`` is the first ~5 rows (csv/tsv) or records (jsonl/json) — never the whole payload.

    Args:
        result: A plain core result dict.
        fmt: A data format (``json`` | ``jsonl`` | ``csv`` | ``tsv``). A csv/tsv request on a
            structured (non-tabular) result raises ``format_unsupported`` BEFORE the path is touched.
        path: The caller-supplied destination (validated by :func:`resolve_out_path`).

    Returns:
        The handle dict (SPEC §2.2).

    Raises:
        SheetsError: ``"bad_out_path"`` (bad/credential/missing-parent path), or
            ``"format_unsupported"`` (csv/tsv on a structured result) — both raised before any write.
    """
    # Render FIRST so a format_unsupported error never leaves a half-written file behind. (render
    # is pure and cheap relative to a real API read; doing it before the path resolution is fine.)
    serialized = render(result, fmt)

    resolved = resolve_out_path(path)

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
    """
    if fmt in ("csv", "tsv"):
        grid = _value_grid(result)
        cols = max((len(row) for row in grid), default=0)
        return len(grid), cols, grid[:_PREVIEW_LIMIT]

    # jsonl / json are record-shaped (reuse the format layer's own record extraction so the
    # preview matches the on-disk lines exactly).
    records = _jsonl_records(result)  # shared core-internal extraction (matches the on-disk lines)
    return len(records), 0, records[:_PREVIEW_LIMIT]


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
