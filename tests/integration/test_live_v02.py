"""Live integration round-trips for the v0.2 feature-gap surface (DESIGN §Extensions).

Companion to ``test_live_smoke.py`` (which covers the base 15-tool surface). This module proves
the **17 v0.2 capabilities** survive a real ``batchUpdate``/Drive round-trip — i.e. that every new
request shape (field path, enum, fields mask) the unit suite only exercises against a *mocked*
service is actually accepted by the live API, and that what we write reads back through the new
serializers. The mocked unit suite cannot catch a wrong enum / bad mask / wrong field path in the
new verbs; these tests can.

Same gating + Production guard as the base smoke module:

- module-level ``pytestmark = pytest.mark.live`` (a plain ``pytest`` run skips them — no network);
- the ``live_service`` fixture skips unless ``GSHEETS_LIVE=1`` AND ``GSHEETS_TEST_SPREADSHEET_ID``
  is set (the target id is supplied entirely from the environment — never committed, never
  Production); a fat-fingered Production id is refused via the salted-hash denylist reused from
  ``test_live_smoke``;
- the WRITE round-trips additionally require ``GSHEETS_TEST_WRITE_RANGE`` (e.g.
  ``"Scratch!A1:D20"``) — a clobberable range whose **tab** the write verbs target. Unset ⇒ the
  write tests skip, so a read-only setup still gets the v0.2 READ coverage below.

Each write test is self-contained: it sets up its own scratch data on the configured tab, performs
the new verb, reads it back through the matching new read path, and clears in a ``finally`` so a run
leaves no residue even if an assertion fails midway.

Boundary note (DESIGN §1): the ``gsheets.core`` imports are lazy (inside each test), so collecting
this module under a normal unit run never imports ``gsheets.auth`` and never resolves credentials.
"""

from __future__ import annotations

import importlib.util
import time
import uuid
from pathlib import Path

import pytest

# Reuse the base smoke module's env helpers + Production denylist so there is ONE source of truth
# for scratch-id resolution and the no-plaintext Production guard (DESIGN §0). Load it by FILE PATH
# (not a `tests.integration.…` import) so this works regardless of whether `tests/` is importable as
# a package under the active pytest rootdir/import mode — mirroring tests/unit/test_live_denylist_guard.py.
# Importing these plain helper functions never runs the `live`-marked tests, so no network is touched.
_SMOKE_PATH = Path(__file__).resolve().parent / "test_live_smoke.py"
_spec = importlib.util.spec_from_file_location("_live_smoke_for_v02_tests", _SMOKE_PATH)
_smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_smoke)

_call_with_quota_retry = _smoke._call_with_quota_retry
_first_sheet_title = _smoke._first_sheet_title
_scratch_write_range = _smoke._scratch_write_range
_test_spreadsheet_id = _smoke._test_spreadsheet_id

pytestmark = pytest.mark.live


# --------------------------------------------------------------------------- write-scope helpers


def _scratch_sheet_and_range() -> tuple[str, str]:
    """Return ``(tab_title, a1_range)`` from ``GSHEETS_TEST_WRITE_RANGE`` or skip.

    The v0.2 write verbs (data_ops/dimensions/structure tables-banding-filters) target a whole
    **tab** or a range on it, so they need a clobberable scratch tab — exactly the one the base
    smoke module's values round-trip uses. We derive the tab title from the configured A1 range
    (``"Scratch!A1:D20"`` → ``"Scratch"``). Unset ⇒ skip (read-only setups keep the READ coverage).
    """
    write_range = _scratch_write_range()
    if not write_range:
        pytest.skip("GSHEETS_TEST_WRITE_RANGE not set (no clobberable scratch tab)")
    if "!" not in write_range:
        pytest.skip(
            f"GSHEETS_TEST_WRITE_RANGE={write_range!r} has no Sheet!A1 form; cannot derive a tab"
        )
    title = write_range.split("!", 1)[0].strip().strip("'")
    if not title:
        pytest.skip(f"could not derive a scratch tab title from {write_range!r}")
    return title, write_range


def _seed_block(live_service, sid, sheet, block):
    """Write a small ``block`` of values to ``{sheet}!A1`` (USER_ENTERED) as scratch input."""
    from gsheets.core import write_values

    rng = f"{sheet}!A1"
    _call_with_quota_retry(
        write_values, live_service, sid, [{"range": rng, "values": block}]
    )


def _clear_scratch(live_service, sid, sheet):
    """Fully clear ``{sheet}!A1:Z200`` (values+formats+validation+notes) — leave no residue."""
    from gsheets.core import clear

    _call_with_quota_retry(
        clear,
        live_service,
        sid,
        [f"{sheet}!A1:Z200"],
        values=True,
        formats=True,
        validation=True,
        notes=True,
    )


def _settle():
    """Tiny pause guarding against rare read-after-write propagation flake (writes are strong)."""
    time.sleep(0.4)


# =============================================================================== data_ops (writes)


def test_data_ops_find_replace_and_sort_roundtrip(live_service):
    """``data_ops`` find_replace + sort_range land on a real sheet and read back changed (§X.2).

    Proves two of the seven data verbs against the live API: ``findReplace`` reports occurrences
    and the replacement is visible on read-back; ``sortRange`` reorders rows in place. These are
    the verbs whose request shape (regex/scope flags, sort specs) the mocked suite cannot validate.
    """
    from gsheets.core import data_ops, read_values

    sid = _test_spreadsheet_id()
    sheet, _ = _scratch_sheet_and_range()
    marker = f"GS{uuid.uuid4().hex[:6].upper()}"

    try:
        # Seed: a 3-row block with a replaceable token and an unsorted numeric column.
        _seed_block(
            live_service,
            sid,
            sheet,
            [
                [f"{marker}_old", 3],
                [f"{marker}_old", 1],
                [f"{marker}_old", 2],
            ],
        )
        _settle()

        # find_replace, scoped to the sheet (exactly one of range/sheet/allSheets).
        fr = _call_with_quota_retry(
            data_ops,
            live_service,
            sid,
            action="find_replace",
            params={
                "find": f"{marker}_old",
                "replacement": f"{marker}_new",
                "sheet": sheet,
                "matchEntireCell": True,
            },
        )
        assert fr["ok"] is True
        assert fr["action"] == "find_replace"
        assert fr["occurrencesChanged"] >= 3

        # sort_range, ascending on the numeric column (B).
        sr = _call_with_quota_retry(
            data_ops,
            live_service,
            sid,
            action="sort_range",
            params={
                "range": f"{sheet}!A1:B3",
                "specs": [{"col": "B", "order": "ASCENDING"}],
            },
        )
        assert sr["ok"] is True
        assert sr["action"] == "sort_range"

        _settle()
        # Read back: replacement applied + column B sorted 1,2,3.
        rb = _call_with_quota_retry(
            read_values, live_service, sid, [f"{sheet}!A1:B3"], render="unformatted"
        )
        rows = rb["ranges"][0]["values"]
        col_a = [r[0] for r in rows]
        col_b = [r[1] for r in rows]
        assert all(c == f"{marker}_new" for c in col_a), col_a
        assert [int(x) for x in col_b] == [1, 2, 3], col_b
    finally:
        _clear_scratch(live_service, sid, sheet)


def test_data_ops_geometry_verbs_roundtrip(live_service):
    """``data_ops`` copy_paste + delete_duplicates + trim_whitespace accepted live (§X.2/#11/#14).

    Exercises three more verbs whose enums/shape the mock can't check: ``copyPaste`` (pasteType),
    ``deleteDuplicates`` (comparisonColumns) — surfacing ``duplicatesRemoved`` — and
    ``trimWhitespace``. Read-back confirms the duplicate row is gone.
    """
    from gsheets.core import data_ops, read_values

    sid = _test_spreadsheet_id()
    sheet, _ = _scratch_sheet_and_range()
    marker = f"DUP{uuid.uuid4().hex[:5].upper()}"

    try:
        # Row 1 carries trimmable whitespace (the trim_whitespace target). Rows 2 and 3 are EXACT
        # duplicates so delete_duplicates deterministically removes one regardless of trimming.
        _seed_block(
            live_service,
            sid,
            sheet,
            [
                [f"  {marker}_ws  ", "x"],  # leading/trailing space → trim target
                [f"{marker}_dup", "y"],  # exact duplicate of row 3 ↓
                [f"{marker}_dup", "y"],  # exact duplicate of row 2 ↑
            ],
        )
        _settle()

        # trim_whitespace, then read row 1 back to PROVE the surrounding spaces were stripped.
        tw = _call_with_quota_retry(
            data_ops,
            live_service,
            sid,
            action="trim_whitespace",
            params={"range": f"{sheet}!A1:B1"},
        )
        assert tw["ok"] is True
        assert tw["action"] == "trim_whitespace"

        _settle()
        rb_trim = _call_with_quota_retry(
            read_values, live_service, sid, [f"{sheet}!A1"], render="unformatted"
        )
        trimmed = rb_trim["ranges"][0]["values"][0][0]
        assert trimmed == f"{marker}_ws", repr(trimmed)

        # copy_paste a value block to a fresh region (PASTE_VALUES) — proves the enum is accepted.
        cp = _call_with_quota_retry(
            data_ops,
            live_service,
            sid,
            action="copy_paste",
            params={
                "source": f"{sheet}!A1:B3",
                "destination": f"{sheet}!D1:E3",
                "pasteType": "PASTE_VALUES",
            },
        )
        assert cp["ok"] is True
        assert cp["action"] == "copy_paste"

        # delete_duplicates over the two exact-duplicate rows (2 & 3): one must be removed.
        _settle()
        dd = _call_with_quota_retry(
            data_ops,
            live_service,
            sid,
            action="delete_duplicates",
            params={"range": f"{sheet}!A2:B3", "comparisonColumns": ["A", "B"]},
        )
        assert dd["ok"] is True
        assert dd["action"] == "delete_duplicates"
        assert dd["duplicatesRemoved"] >= 1, dd
    finally:
        _clear_scratch(live_service, sid, sheet)


# =============================================================================== dimensions


def test_dimensions_insert_setprops_read_delete_roundtrip(live_service):
    """``dimensions`` insert → set_props(hidden) → read(hidden) → delete round-trip (§X.7/#7/#13).

    Proves the row/column verbs against the live API: ``insertDimension`` shifts content,
    ``updateDimensionProperties`` (auto fields mask) hides a row, the ``read`` action surfaces that
    hidden row, ``autoResizeDimensions`` is accepted, and ``deleteDimension`` removes a row. The
    hidden read is the CRUD-symmetry proof for #13.
    """
    from gsheets.core import dimensions, read_values

    sid = _test_spreadsheet_id()
    sheet, _ = _scratch_sheet_and_range()
    marker = f"DIM{uuid.uuid4().hex[:5].upper()}"

    try:
        _seed_block(
            live_service,
            sid,
            sheet,
            [[f"{marker}_r0"], [f"{marker}_r1"], [f"{marker}_r2"]],
        )
        _settle()

        # insert one row at index 1 (0-based half-open [1,2)) → pushes r1/r2 down.
        ins = _call_with_quota_retry(
            dimensions,
            live_service,
            sid,
            action="insert",
            sheet=sheet,
            params={"dimension": "ROWS", "start": 1, "end": 2},
        )
        assert ins["ok"] is True
        assert ins["action"] == "insert"

        # auto_resize the columns (whole sheet) — accepted-shape check.
        ar = _call_with_quota_retry(
            dimensions,
            live_service,
            sid,
            action="auto_resize",
            sheet=sheet,
            params={"dimension": "COLUMNS"},
        )
        assert ar["ok"] is True

        # hide row index 0 via set_props (auto fields mask on hiddenByUser).
        sp = _call_with_quota_retry(
            dimensions,
            live_service,
            sid,
            action="set_props",
            sheet=sheet,
            params={
                "dimension": "ROWS",
                "start": 0,
                "end": 1,
                "hiddenByUser": True,
            },
        )
        assert sp["ok"] is True
        assert sp["action"] == "set_props"

        _settle()
        # read back the hidden rows — row 0 must be reported hidden (the #13 round-trip).
        hr = _call_with_quota_retry(
            dimensions, live_service, sid, action="read", sheet=sheet
        )
        assert hr["ok"] is True
        assert hr["action"] == "read"
        assert isinstance(hr["hiddenRows"], list)
        assert isinstance(hr["hiddenCols"], list)
        assert 0 in hr["hiddenRows"], hr["hiddenRows"]

        # delete the inserted blank row to restore geometry.
        dl = _call_with_quota_retry(
            dimensions,
            live_service,
            sid,
            action="delete",
            sheet=sheet,
            params={"dimension": "ROWS", "start": 1, "end": 2},
        )
        assert dl["ok"] is True
        assert dl["action"] == "delete"

        _settle()
        # sanity: content is still readable (no exception through the value path).
        rb = _call_with_quota_retry(
            read_values, live_service, sid, [f"{sheet}!A1:A3"], render="plain"
        )
        assert rb["ok"] is True
    finally:
        # Unhide before clearing so the scratch tab is pristine for the next run.
        try:
            _call_with_quota_retry(
                dimensions,
                live_service,
                sid,
                action="set_props",
                sheet=sheet,
                params={
                    "dimension": "ROWS",
                    "start": 0,
                    "end": 1,
                    "hiddenByUser": False,
                },
            )
        finally:
            _clear_scratch(live_service, sid, sheet)


# =============================================================================== structure: tables


def test_structure_table_crud_roundtrip(live_service):
    """``structure`` add_table → read(tables) → update_table → delete_table (§X.3/#3).

    The flagship v0.2 write: ``addTable`` with a typed column set (incl. a DROPDOWN requiring a
    ONE_OF_LIST validation) returns a real ``tableId``; ``structure(action="read")`` surfaces the
    table through the new ``tables`` per-sheet serializer (proving CRUD symmetry); ``updateTable``
    (auto fields mask) renames it; ``deleteTable`` removes it.
    """
    from gsheets.core import structure

    sid = _test_spreadsheet_id()
    sheet, _ = _scratch_sheet_and_range()
    # Table names must not resemble an A1 cell reference (a letter followed by digits, e.g. "T8675",
    # is rejected by addTable), must not start with a digit, and allow no special chars but "_". The
    # "Tbl_" prefix + underscore guarantees the name is never a valid cell ref and is unique-enough.
    tname = f"Tbl_{uuid.uuid4().hex[:6]}"

    table_id = None
    try:
        # A table needs a header row matching the declared columns.
        _seed_block(
            live_service,
            sid,
            sheet,
            [["Region", "Status"], ["West", "Open"], ["East", "Closed"]],
        )
        _settle()

        add = _call_with_quota_retry(
            structure,
            live_service,
            sid,
            action="add_table",
            sheet=sheet,
            range=f"{sheet}!A1:B3",
            params={
                "name": tname,
                "columns": [
                    {"name": "Region", "type": "TEXT"},
                    {
                        "name": "Status",
                        "type": "DROPDOWN",
                        # WRITE input is a STRUCTURED ValidationRule (rule_to_validation shape) —
                        # the terse "ONE_OF_LIST(Open,Closed)" string is the READ form, asserted on
                        # round-trip below.
                        "validation": {
                            "type": "ONE_OF_LIST",
                            "values": ["Open", "Closed"],
                        },
                    },
                ],
            },
        )
        assert add["ok"] is True
        assert add["action"] == "add_table"
        table_id = add["tableId"]
        assert table_id, "add_table returned no tableId"

        _settle()
        rd = _call_with_quota_retry(
            structure, live_service, sid, action="read", sheet=sheet
        )
        tables = rd["sheets"][0]["tables"]
        assert any(t.get("tableId") == table_id for t in tables), tables
        mine = next(t for t in tables if t.get("tableId") == table_id)
        assert mine.get("name") == tname
        assert isinstance(mine.get("line"), str) and mine["line"]
        # DROPDOWN column's validation round-trips into the terse line.
        assert any(c.get("type") == "DROPDOWN" for c in mine.get("columns", []))

        new_name = f"{tname}R"
        up = _call_with_quota_retry(
            structure,
            live_service,
            sid,
            action="update_table",
            sheet=sheet,
            params={"tableId": table_id, "name": new_name},
        )
        assert up["ok"] is True
        assert up["action"] == "update_table"

        _settle()
        rd2 = _call_with_quota_retry(
            structure, live_service, sid, action="read", sheet=sheet
        )
        renamed = next(
            t for t in rd2["sheets"][0]["tables"] if t.get("tableId") == table_id
        )
        assert renamed.get("name") == new_name
    finally:
        if table_id is not None:
            try:
                _call_with_quota_retry(
                    structure,
                    live_service,
                    sid,
                    action="delete_table",
                    sheet=sheet,
                    params={"tableId": table_id},
                )
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        _clear_scratch(live_service, sid, sheet)


# =============================================================================== structure: banding


def test_structure_banding_crud_roundtrip(live_service):
    """``structure`` add_banding → read(bandedRanges) → update_banding → delete_banding (§X.9/#9).

    ``addBanding`` with hex row colors returns a real ``bandedRangeId``; the read surfaces it via
    the new ``bandedRanges`` serializer with hexes flattened; ``updateBanding`` (auto fields mask)
    recolors; ``deleteBanding`` removes it.
    """
    from gsheets.core import structure

    sid = _test_spreadsheet_id()
    sheet, _ = _scratch_sheet_and_range()

    banded_id = None
    try:
        _seed_block(
            live_service,
            sid,
            sheet,
            [["a"], ["b"], ["c"], ["d"]],
        )
        _settle()

        add = _call_with_quota_retry(
            structure,
            live_service,
            sid,
            action="add_banding",
            sheet=sheet,
            range=f"{sheet}!A1:A4",
            params={
                "rowBanding": {
                    "header": "#4285F4",
                    "first": "#FFFFFF",
                    "second": "#E8F0FE",
                }
            },
        )
        assert add["ok"] is True
        assert add["action"] == "add_banding"
        banded_id = add["bandedRangeId"]
        assert banded_id is not None, "add_banding returned no bandedRangeId"

        _settle()
        rd = _call_with_quota_retry(
            structure, live_service, sid, action="read", sheet=sheet
        )
        bands = rd["sheets"][0]["bandedRanges"]
        mine = next(b for b in bands if b.get("bandedRangeId") == banded_id)
        assert isinstance(mine.get("line"), str) and mine["line"]
        assert mine.get("rowBanding"), mine

        up = _call_with_quota_retry(
            structure,
            live_service,
            sid,
            action="update_banding",
            sheet=sheet,
            params={
                "bandedRangeId": banded_id,
                "rowBanding": {
                    "header": "#34A853",
                    "first": "#FFFFFF",
                    "second": "#E6F4EA",
                },
            },
        )
        assert up["ok"] is True
        assert up["action"] == "update_banding"
    finally:
        if banded_id is not None:
            try:
                _call_with_quota_retry(
                    structure,
                    live_service,
                    sid,
                    action="delete_banding",
                    sheet=sheet,
                    params={"bandedRangeId": banded_id},
                )
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        _clear_scratch(live_service, sid, sheet)


# =============================================================================== structure: filters


def test_structure_basic_filter_and_filter_view_roundtrip(live_service):
    """``structure`` set/clear_basic_filter + add/update/delete_filter_view round-trip (§X.4/#4).

    ``setBasicFilter`` (sort + criteria) reads back via the new ``basicFilter`` serializer;
    ``addFilterView`` returns a real ``filterViewId`` and reads back via ``filterViews``;
    ``updateFilterView`` (auto fields mask) renames; then both are torn down (``deleteFilterView``,
    ``clearBasicFilter``).
    """
    from gsheets.core import structure

    sid = _test_spreadsheet_id()
    sheet, _ = _scratch_sheet_and_range()

    fv_id = None
    basic_filter_set = False
    try:
        _seed_block(
            live_service,
            sid,
            sheet,
            [["Name", "Qty"], ["a", 5], ["b", 1], ["c", 9]],
        )
        _settle()

        sb = _call_with_quota_retry(
            structure,
            live_service,
            sid,
            action="set_basic_filter",
            sheet=sheet,
            range=f"{sheet}!A1:B4",
            params={"sorted": [{"col": "B", "order": "ASCENDING"}]},
        )
        assert sb["ok"] is True
        assert sb["action"] == "set_basic_filter"
        basic_filter_set = True

        title = f"FV{uuid.uuid4().hex[:5].upper()}"
        afv = _call_with_quota_retry(
            structure,
            live_service,
            sid,
            action="add_filter_view",
            sheet=sheet,
            range=f"{sheet}!A1:B4",
            params={"title": title},
        )
        assert afv["ok"] is True
        assert afv["action"] == "add_filter_view"
        fv_id = afv["filterViewId"]
        assert fv_id is not None, "add_filter_view returned no filterViewId"

        _settle()
        rd = _call_with_quota_retry(
            structure, live_service, sid, action="read", sheet=sheet
        )
        sheet_entry = rd["sheets"][0]
        assert sheet_entry["basicFilter"] is not None
        assert isinstance(sheet_entry["basicFilter"].get("line"), str)
        mine = next(
            fv for fv in sheet_entry["filterViews"] if fv.get("filterViewId") == fv_id
        )
        assert mine.get("title") == title

        new_title = f"{title}R"
        ufv = _call_with_quota_retry(
            structure,
            live_service,
            sid,
            action="update_filter_view",
            sheet=sheet,
            params={"filterViewId": fv_id, "title": new_title},
        )
        assert ufv["ok"] is True
        assert ufv["action"] == "update_filter_view"

        _settle()
        rd2 = _call_with_quota_retry(
            structure, live_service, sid, action="read", sheet=sheet
        )
        renamed = next(
            fv
            for fv in rd2["sheets"][0]["filterViews"]
            if fv.get("filterViewId") == fv_id
        )
        assert renamed.get("title") == new_title
    finally:
        if fv_id is not None:
            try:
                _call_with_quota_retry(
                    structure,
                    live_service,
                    sid,
                    action="delete_filter_view",
                    sheet=sheet,
                    params={"filterViewId": fv_id},
                )
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        if basic_filter_set:
            try:
                _call_with_quota_retry(
                    structure,
                    live_service,
                    sid,
                    action="clear_basic_filter",
                    sheet=sheet,
                )
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        _clear_scratch(live_service, sid, sheet)


# =============================================================================== spreadsheet_props


def test_structure_spreadsheet_props_roundtrip(live_service):
    """``structure(action="spreadsheet_props")`` sets title and ``overview`` reads it back (§X.12).

    The one mutating action needing NEITHER sheet NOR range. We round-trip ONLY the spreadsheet
    *title* (locale/timeZone are global and risky to flip on a shared sheet): set a unique title,
    confirm ``overview`` echoes it, then restore the original title so the run is non-destructive.
    """
    from gsheets.core import overview, structure

    sid = _test_spreadsheet_id()

    original = _call_with_quota_retry(overview, live_service, sid)
    original_title = original["title"]
    new_title = f"{original_title} [gsheets-live-{uuid.uuid4().hex[:6]}]"

    try:
        sp = _call_with_quota_retry(
            structure,
            live_service,
            sid,
            action="spreadsheet_props",
            params={"title": new_title},
        )
        assert sp["ok"] is True
        assert sp["action"] == "spreadsheet_props"
        assert sp["title"] == new_title

        _settle()
        after = _call_with_quota_retry(overview, live_service, sid)
        assert after["title"] == new_title
    finally:
        # Restore the original title regardless of assertion outcome.
        _call_with_quota_retry(
            structure,
            live_service,
            sid,
            action="spreadsheet_props",
            params={"title": original_title},
        )


# =============================================================================== reads (no writes)


def test_structure_read_v02_keys_present(live_service):
    """``structure(action="read")`` per-sheet entries carry the FIVE new v0.2 keys (§X.3/4/9/16).

    A pure READ (no writes, so it runs even on a read-only token): every per-sheet entry must
    expose ``tables``/``filterViews``/``bandedRanges``/``slicers`` (lists) and ``basicFilter``
    (dict-or-None), proving the widened read mask + the new serializers wire through live JSON.
    """
    from gsheets.core import structure

    sid = _test_spreadsheet_id()
    result = _call_with_quota_retry(structure, live_service, sid, action="read")

    assert result["ok"] is True
    assert isinstance(result["sheets"], list) and result["sheets"]
    for sheet in result["sheets"]:
        for key in ("tables", "filterViews", "bandedRanges", "slicers"):
            assert key in sheet, f"structure read sheet missing {key!r}"
            assert isinstance(sheet[key], list), f"{key} must be a list"
        assert "basicFilter" in sheet
        assert sheet["basicFilter"] is None or isinstance(sheet["basicFilter"], dict)


def test_inspect_rich_text_and_pivot_flags_smoke(live_service):
    """``inspect(include_rich_text=True, include_pivot=True)`` runs live without raising (§X.1/§X.6).

    A pure READ: turning on both opt-in enrichments widens the per-cell mask with
    ``textFormatRuns``/``hyperlink``/``pivotTable``. We only assert the call succeeds and the cell
    shape holds — the target range may legitimately carry no runs/pivot, in which case those keys
    are absent (per-cell-only emission), which is itself the contract we verify.
    """
    from gsheets.core import inspect

    sid = _test_spreadsheet_id()
    title = _first_sheet_title(live_service, sid)

    result = _call_with_quota_retry(
        inspect,
        live_service,
        sid,
        f"{title}!A1:D10",
        include_rich_text=True,
        include_pivot=True,
    )
    assert result["ok"] is True
    assert isinstance(result["cells"], list)
    for cell in result["cells"]:
        assert "a1" in cell
        # Per-cell-only emission: runs/hyperlink/pivot attach ONLY when present.
        if "runs" in cell:
            assert isinstance(cell["runs"], list)
        if "hyperlink" in cell:
            assert isinstance(cell["hyperlink"], str)
        if "pivot" in cell:
            assert isinstance(cell["pivot"], dict)


def test_comments_read_smoke(live_service):
    """``comments`` reads the Drive comment thread (or skips cleanly without a Drive scope) (§X.5).

    Uses the Drive API (not Sheets). If the token has no Drive scope, core raises
    ``drive_unavailable`` and we skip (environmental, not a logic defect). Otherwise the envelope
    shape must hold and every serialized comment carries the flattened fields.
    """
    from gsheets.core import comments
    from gsheets.core.errors import SheetsError

    sid = _test_spreadsheet_id()
    try:
        result = _call_with_quota_retry(comments, live_service, sid)
    except SheetsError as exc:
        if getattr(exc, "code", None) == "drive_unavailable":
            pytest.skip("no Drive scope on this token — comments read unavailable")
        raise

    assert result["ok"] is True
    assert result["spreadsheetId"] == sid
    assert isinstance(result["comments"], list)
    for c in result["comments"]:
        assert "id" in c
        assert "content" in c
        assert isinstance(c.get("replies", []), list)
