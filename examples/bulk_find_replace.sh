#!/usr/bin/env bash
#
# Recipe: a safe bulk (regex) find/replace.
#
# `data-ops --action find_replace` runs Google's native findReplace in ONE batch request — far
# safer and cheaper than reading every cell and writing each one back. The safe loop mirrors a
# value write: read the target SCOPE first so you know what you're about to change, run the
# replace, then read it back and check the `occurrencesChanged` count.
#
# find/replace runs over EXACTLY ONE scope — a `range`, a `sheet`, or `allSheets` — never more
# than one at a time. Prefer the narrowest scope you can; this recipe scopes to a single range.
#
# Usage:
#   export GSHEETS_EXAMPLE_SPREADSHEET_ID='<YOUR_SPREADSHEET_ID>'
#   ./examples/bulk_find_replace.sh 'Sheet1!B2:B500' '\bN/?A\b' ''        # regex: blank out N/A, NA
#   ./examples/bulk_find_replace.sh 'Sheet1!A2:A500' 'colour' 'color' plain  # plain (non-regex) swap
#
# Args:
#   RANGE   A1 range to operate on (default: 'Sheet1!A1:Z1000'). Quote it.
#   FIND    the search string. Treated as a REGEX unless MODE is 'plain'.
#   REPL    the replacement string (default: '').
#   MODE    'regex' (default) or 'plain'. 'plain' disables searchByRegex.
#
# Prereqs: `gsheets auth login` done once; `jq` optional (used for the count summary).

set -euo pipefail

SPREADSHEET_ID="${GSHEETS_EXAMPLE_SPREADSHEET_ID:?set GSHEETS_EXAMPLE_SPREADSHEET_ID to your spreadsheet id (the token between /d/ and /edit)}"
RANGE="${1:-Sheet1!A1:Z1000}"
FIND="${2:?usage: bulk_find_replace.sh RANGE FIND [REPL] [regex|plain]}"
REPL="${3:-}"
MODE="${4:-regex}"

# searchByRegex defaults to true (regex). Pass MODE=plain for a literal, non-regex swap.
BY_REGEX=true
[ "$MODE" = "plain" ] && BY_REGEX=false

# 1. READ the scope first — never replace blind. inspect shows the current values + formulas so you
#    can confirm what FIND will match. (Use --no-effective/--no-user-entered to keep it cheap.)
echo "==> BEFORE: current contents of $RANGE"
echo "    \$ gsheets inspect \"\$SPREADSHEET_ID\" \"$RANGE\" --no-effective --no-user-entered"
gsheets inspect "$SPREADSHEET_ID" "$RANGE" --no-effective --no-user-entered

# 2. RUN the find/replace, scoped to this one range. matchCase=false here; flip it for case-sensitive.
#    --params-json carries the verb's params; "range" is the scope (exactly one of range/sheet/allSheets).
PARAMS_JSON=$(jq -nc \
  --arg find "$FIND" --arg repl "$REPL" --arg range "$RANGE" --argjson regex "$BY_REGEX" \
  '{find: $find, replacement: $repl, searchByRegex: $regex, matchCase: false, range: $range}' \
  2>/dev/null || printf '{"find":%s,"replacement":%s,"searchByRegex":%s,"matchCase":false,"range":%s}' \
    "\"${FIND//\"/\\\"}\"" "\"${REPL//\"/\\\"}\"" "$BY_REGEX" "\"$RANGE\"")

echo
echo "==> REPLACE  find=$(printf %q "$FIND")  ->  repl=$(printf %q "$REPL")  (searchByRegex=$BY_REGEX, scope=$RANGE)"
echo "    \$ gsheets data-ops \"\$SPREADSHEET_ID\" --action find_replace --params-json '$PARAMS_JSON'"
if command -v jq >/dev/null 2>&1; then
  gsheets --json data-ops "$SPREADSHEET_ID" --action find_replace --params-json "$PARAMS_JSON" \
    | jq -r '"  occurrencesChanged=\(.occurrencesChanged // 0)  valuesChanged=\(.valuesChanged // 0)  rowsChanged=\(.rowsChanged // 0)  sheetsChanged=\(.sheetsChanged // 0)"'
else
  gsheets data-ops "$SPREADSHEET_ID" --action find_replace --params-json "$PARAMS_JSON"
fi

# 3. READ IT BACK — the cheapest verification the replace did what you meant.
echo
echo "==> AFTER: contents of $RANGE"
echo "    \$ gsheets inspect \"\$SPREADSHEET_ID\" \"$RANGE\" --no-effective --no-user-entered"
gsheets inspect "$SPREADSHEET_ID" "$RANGE" --no-effective --no-user-entered

# Terse find_replace summary looks like:
#   find_replace: occurrencesChanged=12 valuesChanged=12 formulasChanged=0 rowsChanged=9 range=Sheet1!B2:B500
#
# Scope is exactly one of range / sheet / allSheets — supplying two raises `conflicting_args`:
#   gsheets data-ops "$SPREADSHEET_ID" --action find_replace \
#     --params-json '{"find":"2025","replacement":"2026","sheet":"Sheet1"}'   # whole-tab scope
#   gsheets data-ops "$SPREADSHEET_ID" --action find_replace \
#     --params-json '{"find":"draft","replacement":"final","allSheets":true,"matchEntireCell":true}'
# find_replace is destructive (it overwrites matched cells) — read the scope first and confirm.
