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
- [`data-ops` — range data operations](#data-ops--range-data-operations)
- [`dimensions` — row & column operations](#dimensions--row--column-operations)
- [Structure, tabs, metadata](#structure-tabs-metadata)
  - [Tables, banding, filters, spreadsheet props (v0.2)](#tables-banding-filters-spreadsheet-props-v02)
  - [Slicer writes (v0.2)](#slicer-writes-v02)
- [`comments` writes — create / reply / resolve / delete](#comments-writes--create--reply--resolve--delete)
- [`export` — download to a local file (no mutation)](#export--download-to-a-local-file-no-mutation)
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

## `data-ops` — range data operations

`data-ops` groups the range-level data verbs that are each a single `batchUpdate` request — find &
replace, dedupe, trim, sort, split-to-columns, autofill, and copy/cut-paste. One `--action` per
call; the operands go in `--params-json` (same convention as `structure`). An unknown `params` key
raises `unknown_param`.

```sh
# Find & replace within one sheet (scope = range | sheet | allSheets — pass exactly one):
gsheets data-ops <YOUR_SPREADSHEET_ID> --action find_replace \
  --params-json '{"find":"TODO","replacement":"DONE","sheet":"Sheet1"}'

# Sort a range by column B descending:
gsheets data-ops <YOUR_SPREADSHEET_ID> --action sort_range \
  --params-json '{"range":"Sheet1!A2:D100","specs":[{"col":"B","order":"DESCENDING"}]}'

# Remove duplicate rows, comparing only columns A and C:
gsheets data-ops <YOUR_SPREADSHEET_ID> --action delete_duplicates \
  --params-json '{"range":"Sheet1!A1:D100","comparisonColumns":["A","C"]}'

# Copy values only into another range (paste-type aware):
gsheets data-ops <YOUR_SPREADSHEET_ID> --action copy_paste \
  --params-json '{"source":"Sheet1!A1:D10","destination":"Sheet2!A1","pasteType":"PASTE_VALUES"}'
```

The result echoes an action-specific summary from the API reply: `find_replace` →
`occurrencesChanged` / `valuesChanged` / `formulasChanged`, `delete_duplicates` →
`duplicatesRemoved`, `trim_whitespace` → `cellsChangedCount`, and the geometry verbs echo what they
changed. **`find_replace`, `cut_paste`, and `delete_duplicates` are destructive** — read the range
first and confirm. See the full `--params-json` key tables in `commands.md`.

## `dimensions` — row & column operations

`dimensions` is the row/column counterpart to `manage-sheets` (which is tab-level): insert / delete
/ move / append rows or columns, auto-fit, set pixel size / hidden, and `read` the hidden rows/cols.
**Every action targets one tab, so `--sheet` is required.** Spans are 0-based half-open (`start`
inclusive, `end` exclusive).

```sh
# Insert 3 rows starting at row index 5 (0-based) on Sheet1:
gsheets dimensions <YOUR_SPREADSHEET_ID> --action insert --sheet Sheet1 \
  --params-json '{"dimension":"ROWS","start":5,"end":8}'

# Auto-fit every column to its content:
gsheets dimensions <YOUR_SPREADSHEET_ID> --action auto_resize --sheet Sheet1 \
  --params-json '{"dimension":"COLUMNS"}'

# Hide columns C–E (set_props with hiddenByUser):
gsheets dimensions <YOUR_SPREADSHEET_ID> --action set_props --sheet Sheet1 \
  --params-json '{"dimension":"COLUMNS","start":2,"end":5,"hiddenByUser":true}'

# Read which rows/cols are hidden (the "what the viewer actually sees" companion to filter views):
gsheets dimensions <YOUR_SPREADSHEET_ID> --action read --sheet Sheet1
#   -> { "hiddenRows": [...], "hiddenCols": [...] }
```

`delete` is **destructive** — confirm first. `insert`/`append`/`move` shift the rows/cols below
them, so re-read any A1 references you cached afterward. See `commands.md` for the per-action
`params` keys.

## Structure, tabs, metadata

- **`structure`** writes go through the same auto field-mask where applicable, and `--sheet` is
  **required** to mutate the sheet-scoped actions (omit only for `read`, and for the
  spreadsheet-scoped `spreadsheet_props`). `unmerge`, `delete_named`, `unprotect`, `ungroup`, and
  the v0.2 deletes (`delete_table`, `delete_banding`, `clear_basic_filter`, `delete_filter_view`,
  `delete_slicer`) are destructive. New `namedRangeId`/`protectedRangeId`/`tableId`/
  `bandedRangeId`/`filterViewId`/`slicerId`s come back in the result so a create-then-populate flow
  has the id immediately.
- **`manage-sheets`** returns the new `sheetId`/title/index for `add`/`duplicate`. `delete` is
  destructive — confirm first. (Row/column ops live on `dimensions`, above, not here.)
- **`metadata`** writes durable key/value anchors (row/column range, whole sheet, or spreadsheet)
  that survive row/column inserts — prefer them over hard-coded A1 when you need a stable reference.

See the per-action `params` key tables in `commands.md`; an unknown key raises `unknown_param`.

### Tables, banding, filters, spreadsheet props (v0.2)

`structure` gained write CRUD for the structural reads `structure --action read` surfaces, so
everything you can read back you can also create/edit/delete (full CRUD symmetry, slicers included —
see [Slicer writes](#slicer-writes-v02) below):

```sh
# Create a native Table over a range (a DROPDOWN column needs a ONE_OF_LIST validation):
gsheets structure <YOUR_SPREADSHEET_ID> --action add_table --range 'Sheet1!A1:F500' \
  --params-json '{"name":"Sales","columns":[{"name":"Region","type":"TEXT"},
    {"name":"Status","type":"DROPDOWN","validation":{"type":"ONE_OF_LIST","values":["Open","Closed"]}}]}'
#   -> returns the new tableId; pass it to update_table / delete_table.

# Add alternating-row banding:
gsheets structure <YOUR_SPREADSHEET_ID> --action add_banding --range 'Sheet1!A1:F500' \
  --params-json '{"rowBanding":{"header":"#4285F4","first":"#FFFFFF","second":"#E8F0FE"}}'

# Set the basic filter (sort + per-column hidden values):
gsheets structure <YOUR_SPREADSHEET_ID> --action set_basic_filter --range 'Sheet1!A1:F500' \
  --params-json '{"sorted":[{"col":"C","order":"ASCENDING"}],"criteria":[{"col":"B","hidden":["Closed"]}]}'

# Add a named filter view (returns filterViewId):
gsheets structure <YOUR_SPREADSHEET_ID> --action add_filter_view --range 'Sheet1!A1:F500' \
  --params-json '{"title":"Open only","criteria":[{"col":"B","hidden":["Closed"]}]}'

# Set spreadsheet-level locale / timeZone / title (spreadsheet-scoped — no --sheet):
gsheets structure <YOUR_SPREADSHEET_ID> --action spreadsheet_props \
  --params-json '{"locale":"en_US","timeZone":"America/New_York"}'
```

The `tableId` / `bandedRangeId` / `filterViewId` returned on create are how you address the matching
`update_*` / `delete_*` action — read them back with `structure --action read`. `spreadsheet_props`
is the **write side** of `overview`'s `locale` / `timeZone`. See `commands.md` for every action's
`params` keys.

### Slicer writes (v0.2)

Slicers gained write CRUD: `add_slicer` / `update_slicer` / `delete_slicer`, all through the
existing `structure` subcommand. A slicer points at a **data range**, filters one of its columns,
and is positioned at a single **anchor** cell (usually on a different tab):

```sh
# Add a slicer over a data range, anchored at S!E1, filtering column 0:
gsheets structure <YOUR_SPREADSHEET_ID> --action add_slicer --sheet S --range 'S!A1:C4' \
  --params-json '{"title":"Region","columnIndex":0,"anchor":"S!E1"}'
#   -> returns the new slicerId.

# Update a slicer by id (auto fields mask — only the keys you pass are written):
gsheets structure <YOUR_SPREADSHEET_ID> --action update_slicer --sheet S \
  --params-json '{"slicerId":12,"title":"Region (filtered)","criteria":{"condition":"NUMBER_GREATER(0)"}}'

# Delete a slicer by id (DESTRUCTIVE; maps to deleteEmbeddedObject):
gsheets structure <YOUR_SPREADSHEET_ID> --action delete_slicer --sheet S \
  --params-json '{"slicerId":12}'
```

- The **data range** is the top-level `--range` *or* `params["dataRange"]` (top-level `--range`
  wins when both are given). **`anchor`** (a single A1 cell, required on add) goes in `--params-json`.
- `add_slicer` accepts `title`, `columnIndex` (0-based offset into the data range of the filtered
  column), `anchor`, and `criteria` (`{"hidden"?, "visible"?, "condition"?}` — a `condition` reuses
  the same terse condition grammar as conditional formats and filters, e.g. `NUMBER_GREATER(0)`).
- `update_slicer` / `delete_slicer` take `--params-json '{"slicerId":N, ...}'`; `update_slicer`
  builds its `fields` mask from the keys you pass (`title` / `dataRange` / `columnIndex` /
  `criteria`) and refuses an empty change.
- Read it back with `structure --action read`; the new slicer shows up in the host sheet's
  `slicers` list with its terse `@ <anchor>` line (see `reading.md`).

## `comments` writes — create / reply / resolve / delete

`comments` is full CRUD (the read action is in `reading.md`). The write actions go through the
**Drive API**, so they need a Drive scope just like the read — `drive.file` (the default) for files
this tool created/opened, else `GSHEETS_SCOPES=broad` for someone else's shared sheet, otherwise
`drive_unavailable`.

```sh
# Create a top-level comment (--content required; optional opaque --anchor passes through):
gsheets comments <YOUR_SPREADSHEET_ID> --action create --content 'please verify Q3 totals'

# Reply to an existing comment (--comment-id AND --content required):
gsheets comments <YOUR_SPREADSHEET_ID> --action reply --comment-id AAAA --content 'verified'

# Resolve a comment (--comment-id required; --content optional — rides along as the reply body):
gsheets comments <YOUR_SPREADSHEET_ID> --action resolve --comment-id AAAA --content 'closing'

# Delete a comment (DESTRUCTIVE — requires --confirm):
gsheets comments <YOUR_SPREADSHEET_ID> --action delete --comment-id AAAA --confirm
```

- **`resolve` == posting a reply with `action:resolve`.** Drive has no standalone resolve endpoint,
  so resolving is implemented as a reply carrying `action="resolve"`; an optional `--content` is
  that reply's body. The return is `{commentId, resolved:true, reply:{...}}`.
- **`delete` is destructive and requires `--confirm`** — without it the CLI refuses with
  `confirmation_required` rather than removing the comment. (On the MCP side `sheets_comments` is a
  write tool: `readOnlyHint=False`, `destructiveHint=True`.)
- **`--anchor` is opaque** — passed through verbatim on `create`, never interpreted as an A1 range
  (Drive anchors have no documented cell mapping).
- `create` returns the new comment through the same serializer a read uses (so `comments --action
  read` round-trips it); `reply`/`resolve` return the flattened reply.

## `export` — download to a local file (no mutation)

`export` writes a **local file**; it never mutates the spreadsheet (hence `readOnlyHint=True` on
`sheets_export` — read-only against the API, even though it writes to disk). Two backends, picked by
`--format`:

```sh
# Whole-workbook render via Drive (needs a Drive scope) — --sheet is IGNORED:
gsheets export <YOUR_SPREADSHEET_ID> --format pdf
gsheets export <YOUR_SPREADSHEET_ID> --format xlsx --path /tmp/book.xlsx

# Single sheet, serialized locally from values (Sheets scope only) — --sheet REQUIRED:
gsheets export <YOUR_SPREADSHEET_ID> --format csv --sheet Sheet1
```

- **`pdf` / `xlsx` / `ods`** are whole-workbook exports rendered server-side via Drive
  `files.export`. They REQUIRE a Drive scope (else `drive_unavailable`); `--sheet` is ignored.
- **`csv` / `tsv`** export a single named `--sheet`, read through the Sheets API and serialized
  locally (Drive's csv export only ever emits the first sheet, so this path avoids Drive and needs
  only the Sheets scope). `--sheet` is REQUIRED here — omitting it raises `missing_sheet`.
- `--path` defaults to `<spreadsheetId>.<format>` in the cwd. The result is
  `{format, mimeType, path, bytes}` (the byte count lets you verify the download).

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

The v0.2 structural writes round-trip the same way — after `add_table` / `add_banding` /
`set_basic_filter` / `add_filter_view` / `add_slicer` / `spreadsheet_props`, re-read with `structure
--action read` (or `overview` for `locale`/`timeZone`) to confirm; after a `dimensions` op,
`dimensions --action read` reports the new hidden state. `export` is the exception to read-back: it
produces a local file, so verify it by the returned `path`/`bytes`, not by re-reading the sheet.

The remaining read-back gap is **charts**: `charts --action read` returns chart *metadata only*
(`chartId`, `title`, `type`, `anchor`), not the full spec. For full chart-spec fidelity use `batch`
(raw `addChart` / `updateChartSpec`).
