# gsheets — reading deep dive

How to read a Google Sheet richly and cheaply: `overview` → `inspect` →
`read-conditional-formats`, plus render modes, compact runs, the conditional-format line grammar,
structural reads, comments, and the cross-file `read-many` fan-out. Generic tooling reads values;
`gsheets` reads the formulas behind them, the format a cell *actually renders*, and the rules that
color cells dynamically.

For exact flags see `commands.md` or `gsheets <cmd> --help`. Examples use `<YOUR_SPREADSHEET_ID>`.

## Table of contents

- [The reading ladder (why this order)](#the-reading-ladder-why-this-order)
- [`overview` — orient cheaply](#overview--orient-cheaply)
- [`inspect` — the rich per-cell read](#inspect--the-rich-per-cell-read)
  - [Cell shape](#cell-shape)
  - [Trimming the read](#trimming-the-read)
  - [`--rich-text` — per-run rich text & in-cell links](#--rich-text--per-run-rich-text--in-cell-links)
  - [`--pivot` — pivot-table definitions](#--pivot--pivot-table-definitions)
  - [`--compact` rectangular runs](#--compact-rectangular-runs)
- [`read-values` & render modes](#read-values--render-modes)
  - [`--render all` alignment & literal passthrough](#--render-all-alignment--literal-passthrough)
- [`read-conditional-formats` & the line grammar](#read-conditional-formats--the-line-grammar)
  - [Boolean rules](#boolean-rules)
  - [Gradient rules](#gradient-rules)
  - [Index is the only addressing source of truth](#index-is-the-only-addressing-source-of-truth)
- [`structure --action read` — the structural picture](#structure--action-read--the-structural-picture)
- [`comments --action read` — Drive threaded comments](#comments--action-read--drive-threaded-comments)
- [`read-many` — cross-file fan-out](#read-many--cross-file-fan-out)
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
plus spreadsheet-level `namedRanges` (name, range, id) and the spreadsheet `locale` / `timeZone`.

**`locale` / `timeZone`** (e.g. `"en_US"` / `"America/New_York"`, omitted when unset) are the
interpretation signal for the whole sheet: they tell you how dates and numbers are parsed and
displayed (decimal vs. comma separators, date order, the timezone `NOW()`/`TODAY()` resolve in).
Read them before reasoning about any date or number column. (The write side is
`structure --action spreadsheet_props`; see `writing.md`.)

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

### `--rich-text` — per-run rich text & in-cell links

A single cell can hold **multiple styled segments** and **multiple links** (e.g. `"See A then B"`
where `A` and `B` link to different URLs). A plain read flattens that to one value and loses the
per-segment styling and the individual links. `--rich-text` recovers it — and it is the **only** way
to read a multi-link cell:

```sh
gsheets --json inspect <YOUR_SPREADSHEET_ID> 'Sheet1!A1:A20' --rich-text
```

Each cell that carries `textFormatRuns` gains a `runs` array, and a cell with a single whole-cell
link gains a flat `hyperlink`. Emitted **per-cell only when present** (cells without runs/links are
unchanged, so the flag costs tokens only where there is rich text to report):

```jsonc
{ "a1": "A2", "value": "Click here then plain",
  "runs": [ { "start": 0, "text": "Click here", "bold": true, "fg": "#1155CC", "link": "https://x" },
            { "start": 11, "text": " then plain" } ],
  "hyperlink": "https://x" }            // present only for a single whole-cell link
```

- `start` is the 0-based character offset where the run begins; `text` is the substring up to the
  next run. The run's own `bold`/`italic`/`fg`/… are the flattened run-level text format.
- A **run-level `link` takes precedence over the cell `hyperlink`.** For a multi-link cell the flat
  `hyperlink` is absent (Google leaves it empty) and each link lives on its run — so iterate `runs`
  to recover every link.
- The terse rendering is one condformat-style line per cell with runs:
  `runs A1: "Click here"[0:10 bold fg #1155CC link https://x] + " then plain"[11:22]`.

`hyperlink` is a **read-only** Google field — you set an in-cell link by writing a `=HYPERLINK(...)`
formula (via `write-values`), not by writing `hyperlink` back.

### `--pivot` — pivot-table definitions

A pivot table's definition lives on its **anchor (top-left) cell only**. `--pivot` surfaces it so
you can see what a generated block is (and avoid overwriting it):

```sh
gsheets --json inspect <YOUR_SPREADSHEET_ID> 'Sheet1!A1:H40' --pivot
```

The anchor cell gains a `pivot` object (read-only; per-cell only when present):

```jsonc
{ "a1": "A1",
  "pivot": { "source": "Data!A1:F500",
             "rows":    [ { "field": "Region",  "sourceColumnOffset": 0, "showTotals": true, "sortOrder": "ASCENDING" } ],
             "columns": [ { "field": "Quarter", "sourceColumnOffset": 2, "showTotals": true } ],
             "values":  [ { "name": "Sum of Sales", "sourceColumnOffset": 4, "summarize": "SUM" } ],
             "filters": [ { "sourceColumnOffset": 1, "visibleValues": ["X","Y"] } ],
             "valueLayout": "HORIZONTAL" } }
```

Terse line: `pivot A1 <- Data!A1:F500 | rows: Region | cols: Quarter | values: SUM(Sales)`. Pivot
**write** stays in the `batch` escape hatch (read-only here).

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
(they're omitted when unset). The same holds for the rich-text/pivot reads: when `--rich-text` /
`--pivot` are on, a run also carries `runs`/`hyperlink`/`pivot` when present, and cells with
differing runs/links/pivots never merge into one run.

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

## `structure --action read` — the structural picture

`structure --action read` (omit `--sheet` for every tab, or name one) returns a shape-stable
envelope: spreadsheet-scoped `namedRanges` at the top level, and per sheet `merges`, `frozenRows`,
`frozenCols`, `tabColor`, `protectedRanges`, `dimensionGroups`, plus the **v0.2 structural reads**
that close the "reason over a partial/filtered table and edit the wrong row" gap. Each new key is
serialized in the same flattened/terse style as everything else:

```sh
gsheets --json structure <YOUR_SPREADSHEET_ID> --action read --sheet Sheet1
```

- **`tables`** — native Sheets Tables (2024 GA): the table's `name`, `range`, and typed columns.
  Tells you the *schema* of a range and where it ends (so you append safely).
  Terse: `table "Sales" [Sheet1!A1:F500] cols: Region:TEXT, Status:DROPDOWN(Open,Closed)`.
  ```jsonc
  { "tableId": "abc", "name": "Sales", "range": "Sheet1!A1:F500",
    "columns": [ { "name": "Region", "type": "TEXT" },
                 { "name": "Status", "type": "DROPDOWN", "validation": "ONE_OF_LIST(Open,Closed)" } ] }
  ```
- **`basicFilter`** (one per sheet or `null`) and **`filterViews`** (array) — the active filter
  state. A filtered table can *hide rows*; reading this prevents editing the wrong row.
  Terse: `basicFilter [Sheet1!A1:F500] sort C asc | B: hide Closed, NUMBER_GREATER(0)` and
  `filterView 123 "Open only" [Sheet1!A1:F500] | B: hide Closed`. `col` is the column letter; the
  per-column `condition` reuses the same condition serializer as conditional formats.
- **`bandedRanges`** — alternating-color (banded) ranges; a cheap "this rectangle is a deliberate
  table" hint, with the header/first/second/footer hexes.
  Terse: `banding 7 [Sheet1!A1:F500] rows: hdr #4285F4 / #FFFFFF / #E8F0FE`.
- **`slicers`** — on-grid slicer controls: `slicerId`, `title`, source `range`, filtered
  `columnIndex`, a flattened `anchor` (`{sheet, row, col}`), and a terse `criteria`. The slicer's
  anchor sheet is usually a *different* tab from its data range, so the anchor resolves its own
  `sheetId` → sheet name.
  Terse: `slicer 4 "Region" col 0 [Data!A1:F500] @ Dash!I1` (with ` -> <criterion>` appended when a
  filter criterion is set). The anchor renders from `{sheet,row,col}` as a sheet-qualified
  single-cell A1 ref. `row`/`col` are 0-based and a `GridCoordinate` *omits* a 0 index, so an
  absent index reads back as 0 — a top-left/row-0 anchor still renders (e.g. an anchor the API
  returned as `{columnIndex: 4}` reads as `{row: 0, col: 4}` and renders `@ Sheet!E1`). Slicers
  gained write CRUD in v0.2 (`add_slicer`/`update_slicer`/`delete_slicer` — see `writing.md`).

`dimensionGroups` is the flattened row/column-group output (the read mask requests Google's
`rowGroups` + `columnGroups` under the hood). All of these are read with a tight field mask — never
grid data.

## `comments --action read` — Drive threaded comments

Human review intent often lives in **comments**, not the grid — and a value read never sees it.
`comments` defaults to `--action read`, listing the spreadsheet's Drive comment threads (the
create/reply/resolve/delete write actions live in `writing.md`):

```sh
gsheets comments <YOUR_SPREADSHEET_ID>                     # all comments (resolved included)
gsheets comments <YOUR_SPREADSHEET_ID> --no-resolved       # only open threads
gsheets comments <YOUR_SPREADSHEET_ID> --include-deleted    # include deleted comments
```

`--no-resolved` and `--include-deleted` apply to `read` only. Each comment flattens to author,
content, timestamps, resolved state, any quoted snippet, and its replies:

```jsonc
{ "id": "AAAA", "author": "Jane Doe", "content": "please verify Q3",
  "created": "2026-05-01T...", "modified": "2026-05-02T...", "resolved": false,
  "quoted": "1234",
  "replies": [ { "author": "Bob", "content": "done", "action": "resolve" } ],
  "anchorRaw": "<opaque>" }
```

Terse line: `comment AAAA by Jane Doe: "please verify Q3" (open, 1 reply)`.

Two things to know:

- **Comments use the Drive API, so they need a Drive scope** — `drive.file` (the default) reaches
  files this tool created or opened; for a spreadsheet someone else shared with you, run with
  `GSHEETS_SCOPES=broad` (or `--scopes broad`), otherwise the call raises `drive_unavailable`. This
  applies to every action, read and write.
- **The `anchor` is opaque**, not an A1 range — Google encodes it as a document-type-specific blob
  with no documented cell mapping. The raw value is surfaced as `anchorRaw` for reference, but
  comments are document-level and are **never** mapped to a cell.

## `read-many` — cross-file fan-out

One call that reads values or summaries across **many spreadsheets**, capturing per-file errors
instead of aborting the batch. The ids live **inside** `--requests-json` (one per request) — there
is no positional `spreadsheet_id` and no `--ranges` flag on this command. `--json` is global, so it
goes **before** the subcommand:

```sh
# values across two files (per-request ranges; optional per-request render):
gsheets --json read-many --requests-json '[
  {"spreadsheetId":"<YOUR_SPREADSHEET_ID>","ranges":["Sheet1!A1:B2"]},
  {"spreadsheetId":"<ANOTHER_ID>","ranges":["Data!A:A"],"render":"unformatted"}
]'

# cheap orientation across a set (no ranges read):
gsheets --json read-many --mode summary --requests-json '[
  {"spreadsheetId":"<YOUR_SPREADSHEET_ID>"},
  {"spreadsheetId":"<ANOTHER_ID>"}
]'
```

- `--mode values` (default) reads each request's `ranges` (required per request in this mode); an
  optional per-request `"render"` overrides the default `plain` (`plain | unformatted | formula |
  all`, same as `read-values`).
- `--mode summary` runs an `overview` per id (no grid data); `ranges` is ignored.

The envelope is `{ok, mode, count, results}`. Per-file error capture is the point: a bad id (404,
permission denied, bad range) becomes a `{"spreadsheetId":..,"ok":false,"error":{...}}` entry in
`results` rather than failing the whole call — the other files still read. So **a top-level
`ok:true` does NOT mean every file succeeded**; check each `results[]` entry's `ok`. Successful
entries are the underlying core result dict (an `overview` shape in summary mode, a `read-values`
shape in values mode), each carrying its own `spreadsheetId`.

A *malformed batch* (not a per-file failure) still raises: an empty list, a non-dict request, a
request missing `spreadsheetId`, or a values-mode request missing `ranges` all raise `bad_requests`
up front (the whole batch is validated before any read fires).

## Token efficiency notes

- **Start with `overview`** before any grid read — it tells you which tab/range is worth a closer
  look without pulling cells.
- **Use `--compact`** on large or repetitive ranges; it collapses vertical/horizontal runs and
  drops empties.
- **Trim `inspect`** with the `--no-*` flags when you only need part of the picture.
- **`--rich-text` / `--pivot` are opt-in and per-cell-only** — off by default (zero cost), and even
  when on they attach data only to the cells that actually carry runs/links/pivots, so you never pay
  for cells that don't.
- **`--render unformatted`** is cheaper than re-deriving numbers from formatted strings when you
  need raw values for computation.
- Unset format keys are always omitted, and reads never use `includeGridData` — the field mask is
  always as tight as the request allows.
