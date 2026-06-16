# gsheets — basic tier

The everyday read → write → verify loop, covering roughly 80% of what you do with a sheet:
orient, read cells (values + formulas + formatting), read conditional-format rules, write values,
append rows, clear, format a range, and manage tabs. Reach here first for any ordinary task; drop
to `intermediate.md` only for the less-common operations (validation, data-ops, dimensions,
structure, comments, export) and `advanced.md` for fringe features (rich-text/pivot reads, charts,
the raw `batch`). `gsheets <cmd> --help`
is the authoritative, always-current flag source for any single command.

## Core concepts

- **A1 ranges everywhere.** `'Sheet1!A1:D20'`, `'Sheet1!A:A'` (whole column), `'Sheet1'` (whole
  tab). Quote them — `!`, `:`, and spaces are shell-significant. You never fetch a `sheetId` first;
  the sheet name → id resolution happens internally.
- **`--format`, `--json`, and `--scopes` are global — they go BEFORE the subcommand.**
  ```sh
  gsheets --json overview <YOUR_SPREADSHEET_ID>     # correct
  gsheets overview <YOUR_SPREADSHEET_ID> --json     # WRONG -> "unrecognized arguments: --json" (exit 2)
  ```
  `--format {text,json,jsonl,csv,tsv,markdown}` (default `text`) selects the output serialization;
  `--json` is a permanent alias for `--format json`. `--scopes {default,broad}` sets the auth scope.
  Supported formats by result shape:

  | Result shape | Commands | Formats |
  |---|---|---|
  | Rectangular values | `read-values` | text, json, jsonl, csv, tsv, markdown |
  | Structured/rich | `inspect`, `describe`, `read-conditional-formats`, `overview`, `structure`, `read-many` | text, json, jsonl, markdown |
  | Small confirmations | every writer, `auth` | text, json |

  `csv`/`tsv` need a rectangular value read — asking for them on a structured result is a clean
  `format_unsupported` error, not a silent fallback. A single range pipes as plain CSV; multiple
  ranges emit one `# range: <A1>` block each. `jsonl` emits one `{range,row}` per row (or one list
  element per line for list results). `markdown` renders a GitHub table for a value read (embedded
  `|`/newlines escaped so a cell never corrupts the table) and `field: value` key/value lines for a
  structured read — it never errors on a structured shape, but it is verbose, so for bulk values
  prefer csv to a file: `gsheets --format csv read-values <ID> <RANGE> > out.csv`.
- **MCP file-output (`out_path`).** The CLI pipes (`> out.csv`); the MCP tool's output goes into the
  agent's context, so for a big read pass `out_path` to `sheets_read_values` / `sheets_inspect` /
  `sheets_describe` / `sheets_read_many`. The tool writes `render(result, output_format)` to that local file (utf-8;
  `text` resolves to `json`) and returns a small handle instead of the payload:
  `{ok, path, format, rows, cols, bytes, preview}` (`preview` = the first ~5 rows/records). The
  parent directory must already exist (it is never created), and credential/config paths are
  refused (`bad_out_path`). These tools modify no spreadsheet, so they stay read-only. The CLI has
  no `out_path` — it pipes stdout instead.
- **The positional `spreadsheet_id` is always the first argument** of every Sheets subcommand.
- **`USER_ENTERED` is the write default.** Input is parsed like a user typing: `=SUM(B:B)` becomes
  a live formula, `5`/`$10`/`50%`/`2026-06-09` coerce to typed values. `--input raw` stores the
  literal text verbatim (no formula parsing) — only use it when you truly want an inert string.
- **`effectiveFormat` vs `userEnteredFormat`.** `userEnteredFormat` is the format you set (intent);
  `effectiveFormat` is what actually renders, including conditional-format and theme results. When
  you need the color a viewer actually sees, read `effectiveFormat`.
- **The rhythm is read → write → verify.** Read the target first so you craft the smallest write;
  write with a typed command (safe defaults); read it back to confirm. Everything writable is
  readable back, so a follow-up read is the cheapest verification.
- **Errors** print to stderr as `gsheets: error: <code>: <message>` (with a `hint:` line when
  present), or `{"ok": false, "error": {...}}` under `--json`; exit code is `1`. Malformed JSON in
  a `--*-json` flag fails as an argparse error (exit `2`) before any API call.

## Reading

The ladder is `overview` (orient cheaply, no grid data) → `describe` (one-call merged region view)
→ `inspect` (rich per-cell read of one range) → `read-conditional-formats` (the dynamic-coloring
rules a value read never shows). Reading formulas and `effectiveFormat`, not just values, is the
point.

### overview

Cheap orientation snapshot — no grid data. Start here on any unfamiliar sheet.

```sh
gsheets --json overview <YOUR_SPREADSHEET_ID>
```

No flags beyond the positional id. Returns the spreadsheet title; per tab the `sheetId`, title,
index, type, row/column counts, frozen rows/cols, tab color, and the **counts**
`protectedRangeCount` / `conditionalFormatCount`; plus spreadsheet-level `namedRanges` and the
spreadsheet `locale` / `timeZone` (e.g. `"en_US"` / `"America/New_York"`, omitted when unset).
Counts are computed cheaply — overview never pulls full rule bodies, so it stays cheap on any size
of sheet. Use it to decide which tab/range is worth a closer look.

### describe

The "understand a region" verb: ONE `spreadsheets.get` returns, **per requested range**, a merged
view — the cells, the sheet's merges, the conditional-format rules that **intersect** that range,
its native tables, banding, and protected ranges, plus a validation summary. It collapses what used
to be `inspect` + `structure` + `read-conditional-formats` into a single call, so it is the default
first move when you want to know what a region IS (not just its raw values).

```sh
gsheets --json describe <YOUR_SPREADSHEET_ID> 'Sheet1!A1:F50'
gsheets describe <YOUR_SPREADSHEET_ID> 'Sheet1!A1:F50' 'Plan!A1:B20'   # multi-range, multi-sheet
```

| Flag | Effect |
|---|---|
| `RANGE...` (positional, ≥1) | One or more A1 ranges; multi-range AND multi-sheet in one call. |
| `--max-cells N` | Fail with `result_too_large` if the regions span more than `N` cells (default: unlimited). describe pulls full per-cell grid data, so narrow the range for a big region. |

Returns `{regions: [...]}`, one entry per requested range in request order:

```jsonc
{ "range": "Sheet1!A1:F50", "sheet": "Sheet1",
  "cells": [ /* same flattened Cell shape inspect emits — value, formula, both formats, validation */ ],
  "merges": ["Sheet1!B1:C1"],
  "conditionalFormats": [ /* the read-conditional-formats line grammar, ONLY rules whose ranges
                             intersect this region, each keeping its priority `index` */ ],
  "tables": [ ... ], "bandedRanges": [ ... ], "protectedRanges": [ ... ],
  "validationSummary": { "cells": 3, "rules": ["ONE_OF_LIST(Yes,No)"] } }
```

The conditional-format rules are scoped to the region automatically (range-scoped CF) — and each
keeps the positional `index` you pass to `set-conditional-format` to edit it. Use `inspect` instead
when you want one range's rich-text / pivot / compact-runs facets; use `read-conditional-formats`
when you want every rule on a whole tab regardless of range.

### inspect

The flagship rich read: per-cell values + formulas + userEntered & effective formats + merges +
validation, with a tight field mask (never the whole grid).

```sh
gsheets --json inspect <YOUR_SPREADSHEET_ID> 'Sheet1!A1:D20'
```

| Flag | Effect |
|---|---|
| `RANGE` (positional) | A1 range, e.g. `'Sheet1!A1:D20'` or `'Sheet1'`. |
| `--compact` | Collapse identical adjacent cells into rectangular `a1Range` runs; drop empty cells. Big token win on repetitive blocks. |
| `--no-effective` | Omit `effectiveFormat` (what renders, incl. conditional-format results). |
| `--no-user-entered` | Omit `userEnteredFormat` (the format you set, i.e. intent). |
| `--no-formulas` | Omit formulas (values only). |
| `--no-validation` | Omit data validation (both the terse string and the structured `validationRule`). |

Returns `sheet`, `range`, `rows`, `cols`, a row-major `cells` list (padded to a full rectangle),
and `merges` (as A1 strings). Each cell is flattened — Google's nested format collapsed to
top-level keys:

```jsonc
{ "a1": "A2",
  "value": "1234",
  "formula": "=SUM(A:A)",            // present only when the cell has a formula
  "userEnteredFormat": { "bg": "#FFCDD2", "bold": true, "numberFormat": "0.00%" },
  "effectiveFormat":   { "bg": "#FFCDD2", "bold": true },   // what RENDERS (incl. CF results)
  "note": "reviewed",                                       // present only when set
  "validation": "ONE_OF_LIST(Yes,No)" }                     // terse; structured under validationRule
```

Colors are top-level hex (`bg`/`fg`); text styles are top-level booleans (`bold`/`italic`/…);
unset keys are omitted. Trim tokens with the `--no-*` flags when you only need part of the picture
— the field mask shrinks accordingly:

```sh
# values + formulas only, no formatting, no validation:
gsheets inspect <YOUR_SPREADSHEET_ID> 'Sheet1!A1:D20' --no-effective --no-user-entered --no-validation
```

(`--compact` collapses both horizontal and vertical runs of identical cells; `--rich-text` and
`--pivot` exist but are advanced — see `advanced.md`.)

### read-values

Just values for one or more ranges, with a render mode. Uses `values.batchGet`; rows are padded to
a uniform width per range.

```sh
gsheets read-values <YOUR_SPREADSHEET_ID> 'Sheet1!A1:D20' 'Sheet1!F1:F20' --render plain
```

| `--render` | Returns |
|---|---|
| `plain` (default) | `FORMATTED_VALUE` — what the cell displays (locale/format applied). |
| `unformatted` | `UNFORMATTED_VALUE` — raw numbers/strings, no display formatting (cheaper for computation). |
| `formula` | `FORMULA` — formula text; **non-formula cells return their literal value**. |
| `all` | Formula **and** computed value side by side (`values` + `computed`, index-aligned to a common rectangle; terse render shows `formula => computed`). |

A `values` entry that does **not** start with `=` is a **literal**, not a formula (FORMULA render
passes literals through). `--render formula` renders **address-keyed** in text (`C5: =SUM(...)`, one
line per non-empty cell) — the natural shape for sparse formula data. To understand the formula
*logic* across a wide grid without pulling thousands of near-identical formulas, reach for
`formula-patterns` (one template per column; see `intermediate.md`).

Two optional knobs for large reads:

| Flag | Effect |
|---|---|
| `--diff-only` | `--render all` only: null out each `computed` cell that equals `values`, and drop `computed` entirely for a fully-static range. A `null` hole means "computed == values here"; the matrix stays index-aligned. Roughly halves a staticized formula-sheet read. |
| `--max-cells N` | Fail with a structured `result_too_large` error if the read spans more than `N` cells, instead of returning a payload that only fails at the caller's token cap. Counts the padded **rectangle** (rows × cols, blanks included), so size it to the range area. Default: unlimited. |

For a **pure value dump**, prefer `export --format csv` (writes a local file, no token cap) over a
wide `read-values`; CSV can't carry formulas, so pair it with a narrow-band `--render formula` read
over just the formula columns. Reads draw on a small per-user read-RPM quota shared by all callers —
favor a few wide multi-range reads and `export` over many small calls.

### read-conditional-formats

Per-sheet conditional-format rules serialized to terse, readable, round-trippable lines, each with
its positional `index` (0 = highest priority). A value read never reveals these.

```sh
gsheets read-conditional-formats <YOUR_SPREADSHEET_ID> --sheet Sheet1
gsheets read-conditional-formats <YOUR_SPREADSHEET_ID>          # every sheet
```

`--sheet NAME` restricts to one tab; omit for every sheet. Each rule returns structured fields plus
a `line` that is the rule **body only** (no index token — the index lives in the structured `index`
field):

```jsonc
{ "index": 0,
  "line": "[Sheet1!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold",
  "ranges": ["Sheet1!A2:A100"], "kind": "boolean",
  "condition": { "type": "CUSTOM_FORMULA", "values": ["=$B2>10"] },
  "format": { "bg": "#FFCDD2", "bold": true } }
```

CF line grammar (for reading rules):

```
line          := "[" rangelist "] " body
body          := boolean_body | gradient_body
boolean_body  := "if " condition " -> " format
gradient_body := "gradient " gradstop (" | " gradstop)*
condition     := COND_TYPE [ "(" arg ("," arg)* ")" ]    # args verbatim; formulas keep leading "="
format        := fmt_token (" " fmt_token)*              # order: bg, fg, text-styles, num, align
fmt_token     := "bg " hex | "fg " hex | "bold" | "italic" | "underline" | "strike"
               | "num " pattern | "halign " H | "valign " V | "wrap " W
```

- `COND_TYPE` is the Google `BooleanCondition.type` verbatim (`CUSTOM_FORMULA`, `NUMBER_GREATER`,
  `NUMBER_BETWEEN`, `TEXT_CONTAINS`, `BLANK`, `NOT_BLANK`, …); colors are 6-digit uppercase hex
  (`#FFCDD2`), theme colors render as `theme:ACCENT1`.
- Examples:
  ```
  [Sheet1!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold
  [Sheet1!C2:C100] if NUMBER_GREATER(0) -> fg #1B5E20 bold
  [Sheet1!E2:E100] if BLANK -> bg #ECEFF1 italic
  [Sheet1!G2:G100] gradient min=#FFFFFF | max=#1A73E8
  ```

The `index` is the only write-addressing source of truth — there is no `priority` field; array
order *is* priority. (Writing/editing these rules is `set-conditional-format`, in `intermediate.md`.)

## Writing

Writes default to `USER_ENTERED` (formulas go live, `5`/`$10`/`50%` coerce). Field masks are
auto-built from your payload, so partial writes never wipe other subfields. After a write, read it
back to verify.

### write-values

Write/update one or more ranges. Two mutually-exclusive forms.

```sh
# single-range form (RANGE + --values-json):
gsheets write-values <YOUR_SPREADSHEET_ID> 'Sheet1!A1' --values-json '[["=SUM(B:B)"]]'

# multi-range form (--data-json, no positional RANGE):
gsheets write-values <YOUR_SPREADSHEET_ID> \
  --data-json '[{"range":"Sheet1!A1","values":[["Total"]]},{"range":"Sheet1!B1","values":[["=SUM(B2:B100)"]]}]'
```

| Flag | Effect |
|---|---|
| `RANGE` (positional, optional) | Single-range target; pair with `--values-json`. |
| `--values-json '[[...]]'` | 2D array of rows (single-range form). |
| `--data-json '[{"range":..,"values":..}]'` | Multiple ranges in one call (multi-range form). |
| `--input user_entered` (default) | Parse like a user typing: `=…` becomes a formula, values coerce. |
| `--input raw` | Store the literal text/number verbatim — no formula parsing or coercion. |

Pass **either** `RANGE` + `--values-json` **or** `--data-json`, never both (→ `conflicting_args`);
the single-range form needs both halves (→ `missing_args`). `write-values` *overwrites* the target
— read the range first if it may hold data.

### append-rows

Append rows after a table's last populated row (`INSERT_ROWS` — never overwrites what's below). The
safe way to add data.

```sh
gsheets append-rows <YOUR_SPREADSHEET_ID> 'Sheet1!A1' --values-json '[["2026-06-09", 5, 12]]'
```

| Flag | Effect |
|---|---|
| `RANGE` (positional) | A1 range identifying the table (e.g. `'Sheet1!A1'` or `'Sheet1!A1:D1'`). |
| `--values-json` (**Required**) | 2D array of rows to append. |
| `--input` | `user_entered` (default) or `raw`, as for `write-values`. |

Inserts below the last populated row of the table at `RANGE`; returns the `updatedRange` and the
detected `tableRange`.

### clear

Clear values, and optionally formats / validation / notes.

```sh
gsheets clear <YOUR_SPREADSHEET_ID> 'Sheet1!A2:D100'                          # values only
gsheets clear <YOUR_SPREADSHEET_ID> 'Sheet1!A2:D100' --formats --notes        # values + formats + notes
gsheets clear <YOUR_SPREADSHEET_ID> 'Sheet1!A2:D100' --no-values --validation # only validation
```

| Flag | Effect |
|---|---|
| `RANGE...` (positional, 1+) | One or more A1 ranges to clear. |
| `--no-values` | Do **not** clear values (clear only the structural flags below). |
| `--formats` | Also clear cell formatting. |
| `--validation` | Also clear data validation. |
| `--notes` | Also clear cell notes. |

Values-only is a fast `values.batchClear`; adding any of formats/validation/notes routes through a
`batchUpdate` whose fields mask covers exactly the requested subfields. **Destructive** — confirm
and read the range first.

### format

Apply formatting to a range — background, font/bold/italic/underline/strike/size/family, number or
date pattern, alignment, wrap, a cell note, and borders — as one atomic `batchUpdate`. The fields
mask is auto-built from exactly the keys you pass, so only those subfields are written and the rest
are preserved.

```sh
gsheets format <YOUR_SPREADSHEET_ID> 'Sheet1!A1:A10' --bg '#FFCDD2' --bold --number '0.00%'

# header row with fill + bottom border in one atomic call:
gsheets format <YOUR_SPREADSHEET_ID> 'Sheet1!A1:D1' \
  --bg '#263238' --fg '#FFFFFF' --bold --border bottom=SOLID:#000000
```

| Flag | Maps to (flat CellFormat key) |
|---|---|
| `--bg HEX\|theme:NAME` / `--fg HEX\|theme:NAME` | `bg` / `fg`. |
| `--bold` / `--no-bold` | `bold` (tri-state: unset leaves it untouched). |
| `--italic` / `--no-italic`, `--underline` / `--no-underline`, `--strike` / `--no-strike` | the matching text style. |
| `--font-size N` / `--font-family NAME` | `fontSize` / `fontFamily`. |
| `--number PATTERN` | `numberFormat` (e.g. `'0.00%'`, `'$#,##0.00'`, `'yyyy-mm-dd'`). |
| `--halign {LEFT,CENTER,RIGHT}` / `--valign {TOP,MIDDLE,BOTTOM}` | `halign` / `valign`. |
| `--wrap {OVERFLOW_CELL,CLIP,WRAP}` | `wrap`. |
| `--note TEXT` | `note` (cell-level note; writable here, readable via `inspect`). |
| `--border SIDE=STYLE:#hex` (repeatable) | a `borders` side, e.g. `--border top=SOLID:#000000`. SIDE ∈ top/bottom/left/right. |
| `--fmt-json '{...}'` | A raw flat CellFormat dict; **overrides** all the individual flags. |

Colors are `#RRGGBB` hex or `theme:NAME` (e.g. `theme:ACCENT1`). Only the keys you pass are written
— a partial update (just the background, say) leaves bold, borders, number format, etc. untouched.
The result echoes the mask it applied (`appliedFields: …`). An empty payload is refused
(`empty_payload`). Cell format and borders set in the same call are issued as one `batchUpdate`, so
the operation is all-or-nothing.

## Operations

### manage-sheets

Add / delete / duplicate / rename / reorder tabs. Returns new ids.

```sh
gsheets manage-sheets <YOUR_SPREADSHEET_ID> --action add --params-json '{"title":"Q3","rows":200,"cols":12}'
gsheets manage-sheets <YOUR_SPREADSHEET_ID> --action rename --sheet Sheet1 --params-json '{"newName":"Data"}'
gsheets manage-sheets <YOUR_SPREADSHEET_ID> --action duplicate --sheet Data --params-json '{"newName":"Data copy"}'
gsheets manage-sheets <YOUR_SPREADSHEET_ID> --action reorder --sheet Data --params-json '{"newIndex":0}'
gsheets manage-sheets <YOUR_SPREADSHEET_ID> --action delete --sheet "Data copy"
```

`--action` is **Required**. `--sheet` names the target tab for delete/duplicate/rename/reorder. An
unknown `params` key raises `unknown_param`.

| `--action` | `--sheet` | `--params-json` keys |
|---|---|---|
| `add` | — | `{"title": str, "index": int, "rows": int, "cols": int}` (all optional) |
| `delete` | target tab | — (**Destructive** — confirm first) |
| `duplicate` | source tab | `{"newName": str, "newIndex": int}` (both optional) |
| `rename` | target tab | `{"newName": str}` (**Required**, else `missing_param`) |
| `reorder` | target tab | `{"newIndex": int}` (**Required**, else `missing_param`) |

Returns `{"action", "sheet": {"sheetId", "title", "index"}}` — `add`/`duplicate` give you the new
`sheetId` immediately for a create-then-populate flow. (Row/column operations are a different
command, `dimensions`, in `intermediate.md`.)

## See also

- `intermediate.md` (~15%) — `set-validation`, `set-conditional-format` (writing CF rules),
  `data-ops`, `dimensions`, `structure` base edits + `--action read`, `comments`, `export`,
  `read-many`.
- `advanced.md` (~5%) — `inspect --rich-text` / `--pivot`, structure's tables/banding/filter-views/
  slicers CRUD, `metadata`, `charts`, the `batch` escape hatch.
- `gsheets <cmd> --help` — the authoritative, always-current flag source for any single command.
