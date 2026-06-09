#!/usr/bin/env bash
#
# Recipe: a safe value write (read -> write -> read back).
#
# Writes default to USER_ENTERED, so a string like "=SUM(C2:C200)" becomes a LIVE formula (and
# "5"/"$10"/"50%" coerce to number/currency/percent) — not inert text. The safe loop is: read the
# target first so you know what you're changing, write, then read it back to verify (CRUD symmetry).
#
# Usage:
#   export GSHEETS_EXAMPLE_SPREADSHEET_ID='<YOUR_SPREADSHEET_ID>'
#   ./examples/safe_value_write.sh 'Sheet1!E1' '=SUM(C2:C200)'
#   ./examples/safe_value_write.sh 'Sheet1!E1' 'Total' raw     # store literal text, no coercion
#
# Args:
#   RANGE   single-cell/range A1 target (default: 'Sheet1!E1'). Quote it.
#   VALUE   the value to write (default: '=SUM(C2:C200)'). A leading '=' becomes a formula.
#   INPUT   'user_entered' (default) or 'raw' (store verbatim, no formula/number coercion).
#
# Prereqs: `gsheets auth login` done once.

set -euo pipefail

SPREADSHEET_ID="${GSHEETS_EXAMPLE_SPREADSHEET_ID:?set GSHEETS_EXAMPLE_SPREADSHEET_ID to your spreadsheet id (the token between /d/ and /edit)}"
RANGE="${1:-Sheet1!E1}"
VALUE="${2:-=SUM(C2:C200)}"
INPUT="${3:-user_entered}"

# 1. READ the target first — never overwrite blind. inspect shows the existing value + formula.
echo "==> BEFORE: current contents of $RANGE"
echo "    \$ gsheets inspect \"\$SPREADSHEET_ID\" \"$RANGE\""
gsheets inspect "$SPREADSHEET_ID" "$RANGE"

# 2. WRITE. --values-json takes a 2D array of rows. USER_ENTERED (the default) makes a leading '='
#    a live formula; pass --input raw only when you truly want the literal text stored verbatim.
VALUES_JSON=$(printf '[["%s"]]' "$VALUE")
echo
echo "==> WRITE \"$VALUE\" to $RANGE  (input=$INPUT)"
echo "    \$ gsheets write-values \"\$SPREADSHEET_ID\" \"$RANGE\" --values-json '$VALUES_JSON' --input $INPUT"
gsheets write-values "$SPREADSHEET_ID" "$RANGE" --values-json "$VALUES_JSON" --input "$INPUT"

# 3. READ IT BACK — the cheapest verification a write did what you meant. With a formula you'll see
#    the formula AND its computed result.
echo
echo "==> AFTER: read back $RANGE (formula + computed)"
echo "    \$ gsheets read-values \"\$SPREADSHEET_ID\" \"$RANGE\" --render all"
gsheets read-values "$SPREADSHEET_ID" "$RANGE" --render all

# To append a row to a table instead of overwriting a cell (INSERT_ROWS, never clobbers below):
#   gsheets append-rows "$SPREADSHEET_ID" 'Sheet1!A1' --values-json '[["2026-06-09", 5, 12]]'
