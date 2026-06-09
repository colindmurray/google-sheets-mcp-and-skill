# gsheets — reading deep dive

How to *read* a Google Sheet richly and cheaply: `overview` → `inspect` →
`read-conditional-formats`, plus render modes, compact runs, and the conditional-format line
grammar. This is the differentiator — generic tooling reads values; `gsheets` reads the formulas
behind them, the format a cell *actually renders*, and the rules that color cells dynamically.

For exact flags see `commands.md` or `gsheets <cmd> --help`. Examples use `<YOUR_SPREADSHEET_ID>`.

## Table of contents

- [The reading ladder (why this order)](#the-reading-ladder-why-this-order)
- [`overview` — orient cheaply](#overview--orient-cheaply)
- [`inspect` — the rich per-cell read](#inspect--the-rich-per-cell-read)
  - [Cell shape](#cell-shape)
  - [Trimming the read](#trimming-the-read)
  - [`--compact` rectangular runs](#--compact-rectangular-runs)
- [`read-values` & render modes](#read-values--render-modes)
  - [`--render all` alignment & literal passthrough](#--render-all-alignment--literal-passthrough)
- [`read-conditional-formats` & the line grammar](#read-conditional-formats--the-line-grammar)
  - [Boolean rules](#boolean-rules)
  - [Gradient rules](#gradient-rules)
  - [Index is the only addressing source of truth](#index-is-the-only-addressing-source-of-truth)
- [Token efficiency notes](#token-efficiency-notes)

---

## The reading ladder (why this order)

1. **`overview`** — title, tabs, sizes, frozen panes, and *counts* of protected ranges and
   conditional-format rules. No grid data, so it is cheap on any size of sheet. Use it to decide
   *which* tab/range is worth a closer look.
2. **`inspect <range>`** — the rich read of that range: values + formulas + both formats + merges
   + validation, with a tight field mask (never the whole grid).
3. **`read-conditional-formats`** — the rules that color cells dynamically, which neither
   `overview` nor a plain value read reveals.

Reading **formulas and `effectiveFormat`, not just values**, is the whole point: the value alone
hides what a cell computes and how it actually renders (including conditional-format results).

## `overview` — orient cheaply

```sh
gsheets --json overview <YOUR_SPREADSHEET_ID>   # --json is GLOBAL: it goes before the subcommand
```

Returns the spreadsheet title; per tab the `sheetId`, title, index, type, row/column counts,
frozen rows/cols, tab color, and the **counts** `protectedRangeCount` / `conditionalFormatCount`;
plus spreadsheet-level `namedRanges` (name, range, id).

**Why it stays cheap:** a Google field mask cannot return an array length, so the counts are
`len()`-ed in core from the *cheapest length-yielding subfield* of each array
(`protectedRanges.protectedRangeId`, `conditionalFormats.ranges`) — never the full rule or
protected-range bodies. A tab with 100 CF rules costs ~100 short range-strings here, not 100 full
rule bodies. Full rule detail lives in `read-conditional-formats`; full protected-range detail in
`structure --action read`.

## `inspect` — the rich per-cell read

```sh
gsheets --json inspect <YOUR_SPREADSHEET_ID> 'Sheet1!A1:D20'
```

Returns `sheet`, `range`, `rows`, `cols`, a list of `cells` (row-major, padded to a full
rectangle), and `merges` (as A1 strings). Never uses `includeGridData`.

### Cell shape

Each cell is a flattened object — Google's nested format is collapsed to top-level keys:

```jsonc
{ "a1": "A2",
  "value": "1234",
  "formula": "=SUM(A:A)",            // present only when the cell has a formula
  "userEnteredFormat": { "bg": "#FFCDD2", "bold": true, "numberFormat": "0.00%", ... },
  "effectiveFormat":   { "bg": "#FFCDD2", "bold": true, ... },   // what RENDERS (incl. CF results)
  "note": "reviewed",                                            // present only when set
  "validation": "ONE_OF_LIST(Yes,No)",                          // terse, human/token-cheap
  "validationRule": { "type": "ONE_OF_LIST", "values": ["Yes","No"], "strict": true, "showDropdown": true } }
```

- **`userEnteredFormat` is intent; `effectiveFormat` is reality.** A conditional-format rule or a
  theme can make `effectiveFormat` differ from `userEnteredFormat` — `effectiveFormat` is the color
  a viewer actually sees. Read it when you need the truth.
- **Flattened, never nested-Google.** Colors are top-level hex (`bg`/`fg`); text styles are
  top-level booleans (`bold`/`italic`/…); `numberFormat` is the pattern string with
  `numberFormatType` alongside; borders render as `"<style> <hex>"` per side. Unset keys are
  omitted (token efficiency).
- **Validation round-trips.** The terse `validation` string is for humans/tokens; the structured
  `validationRule` feeds straight back into `set-validation --rule-json` unchanged — read a cell's
  validation, edit, write it back.

### Trimming the read

Drop pieces you don't need to cut tokens — the field mask shrinks accordingly:

```sh
# values + formulas only, no formatting, no validation:
gsheets inspect <YOUR_SPREADSHEET_ID> 'Sheet1!A1:D20' --no-effective --no-user-entered --no-validation
```

`--no-effective`, `--no-user-entered`, `--no-formulas`, `--no-validation` each remove their slice.

### `--compact` rectangular runs

For large or repetitive blocks, `--compact` replaces `cells` with `runs` and drops empty cells:

```sh
gsheets --json inspect <YOUR_SPREADSHEET_ID> 'Sheet1!A1:Z1000' --compact
```

A **run** is a maximal *rectangle* of cells whose `value`, `formula`, `format`, `note`, **and**
`validationRule` are all identical. This collapses both horizontal repeats and vertical blocks —
e.g. a 15-row config block in one column collapses to a single `AS986:AS1000` run. A unique cell
degenerates to a 1×1 range (`"D7:D7"`).

```jsonc
{ "a1Range": "AS986:AS1000", "value": "config", "formula": null,
  "format": { ... }, "note": "...", "validationRule": { ... } }   // note/validationRule present only when set
```

**Compact does NOT silently drop notes or validation** — two cells with differing notes or
validation never merge into one run, so a run still carries `note`/`validationRule` when present.
If you want the absolute minimum tokens and don't care about notes/validation, ignore those keys
(they're omitted when unset).

## `read-values` & render modes

```sh
gsheets read-values <YOUR_SPREADSHEET_ID> 'Sheet1!A1:D20' 'Sheet1!F1:F20' --render plain
```

Reads one or more ranges via `values.batchGet`. Rows are padded to a uniform width per range.

| `--render` | Returns |
|---|---|
| `plain` (default) | `FORMATTED_VALUE` — what the cell displays (locale/format applied). |
| `unformatted` | `UNFORMATTED_VALUE` — raw numbers/strings, no display formatting. |
| `formula` | `FORMULA` — formula text; **non-formula cells return their literal value**. |
| `all` | Formula **and** computed value side by side. |

### `--render all` alignment & literal passthrough

```sh
gsheets read-values <YOUR_SPREADSHEET_ID> 'Sheet1!A1:D20' --render all
```

`all` issues two render passes over the same ranges (`FORMULA` → `values`, `FORMATTED_VALUE` →
`computed`). The two passes can have different jagged extents, so core pads **both** arrays to a
**common rectangle** (the element-wise max of both passes' row count and per-row width). Therefore
`values[r][c]` and `computed[r][c]` are index-aligned — a formula and its result line up.

The default terse rendering shows this as `formula => computed` per cell.

**Literal passthrough:** under FORMULA render, a non-formula cell returns its literal value, not a
formula. So a `values` entry that **does not start with `=` is a literal**, not a formula. Treat it
as such (this also applies to `--render formula`'s single `values` array).

## `read-conditional-formats` & the line grammar

```sh
gsheets read-conditional-formats <YOUR_SPREADSHEET_ID> --sheet Sheet1
gsheets read-conditional-formats <YOUR_SPREADSHEET_ID>          # every sheet
```

Each rule serializes to a terse, readable, **round-trippable** line plus structured fields:

```jsonc
{ "index": 0,
  "line": "[Sheet1!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold",
  "ranges": ["Sheet1!A2:A100"], "kind": "boolean",
  "condition": { "type": "CUSTOM_FORMULA", "values": ["=$B2>10"] },
  "format": { "bg": "#FFCDD2", "bold": true } }
```

The `line` is the rule **body only**. Grammar:

```
line          := "[" rangelist "] " body
rangelist     := a1range ("," a1range)*
body          := boolean_body | gradient_body
boolean_body  := "if " condition " -> " format
gradient_body := "gradient " gradstop (" | " gradstop)*
condition     := COND_TYPE [ "(" arg ("," arg)* ")" ]   # args verbatim; formulas keep the leading "="
format        := fmt_token (" " fmt_token)*             # order: bg, fg, text-styles, number, align
fmt_token     := "bg " hex | "fg " hex | "bold" | "italic" | "underline" | "strike"
               | "num " pattern | "halign " H | "valign " V | "wrap " W
```

- `COND_TYPE` is the Google `BooleanCondition.type` verbatim (`CUSTOM_FORMULA`, `NUMBER_GREATER`,
  `NUMBER_BETWEEN`, `TEXT_CONTAINS`, `TEXT_EQ`, `BLANK`, `NOT_BLANK`, `DATE_AFTER`, `ONE_OF_LIST`,
  …). Args map to the condition's values.
- Colors are 6-digit uppercase hex (`#FFCDD2`); theme colors render as `theme:ACCENT1`.

### Boolean rules

```
[Sheet1!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold
[Sheet1!C2:C100] if NUMBER_GREATER(0) -> fg #1B5E20 bold
[Sheet1!D2:D100,Sheet1!F2:F100] if TEXT_CONTAINS(done) -> bg #C8E6C9
[Sheet1!E2:E100] if BLANK -> bg #ECEFF1 italic
```

### Gradient rules

A `GradientRule` has up to three slot-keyed stops, exactly one `=` per stop, joined by `" | "` in
canonical `min | mid | max` order:

```
[Sheet1!G2:G100] gradient min=#FFFFFF | max=#1A73E8
[Sheet1!H2:H100] gradient min=#F44336 | mid:num:50=#FFEB3B | max=#4CAF50
[Sheet1!I2:I100] gradient min=#FFFFFF | mid:pct:50=#FFEB3B | max=#1A73E8
```

- `min=<hex>` / `max=<hex>` carry the implicit interpolation type `MIN`/`MAX` and **never take a
  value** (the slot keyword *is* the type).
- Only `mid` carries an explicit value: `mid:<interp>=<hex>`, where `<interp>` is `num:<n>` (→
  `NUMBER`), `pct:<n>` (→ `PERCENT`), or `pctile:<n>` (→ `PERCENTILE`). Attaching an interp to
  `min`/`max` is invalid.

### Index is the only addressing source of truth

There is **no `priority` field** on a Sheets conditional-format rule — array order in
`conditionalFormats[]` *is* the priority, and the structured `index` (0 = highest) carries it. The
`line` deliberately omits any index/priority token, so there is exactly one source of index truth.
When you edit a read `line` and write it back via `set-conditional-format --action update --index
N`, the target is the `--index` kwarg alone; the parsed line never supplies one. See `writing.md`.

## Token efficiency notes

- **Start with `overview`** before any grid read — it tells you which tab/range is worth a closer
  look without pulling cells.
- **Use `--compact`** on large or repetitive ranges; it collapses vertical/horizontal runs and
  drops empties.
- **Trim `inspect`** with the `--no-*` flags when you only need part of the picture.
- **`--render unformatted`** is cheaper than re-deriving numbers from formatted strings when you
  need raw values for computation.
- Unset format keys are always omitted, and reads never use `includeGridData` — the field mask is
  always as tight as the request allows.
