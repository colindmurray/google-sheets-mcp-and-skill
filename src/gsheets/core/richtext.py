"""Rich-text runs (``textFormatRuns``) + cell hyperlink serialization (DESIGN §X.0a / §X.1).

Covers the per-run rich-text (+ in-cell links) and cell-level hyperlink reads. Both are
READ-side enrichments surfaced per-cell ONLY when present (token-safe): a cell carries
``runs`` only when it has ``textFormatRuns`` and ``hyperlink`` only when Google sets it.

``serialize_text_runs(runs, full_text)`` turns a Google ``CellData.textFormatRuns`` array
into the flattened per-run struct list (DESIGN §X.0a):

    { "start": 0, "text": "Click here", "format": {CellFormat-subset}, "link": "https://..." }

    - ``start``  = ``TextFormatRun.startIndex`` (0-based char offset; absent ⇒ 0 for run 0)
    - ``text``   = the substring ``full_text[start .. nextStart)`` (the run's own characters)
    - ``format`` = flattened run-level ``TextFormat`` (bold/italic/fg/fontSize/... via
                   :func:`gsheets.core.flatten.flatten_cell_format`'s textFormat subset); omitted
                   when the run carries no displayable text styling
    - ``link``   = ``TextFormatRun.format.link.uri`` when present. A RUN-level link TAKES
                   PRECEDENCE over the cell-level ``hyperlink`` (a multi-link cell has an empty
                   cell ``hyperlink`` and is recoverable only via the per-run ``link``).

The cell-level ``hyperlink`` is a READ-ONLY Google field; the owning read (``reads.inspect``)
attaches it FLAT on the cell as ``"hyperlink": "https://..."`` (only when set) — that flat
attach is the caller's job, not this module's. This module only handles the runs + a terse
line renderer; both stay self-contained and serviceless.

Terse line form (condformat-style, one line per cell with runs, DESIGN §X.0a):

    runs A1: "Click here"[0:10 fg #1155CC bold link https://x] + " then plain"[10:21]

PURE leaf serializer module. Imports only stdlib + the sibling ``flatten``/``colors``/``errors``
core leaves (its declared deps). It must NEVER import ``fastmcp``/``mcp``/``argparse``,
``pydantic``, or ``gsheets.models`` (DESIGN §1 boundary). It is SERVICELESS — it takes
already-resolved Google JSON (runs + the cell's plain text) and returns plain dicts/lists; the
owning read fn (``reads.inspect``) holds the ``SheetsServices`` handle and calls this at the edge.
"""

from __future__ import annotations

from .errors import SheetsError
from .flatten import flatten_cell_format


def serialize_text_runs(runs: list[dict] | None, full_text: str | None) -> list[dict]:
    """Serialize a Google ``CellData.textFormatRuns`` array to the flat per-run struct list.

    Each Google ``TextFormatRun`` is ``{"startIndex": int, "format": TextFormat}``. The run's
    ``text`` is the slice of ``full_text`` from this run's ``startIndex`` up to the next run's
    ``startIndex`` (or end of text for the last run). The first run's ``startIndex`` is omitted
    by Google when it is ``0``; we treat a missing ``startIndex`` as ``0``.

    Args:
        runs: A Google ``textFormatRuns`` array (list of ``{startIndex, format}`` dicts). An
            empty/``None`` array yields ``[]`` (the caller then omits the cell's ``runs`` key,
            per the omit-when-absent token rule).
        full_text: The cell's plain display text (``CellData.formattedValue`` /
            ``effectiveValue.stringValue``). Used to slice each run's substring. ``None`` is
            treated as the empty string (runs degenerate to empty ``text``).

    Returns:
        A list of per-run dicts ``{"start", "text", "format"?, "link"?}`` in start order.
        ``format`` and ``link`` keys are omitted when absent (token efficiency).

    Raises:
        SheetsError: if ``runs`` is not a list, or a run is not a dict, or a ``startIndex`` is
            not a non-negative integer.
    """
    if runs is None:
        return []
    if not isinstance(runs, list):
        raise SheetsError(
            "bad_text_runs", f"textFormatRuns must be a list, got {type(runs).__name__}"
        )
    if not runs:
        return []

    text = full_text if isinstance(full_text, str) else ""

    # Normalize each run to (start, format_dict) and sort by start. Google returns them in
    # order, but we sort defensively so the substring slicing (start -> nextStart) is correct
    # even if a caller hands us an out-of-order array.
    normalized: list[tuple[int, dict]] = []
    for run in runs:
        if not isinstance(run, dict):
            raise SheetsError(
                "bad_text_runs", f"each textFormatRun must be a dict, got {type(run).__name__}"
            )
        start = run.get("startIndex", 0)
        if start is None:
            start = 0
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise SheetsError(
                "bad_text_runs",
                f"textFormatRun startIndex must be a non-negative int, got {start!r}",
            )
        run_format = run.get("format")
        if run_format is not None and not isinstance(run_format, dict):
            raise SheetsError(
                "bad_text_runs",
                f"textFormatRun format must be a dict, got {type(run_format).__name__}",
            )
        normalized.append((start, run_format or {}))

    normalized.sort(key=lambda item: item[0])

    out: list[dict] = []
    n = len(normalized)
    for i, (start, run_format) in enumerate(normalized):
        next_start = normalized[i + 1][0] if i + 1 < n else len(text)
        # Clamp the slice bounds into [0, len(text)] so an out-of-range startIndex (a cell
        # whose text was shortened after the runs were set) never raises and never produces a
        # negative-width slice — it just yields an empty substring.
        lo = min(max(start, 0), len(text))
        hi = min(max(next_start, lo), len(text))
        substring = text[lo:hi]

        run_out: dict = {"start": start, "text": substring}

        fmt = _flatten_run_format(run_format)
        if fmt:
            run_out["format"] = fmt

        link = _run_link(run_format)
        if link is not None:
            run_out["link"] = link

        out.append(run_out)

    return out


def _flatten_run_format(run_format: dict) -> dict:
    """Flatten a run-level Google ``TextFormat`` to the flat CellFormat textFormat subset.

    Reuses :func:`flatten.flatten_cell_format` by wrapping the run's ``TextFormat`` as the
    ``textFormat`` child of a ``CellFormat`` (that is exactly where ``flatten`` expects bold/
    italic/underline/strikethrough/fontSize/fontFamily and the foreground color). The run's
    ``link`` lives inside ``TextFormat`` too but is intentionally NOT surfaced here — it is the
    run's ``link`` key (extracted separately), not a format token.
    """
    if not run_format:
        return {}
    return flatten_cell_format({"textFormat": run_format})


def _run_link(run_format: dict) -> str | None:
    """Extract ``TextFormatRun.format.link.uri`` (the run-level link), or ``None``.

    A run-level link takes precedence over the cell-level ``hyperlink`` (DESIGN §X.0a): the
    cell's ``hyperlink`` is empty for a multi-link cell, so the per-run ``link`` is the only way
    to recover those URIs.
    """
    if not isinstance(run_format, dict):
        return None
    link = run_format.get("link")
    if not isinstance(link, dict):
        return None
    uri = link.get("uri")
    if isinstance(uri, str) and uri:
        return uri
    return None


def text_runs_line(a1: str, runs: list[dict]) -> str:
    """Render a serialized per-run list to the terse condformat-style ``runs`` line.

    Example (DESIGN §X.0a):
        ``runs A1: "Click here"[0:10 fg #1155CC bold link https://x] + " then plain"[10:21]``

    Each segment is ``"<text>"[<start>:<end> <fmt-tokens> link <uri>]`` where ``<end>`` is the
    exclusive end offset (``start + len(text)``) and the bracket body lists the canonical format
    tokens (``bg``/``fg``/``bold``/``italic``/``underline``/``strike``) followed by an optional
    ``link <uri>``. Segments are joined by ``" + "``. The line is purely cosmetic (human/AI
    facing); the structured ``runs`` list is the round-trip/machine source.

    Args:
        a1: The cell's A1 address (e.g. ``"A1"``), for the line prefix.
        runs: A serialized per-run list (the output of :func:`serialize_text_runs`).

    Returns:
        The terse one-line rendering, or ``""`` when ``runs`` is empty.
    """
    if not runs:
        return ""
    segments: list[str] = []
    for run in runs:
        start = run.get("start", 0)
        text = run.get("text", "")
        end = start + len(text)
        inner = [f"{start}:{end}"]
        fmt = run.get("format") or {}
        inner.extend(_format_tokens(fmt))
        link = run.get("link")
        if link:
            inner.append(f"link {link}")
        segments.append(f'"{text}"[{" ".join(inner)}]')
    return f"runs {a1}: " + " + ".join(segments)


# Canonical fmt-token order for the terse run line, mirroring condformat's _flat_format_to_tokens
# (bg, fg, then the boolean style flags). Run formats carry only textFormat-derived keys, so
# number/align/wrap tokens never appear here, but bg is kept for completeness/robustness.
_STYLE_FLAG_TOKENS: tuple[tuple[str, str], ...] = (
    ("bold", "bold"),
    ("italic", "italic"),
    ("underline", "underline"),
    ("strikethrough", "strike"),
)


def _format_tokens(flat: dict) -> list[str]:
    """Render a flat run ``format`` dict to the canonical space-token order (no number/align)."""
    tokens: list[str] = []
    bg = flat.get("bg")
    if bg:
        tokens.append(f"bg {bg}")
    fg = flat.get("fg")
    if fg:
        tokens.append(f"fg {fg}")
    for key, word in _STYLE_FLAG_TOKENS:
        if flat.get(key):
            tokens.append(word)
    return tokens
