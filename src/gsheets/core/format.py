"""Shared output-format layer (SPEC §1) — serialize a core result dict to a string.

PURE core module: imports ONLY stdlib (``csv``, ``io``, ``json``). It must NEVER import
``fastmcp``, ``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models`` (SPEC §0.2, DESIGN §1
boundary). Both adapters drive the SAME ``render`` so MCP file output and CLI piped output are
byte-identical.

:func:`render` serializes a core result dict to a string in one of the data formats:

* ``json``  — ``json.dumps(result, ensure_ascii=False, indent=2)``.
* ``jsonl`` — one JSON record per line. For a ``read_values`` result that is one
  ``{"range": <A1>, "row": [...]}`` per grid row; for a list-shaped result (e.g. ``read_many``,
  ``comments``) it is one top-level list element per line.
* ``csv`` / ``tsv`` — the stdlib ``csv`` module over the result's rectangular value grid(s).
  A single-range read is plain RFC-4180 CSV; a multi-range read emits each range as a block
  preceded by a ``# range: <A1>`` comment line. A non-tabular (structured) result raises
  ``SheetsError("format_unsupported")``.

``text`` is NOT handled here — it is the adapters' existing terse renderer (SPEC §1.5).

``export`` (``core/export.py``) delegates its single-sheet csv/tsv serialization to
:func:`render_grid` here, so there is ONE csv path, not two (its on-disk bytes are unchanged).
"""

from __future__ import annotations

import csv
import io
import json

from .errors import SheetsError

#: The data formats this module serializes. ``text`` lives in the adapters (SPEC §1.5);
#: ``markdown`` is gated to a later phase (SPEC §6) and is intentionally absent here.
SUPPORTED: tuple[str, ...] = ("json", "jsonl", "csv", "tsv")

#: Normalized format -> the csv-module delimiter for the tabular renderers.
_DELIMITER: dict[str, str] = {"csv": ",", "tsv": "\t"}


def render(result: dict, fmt: str) -> str:
    """Serialize a core result dict to a string in ``fmt`` (SPEC §1.2).

    Args:
        result: A plain JSON-serializable core result dict (``"ok": True``).
        fmt: One of ``"json"`` | ``"jsonl"`` | ``"csv"`` | ``"tsv"``. ``"text"`` is the
            adapters' job and is rejected here.

    Returns:
        The serialized string. csv/tsv use RFC-4180 ``\\r\\n`` line terminators (matching
        ``export``); json/jsonl use ``\\n``.

    Raises:
        SheetsError: ``"format_unsupported"`` when an unknown format is requested, or when a
            tabular format (csv/tsv) is asked for a structured (non-tabular) result.
    """
    if fmt == "json":
        return _render_json(result)
    if fmt == "jsonl":
        return _render_jsonl(result)
    if fmt in _DELIMITER:
        return _render_tabular(result, fmt)
    raise SheetsError(
        "format_unsupported",
        f"unknown output format {fmt!r}",
        hint="use one of: text, json, jsonl, csv, tsv",
    )


# --------------------------------------------------------------------------- json / jsonl


def _render_json(result: dict) -> str:
    """``json.dumps`` with ``ensure_ascii=False`` (token-efficient) and ``indent=2``."""
    return json.dumps(result, ensure_ascii=False, indent=2)


def _render_jsonl(result: dict) -> str:
    """One JSON record per line (SPEC §1.2).

    A ``read_values`` result (rectangular ``ranges[].values``) emits one
    ``{"range": <A1>, "row": [...]}`` per grid row — an embedded newline inside a value stays
    inside one physical line because ``json`` escapes it. Any other list-shaped result emits one
    top-level list element per line. A result with no obvious record list falls back to a single
    JSON object line (so the format never errors on a small confirmation).
    """
    records = _jsonl_records(result)
    return "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in records)


def _jsonl_records(result: dict) -> list:
    """Extract the per-line records for jsonl (SPEC §1.2)."""
    if _is_tabular(result):
        records: list = []
        for entry in result["ranges"]:
            a1 = entry.get("range")
            for row in entry.get("values", []) or []:
                records.append({"range": a1, "row": row})
        return records
    # List-shaped result: the single top-level list value (excluding scalars / envelope keys).
    list_value = _primary_list(result)
    if list_value is not None:
        return list(list_value)
    # No record list — emit the whole dict as one line.
    return [result]


def _primary_list(result: dict) -> list | None:
    """Return the result's single record-list value, or ``None`` if it isn't list-shaped.

    Scans the top-level values (skipping the ``ok``/``spreadsheetId`` envelope keys) for exactly
    one ``list`` value — e.g. ``comments`` on a comments read or ``results`` on a read_many
    envelope. If there is not exactly one list, the result isn't list-shaped for jsonl.
    """
    lists = [
        (key, val)
        for key, val in result.items()
        if key not in ("ok", "spreadsheetId") and isinstance(val, list)
    ]
    if len(lists) == 1:
        return lists[0][1]
    return None


# --------------------------------------------------------------------------- csv / tsv


def _render_tabular(result: dict, fmt: str) -> str:
    """Serialize the rectangular value grid(s) of a ``read_values`` result (SPEC §1.2).

    Single range -> clean RFC-4180 CSV (no header). Multiple ranges -> each range as a block
    preceded by a ``# range: <A1>`` comment line so the common single-range pipe stays clean
    while a multi-range read is still parseable. A structured (non-tabular) result raises
    ``format_unsupported`` — the agent learns the right tool.
    """
    if not _is_tabular(result):
        raise SheetsError(
            "format_unsupported",
            f"a {fmt} render needs a rectangular value read, but this result is structured",
            hint="use json or text; csv/tsv need a rectangular value read (e.g. read_values)",
        )
    delimiter = _DELIMITER[fmt]
    ranges = result.get("ranges") or []
    if len(ranges) == 1:
        return render_grid(ranges[0].get("values", []) or [], delimiter)

    blocks: list[str] = []
    for entry in ranges:
        a1 = entry.get("range")
        body = render_grid(entry.get("values", []) or [], delimiter)
        blocks.append(f"# range: {a1}\r\n{body}")
    return "".join(blocks)


def render_grid(rows: list[list], delimiter: str) -> str:
    """Serialize one rectangular grid of rows to a csv/tsv string (SPEC §1.2 shared path).

    Uses the stdlib ``csv`` module with RFC-4180 ``\\r\\n`` line terminators — the exact path
    ``export`` used inline, extracted here so there is ONE csv serializer. ``export`` calls this
    and encodes the result utf-8, so its on-disk bytes are byte-identical.

    Args:
        rows: A list of rows (each a list of cell values).
        delimiter: ``","`` (csv) or ``"\\t"`` (tsv).

    Returns:
        The serialized string (empty string for an empty grid).
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\r\n")
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def _is_tabular(result: dict) -> bool:
    """True iff ``result`` is a ``read_values``-style rectangular grid result (SPEC §1.2).

    The contract: a ``ranges`` key holding a list of entries that each carry a ``values`` grid.
    Structured reads (``inspect`` -> ``cells``, ``structure``/``read_conditional_formats`` ->
    ``sheets``) lack this shape and are therefore not tabular.
    """
    ranges = result.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        # An empty-ranges read_values result is still tabular (renders to "").
        return isinstance(ranges, list) and "render" in result
    return all(isinstance(entry, dict) and "values" in entry for entry in ranges)
