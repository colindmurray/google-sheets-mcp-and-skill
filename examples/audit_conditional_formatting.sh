#!/usr/bin/env bash
#
# Recipe: audit a sheet's conditional formatting.
#
# Conditional-format rules are the rules that color cells dynamically (e.g. "red when overdue").
# Generic tooling reads cell *values*; it cannot read these rules. `gsheets` serializes every rule
# to a terse, readable, round-trippable line with its positional index (0 = highest priority).
#
# Usage:
#   export GSHEETS_EXAMPLE_SPREADSHEET_ID='<YOUR_SPREADSHEET_ID>'
#   ./examples/audit_conditional_formatting.sh [SHEET_NAME]
#
# Args:
#   SHEET_NAME   optional tab to restrict to; omit to audit every tab.
#
# Prereqs: `gsheets auth login` done once; `jq` for the JSON summary (optional).

set -euo pipefail

SPREADSHEET_ID="${GSHEETS_EXAMPLE_SPREADSHEET_ID:?set GSHEETS_EXAMPLE_SPREADSHEET_ID to your spreadsheet id (the token between /d/ and /edit)}"
SHEET="${1:-}"

# 1. Cheap orientation first — counts of CF rules per tab, no grid data pulled.
#    (--json is GLOBAL: it precedes the subcommand.)
echo "==> overview (per-tab conditional-format counts)"
echo "    \$ gsheets --json overview \"\$SPREADSHEET_ID\""
if command -v jq >/dev/null 2>&1; then
  gsheets --json overview "$SPREADSHEET_ID" \
    | jq -r '.sheets[] | "  \(.title): \(.conditionalFormatCount // 0) CF rules, \(.protectedRangeCount // 0) protected"'
else
  gsheets --json overview "$SPREADSHEET_ID"
fi

# 2. Read the actual rules. Each line is body-only; the index is the write-addressing source of truth.
echo
if [ -n "$SHEET" ]; then
  echo "==> conditional-format rules for tab: $SHEET"
  echo "    \$ gsheets read-conditional-formats \"\$SPREADSHEET_ID\" --sheet \"$SHEET\""
  gsheets read-conditional-formats "$SPREADSHEET_ID" --sheet "$SHEET"
else
  echo "==> conditional-format rules for ALL tabs"
  echo "    \$ gsheets read-conditional-formats \"\$SPREADSHEET_ID\""
  gsheets read-conditional-formats "$SPREADSHEET_ID"
fi

# Example output line:
#   # Sheet1 (id=0)
#     [0] [Sheet1!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold
#     [1] [Sheet1!G2:G100] gradient min=#FFFFFF | max=#1A73E8
#
# To EDIT a rule you read here, copy the body line, change it, and write it back to the SAME index:
#   gsheets set-conditional-format "$SPREADSHEET_ID" --action update --index 0 \
#     --rule '[Sheet1!A2:A100] if CUSTOM_FORMULA(=$B2>20) -> bg #FFCDD2 bold'
# (The line carries no index — addressing comes only from --index. When changing several rules in
#  separate calls, mutate high index -> low, or use the --rules-json batch form.)
