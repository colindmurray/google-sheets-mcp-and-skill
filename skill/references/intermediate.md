# gsheets — intermediate tier

The intermediate tier (~15% of tasks): operations that enter a workflow only for a specific need —
the structural read, cross-file fan-out, conditional-format and validation writes, comment threads,
the base structural mutations (merge/protect/freeze/group/tab color/named ranges), range data verbs
(find/replace, sort, dedupe, …), row/column dimension ops, and local export. **Read `basic.md`
first** — this assumes you already know the read → write → verify loop, A1 addressing, the global
`--json`/`--scopes` flags (before the subcommand), USER_ENTERED defaults, and the auto field-mask.
`gsheets <cmd> --help` is the authoritative flag source. Examples use `<YOUR_SPREADSHEET_ID>`.

## Reading

### `structure --action read` — the structural picture

One read for everything structural on a sheet: merges, named/protected ranges, frozen panes, row/
column groups, and the per-sheet tables / basic filter / filter views / banded ranges / slicers
overview. Omit `--sheet` for every tab; name one to scope it.

```sh
gsheets --json structure <YOUR_SPREADSHEET_ID> --action read --sheet Sheet1
```

Shape-stable envelope: top-level `namedRanges` (name, range, id); per sheet `merges` (A1 strings),
`frozenRows`, `frozenCols`, `tabColor`, `protectedRanges` (id, range, description, editors,
warningOnly), `dimensionGroups`, plus `tables`, `basicFilter` (one or `null`), `filterViews`
(array), `bandedRanges`, and `slicers`. Each is flattened/terse — never grid data.

The v0.2 keys exist to stop you editing a hidden/filtered row by mistake:

- `tables` — native Tables: `name`, `range`, typed `columns`. Tells you a range's schema and where
  it ends. Terse: `table "Sales" [Sheet1!A1:F500] cols: Region:TEXT, Status:DROPDOWN(Open,Closed)`.
- `basicFilter` / `filterViews` — active filter state; a filter can *hide rows*. Terse:
  `basicFilter [Sheet1!A1:F500] sort C asc | B: hide Closed` and
  `filterView 123 "Open only" [Sheet1!A1:F500] B: hide Closed`.
- `bandedRanges` — alternating-color ranges; a "this rectangle is a deliberate table" hint with
  header/first/second/footer hexes. Terse: `banding 7 [Sheet1!A1:F500] rows: hdr #4285F4 / #FFFFFF`.
- `slicers` — on-grid slicer controls: `slicerId`, `title`, source `range`, filtered `columnIndex`,
  a flattened `anchor` (`{sheet,row,col}`, 0-based, usually a *different* tab than the data range).
  Terse: `slicer 4 "Region" col 0 [Data!A1:F500] @ Dash!I1`.

This is the read side; the base write actions are in [Operations](#operations) and the table/banding/
filter/slicer object CRUD is advanced. Pair with `dimensions --action read` to see what rows/cols a
viewer actually has hidden.

### `read-values` — major dimension & non-A1 addressing

The everyday `read-values` (range list, `--render`) is in `basic.md`. Two intermediate-tier knobs:

**Transpose the grid (`--major`).** Read column-major instead of the default row-major — handy when a
column *is* the record and you want each column as one inner array.

```sh
gsheets read-values <YOUR_SPREADSHEET_ID> 'Sheet1!A1:D100' --major columns
```

`--major {rows,columns}` (default `rows`). MCP names the same knob `major_dimension` (Google's own
`majorDimension` spelling). The result echoes the chosen mode back under `"major"`.

**Address by data filter (`--data-filter-json`).** Instead of A1 `ranges`, select cells by **grid
range** or **developer metadata** — read a named block without knowing its current A1 (rows shift,
the metadata key doesn't). Each selector is **exactly one** of:

```sh
gsheets --json read-values <YOUR_SPREADSHEET_ID> --data-filter-json '[
  {"a1":"Sheet1!A1:B10"},
  {"gridRange":{"sheetId":0,"startRowIndex":0,"endRowIndex":10}},
  {"developerMetadataLookup":{"metadataKey":"block:totals"}}
]'
```

`ranges` and `--data-filter-json` are the **two mutually exclusive addressing paths** — pass exactly
one (CLI accepts `@file.json`; MCP arg is `data_filters`). An invalid selector → `bad_data_filters`.
`describe` takes the same selector grammar; `read-many` accepts per-request `data_filters` in values
mode. Pair developer-metadata addressing with `metadata` (advanced) to set the keys you look up here.

`--max-cells N` caps the read up front: it fails with `result_too_large` **before** returning a
payload, instead of letting an oversized read fail only at the token cap.

### `read-conditional-formats --range` — scope CF reads to a rectangle

`read-conditional-formats` (CF rules with their priority index; in `basic.md`) defaults to every
sheet, or `--sheet NAME` for one tab. `--range` narrows further: only rules **whose range overlaps**
the given A1 rectangle, with their original sheet-level indices preserved (so an index you read here
still addresses the right rule in `set-conditional-format`).

```sh
gsheets --json read-conditional-formats <YOUR_SPREADSHEET_ID> --range 'Sheet1!A1:C50'
```

`--range` carries its own sheet, so it is **mutually exclusive with `--sheet`** (both → `conflicting_args`).

### `read-many` — cross-file fan-out

Read values or summaries across **many spreadsheets** in one call, capturing each file's failure as
a per-entry error instead of aborting the batch. There is **no positional `spreadsheet_id` and no
`--ranges` flag** here — the ids (and per-request ranges) live **inside** `--requests-json`. `--json`
is global, so it goes **before** the subcommand.

```sh
# values across two files (per-request ranges; optional per-request render):
gsheets --json read-many --requests-json '[
  {"spreadsheetId":"<YOUR_SPREADSHEET_ID>","ranges":["Sheet1!A1:B2"]},
  {"spreadsheetId":"<OTHER_ID>","ranges":["Data!A:A"],"render":"unformatted"}
]'

# cheap orientation across a set (no ranges read):
gsheets --json read-many --mode summary --requests-json '[
  {"spreadsheetId":"<YOUR_SPREADSHEET_ID>"},{"spreadsheetId":"<OTHER_ID>"}
]'
```

| Flag | Effect |
|---|---|
| `--requests-json` (**Required**) | `requests[]` (or `@file.json`). Each item is `{"spreadsheetId", "ranges"?, "render"?}`; `render` ∈ `plain`(default)\|`unformatted`\|`formula`\|`all`. |
| `--mode values` (default) | Read each request's `ranges` via `values.batchGet`. `ranges` **required** per request. |
| `--mode summary` | Per-id `overview` (no grid data); `ranges` ignored. |

Envelope: `{ok, mode, count, results}`. A bad id (404, permission, bad range) becomes a
`{"spreadsheetId":…,"ok":false,"error":{…}}` entry in `results` — the other files still read, so a
top-level `ok:true` does **not** mean every file succeeded; check each `results[]` entry's `ok`. A
*malformed* batch (empty list, non-dict item, missing `spreadsheetId`, or a values-mode item missing
`ranges`) fails fast up front as `bad_requests`.

### `formula-patterns` — collapse repeated formulas per column

A wide tracker is mostly **one formula repeated down many rows**. `formula-patterns` reads only
formulas (column-major, no computed bloat) and, per column, dedupes to the distinct templates —
relative row refs normalized to `{r}` / `{r±k}` — with the row span each covers and (by default)
one sample computed value. Token-cheap where a full formula dump would blow the cap.

```sh
gsheets formula-patterns <YOUR_SPREADSHEET_ID> 'Cliff!K1:K200'              # one column
gsheets formula-patterns <YOUR_SPREADSHEET_ID> 'Cliff!A1:CF1' --no-sample   # wide, skip samples
gsheets --json formula-patterns <YOUR_SPREADSHEET_ID> 'Cliff!K1:M200'       # structured shape
```

| Flag | Effect |
|---|---|
| `--no-sample` | Skip the sample computed value (no second FORMATTED pass). |

Shape: `{ok, spreadsheetId, columns:[{col:"Cliff!K", reduced, templates:[{formula:"=SUM(J{r}:R{r})", rows:"3:52", cells:50, sample:{a1:"K3", value:185}}]}]}`. Terse render:

```
Cliff!K  =SUM(J{r}:R{r})        rows 3:52   (50)   K3 -> 185
         =IFERROR(K{r-1}+1,0)   rows 53:55  (3)    K53 -> 0
```

- **Lossy-but-honest.** Normalization is best-effort: absolute (`$`) rows stay verbatim, and a
  column whose formulas do not reduce cleanly is emitted **verbatim** with `reduced:false`. For the
  lossless ground truth use `read-values --render formula` (which renders address-keyed,
  `C5: =SUM(...)`, for sparse formula reads).
- Multi-column AND multi-sheet in one call; columns come back left-to-right in request order. A
  literal-only column has an empty `templates` list.

### `comments --action read` — Drive threaded comments

Review intent often lives in comments, not the grid. Defaults to `--action read`, listing the
spreadsheet's Drive comment threads.

```sh
gsheets comments <YOUR_SPREADSHEET_ID>                    # all comments (resolved included)
gsheets comments <YOUR_SPREADSHEET_ID> --no-resolved      # only open threads
gsheets comments <YOUR_SPREADSHEET_ID> --include-deleted   # also include deleted
```

`--no-resolved` and `--include-deleted` apply to `read` only. Each comment flattens to `id`,
`author`, `content`, `created`, `modified`, `resolved`, `quoted`, `replies[]`, `anchorRaw`. Terse:
`comment AAAA by Jane Doe: "please verify Q3" (open, 1 reply)`.

- **Drive scope required.** Comments use the Drive API (Sheets has no comment surface). `drive.file`
  (the default) reaches files this tool created/opened; for a sheet someone else shared with you,
  run with `--scopes broad` (or `GSHEETS_SCOPES=broad`), else `drive_unavailable`. Applies to every
  comment action, read and write.
- **The `anchor` is opaque** — a document-type-specific blob, not an A1 range. Surfaced as
  `anchorRaw`; comments are document-level and never mapped to a cell.

## Writing

### `set-conditional-format` — index-safe CF writes

Add / update / delete a boolean or gradient rule by **positional index** (array order = priority; no
stable rule id; 0 = highest). Two forms.

```sh
# Single form — add at top, edit at an index, or delete an index:
gsheets set-conditional-format <YOUR_SPREADSHEET_ID> --action add --sheet Sheet1 --index 0 \
  --rule '[Sheet1!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold'
gsheets set-conditional-format <YOUR_SPREADSHEET_ID> --action update --index 2 \
  --rule '[Sheet1!C2:C100] if NUMBER_GREATER(0) -> fg #1B5E20 bold'
gsheets set-conditional-format <YOUR_SPREADSHEET_ID> --action delete --index 5
```

| Flag | Effect |
|---|---|
| `--action {add,update,delete}` | Single-form action. Omit with `--rules-json`. |
| `--sheet NAME` | Target tab. |
| `--index N` | Positional index. **Required** for `update`/`delete`; insert position for `add`. |
| `--rule 'LINE'` | A **body** line (carries no index — addressing is `--index` alone). See the CF line grammar in `basic.md`. |
| `--rule-json '{ranges,kind,condition,format}'` | Structured form instead of a line. |
| `--rules-json '[{"action","index","rule"}]'` | **Batch** form. `rule` omitted for delete. |

`--rule` and `--rule-json` are mutually exclusive (→ `conflicting_args`); the single-form
`--action`/`--index`/`--rule` and `--rules-json` are mutually exclusive too.

**Index ordering is the trap.** A single call mutates exactly one rule, so its ordering is moot. But
**separate single calls are NOT auto-ordered** — deleting/inserting a low index shifts every rule
above it, so mutate **high index → low** yourself, or re-read indices between calls. The
`--rules-json` batch *is* index-safe: core sorts the mutations **descending by index** into one
`batchUpdate`, so earlier edits never shift later targets:

```sh
gsheets set-conditional-format <YOUR_SPREADSHEET_ID> --rules-json '[
  {"action":"delete","index":5},
  {"action":"update","index":2,"rule":"[Sheet1!C2:C100] if NUMBER_GREATER(0) -> fg #1B5E20"},
  {"action":"add","index":0,"rule":"[Sheet1!A2:A100] if BLANK -> bg #ECEFF1"}
]'
```

The high→low guarantee applies **only** inside one `--rules-json` batch. `delete` is **Destructive**.
Read back with `read-conditional-formats` to confirm index/body.

### `set-validation` — round-trip dropdowns & rules

Set or clear data validation on a range. The `ValidationRule` you pass to `--rule-json` is the
**same shape `inspect` reads back** under each cell's `validationRule` — read, edit the dict, write
it straight back.

```sh
# Dropdown of literals:
gsheets set-validation <YOUR_SPREADSHEET_ID> 'Sheet1!C2:C100' \
  --rule-json '{"type":"ONE_OF_LIST","values":["Yes","No"]}'

# Clear validation on a range (OMIT --rule-json):
gsheets set-validation <YOUR_SPREADSHEET_ID> 'Sheet1!C2:C100'
```

| Flag | Effect |
|---|---|
| `RANGE` (positional) | A1 range to validate. |
| `--rule-json '{…}'` | Structured `ValidationRule`. **Omit to CLEAR** validation on the range. |
| `--no-strict` | Allow invalid input (warn instead of reject). Default strict. |
| `--no-dropdown` | Hide the in-cell dropdown chip. Default shows it. |

`ValidationRule` variants:

| Type | Shape |
|---|---|
| List of literals | `{"type":"ONE_OF_LIST","values":["Yes","No"]}` |
| List from a range | `{"type":"ONE_OF_RANGE","source":"Sheet1!Z1:Z10"}` |
| Checkbox | `{"type":"BOOLEAN"}` |
| Number range | `{"type":"NUMBER_BETWEEN","values":[0,100]}` |
| Custom formula | `{"type":"CUSTOM_FORMULA","values":["=ISNUMBER(A1)"]}` |
| Non-blank / blank | `{"type":"NOT_BLANK"}` / `{"type":"BLANK"}` |

`strict`/`showDropdown` may also ride inside `--rule-json` (`"strict"`/`"showDropdown"`); the CLI
flags (`--no-strict`/`--no-dropdown`) win on conflict. Verify with `inspect` and confirm
`validationRule` round-trips.

### `comments` writes — create / reply / resolve / delete

Same Drive-scope rule as the comment read (above): `drive.file` for files this tool created/opened,
else `--scopes broad`, otherwise `drive_unavailable`.

```sh
gsheets comments <YOUR_SPREADSHEET_ID> --action create --content 'please verify Q3 totals'
gsheets comments <YOUR_SPREADSHEET_ID> --action reply --comment-id AAAA --content 'verified'
gsheets comments <YOUR_SPREADSHEET_ID> --action resolve --comment-id AAAA --content 'closing'
gsheets comments <YOUR_SPREADSHEET_ID> --action delete --comment-id AAAA --confirm
```

| `--action` | Needs | Does |
|---|---|---|
| `create` | `--content` (opt. `--anchor`) | Post a top-level comment; returns it under `comment`. |
| `reply` | `--comment-id` + `--content` | Post a reply; returns the flattened reply. |
| `resolve` | `--comment-id` (opt. `--content`) | Resolve by posting a reply with `action:resolve` (Drive has no standalone resolve verb); returns `{commentId, resolved:true, reply}`. |
| `delete` | `--comment-id` + `--confirm` | **Destructive.** Without `--confirm` it refuses with `confirmation_required`; returns `deleted:true`. |

`--anchor` is **opaque** — passed through verbatim on `create`, never an A1 range. `create` returns
the new comment through the read serializer, so `comments --action read` round-trips it.

## Operations

### `structure` — base structural mutations

The base (non-object-CRUD) write actions. `--action` is **Required**; `--sheet` is **Required** for
the sheet-scoped mutating actions (→ `missing_sheet` otherwise). Each action consumes only its listed
`params` keys; an unknown key raises `unknown_param`. New `namedRangeId`/`protectedRangeId` come back
in the result.

| `--action` | Needs | `--params-json` keys |
|---|---|---|
| `merge` | `--range` | `{"mergeType":"MERGE_ALL"\|"MERGE_COLUMNS"\|"MERGE_ROWS"}` (default `MERGE_ALL`) |
| `unmerge` | `--range` | — (**Destructive**) |
| `add_named` | `--range` | `{"name": str}` |
| `delete_named` | — | `{"name": str}` **or** `{"namedRangeId": str}` (**Destructive**) |
| `protect` | `--range` | `{"description": str, "editors": [email,…], "warningOnly": bool}` (all optional) |
| `unprotect` | — | `{"protectedRangeId": int}` (**Destructive**) |
| `freeze` | `--sheet` | `{"rows": int, "cols": int}` (either/both) |
| `tab_color` | `--sheet` | `{"color": "#RRGGBB"\|"theme:NAME"}` |
| `group` | `--sheet` | `{"dimension":"ROWS"\|"COLUMNS", "start": int, "end": int}` (0-based half-open) |
| `ungroup` | `--sheet` | `{"dimension":"ROWS"\|"COLUMNS", "start": int, "end": int}` (**Destructive**) |

```sh
gsheets structure <YOUR_SPREADSHEET_ID> --action merge --sheet Sheet1 --range 'Sheet1!A1:C1'
gsheets structure <YOUR_SPREADSHEET_ID> --action protect --sheet Sheet1 --range 'Sheet1!A1:Z1' \
  --params-json '{"description":"header row","warningOnly":true}'
gsheets structure <YOUR_SPREADSHEET_ID> --action freeze --sheet Sheet1 --params-json '{"rows":1}'
gsheets structure <YOUR_SPREADSHEET_ID> --action tab_color --sheet Sheet1 \
  --params-json '{"color":"theme:ACCENT1"}'
```

Read back with `structure --action read`. The table/banding/filter-view/slicer object CRUD
(`add_table`, `add_banding`, `set_basic_filter`, `add_filter_view`, `add_slicer`, …) is advanced.

### `data-ops` — range data verbs

Range-level data operations, each one `batchUpdate`. One `--action` per call; operands in
`--params-json`. Unknown `params` key → `unknown_param`.

```sh
gsheets data-ops <YOUR_SPREADSHEET_ID> --action <ACTION> --params-json '{…}'
```

| `--action` | What it does | `--params-json` keys |
|---|---|---|
| `find_replace` | Find/replace across a range, a sheet, or all sheets | `{"find": str, "replacement": str, "searchByRegex"?: bool, "matchCase"?: bool, "matchEntireCell"?: bool, "includeFormulas"?: bool, "range"?: A1, "sheet"?: str, "allSheets"?: bool}` — **exactly one** scope of `range`/`sheet`/`allSheets` (else error). (**Destructive**) |
| `delete_duplicates` | Remove duplicate rows | `{"range": A1, "comparisonColumns"?: ["A","C",…]}` (**Destructive**) |
| `trim_whitespace` | Trim leading/trailing whitespace | `{"range": A1}` |
| `sort_range` | Sort by one or more columns | `{"range": A1, "specs": [{"col":"B","order":"ASCENDING"\|"DESCENDING"}, …]}` |
| `text_to_columns` | Split a column on a delimiter | `{"range": A1, "delimiter"?: str, "delimiterType"?: "COMMA"\|"SEMICOLON"\|"PERIOD"\|"SPACE"\|"CUSTOM"\|"AUTODETECT"}` |
| `auto_fill` | Extend a series into adjacent cells | `{"range": A1, "useAlternateSeries"?: bool}` **or** `{"source": A1, "destination": A1}` |
| `copy_paste` | Copy a range to a destination (paste-type aware) | `{"source": A1, "destination": A1, "pasteType"?: PASTE_NORMAL\|PASTE_VALUES\|PASTE_FORMAT\|PASTE_FORMULA\|PASTE_NO_BORDERS\|PASTE_CONDITIONAL_FORMATTING\|PASTE_DATA_VALIDATION, "pasteOrientation"?: "NORMAL"\|"TRANSPOSE"}` |
| `cut_paste` | Move a range to a destination top-left cell | `{"source": A1, "destination": A1, "pasteType"?: …}` (**Destructive**) |

```sh
gsheets data-ops <YOUR_SPREADSHEET_ID> --action find_replace \
  --params-json '{"find":"TODO","replacement":"DONE","sheet":"Sheet1"}'
gsheets data-ops <YOUR_SPREADSHEET_ID> --action find_replace \
  --params-json '{"find":"q[0-9]","replacement":"Q","searchByRegex":true,"allSheets":true}'
gsheets data-ops <YOUR_SPREADSHEET_ID> --action sort_range \
  --params-json '{"range":"Sheet1!A2:D100","specs":[{"col":"B","order":"DESCENDING"}]}'
gsheets data-ops <YOUR_SPREADSHEET_ID> --action copy_paste \
  --params-json '{"source":"Sheet1!A1:D10","destination":"Sheet2!A1","pasteType":"PASTE_VALUES"}'
```

The result echoes an action-specific summary from the API reply: `find_replace` →
`occurrencesChanged`/`valuesChanged`/`formulasChanged`, `delete_duplicates` → `duplicatesRemoved`,
`trim_whitespace` → `cellsChangedCount`, geometry verbs echo what they changed. Read the range first
on the destructive verbs.

### `dimensions` — row & column ops

Insert / delete / move / append rows or columns, auto-fit, set pixel size / hidden, and `read` which
rows/cols are hidden. **`--action` and `--sheet` are both Required** (every action targets one tab).
Spans are 0-based half-open (`start` inclusive, `end` exclusive). Unknown `params` key →
`unknown_param`.

| `--action` | What it does | `--params-json` keys |
|---|---|---|
| `insert` | Insert rows/columns | `{"dimension":"ROWS"\|"COLUMNS", "start": int, "end": int, "inheritFromBefore"?: bool}` |
| `delete` | Delete rows/columns | `{"dimension", "start", "end"}` (**Destructive**) |
| `move` | Move a band | `{"dimension", "start", "end", "destinationIndex": int}` |
| `append` | Append at the end | `{"dimension", "length": int}` |
| `auto_resize` | Auto-fit to content | `{"dimension", "start"?: int, "end"?: int}` (omit span ⇒ whole sheet) |
| `set_props` | Set pixel size / hide | `{"dimension", "start", "end", "pixelSize"?: int, "hiddenByUser"?: bool}` |
| `read` | Report hidden rows/cols | `{"range"?: A1}` (omit ⇒ whole sheet) → `{"hiddenRows":[…], "hiddenCols":[…]}` |

```sh
gsheets dimensions <YOUR_SPREADSHEET_ID> --action insert --sheet Sheet1 \
  --params-json '{"dimension":"ROWS","start":5,"end":8}'
gsheets dimensions <YOUR_SPREADSHEET_ID> --action set_props --sheet Sheet1 \
  --params-json '{"dimension":"COLUMNS","start":2,"end":5,"hiddenByUser":true}'
gsheets dimensions <YOUR_SPREADSHEET_ID> --action auto_resize --sheet Sheet1 \
  --params-json '{"dimension":"COLUMNS"}'
gsheets dimensions <YOUR_SPREADSHEET_ID> --action read --sheet Sheet1
```

`insert`/`append`/`move` shift the rows/cols below them — re-read any A1 references you cached
afterward. `read` is the "what is the viewer actually seeing" companion to a filter-view read.

### `export` — download to a local file

Download to a **local file**; never mutates the spreadsheet. Two backends, picked by `--format`.

```sh
gsheets export <YOUR_SPREADSHEET_ID> --format pdf --path book.pdf
gsheets export <YOUR_SPREADSHEET_ID> --format csv --sheet Sheet1 --path sheet1.csv
```

| Flag | Effect |
|---|---|
| `--format pdf` (default) `/ xlsx / ods` | Whole-workbook render via Drive `files.export`. `--sheet` is **ignored**. Needs a Drive scope; the default `drive.file` covers files this tool created or opened — only a sheet you merely have a link to needs `--scopes broad`. No Drive service at all → `drive_unavailable`. |
| `--format csv / tsv` | One named `--sheet`, read via the Sheets API and serialized locally (Drive's csv export only emits the first sheet, so this avoids Drive). `--sheet` is **Required** (omit ⇒ `missing_sheet`). Sheets scope only. |
| `--path P` | Output path. Defaults to `<spreadsheetId>.<format>` in the cwd. |
| `--sheet S` | Required for csv/tsv; ignored for pdf/xlsx/ods. |

Returns `{format, mimeType, path, bytes}` (terse: `exported <format> -> <path> (<n> bytes)`). Verify
by the returned `path`/`bytes`, not by re-reading the sheet.

---

For the everyday read → write → verify loop (overview, inspect, read-values, write-values,
append-rows, clear, format, manage-sheets) see `basic.md`. For the fringe ~5% — table/banding/
filter-view/slicer object CRUD, developer `metadata`, `charts`, the raw `batch` escape hatch, and the
`--rich-text` / `--pivot` reads — see `advanced.md`. `gsheets <cmd> --help` is the authoritative
flag source for any command.
