#!/usr/bin/env bash
#
# Recipe: read the formulas of a column (not just the computed values).
#
# A plain value read hides what a cell actually computes. `gsheets` reads the FORMULA behind each
# value, and `--render all` shows the formula and its computed result side by side, index-aligned.
#
# Usage:
#   export GSHEETS_EXAMPLE_SPREADSHEET_ID='<YOUR_SPREADSHEET_ID>'
#   ./examples/read_column_formulas.sh 'Sheet1!C1:C200'
#   ./examples/read_column_formulas.sh 'Sheet1!C:C'          # whole column
#
# Args:
#   RANGE   A1 range for the column to read (default: 'Sheet1!C1:C200'). Quote it.
#
# Prereqs: `gsheets auth login` done once; `jq` optional.

set -euo pipefail

SPREADSHEET_ID="${GSHEETS_EXAMPLE_SPREADSHEET_ID:?set GSHEETS_EXAMPLE_SPREADSHEET_ID to your spreadsheet id (the token between /d/ and /edit)}"
RANGE="${1:-Sheet1!C1:C200}"

# 1. Formula + computed value side by side. --render all issues two render passes (FORMULA and
#    FORMATTED_VALUE) and pads both to a common rectangle so values[r][c] / computed[r][c] line up.
#    Terse output renders each cell as "formula => computed".
echo "==> formula + result for $RANGE"
echo "    \$ gsheets read-values \"\$SPREADSHEET_ID\" \"$RANGE\" --render all"
gsheets read-values "$SPREADSHEET_ID" "$RANGE" --render all

# 2. Machine-readable form (paired arrays). A `values` entry that does NOT start with '=' is a
#    literal (FORMULA render passes non-formula cells through verbatim), not a formula.
echo
echo "==> same read as JSON (formula vs computed, machine-readable)"
echo "    \$ gsheets --json read-values \"\$SPREADSHEET_ID\" \"$RANGE\" --render all"
if command -v jq >/dev/null 2>&1; then
  gsheets --json read-values "$SPREADSHEET_ID" "$RANGE" --render all \
    | jq -r '.ranges[]
             | "# \(.range)",
               ( [.values, .computed]
                 | transpose[]                      # pair each [formula_row, computed_row]
                 | [ (.[0] // []), (.[1] // []) ]
                 | transpose[]                       # pair each [formula_cell, computed_cell]
                 | "  \(.[0])  =>  \(.[1])" )'
else
  gsheets --json read-values "$SPREADSHEET_ID" "$RANGE" --render all
fi

# Tip: to inspect formatting AND formulas together for the same range (incl. effectiveFormat — the
# color a cell actually renders, including conditional-format results), use:
#   gsheets --json inspect "$SPREADSHEET_ID" "$RANGE"
