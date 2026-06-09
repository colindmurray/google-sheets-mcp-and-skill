# Examples — `gsheets` recipes

Small, copy-pasteable recipes that show off the read-side richness and the safe-write
defaults of the `gsheets` CLI. Each is a runnable shell script that reads the **spreadsheet id from
the environment** (never hard-coded) and uses placeholder ids in its comments.

| Recipe | What it shows |
|---|---|
| [`audit_conditional_formatting.sh`](audit_conditional_formatting.sh) | Audit every conditional-format rule in a sheet — the rules that color cells dynamically, which generic tooling can't read. |
| [`audit_tables_and_filters.sh`](audit_tables_and_filters.sh) | Audit a sheet's native **tables**, **filter views**, **banding**, and **slicers** — structural objects a value read never sees — each as a terse round-trippable line with its id. |
| [`read_column_formulas.sh`](read_column_formulas.sh) | Read the **formulas** behind a column (not just the computed values), with formula + result side by side. |
| [`safe_value_write.sh`](safe_value_write.sh) | A safe value write: read the target first, write with `USER_ENTERED`, then read it back to verify (full CRUD symmetry). |
| [`bulk_find_replace.sh`](bulk_find_replace.sh) | A safe bulk (regex) find/replace via `data-ops`: read the scope first, replace in one batch, then read back and check the `occurrencesChanged` count. |

## Prerequisites

1. Install the package (it provides the `gsheets` console script):

   ```sh
   uv sync
   # or: pip install -e .
   ```

2. Bootstrap credentials once (OAuth desktop consent, or point at a service account / ADC — see the
   repo README and `skill/references/commands.md`):

   ```sh
   gsheets auth login
   gsheets auth status      # verify the resolved mode/scopes/token
   ```

3. Export the spreadsheet id you want to operate on. **Never commit a real id** — it comes from your
   environment at runtime:

   ```sh
   export GSHEETS_EXAMPLE_SPREADSHEET_ID='<YOUR_SPREADSHEET_ID>'   # the token between /d/ and /edit in the URL
   ```

## Running

```sh
chmod +x examples/*.sh          # once
./examples/audit_conditional_formatting.sh
./examples/audit_tables_and_filters.sh                       # all tabs (or pass a tab name)
./examples/read_column_formulas.sh 'Sheet1!C1:C200'
./examples/safe_value_write.sh 'Sheet1!E1' '=SUM(C2:C200)'
./examples/bulk_find_replace.sh 'Sheet1!B2:B500' '\bN/?A\b' ''   # regex blank-out of N/A, NA
```

Each script prints the exact `gsheets` commands it runs, so you can copy individual lines.

## Conventions used here

- **`--json` and `--scopes` are GLOBAL flags** — they go **before** the subcommand
  (`gsheets --json overview <ID>`), never after it (`gsheets overview <ID> --json` is an argparse
  error). The recipes use `--json` + `jq` where machine output helps, and plain terse output
  otherwise.
- **Quote A1 ranges** — `!`, `:`, and spaces are shell-significant: `'Sheet1!A1:D20'`.
- **The spreadsheet id is read from `$GSHEETS_EXAMPLE_SPREADSHEET_ID`**, so nothing real is ever
  written into the committed tree.
</content>
