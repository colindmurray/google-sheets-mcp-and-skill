# gsheets — command reference

The complete per-subcommand flag surface for the `gsheets` CLI. Terse but authoritative.

**`gsheets <command> --help` is the always-current source of truth** for the exact flags of any
single command; this file is the scannable map. Every example uses `<YOUR_SPREADSHEET_ID>` as a
placeholder — the real ID comes from the user, the URL they paste (the token between `/d/` and
`/edit`), or the environment, and is never committed.

## Table of contents

- [Global flags & conventions](#global-flags--conventions)
- [Reading](#reading)
  - [`overview`](#overview)
  - [`inspect`](#inspect)
  - [`read-values`](#read-values)
  - [`read-conditional-formats`](#read-conditional-formats)
  - [`comments`](#comments)
- [Writing](#writing)
  - [`write-values`](#write-values)
  - [`append-rows`](#append-rows)
  - [`clear`](#clear)
  - [`format`](#format)
  - [`set-conditional-format`](#set-conditional-format)
  - [`set-validation`](#set-validation)
  - [`data-ops`](#data-ops)
- [Structure & tabs](#structure--tabs)
  - [`structure`](#structure)
  - [`dimensions`](#dimensions)
  - [`manage-sheets`](#manage-sheets)
  - [`metadata`](#metadata)
  - [`charts`](#charts)
- [Escape hatch](#escape-hatch)
  - [`batch`](#batch)
- [Auth](#auth)
  - [`auth login` / `auth status`](#auth-login--auth-status)
- [JSON flag values & `@file.json`](#json-flag-values--filejson)

For the *why* behind reads (render modes, compact runs, the conditional-format line grammar) see
`reading.md`; for the *why* behind writes (USER_ENTERED, the auto field-mask, index-safe CF,
CRUD read-back) see `writing.md`.

---

## Global flags & conventions

`--json` and `--scopes` are **global** flags owned by the top-level parser, so they go **before**
the subcommand name, never after it:

```sh
gsheets --json overview <YOUR_SPREADSHEET_ID>          # correct
gsheets overview <YOUR_SPREADSHEET_ID> --json          # WRONG -> "error: unrecognized arguments: --json" (exit 2)
```

| Flag | Effect |
|---|---|
| `--json` | Emit the raw core result dict as pretty JSON. Default is terse readable text. Prefer `--json` when piping to `jq` or parsing. **Goes before the subcommand.** |
| `--scopes {default,broad}` | Auth scope mode for this invocation (overrides `GSHEETS_SCOPES`). `default` = `spreadsheets` + `drive.file`; `broad` adds full `drive`. **Goes before the subcommand.** |

The per-command synopses below therefore show only that command's **own** flags; prepend `--json`
to any of them for machine output (e.g. `gsheets --json inspect <ID> 'Sheet1!A1:D20'`).

- Positional `spreadsheet_id` (`<YOUR_SPREADSHEET_ID>`) is the **first** argument of every Sheets
  subcommand.
- Ranges are **A1 strings**: `'Sheet1!A1:D20'`, `'Sheet1!A:A'` (whole column), `'Sheet1'` (whole
  tab). Quote them — `!`, `:`, and spaces are shell-significant.
- Errors print to **stderr** as `gsheets: error: <code>: <message>` (plus a `hint:` line when
  present), or as `{"ok": false, "error": {...}}` under `--json`; exit code is `1`.

---

## Reading

### `overview`

Cheap orientation snapshot — **no grid data**. Start here for any unfamiliar sheet.

```
gsheets overview <YOUR_SPREADSHEET_ID>
```

No flags beyond the positional id. Returns the title; per-tab `sheetId`/title/index/type, row &
column counts, frozen rows/cols, tab color, and **counts** of protected ranges and
conditional-format rules; plus spreadsheet-level named ranges and the spreadsheet `locale` /
`timeZone` (e.g. `"en_US"` / `"America/New_York"`, omitted when unset — the signal for how dates
and numbers are interpreted). The counts are computed in core (`len()` of length-yielding
subfields) — overview never pulls full rule/protected-range bodies. See `reading.md`.

### `inspect`

Flagship rich read: per-cell **values + formulas + userEntered & effective formats + merges +
validation**, with a tight field mask (never the whole grid).

```
gsheets inspect <YOUR_SPREADSHEET_ID> <RANGE> [--compact] \
  [--no-effective] [--no-user-entered] [--no-formulas] [--no-validation] \
  [--rich-text] [--pivot]
```

| Flag | Effect |
|---|---|
| `RANGE` (positional) | A1 range, e.g. `'Sheet1!A1:D20'` or `'Sheet1'`. |
| `--compact` | Collapse identical adjacent cells into rectangular `a1Range` runs; drop empty cells. Big token win on repetitive blocks. Runs still carry `note`/`validationRule` (and `runs`/`hyperlink`/`pivot` when present). |
| `--no-effective` | Omit `effectiveFormat` (what actually renders, incl. conditional-format results). |
| `--no-user-entered` | Omit `userEnteredFormat` (the format you set, i.e. intent). |
| `--no-formulas` | Omit formulas (values only). |
| `--no-validation` | Omit data validation (both the terse string and the structured `validationRule`). |
| `--rich-text` | Add per-run rich text and links: each cell with `textFormatRuns` gains `runs` (styled segments with their own `bold`/`fg`/… and a per-run `link`), plus the cell-level `hyperlink`. Off by default (zero token cost). The only way to read a multi-link cell. |
| `--pivot` | Add pivot-table definitions: a pivot's anchor (top-left) cell gains a `pivot` object (source, rows, columns, values, filters). Read-only. Off by default. |

Trim with the `--no-*` flags to cut tokens when you only need part of the picture; `--rich-text`
and `--pivot` are opt-in additive reads (per-cell only when present, so the cost is paid only for
cells that actually carry runs/links/pivots). See `reading.md` for the cell/run shape, the rich-text
run grammar, the pivot shape, and the compact RLE semantics.

### `read-values`

Just values for one or more ranges, with a render mode. Uses `values.batchGet`.

```
gsheets read-values <YOUR_SPREADSHEET_ID> <RANGE...> [--render {plain,unformatted,formula,all}]
```

| Flag | Effect |
|---|---|
| `RANGE...` (positional, 1+) | One or more A1 ranges. |
| `--render plain` (default) | `FORMATTED_VALUE` — what the cell displays. |
| `--render unformatted` | `UNFORMATTED_VALUE` — raw numbers/strings, no display formatting. |
| `--render formula` | `FORMULA` — the formula text; non-formula cells return their literal value. |
| `--render all` | Formula **and** computed value side by side (`values` + `computed`, index-aligned to a common rectangle). |

A `values` entry that does not start with `=` is a **literal**, not a formula (FORMULA render
passes literals through). See `reading.md` for the `all` alignment contract.

### `read-conditional-formats`

Priority read: per-sheet conditional-format rules serialized to terse, readable,
**round-trippable** lines, each with its positional `index` (0 = highest priority).

```
gsheets read-conditional-formats <YOUR_SPREADSHEET_ID> [--sheet NAME]
```

| Flag | Effect |
|---|---|
| `--sheet NAME` | Restrict to one tab. Omit for every sheet (shape-stable multi-sheet envelope). |

Each rule prints as `[<ranges>] <body>` plus structured fields. The `line` is **body-only** (no
index token) — the index lives in the structured `index` field and is the sole write-addressing
source of truth. See `reading.md` for the full line grammar.

### `comments`

Read the spreadsheet's Drive **threaded comments** — author, text, resolved state, replies, and any
quoted snippet. Read-only in v1. Uses the **Drive API** (not the Sheets API).

```
gsheets comments <YOUR_SPREADSHEET_ID> [--no-resolved] [--include-deleted]
```

| Flag | Effect |
|---|---|
| `--no-resolved` | Hide comments that have been resolved (show only open ones). Resolved are shown by default. |
| `--include-deleted` | Also include deleted comments (passes Drive's `includeDeleted`). Off by default. |

**Drive scope is required.** `drive.file` (the default scope) covers files this tool created or
opened; a spreadsheet shared with you by someone else needs `GSHEETS_SCOPES=broad` (or
`--scopes broad`) — otherwise the call raises `drive_unavailable`. The comment **`anchor` is opaque**
(document-type-specific, not an A1 range), so comments are surfaced at the document level only and
are never mapped to a cell. Each comment prints as
`comment <id> by <author>: "<content>" (open|resolved, N replies)` plus structured fields (`author`,
`content`, `created`, `modified`, `resolved`, `quoted`, `replies[]`, `anchorRaw`). See `reading.md`.

---

## Writing

> Writes default to **USER_ENTERED** (formulas go live, `5`/`$10`/`50%` coerce to typed values).
> Field masks are **auto-built** from your payload, so partial writes never wipe other subfields.
> After a write, **read it back** to verify (CRUD is symmetric). See `writing.md`.

### `write-values`

Write/update one or more ranges. Two mutually-exclusive forms.

```
# single-range form
gsheets write-values <YOUR_SPREADSHEET_ID> <RANGE> --values-json '[[...]]' [--input {user_entered,raw}]

# multi-range form
gsheets write-values <YOUR_SPREADSHEET_ID> --data-json '[{"range":"Sheet1!A1","values":[["x"]]}, ...]'
```

| Flag | Effect |
|---|---|
| `RANGE` (positional, optional) | Single-range target; pair with `--values-json`. |
| `--values-json '[[...]]'` | 2D array of rows (single-range form). |
| `--data-json '[{"range":..,"values":..}]'` | Multiple ranges in one call (multi-range form). |
| `--input user_entered` (default) | Parse like a user typing: `=...` becomes a formula, values coerce to typed. |
| `--input raw` | Store the literal text/number verbatim — no formula parsing or coercion. |

Pass **either** the `RANGE` + `--values-json` pair **or** `--data-json`, never both
(→ `conflicting_args`); the single-range form requires both halves (→ `missing_args`).

### `append-rows`

Append rows after a table's last populated row (`INSERT_ROWS` — never overwrites below).

```
gsheets append-rows <YOUR_SPREADSHEET_ID> <RANGE> --values-json '[[...],[...]]' [--input {user_entered,raw}]
```

| Flag | Effect |
|---|---|
| `RANGE` (positional) | A1 range identifying the table (e.g. `'Sheet1!A1'` or `'Sheet1!A1:D1'`). |
| `--values-json` (**required**) | 2D array of rows to append. |
| `--input` | `user_entered` (default) or `raw`, as for `write-values`. |

### `clear`

Clear values, and optionally formats / validation / notes.

```
gsheets clear <YOUR_SPREADSHEET_ID> <RANGE...> [--no-values] [--formats] [--validation] [--notes]
```

| Flag | Effect |
|---|---|
| `RANGE...` (positional, 1+) | One or more A1 ranges to clear. |
| `--no-values` | Do **not** clear values (clear only the structural flags below). |
| `--formats` | Also clear cell formatting. |
| `--validation` | Also clear data validation. |
| `--notes` | Also clear cell notes. |

Values-only is a `values.batchClear`; any of formats/validation/notes routes through a
`batchUpdate` whose fields mask covers exactly the requested subfields. **Destructive** — confirm
and read the range first.

### `format`

Apply formatting to a range — background, font/bold/italic/size/color, number/date pattern,
alignment, wrap, borders, and a cell note — as one atomic `batchUpdate`. The fields mask is
auto-built from exactly the keys you pass.

```
gsheets format <YOUR_SPREADSHEET_ID> <RANGE> [style flags ...]
```

| Flag | Maps to (flat CellFormat key) |
|---|---|
| `--bg HEX|theme:NAME` | `bg` (background). |
| `--fg HEX|theme:NAME` | `fg` (text/foreground). |
| `--bold` / `--no-bold` | `bold` (tri-state: unset leaves it untouched). |
| `--italic` / `--no-italic` | `italic`. |
| `--underline` / `--no-underline` | `underline`. |
| `--strike` / `--no-strike` | `strikethrough`. |
| `--font-size N` | `fontSize`. |
| `--font-family NAME` | `fontFamily`. |
| `--number PATTERN` | `numberFormat` (e.g. `'0.00%'`, `'$#,##0.00'`, `'yyyy-mm-dd'`). |
| `--halign {LEFT,CENTER,RIGHT}` | `halign`. |
| `--valign {TOP,MIDDLE,BOTTOM}` | `valign`. |
| `--wrap {OVERFLOW_CELL,CLIP,WRAP}` | `wrap`. |
| `--note TEXT` | `note` (cell-level note; writable here, readable via `inspect`). |
| `--border SIDE=STYLE:#hex` (repeatable) | a `borders` side, e.g. `--border top=SOLID:#000000`. SIDE ∈ top/bottom/left/right. |
| `--fmt-json '{...}'` | A raw flat CellFormat dict; **overrides** all the individual flags. |

Colors are `#RRGGBB` hex or `theme:NAME` (e.g. `theme:ACCENT1`). Only the keys you pass are
written; unspecified subfields are preserved. See `writing.md` for the auto fields-mask and the
atomic-leaf set (`numberFormat`, `padding`, any `*ColorStyle` are masked at the parent).

### `set-conditional-format`

Add / update / delete a boolean or gradient conditional-format rule by **positional index** (array
order = priority; no stable rule id).

```
# single form
gsheets set-conditional-format <YOUR_SPREADSHEET_ID> --action {add,update,delete} \
  [--sheet NAME] [--index N] [--rule 'LINE' | --rule-json '{...}']

# batch form (index-safe: sorted high->low in one batchUpdate)
gsheets set-conditional-format <YOUR_SPREADSHEET_ID> \
  --rules-json '[{"action":"delete","index":5},{"action":"update","index":2,"rule":"..."}]'
```

| Flag | Effect |
|---|---|
| `--action {add,update,delete}` | Single-form action. Omit when using `--rules-json`. |
| `--sheet NAME` | Target tab. |
| `--index N` | Positional index. **Required** for `update`/`delete`; insert position for `add` (0 = top priority). |
| `--rule 'LINE'` | A body line, e.g. `'[Sheet1!A2:A100] if NUMBER_GREATER(0) -> bg #C8E6C9'`. The line carries **no** index — addressing comes only from `--index`. |
| `--rule-json '{...}'` | Structured `{ranges,kind,condition,format}` instead of a line. |
| `--rules-json '[{"action","index","rule"}]'` | Batch form; core orders the mutations high→low so earlier edits never shift later targets. `rule` omitted for delete. |

Pass `--rule` **or** `--rule-json`, not both (→ `conflicting_args`). `--action`/`--index`/`--rule`
and `--rules-json` are mutually exclusive. **Separate** single calls are NOT auto-ordered — mutate
high index → low yourself, or re-read indices between calls. The `delete` path is **destructive**.
See `writing.md` and the line grammar in `reading.md`.

### `set-validation`

Set or clear data validation on a range (dropdowns, number ranges, custom formulas).

```
gsheets set-validation <YOUR_SPREADSHEET_ID> <RANGE> [--rule-json '{...}'] [--no-strict] [--no-dropdown]
```

| Flag | Effect |
|---|---|
| `RANGE` (positional) | A1 range to validate. |
| `--rule-json '{...}'` | Structured `ValidationRule`. **Omit to CLEAR** validation on the range. |
| `--no-strict` | Allow invalid input (a warning instead of rejection). Default is strict. |
| `--no-dropdown` | Hide the in-cell dropdown chip. Default shows it. |

`ValidationRule` variants (the same shape `inspect` reads back under `validationRule`):

| Type | Shape |
|---|---|
| List of literals | `{"type":"ONE_OF_LIST","values":["Yes","No"]}` |
| List from a range | `{"type":"ONE_OF_RANGE","source":"Sheet1!Z1:Z10"}` |
| Checkbox | `{"type":"BOOLEAN"}` |
| Number range | `{"type":"NUMBER_BETWEEN","values":[0,100]}` |
| Custom formula | `{"type":"CUSTOM_FORMULA","values":["=ISNUMBER(A1)"]}` |
| Non-blank / blank | `{"type":"NOT_BLANK"}` / `{"type":"BLANK"}` |

`strict`/`showDropdown` may also be carried inside `--rule-json` (`"strict"`/`"showDropdown"`); the
kwargs (`--no-strict`/`--no-dropdown`) win on conflict.

### `data-ops`

Range-level data operations, each a single `batchUpdate` request. One `--action` per call; the
operands go in `--params-json` (same shape convention as `structure`/`manage-sheets`). An unknown
`params` key raises `unknown_param`.

```
gsheets data-ops <YOUR_SPREADSHEET_ID> --action <ACTION> --params-json '{...}'
```

| `--action` | What it does | `--params-json` keys |
|---|---|---|
| `find_replace` | Find/replace across a range, a sheet, or all sheets | `{"find": str, "replacement": str, "searchByRegex"?: bool, "matchCase"?: bool, "matchEntireCell"?: bool, "includeFormulas"?: bool, "range"?: A1, "sheet"?: str, "allSheets"?: bool}` (exactly one scope: `range` / `sheet` / `allSheets`) |
| `delete_duplicates` | Remove duplicate rows in a range | `{"range": A1, "comparisonColumns"?: ["A","C",...]}` |
| `trim_whitespace` | Trim leading/trailing whitespace in cells | `{"range": A1}` |
| `sort_range` | Sort a range by one or more columns | `{"range": A1, "specs": [{"col":"B","order":"ASCENDING"|"DESCENDING"}, ...]}` |
| `text_to_columns` | Split a column on a delimiter | `{"range": A1, "delimiter"?: str, "delimiterType"?: "COMMA"|"SEMICOLON"|"PERIOD"|"SPACE"|"CUSTOM"|"AUTODETECT"}` |
| `auto_fill` | Extend a series into adjacent cells | `{"range": A1, "useAlternateSeries"?: bool}` **or** `{"source": A1, "destination": A1}` |
| `copy_paste` | Copy a range to a destination (paste-type aware) | `{"source": A1, "destination": A1, "pasteType"?: "PASTE_NORMAL"|"PASTE_VALUES"|"PASTE_FORMAT"|"PASTE_FORMULA"|"PASTE_NO_BORDERS"|"PASTE_CONDITIONAL_FORMATTING"|"PASTE_DATA_VALIDATION", "pasteOrientation"?: "NORMAL"|"TRANSPOSE"}` |
| `cut_paste` | Move a range to a destination top-left cell | `{"source": A1, "destination": A1, "pasteType"?: ...}` |

The result echoes an action-specific summary from the API reply — e.g. `find_replace` returns
`occurrencesChanged` / `valuesChanged` / `formulasChanged`, `delete_duplicates` returns
`duplicatesRemoved`, `trim_whitespace` returns `cellsChangedCount`. `find_replace`, `cut_paste`, and
`delete_duplicates` are **destructive** — read the range first and confirm. See `writing.md`.

```sh
gsheets data-ops <YOUR_SPREADSHEET_ID> --action find_replace \
  --params-json '{"find":"TODO","replacement":"DONE","sheet":"Sheet1"}'
gsheets data-ops <YOUR_SPREADSHEET_ID> --action sort_range \
  --params-json '{"range":"Sheet1!A2:D100","specs":[{"col":"B","order":"DESCENDING"}]}'
```

---

## Structure & tabs

### `structure`

One interface for merges, named ranges, protected ranges, frozen rows/cols, tab color, row/column
groups, native **tables**, **banding**, **basic filter / filter views**, and spreadsheet-level
properties. Read or modify.

```
gsheets structure <YOUR_SPREADSHEET_ID> --action <ACTION> [--sheet NAME] [--range A1] [--params-json '{...}']
```

`--action` is **required**. `--sheet` is optional for `read` (omit ⇒ every tab) and **required**
for the sheet-scoped mutating actions (→ `missing_sheet` otherwise); `spreadsheet_props` is
spreadsheet-scoped and needs no `--sheet`. Each action consumes only its listed `params` keys; an
unknown key raises `unknown_param`.

**`--action read`** returns the full structural picture with a shape-stable envelope: top-level
`namedRanges`, and per-sheet `merges`, `frozenRows`, `frozenCols`, `tabColor`, `protectedRanges`,
`dimensionGroups`, **and the v0.2 additions** `tables`, `basicFilter`, `filterViews`,
`bandedRanges`, `slicers`. See `reading.md` for those shapes and terse lines.

Base / structural actions:

| `--action` | Needs | `--params-json` keys |
|---|---|---|
| `read` | `--sheet` optional | — |
| `merge` | `--range` | `{"mergeType":"MERGE_ALL"|"MERGE_COLUMNS"|"MERGE_ROWS"}` (default `MERGE_ALL`) |
| `unmerge` | `--range` | — |
| `add_named` | `--range` | `{"name": str}` |
| `delete_named` | — | `{"name": str}` **or** `{"namedRangeId": str}` |
| `protect` | `--range` | `{"description": str, "editors": [email,...], "warningOnly": bool}` (all optional) |
| `unprotect` | — | `{"protectedRangeId": int}` |
| `freeze` | `--sheet` | `{"rows": int, "cols": int}` (either/both) |
| `tab_color` | `--sheet` | `{"color": "#RRGGBB"|"theme:NAME"}` |
| `group` | `--sheet` | `{"dimension":"ROWS"|"COLUMNS", "start": int, "end": int}` (0-based half-open) |
| `ungroup` | `--sheet` | `{"dimension":"ROWS"|"COLUMNS", "start": int, "end": int}` |

Tables / banding / filters / spreadsheet props (v0.2):

| `--action` | Needs | `--params-json` keys |
|---|---|---|
| `add_table` | `--range` | `{"name": str, "columns": [{"name": str, "type": "TEXT"|"DOUBLE"|"CURRENCY"|"PERCENT"|"DATE"|"TIME"|"DATETIME"|"DROPDOWN"|"CHECKBOX"|"SMART_CHIP"|"RATING", "validation"?: {ValidationRule}}, ...]}` (a `DROPDOWN` column needs a `ONE_OF_LIST` `validation`). Returns the new `tableId`. |
| `update_table` | — | `{"tableId": str, "name"?: str, "columns"?: [...], "range"?: A1}` |
| `delete_table` | — | `{"tableId": str}` (**destructive**) |
| `add_banding` | `--range` | `{"rowBanding"?: {"header"?,"first"?,"second"?,"footer"? hex}, "columnBanding"?: {...}}`. Returns the new `bandedRangeId`. |
| `update_banding` | — | `{"bandedRangeId": int, "rowBanding"?, "columnBanding"?, "range"?: A1}` |
| `delete_banding` | — | `{"bandedRangeId": int}` (**destructive**) |
| `set_basic_filter` | `--range` | `{"sorted"?: [{"col": "C", "order": "ASCENDING"|"DESCENDING"}], "criteria"?: [{"col": "B", "hidden"?: [...], "condition"?: "NUMBER_GREATER(0)"}]}` |
| `clear_basic_filter` | `--sheet` | — (**destructive**) |
| `add_filter_view` | `--range` | `{"title": str, "sorted"?: [...], "criteria"?: [...]}`. Returns the new `filterViewId`. |
| `update_filter_view` | — | `{"filterViewId": int, "title"?, "range"?: A1, "sorted"?, "criteria"?}` |
| `delete_filter_view` | — | `{"filterViewId": int}` (**destructive**) |
| `spreadsheet_props` | — (spreadsheet-scoped) | `{"title"?: str, "locale"?: str, "timeZone"?: str}` (the write side of `overview`'s `locale`/`timeZone`) |

`unmerge`, `delete_named`, `unprotect`, `ungroup`, `delete_table`, `delete_banding`,
`clear_basic_filter`, and `delete_filter_view` are **destructive** — confirm first. New
`namedRangeId`/`protectedRangeId`/`tableId`/`bandedRangeId`/`filterViewId`s are returned in the
result. Slicer **write** is out of v1 (slicers are readable via `--action read`; create/edit one
through `batch`).

### `dimensions`

Row and column operations on one tab: insert / delete / move / append rows or columns, auto-fit,
set height/width/hidden, and read which rows/cols are hidden from a viewer.

```
gsheets dimensions <YOUR_SPREADSHEET_ID> --action <ACTION> --sheet NAME --params-json '{...}'
```

`--action` required; **every action targets one tab, so `--sheet` is required.** Spans are 0-based
half-open (`start` inclusive, `end` exclusive). Unknown `params` key → `unknown_param`.

| `--action` | What it does | `--params-json` keys |
|---|---|---|
| `insert` | Insert rows/columns | `{"dimension": "ROWS"|"COLUMNS", "start": int, "end": int, "inheritFromBefore"?: bool}` |
| `delete` | Delete rows/columns (**destructive**) | `{"dimension", "start", "end"}` |
| `move` | Move a band of rows/columns | `{"dimension", "start", "end", "destinationIndex": int}` |
| `append` | Append rows/columns at the end | `{"dimension", "length": int}` |
| `auto_resize` | Auto-fit to content | `{"dimension", "start"?: int, "end"?: int}` (omit `start`/`end` ⇒ whole sheet) |
| `set_props` | Set pixel size / hide | `{"dimension", "start", "end", "pixelSize"?: int, "hiddenByUser"?: bool}` |
| `read` | Report hidden rows/cols | `{"range"?: A1}` (omit ⇒ whole sheet); returns `{"hiddenRows": [...], "hiddenCols": [...]}` |

`read` is the "what is the viewer actually seeing" companion to filter-view reads — it surfaces
rows/cols hidden via `hiddenByUser`. See `writing.md`.

```sh
gsheets dimensions <YOUR_SPREADSHEET_ID> --action insert --sheet Sheet1 \
  --params-json '{"dimension":"ROWS","start":5,"end":8}'
gsheets dimensions <YOUR_SPREADSHEET_ID> --action auto_resize --sheet Sheet1 \
  --params-json '{"dimension":"COLUMNS"}'
gsheets dimensions <YOUR_SPREADSHEET_ID> --action read --sheet Sheet1
```

### `manage-sheets`

Add / delete / duplicate / rename / reorder tabs. Returns new ids.

```
gsheets manage-sheets <YOUR_SPREADSHEET_ID> --action <ACTION> [--sheet NAME] [--params-json '{...}']
```

`--action` required. `--sheet` names the target tab for delete/duplicate/rename/reorder. Unknown
`params` key → `unknown_param`.

| `--action` | `--sheet` | `--params-json` keys |
|---|---|---|
| `add` | — | `{"title": str, "index": int, "rows": int, "cols": int}` (all optional) |
| `delete` | target tab | — (**destructive** — confirm first) |
| `duplicate` | source tab | `{"newName": str, "newIndex": int}` |
| `rename` | target tab | `{"newName": str}` (**required**) |
| `reorder` | target tab | `{"newIndex": int}` (**required**) |

### `metadata`

Read/write developer metadata — durable key/value anchors on a row/column range, a whole sheet, or
the spreadsheet (survive row/column inserts; better than hard-coded A1 for stable references).

```
gsheets metadata <YOUR_SPREADSHEET_ID> --action {read,create,update,delete} \
  [--key K] [--value V] [--location-json '{...}'] [--visibility DOCUMENT] [--metadata-id N]
```

| Flag | Effect |
|---|---|
| `--action` (required) | `read` (search) / `create` / `update` / `delete`. |
| `--key K` / `--value V` | Metadata key and value. |
| `--location-json` | Anchor — one of: `{"sheet":"S","dimension":"ROWS"|"COLUMNS","start":int,"end":int}`, `{"sheet":"S"}` (whole sheet), or `{}` (spreadsheet). Unknown key → `unknown_param`. |
| `--visibility` | `DOCUMENT` (default) or `PROJECT`. |
| `--metadata-id N` | Target a specific metadata entry (update/delete). |

`delete` is **destructive**.

### `charts`

Create / update / delete / read embedded charts. **v1 scope: `read` returns chart metadata only**
(`chartId`, `title`, `type`, `anchor`) — not the full spec. For full chart-spec fidelity, use
`batch`.

```
gsheets charts <YOUR_SPREADSHEET_ID> --action {create,update,delete,read} \
  [--sheet NAME] [--chart-id N] [--spec-json '{...}']
```

| Flag | Effect |
|---|---|
| `--action` (required) | `create` / `update` / `delete` / `read`. |
| `--sheet NAME` | Target tab (for `read`, and the anchor on create). |
| `--chart-id N` | Target chart (update/delete). |
| `--spec-json` | Flat spec for create/update; unknown key → `unknown_param`. |

`--spec-json` keys (create/update): `{"title": str, "type": "LINE"|"COLUMN"|"BAR"|"PIE"|"SCATTER"|"AREA",
"series": ["Sheet1!B1:B100", ...], "domain": "Sheet1!A1:A100", "anchor": {"sheet": str, "row": int, "col": int}}`.
`delete` is **destructive**.

---

## Escape hatch

### `batch`

Power-user last resort: a raw ordered list of Sheets `batchUpdate` requests, passed straight
through. Use only when no typed command above covers the need (e.g. full chart specs).

```
gsheets batch <YOUR_SPREADSHEET_ID> --requests-json '[{...}, {...}]'
gsheets batch <YOUR_SPREADSHEET_ID> --requests-json @requests.json
```

| Flag | Effect |
|---|---|
| `--requests-json` (**required**) | Raw ordered `requests[]` array, or `@file.json` to read from a file. |

Returns the raw `replies` plus captured `newIds` (sheetIds/chartIds/namedRangeIds).
**Unguarded** — you own request ordering and correctness. Confirm before running.

---

## Auth

### `auth login` / `auth status`

The one adapter-only command family — touches the auth layer, never the Sheets API.

```
gsheets auth login                                 # OAuth consent (or refresh) -> persist token.json
gsheets auth status                                 # report mode/scopes/token state; non-zero if unusable
gsheets --scopes broad auth login                  # --scopes is GLOBAL: it precedes the `auth` subcommand
```

- `auth login` runs the OAuth **desktop** consent flow (the only place interactive consent is
  allowed) or refreshes/validates an existing token, then writes it to `GSHEETS_TOKEN_FILE`. It
  needs `GSHEETS_OAUTH_CLIENT_FILE` only when no usable token exists yet (a present, refreshable
  token needs no client file).
- `auth status` reports the resolved auth mode, scopes, token path, and expiry/refreshability
  (account email only under `GSHEETS_VERBOSE_ERRORS`); exits non-zero when no usable credentials
  resolve. Makes no Sheets call.

Auth env vars (read at runtime, never committed):

| Env var | Meaning |
|---|---|
| `GSHEETS_AUTH_MODE` | `service_account` / `oauth` / `adc` / `auto` (default `auto`). |
| `GSHEETS_SERVICE_ACCOUNT_FILE` | Path to a service-account JSON key. |
| `GSHEETS_OAUTH_CLIENT_FILE` | Desktop OAuth client secrets (only for first-time consent). |
| `GSHEETS_TOKEN_FILE` | Cached authorized-user token (written after consent). |
| `GOOGLE_APPLICATION_CREDENTIALS` | Standard ADC/SA path. |
| `GSHEETS_SCOPES` | `default` / `broad` / explicit comma list. |
| `GSHEETS_CONFIG_DIR` | Override the default `~/.config/google-sheets-mcp/` dir. |
| `GSHEETS_VERBOSE_ERRORS` | `1` lets error hints include the account email (off by default). |

---

## JSON flag values & `@file.json`

Every `--*-json` flag (`--values-json`, `--data-json`, `--rule-json`, `--rules-json`,
`--fmt-json`, `--params-json`, `--location-json`, `--spec-json`, `--requests-json`) takes a JSON
string. Two conveniences:

- Pass `@path/to/file.json` to read the JSON from a file instead of inline (handy for large
  payloads and to keep shell quoting sane).
- Malformed JSON fails fast as an argparse error (exit `2`), before any API call.

Quote inline JSON in single quotes so the shell leaves `"` and `$` alone:

```sh
gsheets write-values <YOUR_SPREADSHEET_ID> 'Sheet1!A1' --values-json '[["=SUM(B:B)"]]'
gsheets batch <YOUR_SPREADSHEET_ID> --requests-json @big_request.json
```
