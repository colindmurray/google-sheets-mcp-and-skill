# gsheets — writing deep dive

How to *change* a Google Sheet safely: USER_ENTERED by default, an auto-built field mask that
never wipes what you didn't touch, index-safe conditional-format edits, validation round-trips, and
the CRUD-symmetry rule of reading every write back. Generic tooling makes you hand-build masks and
guess at formula coercion; `gsheets` does the safe thing by default.

For exact flags see `commands.md` or `gsheets <cmd> --help`. Examples use `<YOUR_SPREADSHEET_ID>`.

## Table of contents

- [The writing loop (read → write → read back)](#the-writing-loop-read--write--read-back)
- [USER_ENTERED vs raw](#user_entered-vs-raw)
- [`write-values` & `append-rows`](#write-values--append-rows)
- [`format` & the auto field-mask](#format--the-auto-field-mask)
  - [The atomic-leaf set](#the-atomic-leaf-set)
  - [Borders + format atomicity](#borders--format-atomicity)
- [Conditional-format writes (index-safe)](#conditional-format-writes-index-safe)
  - [Single mutations](#single-mutations)
  - [Batch mutations (high → low)](#batch-mutations-high--low)
- [`set-validation` round-trip](#set-validation-round-trip)
- [`clear` (what gets wiped)](#clear-what-gets-wiped)
- [Structure, tabs, metadata](#structure-tabs-metadata)
- [CRUD symmetry — read it back](#crud-symmetry--read-it-back)

---

## The writing loop (read → write → read back)

1. **Read the target first** (`inspect` / `read-values` / `read-conditional-formats`) so you know
   what you're changing and can craft the smallest write.
2. **Write** with the typed command — defaults are safe (USER_ENTERED, auto mask).
3. **Read it back** to verify. Everything writable is readable back, so a follow-up read is the
   cheapest confirmation the write did what you meant.

## USER_ENTERED vs raw

Writes default to **`--input user_entered`**, which parses input like a user typing into the grid:

- `'=SUM(B:B)'` becomes a **live formula**, not the literal text `=SUM(B:B)`.
- `'5'` / `'$10'` / `'50%'` / `'2026-06-09'` coerce to number / currency / percent / date.

Pass **`--input raw`** only when you truly want the literal text/number stored verbatim — no
formula parsing, no type coercion. This is the classic footgun the default protects you from:
writing a formula as raw text leaves an inert string in the cell.

## `write-values` & `append-rows`

```sh
# Single live formula (USER_ENTERED):
gsheets write-values <YOUR_SPREADSHEET_ID> 'Sheet1!A1' --values-json '[["=SUM(B:B)"]]'

# Several ranges in one call (multi-range form):
gsheets write-values <YOUR_SPREADSHEET_ID> \
  --data-json '[{"range":"Sheet1!A1","values":[["Total"]]},{"range":"Sheet1!B1","values":[["=SUM(B2:B100)"]]}]'

# Append after the table's last row — INSERT_ROWS, never overwrites what's below:
gsheets append-rows <YOUR_SPREADSHEET_ID> 'Sheet1!A1' --values-json '[["2026-06-09", 5, 12]]'
```

- `write-values` *overwrites* the target cells. Read the range first if it may hold data.
- `append-rows` *inserts* below the last populated row of the table at `RANGE`, so it is the safe
  way to add data without clobbering. It returns the `updatedRange` and the detected `tableRange`.

## `format` & the auto field-mask

```sh
gsheets format <YOUR_SPREADSHEET_ID> 'Sheet1!A1:A10' --bg '#FFCDD2' --bold --number '0.00%'
```

The **fields mask is auto-built from exactly the keys you pass.** `format` writes *only* the
subfields you specify and **never wipes the rest** — so a partial format update (just the
background, say) leaves bold, borders, number format, etc. untouched. The result echoes the mask it
applied, e.g.:

```
appliedFields: userEnteredFormat(backgroundColorStyle,textFormat.bold,numberFormat),note
```

Colors are `#RRGGBB` hex or `theme:NAME`. A cell `note` is writable here (`--note`) and readable via
`inspect`. Pass `--fmt-json '{...}'` to supply a raw flat CellFormat dict (it overrides the
individual flags).

### The atomic-leaf set

The mask builder treats certain Google sub-dicts as **atomic leaves** — masked at the parent, never
recursed into children (Google treats them atomically; a child-level mask errors or partially
wipes):

- **any `*ColorStyle`** (`backgroundColorStyle`, `foregroundColorStyle`, border colors) → the
  parent key, never `...colorStyle.rgbColor`;
- **`numberFormat`** (`{type,pattern}`) → `numberFormat`, never `numberFormat(type,pattern)`;
- **`padding`** (`{top,right,bottom,left}`) → `padding`, never `padding(top,left)`;
- **`textRotation`** → `textRotation`.

`textFormat` is **not** atomic — its children mask individually, which is why `--bold` yields
`userEnteredFormat.textFormat.bold`. You don't manage any of this by hand; it's why a partial
format write is always safe. An empty payload is refused (`empty_payload`) — no no-op writes.

### Borders + format atomicity

Borders apply via a separate `updateBorders` request. When you set both cell format/note **and**
borders in one `format` call, `gsheets` issues them as **one `batchUpdate` containing both
requests**, so the operation is all-or-nothing — you never land the fill but not the borders.

```sh
gsheets format <YOUR_SPREADSHEET_ID> 'Sheet1!A1:D1' \
  --bg '#263238' --fg '#FFFFFF' --bold --border bottom=SOLID:#000000
```

`--border SIDE=STYLE:#hex` is repeatable; SIDE ∈ top/bottom/left/right.

## Conditional-format writes (index-safe)

Conditional-format rules are **positional**: index 0 is top priority, and there is **no stable rule
id** — array order in `conditionalFormats[]` *is* the priority. All write addressing comes from the
`--index` kwarg; the rule line itself carries no index (see the line grammar in `reading.md`).

### Single mutations

```sh
# Add a rule at the top (index 0 = highest priority) from a readable body line:
gsheets set-conditional-format <YOUR_SPREADSHEET_ID> --action add --sheet Sheet1 --index 0 \
  --rule '[Sheet1!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold'

# Update the rule at index 2 (edit a line you read back, write it to the same index):
gsheets set-conditional-format <YOUR_SPREADSHEET_ID> --action update --index 2 \
  --rule '[Sheet1!C2:C100] if NUMBER_GREATER(0) -> fg #1B5E20 bold'

# Delete the rule at index 5:
gsheets set-conditional-format <YOUR_SPREADSHEET_ID> --action delete --index 5
```

Use `--rule-json '{ranges,kind,condition,format}'` instead of `--rule` when you prefer structured
input (pass one or the other, not both). A read line is meant to be edited and written straight
back, with the target `--index` supplied separately.

**A single call mutates exactly one rule, so its ordering is moot.** But **separate single calls
are NOT auto-ordered** — if you issue several, mutate **high index → low** yourself (or re-read
indices between calls), because deleting/inserting a low index shifts the position of every rule
above it.

### Batch mutations (high → low)

To change several rules safely in one shot, use `--rules-json`. Core sorts the
delete/update/add requests **descending by index** and emits them in one `batchUpdate`, so earlier
mutations never shift the array position of later targets:

```sh
gsheets set-conditional-format <YOUR_SPREADSHEET_ID> --rules-json '[
  {"action":"delete","index":5},
  {"action":"update","index":2,"rule":"[Sheet1!C2:C100] if NUMBER_GREATER(0) -> fg #1B5E20"},
  {"action":"add","index":0,"rule":"[Sheet1!A2:A100] if BLANK -> bg #ECEFF1"}
]'
```

`rule` is omitted for `delete`. Don't combine `--rules-json` with the single-form
`--action`/`--index`/`--rule` flags (→ `conflicting_args`). The high→low guarantee applies **only**
inside one `--rules-json` batch.

## `set-validation` round-trip

```sh
# Set a dropdown:
gsheets set-validation <YOUR_SPREADSHEET_ID> 'Sheet1!C2:C100' \
  --rule-json '{"type":"ONE_OF_LIST","values":["Yes","No"]}'

# Clear validation (omit --rule-json):
gsheets set-validation <YOUR_SPREADSHEET_ID> 'Sheet1!C2:C100'
```

The structured `ValidationRule` you pass to `--rule-json` is the **same shape `inspect` reads back**
under each cell's `validationRule` key — read a cell's validation, edit the dict, write it straight
back. Variants:

| Type | Shape |
|---|---|
| List of literals | `{"type":"ONE_OF_LIST","values":["Yes","No"]}` |
| List from a range | `{"type":"ONE_OF_RANGE","source":"Sheet1!Z1:Z10"}` |
| Checkbox | `{"type":"BOOLEAN"}` |
| Number range | `{"type":"NUMBER_BETWEEN","values":[0,100]}` |
| Custom formula | `{"type":"CUSTOM_FORMULA","values":["=ISNUMBER(A1)"]}` |
| Non-blank / blank | `{"type":"NOT_BLANK"}` / `{"type":"BLANK"}` |

`--no-strict` allows (but flags) invalid input; `--no-dropdown` hides the in-cell chip. These may
also be carried inside the rule JSON (`"strict"`/`"showDropdown"`); the CLI flags win on conflict.
The terse `validation` string in `inspect` is for humans; `validationRule` is the machine
round-trip path.

## `clear` (what gets wiped)

```sh
gsheets clear <YOUR_SPREADSHEET_ID> 'Sheet1!A2:D100'                       # values only
gsheets clear <YOUR_SPREADSHEET_ID> 'Sheet1!A2:D100' --formats --notes     # values + formats + notes
gsheets clear <YOUR_SPREADSHEET_ID> 'Sheet1!A2:D100' --no-values --validation  # only validation
```

Values-only is a fast `values.batchClear`. Adding `--formats`/`--validation`/`--notes` routes
through a `batchUpdate` whose fields mask covers **exactly** the requested subfields over the
range — so clearing formats won't touch values unless you ask. `--no-values` clears the structural
flags without touching values. **Destructive** — confirm and read the range first.

## Structure, tabs, metadata

- **`structure`** writes go through the same auto field-mask where applicable, and `--sheet` is
  **required** to mutate (omit only for `read`). `unmerge`, `delete_named`, `unprotect`, and
  `ungroup` are destructive. New `namedRangeId`/`protectedRangeId`s come back in the result so a
  create-then-populate flow has the id immediately.
- **`manage-sheets`** returns the new `sheetId`/title/index for `add`/`duplicate`. `delete` is
  destructive — confirm first.
- **`metadata`** writes durable key/value anchors (row/column range, whole sheet, or spreadsheet)
  that survive row/column inserts — prefer them over hard-coded A1 when you need a stable reference.

See the per-action `params` key tables in `commands.md`; an unknown key raises `unknown_param`.

## CRUD symmetry — read it back

**Everything writable is readable back** — that is the strongest verification you have:

```sh
# After a CF write, re-read the rules to confirm index/body:
gsheets read-conditional-formats <YOUR_SPREADSHEET_ID> --sheet Sheet1

# After a format write, inspect the range to confirm effectiveFormat (--json is GLOBAL, before the cmd):
gsheets --json inspect <YOUR_SPREADSHEET_ID> 'Sheet1!A1:A10'

# After a validation write, inspect a cell and check validationRule round-trips:
gsheets --json inspect <YOUR_SPREADSHEET_ID> 'Sheet1!C2'
```

The one v1 exception is **charts**: `charts --action read` returns chart *metadata only*
(`chartId`, `title`, `type`, `anchor`), not the full spec — so a chart's spec can't be read back to
recreate it via the typed tool. For full chart-spec fidelity, use `batch` (raw `addChart` /
`updateChartSpec`).
