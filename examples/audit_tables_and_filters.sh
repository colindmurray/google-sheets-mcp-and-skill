#!/usr/bin/env bash
#
# Recipe: audit a sheet's native tables, filter views, banding, and slicers.
#
# A sheet's *structure* — native Tables (typed columns + dropdowns), the basic filter, saved
# Filter Views, banded ranges, and slicers — is invisible to a plain value read. `gsheets`
# surfaces all of it via `structure --action read`, each serialized to a terse, round-trippable
# `line` alongside its structured fields (tableId / filterViewId / bandedRangeId / criteria).
#
# NOTE: these v0.2 structural reads are surfaced in the `--json` output. The terse (non-json)
# `structure --action read` prints merges / frozen panes / protected ranges; use `--json` (as
# below) to see tables / filterViews / basicFilter / bandedRanges / slicers.
#
# Usage:
#   export GSHEETS_EXAMPLE_SPREADSHEET_ID='<YOUR_SPREADSHEET_ID>'
#   ./examples/audit_tables_and_filters.sh [SHEET_NAME]
#
# Args:
#   SHEET_NAME   optional tab to restrict to; omit to audit every tab.
#
# Prereqs: `gsheets auth login` done once; `jq` for the JSON summary (optional but recommended).

set -euo pipefail

SPREADSHEET_ID="${GSHEETS_EXAMPLE_SPREADSHEET_ID:?set GSHEETS_EXAMPLE_SPREADSHEET_ID to your spreadsheet id (the token between /d/ and /edit)}"
SHEET="${1:-}"

# Build the optional --sheet argument (omit ⇒ every tab; structure read never raises for sheet=None).
SHEET_ARG=()
if [ -n "$SHEET" ]; then
  SHEET_ARG=(--sheet "$SHEET")
fi

# 1. The structural picture, as JSON. structure --action read uses a tight fields mask (never grid
#    data); v0.2 adds tables / basicFilter / filterViews / bandedRanges / slicers per sheet.
#    (--json is GLOBAL: it precedes the subcommand.)
echo "==> structure --action read${SHEET:+ (tab: $SHEET)}"
echo "    \$ gsheets --json structure \"\$SPREADSHEET_ID\" --action read ${SHEET_ARG[*]}"

if ! command -v jq >/dev/null 2>&1; then
  # No jq: dump the raw JSON; the tables/filterViews live under .sheets[].tables / .filterViews.
  gsheets --json structure "$SPREADSHEET_ID" --action read "${SHEET_ARG[@]}"
  exit 0
fi

# 2. Pretty per-tab summary. Each serialized table / filter view / banding carries a terse `line`
#    (e.g. `table "Sales" [Sheet1!A1:F500] cols: Region:TEXT, Status:DROPDOWN(Open,Closed)`), plus
#    the structured ids you'd use to edit it (tableId / filterViewId / bandedRangeId).
gsheets --json structure "$SPREADSHEET_ID" --action read "${SHEET_ARG[@]}" \
  | jq -r '
      .sheets[]
      | "# \(.sheet) (id=\(.sheetId))",
        ( (.tables // [])      [] | "  \(.line)    [tableId=\(.tableId)]" ),
        ( (.basicFilter // empty) | "  \(.line)" ),
        ( (.filterViews // []) [] | "  \(.line)    [filterViewId=\(.filterViewId)]" ),
        ( (.bandedRanges // []) [] | "  \(.line)    [bandedRangeId=\(.bandedRangeId)]" ),
        ( (.slicers // [])     [] | "  \(.line)    [slicerId=\(.slicerId)]" )
    '

# Example output:
#   # Sheet1 (id=0)
#     table "Sales" [Sheet1!A1:F500] cols: Region:TEXT, Status:DROPDOWN(Open,Closed)    [tableId=t_abc]
#     basicFilter [Sheet1!A1:F500] sort C asc | B: hide Closed, NUMBER_GREATER(0)
#     filterView 123 "Open only" [Sheet1!A1:F500] | B: hide Closed    [filterViewId=123]
#     banding 7 [Sheet1!A1:F500] rows: hdr #4285F4 / #FFFFFF / #E8F0FE    [bandedRangeId=7]
#
# To EDIT what you found here, address it by its id and re-read to verify (full CRUD symmetry):
#   gsheets structure "$SPREADSHEET_ID" --action update_filter_view \
#     --params-json '{"filterViewId": 123, "title": "Open + recent"}'
#   gsheets structure "$SPREADSHEET_ID" --action delete_table --params-json '{"tableId": "t_abc"}'
# (delete_table / delete_filter_view / delete_banding are destructive — read first, then confirm.)
