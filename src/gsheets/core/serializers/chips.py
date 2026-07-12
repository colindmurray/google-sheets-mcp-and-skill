"""Smart-chip (``chipRuns``) read serializer (DESIGN §T2.2 — read-richness for smart chips).

Person / file / place / finance / rating smart chips otherwise read back as plain text — the
only signal in the API is ``CellData.chipRuns``. This serializer renders each Google ``ChipRun``
to a terse, round-trippable dict so an AI agent can see what a viewer sees:

    {"start": 0,  "kind": "person", "email": "jane@x.com", "displayFormat": "DEFAULT"}
    {"start": 18, "kind": "file",   "uri": "https://docs.google.com/...", "mimeType": "..."}
    {"start": 40, "kind": "place",  "uri": "https://www.google.com/maps/..."}

Shape (per chip run that carries a chip):
    - ``start``  = ``ChipRun.startIndex`` (0-based UTF-16 char offset; Google omits it for a run
                   starting at 0, so a missing ``startIndex`` is treated as ``0``).
    - ``kind``   = a heuristic classification of the chip (see :func:`_classify_rich_link`).
    - person chips carry ``email`` (always present per the discovery doc) and ``displayFormat``
      (omitted when unspecified).
    - rich-link chips carry ``uri`` (always present) and ``mimeType`` (output-only; omitted when
      absent). ``uri``/``mimeType`` are the AUTHORITATIVE fields — ``kind`` is only a hint derived
      from them, so a mis-hinted ``kind`` never loses information.

Chip ``kind`` vocabulary: ``person``, ``file``, ``place``, ``finance``, ``rating``, ``richlink``
(the catch-all). Only ``personProperties`` (person) and Drive-file ``richLinkProperties`` are
WRITABLE via the API; every other rich link (place / finance / rating / calendar / YouTube) is
read-only. ``rating`` has no documented uri/mimeType signal, so it stays in the vocabulary but a
rating chip surfaces as ``richlink`` with its uri preserved rather than being mis-classified.

PURE leaf serializer. Imports only stdlib + the sibling ``errors`` leaf. It must NEVER import
``fastmcp``/``mcp``/``argparse``/``pydantic``/``gsheets.models`` (DESIGN §1 boundary). It is
SERVICELESS — it takes an already-fetched Google ``chipRuns`` array and returns plain dicts; the
owning read (``reads.inspect``) holds the ``SheetsServices`` handle and calls this at the edge.
"""

from __future__ import annotations

from ..errors import SheetsError


def serialize_chip_runs(chip_runs: list[dict] | None) -> list[dict]:
    """Serialize a Google ``CellData.chipRuns`` array to the terse per-chip struct list.

    Each Google ``ChipRun`` is ``{"startIndex": int, "chip": Chip}``. Only runs that actually
    carry a ``chip`` are emitted (a chip-less run is a plain-text gap whose text already lives in
    the cell value); the surviving runs are returned in ``start`` order.

    Args:
        chip_runs: A Google ``chipRuns`` array (list of ``{startIndex, chip}`` dicts). An empty or
            ``None`` array yields ``[]`` (the caller then omits the cell's ``chips`` key, per the
            omit-when-absent token rule).

    Returns:
        A list of per-chip dicts ``{"start", "kind", ...}`` in start order. Person chips add
        ``email``/``displayFormat``; rich-link chips add ``uri``/``mimeType`` (each omitted when
        absent).

    Raises:
        SheetsError: if ``chip_runs`` is not a list, a run is not a dict, or a ``startIndex`` is
            not a non-negative integer.
    """
    if chip_runs is None:
        return []
    if not isinstance(chip_runs, list):
        raise SheetsError(
            "bad_chip_runs", f"chipRuns must be a list, got {type(chip_runs).__name__}"
        )
    if not chip_runs:
        return []

    normalized: list[tuple[int, dict]] = []
    for run in chip_runs:
        if not isinstance(run, dict):
            raise SheetsError(
                "bad_chip_runs", f"each chipRun must be a dict, got {type(run).__name__}"
            )
        start = run.get("startIndex", 0)
        if start is None:
            start = 0
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise SheetsError(
                "bad_chip_runs",
                f"chipRun startIndex must be a non-negative int, got {start!r}",
            )
        chip = run.get("chip")
        # A run with no chip is a plain-text gap — nothing to surface (its text is in the value).
        if not isinstance(chip, dict) or not chip:
            continue
        normalized.append((start, chip))

    # Sort defensively by start; Google returns them in order, but this keeps the substring-free
    # emit deterministic even for an out-of-order array.
    normalized.sort(key=lambda item: item[0])

    out: list[dict] = []
    for start, chip in normalized:
        entry: dict = {"start": start}
        entry.update(_serialize_chip(chip))
        out.append(entry)
    return out


def _serialize_chip(chip: dict) -> dict:
    """Render one Google ``Chip`` to its ``{kind, ...key fields}`` fragment (no ``start``).

    Person chips take precedence (``personProperties`` is mutually exclusive with
    ``richLinkProperties`` in practice, but if both were somehow present the person reading wins).
    """
    person = chip.get("personProperties")
    if isinstance(person, dict) and person:
        frag: dict = {"kind": "person"}
        email = person.get("email")
        if isinstance(email, str) and email:
            frag["email"] = email
        display_format = person.get("displayFormat")
        if (
            isinstance(display_format, str)
            and display_format
            and display_format != "DISPLAY_FORMAT_UNSPECIFIED"
        ):
            frag["displayFormat"] = display_format
        return frag

    rich = chip.get("richLinkProperties")
    if isinstance(rich, dict) and rich:
        uri = rich.get("uri") if isinstance(rich.get("uri"), str) else None
        mime = rich.get("mimeType") if isinstance(rich.get("mimeType"), str) else None
        frag = {"kind": _classify_rich_link(uri, mime)}
        if uri:
            frag["uri"] = uri
        if mime:
            frag["mimeType"] = mime
        return frag

    # A chip carrying neither known property — surface it as an unknown chip rather than dropping
    # it, so the run boundary (start) is still visible to the agent.
    return {"kind": "unknown"}


def _classify_rich_link(uri: str | None, mime: str | None) -> str:
    """Classify a rich-link chip's ``kind`` from its ``uri`` / ``mimeType`` (a best-effort HINT).

    The uri/mimeType are the authoritative fields (always carried on the output), so this
    classification is a convenience label, never the source of truth. Recognized signals:

    - a ``docs.google.com`` / ``drive.google.com`` host, OR any output-only Drive ``mimeType`` →
      ``file`` (Drive files are the only writable rich link and always carry a mimeType);
    - a Google Maps host → ``place``;
    - a Google Finance host → ``finance``;
    - anything else (calendar events, YouTube videos, rating chips, arbitrary links) → ``richlink``.
    """
    u = (uri or "").lower()
    if "docs.google.com" in u or "drive.google.com" in u:
        return "file"
    if "google.com/maps" in u or "maps.google." in u or "/maps/" in u:
        return "place"
    if "google.com/finance" in u or "/finance/" in u:
        return "finance"
    # A Drive file always carries an output-only mimeType even if the host wasn't recognized above.
    if mime:
        return "file"
    return "richlink"
