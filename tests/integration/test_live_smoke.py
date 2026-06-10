"""Live integration smoke tests (DESIGN §10; research §5 / packaging-and-api.md §Tests).

These hit the **real** Google Sheets API. They are:

- marked ``@pytest.mark.live`` (module-level ``pytestmark``), so a plain ``pytest`` run that
  deselects ``live`` never touches the network;
- gated on ``GSHEETS_LIVE=1`` AND a ``GSHEETS_TEST_SPREADSHEET_ID`` env var — the target sheet
  id is supplied **entirely from the environment**, never committed (this file goes public);
- guarded against ever touching a known Production id (a belt-and-suspenders check so a
  fat-fingered env var can never let the round-trip write land on a live sheet).

What they exercise end-to-end against a real authed service:

1. ``overview`` — the cheap orientation read (no grid data), proving auth + a real GET work and
   the flattened envelope shape holds.
2. The read surface (``inspect``, ``read_values``, ``read_conditional_formats``,
   ``structure(action="read")``) — proving the rich reads round-trip real API JSON through the
   flatten/serialize layers without raising and in the documented shapes.
3. A **values write round-trip** (``write_values`` → ``read_values`` → ``append_rows`` →
   ``clear``) confined to a scratch range, proving USER_ENTERED writes are readable back (the
   CRUD-symmetry thesis) and cleaning up after itself.

Everything is scoped to a single scratch sheet/range so a run is self-contained and idempotent;
the write test is skipped unless a writable scratch range is configured, so a read-only token or
a "don't write to my sheet" setup still gets the full read coverage.

Boundary note (DESIGN §1): the ``auth`` import is lazy (inside the ``live_service`` conftest
fixture and the helpers here), so collecting this module under a normal unit run never imports
``gsheets.auth`` and never resolves credentials.
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid

import pytest

pytestmark = pytest.mark.live

# --- Production denylist (no plaintext ids in the committed tree) ------------------------------
#
# The live suite must NEVER run against a Production spreadsheet. We guard against a fat-fingered
# GSHEETS_TEST_SPREADSHEET_ID pointing at one — but this repo is PUBLIC, so a real Production id
# must not appear in the committed tree (DESIGN §0), not even defensively. The guard therefore
# carries NO plaintext id. Two non-leaking sources feed it:
#
#   1. Salted SHA-256 hashes of known-Production ids (one-way; the id cannot be recovered from
#      the digest). A candidate id is hashed with the same salt and compared. This pins the
#      a known production id without committing it.
#   2. An optional runtime denylist via GSHEETS_PRODUCTION_DENYLIST (comma-separated ids), read
#      from the environment at test time — so an operator can add their own Production ids
#      locally without ever committing them.
_DENYLIST_SALT = "gsheets-live-denylist-v1"
#: Salted SHA-256 digests of known-Production ids (NOT the ids themselves). Recomputable via
#: ``hashlib.sha256(f"{_DENYLIST_SALT}:{spreadsheet_id}".encode()).hexdigest()``.
_PRODUCTION_DENYLIST_HASHES = frozenset(
    {
        # A maintainer production spreadsheet — the plaintext id lives only outside the repo.
        "c626b32ea1c7e1b343f9b160b53295ca5606e76f5011762bcae59114a279585b",
    }
)


def _salted_hash(spreadsheet_id: str) -> str:
    """Salted SHA-256 digest of ``spreadsheet_id`` (one-way; used for the no-plaintext denylist)."""
    return hashlib.sha256(f"{_DENYLIST_SALT}:{spreadsheet_id}".encode()).hexdigest()


def _env_denylist() -> frozenset[str]:
    """Runtime Production denylist from ``GSHEETS_PRODUCTION_DENYLIST`` (comma-separated ids)."""
    raw = os.environ.get("GSHEETS_PRODUCTION_DENYLIST", "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _is_denied(spreadsheet_id: str) -> bool:
    """True when ``spreadsheet_id`` is a known/declared Production id (hash or env match)."""
    if _salted_hash(spreadsheet_id) in _PRODUCTION_DENYLIST_HASHES:
        return True
    return spreadsheet_id in _env_denylist()


# --------------------------------------------------------------------------- helpers


def _test_spreadsheet_id() -> str:
    """The env-supplied scratch spreadsheet id, refusing any Production-denylisted id.

    Caller is reached only when ``live_service`` already confirmed ``GSHEETS_LIVE=1`` and that
    the env var is set, but we re-read + re-guard here so the Production check is enforced at the
    point of use (defense in depth) rather than trusting a fixture.
    """
    sid = os.environ.get("GSHEETS_TEST_SPREADSHEET_ID")
    if not sid:  # pragma: no cover - live_service already skips without it
        pytest.skip("GSHEETS_TEST_SPREADSHEET_ID not set")
    if _is_denied(sid):
        pytest.fail(
            "GSHEETS_TEST_SPREADSHEET_ID points at a PRODUCTION spreadsheet id; "
            "live tests must never run against Production (set it to a throwaway sheet)"
        )
    return sid


def _scratch_write_range() -> str | None:
    """An A1 range the write round-trip may safely clobber, or ``None`` to skip writes.

    Read from ``GSHEETS_TEST_WRITE_RANGE`` (e.g. ``"Scratch!A1:B2"``). Unset ⇒ the write
    round-trip is skipped, so a read-only token / read-only setup still gets full read coverage
    without ever attempting a mutation.
    """
    return os.environ.get("GSHEETS_TEST_WRITE_RANGE") or None


def _is_quota_error(exc: Exception) -> bool:
    """True when ``exc`` is a per-minute read-quota / rate-limit condition (HTTP 429).

    The shared dev project enforces a small ``ReadRequestsPerMinutePerUser`` quota (default 60),
    which a multi-test live run — or other processes sharing the same OAuth project — can exhaust
    transiently. Such failures are environmental, not logic/auth defects, so the live suite backs
    off and retries rather than reporting a spurious failure.
    """
    text = str(exc).lower()
    status = getattr(exc, "status", None)
    return status == 429 or "quota exceeded" in text or "rate_limit" in text or "ratelimit" in text


def _call_with_quota_retry(fn, *args, _attempts: int = 4, _base_delay: float = 8.0, **kwargs):
    """Call ``fn(*args, **kwargs)``, retrying with backoff ONLY on a transient quota/429 error.

    Any non-quota error propagates immediately (we never mask a real failure). Quota errors back
    off (``_base_delay`` × attempt) to let the per-minute window refill, then retry. This keeps
    the live suite green under the small shared read quota without weakening any assertion.
    """
    last: Exception | None = None
    for attempt in range(1, _attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised below unless it is a quota error
            if not _is_quota_error(exc):
                raise
            last = exc
            if attempt < _attempts:
                time.sleep(_base_delay * attempt)
    # Exhausted retries on a persistent quota condition — skip (environmental), don't fail.
    pytest.skip(f"read quota exhausted after {_attempts} attempts (transient): {last}")


def _first_sheet_title(services, spreadsheet_id: str) -> str:
    """Resolve the first tab's title via ``overview`` so reads target a range that exists."""
    from gsheets.core import overview

    ov = _call_with_quota_retry(overview, services, spreadsheet_id)
    sheets = ov.get("sheets") or []
    assert sheets, "live test sheet has no tabs to read"
    title = sheets[0].get("title")
    assert isinstance(title, str) and title, "first sheet has no title"
    return title


# --------------------------------------------------------------------------- read smokes


def test_overview_smoke(live_service):
    """``overview`` returns the flattened orientation envelope against a real sheet."""
    from gsheets.core import overview

    sid = _test_spreadsheet_id()
    result = _call_with_quota_retry(overview, live_service, sid)

    assert result["ok"] is True
    assert result["spreadsheetId"] == sid
    assert isinstance(result["title"], str)
    assert isinstance(result["sheets"], list) and result["sheets"]
    assert isinstance(result["namedRanges"], list)

    first = result["sheets"][0]
    # Per-sheet shape (DESIGN §3.3): flattened scalars + len()-ed counts, never raw arrays.
    for key in ("sheetId", "title", "index", "type", "rows", "cols"):
        assert key in first, f"overview sheet missing {key!r}"
    assert isinstance(first["protectedRangeCount"], int)
    assert isinstance(first["conditionalFormatCount"], int)


def test_inspect_smoke(live_service):
    """``inspect`` performs the flagship rich read over a real range without raising."""
    from gsheets.core import inspect

    sid = _test_spreadsheet_id()
    title = _first_sheet_title(live_service, sid)

    result = _call_with_quota_retry(inspect, live_service, sid, f"{title}!A1:B3")

    assert result["ok"] is True
    assert result["spreadsheetId"] == sid
    assert result["sheet"] == title
    assert result["compact"] is False
    assert isinstance(result["cells"], list)
    assert isinstance(result["merges"], list)
    # Every returned cell is the flattened per-cell shape: an A1 address at minimum.
    for cell in result["cells"]:
        assert "a1" in cell


def test_inspect_compact_smoke(live_service):
    """``inspect(compact=True)`` returns RLE ``runs`` (not ``cells``) over a real range."""
    from gsheets.core import inspect

    sid = _test_spreadsheet_id()
    title = _first_sheet_title(live_service, sid)

    result = _call_with_quota_retry(inspect, live_service, sid, f"{title}!A1:D10", compact=True)

    assert result["ok"] is True
    assert result["compact"] is True
    assert "runs" in result and isinstance(result["runs"], list)
    assert "cells" not in result, "compact read must not also emit per-cell `cells`"
    for run in result["runs"]:
        assert "a1Range" in run


def test_read_values_render_modes_smoke(live_service):
    """``read_values`` works for every render mode; ``all`` aligns ``values``/``computed``."""
    from gsheets.core import read_values

    sid = _test_spreadsheet_id()
    title = _first_sheet_title(live_service, sid)
    rng = f"{title}!A1:C5"

    for mode in ("plain", "unformatted", "formula"):
        res = _call_with_quota_retry(read_values, live_service, sid, [rng], render=mode)
        assert res["ok"] is True
        assert res["render"] == mode
        assert isinstance(res["ranges"], list) and res["ranges"]
        entry = res["ranges"][0]
        assert "values" in entry
        assert "computed" not in entry, f"{mode!r} must not emit `computed`"

    res_all = _call_with_quota_retry(read_values, live_service, sid, [rng], render="all")
    assert res_all["render"] == "all"
    entry = res_all["ranges"][0]
    assert "values" in entry and "computed" in entry
    # `all` pads both passes to a COMMON rectangle so values[r][c] aligns with computed[r][c].
    assert len(entry["values"]) == len(entry["computed"])
    for vrow, crow in zip(entry["values"], entry["computed"]):
        assert len(vrow) == len(crow)


def test_read_conditional_formats_smoke(live_service):
    """``read_conditional_formats`` serializes real CF rules into the multi-sheet envelope."""
    from gsheets.core import read_conditional_formats

    sid = _test_spreadsheet_id()
    result = _call_with_quota_retry(read_conditional_formats, live_service, sid)

    assert result["ok"] is True
    assert result["spreadsheetId"] == sid
    assert isinstance(result["sheets"], list)
    for sheet in result["sheets"]:
        assert "sheet" in sheet and "sheetId" in sheet
        assert isinstance(sheet["rules"], list)
        for rule in sheet["rules"]:
            # The priority read: index addressing + a body-only readable line (DESIGN §4).
            assert isinstance(rule["index"], int)
            assert isinstance(rule["line"], str) and rule["line"]
            assert isinstance(rule["ranges"], list)


def test_structure_read_smoke(live_service):
    """``structure(action="read")`` returns the shape-stable multi-sheet envelope."""
    from gsheets.core import structure

    sid = _test_spreadsheet_id()
    result = _call_with_quota_retry(structure, live_service, sid, action="read")

    assert result["ok"] is True
    assert result["spreadsheetId"] == sid
    # Spreadsheet-scoped namedRanges at top level; sheet-scoped data is a per-entry list.
    assert isinstance(result["namedRanges"], list)
    assert isinstance(result["sheets"], list) and result["sheets"]
    for sheet in result["sheets"]:
        assert "sheet" in sheet and "sheetId" in sheet
        assert isinstance(sheet["merges"], list)


# --------------------------------------------------------------------------- write round-trip


def test_values_write_roundtrip(live_service):
    """End-to-end CRUD symmetry: write → read back → append → clear, on a scratch range.

    Skipped unless ``GSHEETS_TEST_WRITE_RANGE`` names a clobberable range, so a read-only
    token or a "reads only" setup still gets the full read coverage above. The range is fully
    cleared in a ``finally`` so a run leaves no residue even if an assertion fails midway.
    """
    write_range = _scratch_write_range()
    if not write_range:
        pytest.skip("GSHEETS_TEST_WRITE_RANGE not set (no clobberable scratch range)")

    from gsheets.core import append_rows, clear, read_values, write_values

    sid = _test_spreadsheet_id()

    # A unique marker so this run's writes are unambiguously ours (and not confused with any
    # pre-existing content) when we read them back.
    marker = f"gsheets-live-{uuid.uuid4().hex[:8]}"
    formula = "=1+2"

    try:
        # 1) WRITE values + a formula (USER_ENTERED default: the formula must stay live).
        w = write_values(
            live_service,
            sid,
            [{"range": write_range, "values": [[marker, formula]]}],
        )
        assert w["ok"] is True
        assert w["updatedCells"] >= 2
        assert w["updatedRanges"], "write_values returned no updatedRanges"

        # Tiny settle pause: writes are strongly consistent, but guard against rare propagation
        # flake on the immediate read-back.
        time.sleep(0.5)

        # 2) READ BACK as formulas: the literal stays literal, the formula stays a formula
        #    (USER_ENTERED did not inert it — the Prajapdh RAW footgun is avoided).
        rb_formula = _call_with_quota_retry(
            read_values, live_service, sid, [write_range], render="formula"
        )
        cells = rb_formula["ranges"][0]["values"]
        flat = [c for row in cells for c in row]
        assert marker in flat, f"marker {marker!r} not found in formula read-back {flat!r}"
        assert any(
            isinstance(c, str) and c.startswith("=") for c in flat
        ), f"no live formula survived the round-trip: {flat!r}"

        # 3) READ BACK with render="all": the formula's computed value resolves to 3.
        rb_all = _call_with_quota_retry(
            read_values, live_service, sid, [write_range], render="all"
        )
        entry = rb_all["ranges"][0]
        assert len(entry["values"]) == len(entry["computed"])  # common-rectangle alignment
        computed_flat = [str(c) for row in entry["computed"] for c in row]
        assert "3" in computed_flat, f"=1+2 did not compute to 3: {computed_flat!r}"

        # 4) APPEND a row (INSERT_ROWS, no overwrite) using the same scratch range as the table.
        append_marker = f"{marker}-appended"
        a = append_rows(live_service, sid, write_range, [[append_marker]])
        assert a["ok"] is True
        assert a["updates"]["updatedRows"] >= 1
        assert a["updates"]["updatedCells"] >= 1
    finally:
        # 5) CLEAR everything we touched — values, formats, validation, and notes — so the
        #    scratch range is pristine for the next run regardless of assertion outcome.
        c = clear(
            live_service,
            sid,
            [write_range],
            values=True,
            formats=True,
            validation=True,
            notes=True,
        )
        assert c["ok"] is True
        assert write_range in c["clearedRanges"]
