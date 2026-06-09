# gsheets — advanced tier (~5%)

The fringe. This tier covers the per-cell deep reads (`inspect --rich-text`, `inspect --pivot`) and
the advanced structural object CRUD (native Tables, banding, basic filter / filter views, slicers,
spreadsheet props), plus developer metadata, charts, and the raw `batch` escape hatch. **Almost no
task needs this** — most stop at `basic.md` (read → write → verify) or `intermediate.md` (CF, data
validation, structure base actions, data-ops, dimensions, export). Reach here only when a specific
sheet uses one of these niche objects. **Read `basic.md` and `intermediate.md` first** — this tier
assumes their commands, the `--json`-before-subcommand rule, the auto field-mask, A1 addressing, and
the read-it-back loop. Examples use `<YOUR_SPREADSHEET_ID>`.

## Reading

Two opt-in additive per-cell reads on `inspect`. Both default off (zero token cost) and attach data
**per-cell only when present**, so you pay only for the cells that actually carry runs/links/pivots.

### `inspect --rich-text` — per-run styled segments + in-cell links

One cell can hold multiple styled segments and multiple links (`"See A then B"` where `A` and `B`
link to different URLs). A plain read flattens that to one value and loses the per-segment styling
and the individual links. `--rich-text` recovers it — and it is the **only** way to read a
multi-link cell.

```sh
gsheets --json inspect <YOUR_SPREADSHEET_ID> 'Sheet1!A1:A20' --rich-text
```

A cell with `textFormatRuns` gains a `runs` array; a cell with a single whole-cell link gains a flat
`hyperlink`:

```jsonc
{ "a1": "A2", "value": "Click here then plain",
  "runs": [ { "start": 0,  "text": "Click here",  "format": { "bold": true, "fg": "#1155CC" }, "link": "https://x" },
            { "start": 10, "text": " then plain" } ],
  "hyperlink": "https://x" }           // present only for a single whole-cell link
```

- `start` is the 0-based char offset where the run begins; `text` is the substring up to the next
  run's start (or end of text). The run's flattened text styling lives under a nested `format`
  object (`bold`/`italic`/`underline`/`strike`/`fontSize`/`fontFamily`/`fg`), **not** at the run top
  level — `run["format"]["bold"]`, not `run["bold"]`. `format` is omitted when the run carries no
  styling.
- A **run-level `link` takes precedence over the cell `hyperlink`.** For a multi-link cell the flat
  `hyperlink` is absent (Google leaves it empty) and each link lives on its run — iterate `runs` to
  recover every link.
- Terse line (one per cell with runs):
  `runs A2: "Click here"[0:10 bold fg #1155CC link https://x] + " then plain"[10:21]`.
- `hyperlink` is a **read-only** Google field. You set an in-cell link by writing a `=HYPERLINK(...)`
  formula via `write-values` (see `basic.md`), never by writing `hyperlink` back.

In `--compact` mode, two cells differing in `runs`/`hyperlink` never merge into one run, and a run
carries `runs`/`hyperlink` when present.

### `inspect --pivot` — pivot-table definitions

A pivot table's definition lives on its **anchor (top-left) cell only**. `--pivot` surfaces it so
you can see what a generated block is (and avoid overwriting it):

```sh
gsheets --json inspect <YOUR_SPREADSHEET_ID> 'Sheet1!A1:H40' --pivot
```

The anchor cell gains a `pivot` object (read-only; present only on the anchor):

```jsonc
{ "a1": "A1",
  "pivot": { "source": "Data!A1:F500",
             "rows":    [ { "field": "Region",  "sourceColumnOffset": 0, "showTotals": true, "sortOrder": "ASCENDING" } ],
             "columns": [ { "field": "Quarter", "sourceColumnOffset": 2, "showTotals": true } ],
             "values":  [ { "name": "Sum of Sales", "sourceColumnOffset": 4, "summarize": "SUM" } ],
             "filters": [ { "sourceColumnOffset": 1, "visibleValues": ["X","Y"] } ],
             "valueLayout": "HORIZONTAL",
             "line": "pivot <- Data!A1:F500 | rows: Region | cols: Quarter | values: SUM(Sales)" } }
```

- The pivot object carries its own terse `line`. `source` resolves the pivot's GridRange to A1;
  `rows`/`columns`/`values`/`filters` are omitted when empty; `valueLayout` always present
  (default `HORIZONTAL`).
- Pivot **write** is not a typed command — it stays in the `batch` escape hatch (read-only here).

## Operations

All of the structural-object CRUD below runs through `structure --action <ACTION> --params-json
'{...}'`. `--action` is **required**; sheet-scoped mutating actions need `--sheet` (→ `missing_sheet`
otherwise); `spreadsheet_props` is spreadsheet-scoped and needs no `--sheet`. Each action consumes
only its listed `params` keys — an unknown key raises `unknown_param`. Read everything back with
`structure --action read` (see `intermediate.md` for that envelope's `tables`/`basicFilter`/
`filterViews`/`bandedRanges`/`slicers` shapes). `--params-json` accepts `@file.json`.

### Native Tables

A native Sheets Table (2024 GA) gives a range a `name`, a typed column schema, and a known end (so
you append safely). `add_table` needs `--range`; columns are typed.

```sh
gsheets structure <YOUR_SPREADSHEET_ID> --action add_table --range 'Sheet1!A1:F500' \
  --params-json '{"name":"Sales","columns":[
    {"name":"Region","type":"TEXT"},
    {"name":"Status","type":"DROPDOWN","validation":{"type":"ONE_OF_LIST","values":["Open","Closed"]}}]}'
#   -> { "ok": true, "action": "add_table", "tableId": "<new id>" }
```

- Column `type` ∈ `TEXT` / `DOUBLE` / `CURRENCY` / `PERCENT` / `DATE` / `TIME` / `DATETIME` /
  `DROPDOWN` / `CHECKBOX` / `SMART_CHIP` / `RATING`.
- **Required:** a `DROPDOWN` column must carry a `ONE_OF_LIST` `validation` (the same structured
  `ValidationRule` shape `set-validation` uses) — omitting it raises `bad_table`.
- `update_table` takes `{"tableId","name"?,"columns"?,"range"?}` (auto field-mask; `tableId`-only
  raises `empty_payload`). `delete_table` takes `{"tableId"}` and is **destructive**.

```sh
gsheets structure <YOUR_SPREADSHEET_ID> --action update_table \
  --params-json '{"tableId":"<id>","name":"Sales 2026"}'
gsheets structure <YOUR_SPREADSHEET_ID> --action delete_table \
  --params-json '{"tableId":"<id>"}'
```

### Banding

Alternating-color (banded) ranges — a "this rectangle is a deliberate table" hint. `add_banding`
needs `--range`; supply at least one of `rowBanding` / `columnBanding`, each a `{header, first,
second, footer}` hex map (any slot optional, but at least one group must be present).

```sh
gsheets structure <YOUR_SPREADSHEET_ID> --action add_banding --range 'Sheet1!A1:F500' \
  --params-json '{"rowBanding":{"header":"#4285F4","first":"#FFFFFF","second":"#E8F0FE"}}'
#   -> { "ok": true, "action": "add_banding", "bandedRangeId": <new id> }
```

- `update_banding` takes `{"bandedRangeId","rowBanding"?,"columnBanding"?,"range"?}` (auto
  field-mask; a partial color update masks down to the single changed band, leaving the rest;
  `bandedRangeId`-only raises `empty_payload`). `delete_banding` takes `{"bandedRangeId"}` and is
  **destructive**.
- Reads back as `banding 7 [Sheet1!A1:F500] rows: hdr #4285F4 / #FFFFFF / #E8F0FE` (header/footer
  labelled `hdr`/`ftr`; body bands bare). Unset slots read as `null`.

### Basic filter & filter views

The sheet's one **basic filter** (sort + per-column hidden values) vs. named, non-destructive
**filter views**. `set_basic_filter` / `add_filter_view` need `--range`; `clear_basic_filter` needs
`--sheet`.

```sh
# Set the sheet's basic filter (sort + per-column hidden values / condition):
gsheets structure <YOUR_SPREADSHEET_ID> --action set_basic_filter --range 'Sheet1!A1:F500' \
  --params-json '{"sorted":[{"col":"C","order":"ASCENDING"}],"criteria":[{"col":"B","hidden":["Closed"]}]}'

# Add a named filter view (returns filterViewId):
gsheets structure <YOUR_SPREADSHEET_ID> --action add_filter_view --range 'Sheet1!A1:F500' \
  --params-json '{"title":"Open only","criteria":[{"col":"B","hidden":["Closed"]}]}'
```

- A per-column `criteria` entry is `{"col": "<letter>", "hidden"?: [...], "condition"?: "NUMBER_GREATER(0)"}`
  — the `condition` reuses the same terse grammar as conditional formats.
- `clear_basic_filter` (`--sheet`, no params) is **destructive**. `update_filter_view` takes
  `{"filterViewId","title"?,"range"?,"sorted"?,"criteria"?}`; `delete_filter_view` takes
  `{"filterViewId"}` and is **destructive**.
- A filtered table *hides rows*. Read `basicFilter` / `filterViews` (via `structure --action read`)
  or `dimensions --action read` before editing so you don't write the wrong row.

### Slicers

A slicer is an on-grid filter control: it points at a **data range**, filters one of its columns,
and is anchored at a single **anchor** cell (usually on a *different* tab from the data).

```sh
# Add a slicer over a data range, anchored at Dash!I1, filtering column 0:
gsheets structure <YOUR_SPREADSHEET_ID> --action add_slicer --sheet Dash --range 'Data!A1:F500' \
  --params-json '{"title":"Region","columnIndex":0,"anchor":"Dash!I1"}'
#   -> { "ok": true, "action": "add_slicer", "slicerId": <new id> }
```

- **Data range:** the top-level `--range` *or* `params.dataRange` (top-level `--range` wins when both
  given); one of them is **required** (→ `bad_range`). **`anchor`** (a single A1 cell) is
  **required** in params (a multi-cell range is rejected).
- `columnIndex` is the 0-based offset (into the data range) of the filtered column. `criteria` is
  `{"hidden"?, "visible"?, "condition"?}` (`condition` reuses the CF/filter condition grammar).
- `update_slicer` takes `{"slicerId","title"?,"dataRange"?,"columnIndex"?,"criteria"?}` (auto
  field-mask; the **anchor is immutable**). `delete_slicer` takes `{"slicerId"}` and is
  **destructive** (it maps to `deleteEmbeddedObject` — slicers share the embedded-object id space
  with charts).
- Reads back as `slicer 4 "Region" col 0 [Data!A1:F500] @ Dash!I1` (` -> <criterion>` appended when
  a criterion is set). The anchor is flattened to `{sheet, row, col}` (0-based). A `GridCoordinate`
  **omits a 0 index**, so an absent `row`/`col` reads back as 0 — a top-left/row-0 anchor still
  renders (an anchor the API returned as `{columnIndex: 4}` reads as `{row: 0, col: 4}` →
  `@ Sheet!E1`).

### Spreadsheet props

The spreadsheet-scoped write of title / locale / timeZone — the write side of `overview`'s
`locale` / `timeZone`. **No `--sheet`.**

```sh
gsheets structure <YOUR_SPREADSHEET_ID> --action spreadsheet_props \
  --params-json '{"locale":"en_US","timeZone":"America/New_York"}'
```

Params `{"title"?, "locale"?, "timeZone"?}` (auto field-mask; an empty payload raises
`empty_payload`). The result echoes the properties you set. Verify with `overview`.

### Developer metadata

Durable key/value anchors on a row/column dimension range, a whole sheet, or the spreadsheet. They
**survive row/column inserts**, so they beat hard-coded A1 for stable references. Its own subcommand,
not `structure`.

```sh
# Create a metadata anchor on rows 10–11 (0-based half-open) of Sheet1:
gsheets metadata <YOUR_SPREADSHEET_ID> --action create --key "section" --value "summary" \
  --location-json '{"sheet":"Sheet1","dimension":"ROWS","start":10,"end":11}'

# Read (search) all metadata, or by key:
gsheets metadata <YOUR_SPREADSHEET_ID> --action read --key "section"

# Update / delete a specific entry by id:
gsheets metadata <YOUR_SPREADSHEET_ID> --action update --metadata-id 7 --value "totals"
gsheets metadata <YOUR_SPREADSHEET_ID> --action delete --metadata-id 7
```

- `--location-json` anchor forms: a dimension anchor
  `{"sheet","dimension":"ROWS"|"COLUMNS","start","end"}` (all four required together), a whole-sheet
  anchor `{"sheet"}`, or a spreadsheet anchor `{}`. Unknown key → `unknown_param`.
- `--visibility` is `DOCUMENT` (default) or `PROJECT`.
- `read` returns `{"ok": true, "action": "read", "metadata": [...]}`; `create` captures the assigned
  `metadataId`. `delete` is **destructive**.

### Charts

Create / update / delete / read embedded charts. **v1 scope: `read` returns metadata only**
(`chartId`, `title`, `type`, `anchor:{sheet,row,col}`) — not the full spec.

```sh
gsheets charts <YOUR_SPREADSHEET_ID> --action create --sheet Sheet1 \
  --spec-json '{"type":"LINE","title":"Trend","series":["Sheet1!B1:B100"],"domain":"Sheet1!A1:A100",
                "anchor":{"sheet":"Sheet1","row":0,"col":6}}'
gsheets charts <YOUR_SPREADSHEET_ID> --action read --sheet Sheet1
gsheets charts <YOUR_SPREADSHEET_ID> --action delete --chart-id 12345
```

- `--spec-json` keys (create/update): `{"title","type","series","domain","anchor"}`; `type` ∈
  `LINE`/`COLUMN`/`BAR`/`PIE`/`SCATTER`/`AREA`. Unknown key → `unknown_param`. `create` captures the
  new `chartId`. `delete` is **destructive**.
- **Read-back gap:** `charts --action read` is metadata-only. For full chart-spec fidelity (any
  property beyond title/type/anchor — axes, colors, the rich series union), read and write via
  `batch` (raw `addChart` / `updateChartSpec`).

### `batch` — the raw batchUpdate escape hatch

A raw ordered list of Sheets `batchUpdate` requests passed straight through. Use only when **no
typed command above covers the need** — e.g. full chart specs, pivot-table writes, Connected Sheets
/ data sources (all batch-only). You own request ordering and correctness.

```sh
gsheets batch <YOUR_SPREADSHEET_ID> --requests-json '[{...}, {...}]'
gsheets batch <YOUR_SPREADSHEET_ID> --requests-json @requests.json
```

- `--requests-json` (**required**) is the raw ordered `requests[]` array (or `@file.json`). Order is
  preserved exactly — core never sorts or rewrites. An empty list raises `empty_payload`.
- Returns `{"ok": true, "replies": [...], "newIds": {...}}`. `newIds` buckets the ids the API only
  surfaces in `replies[]` (`sheetIds` / `chartIds` / `namedRangeIds` / `protectedRangeIds` /
  `metadataIds` / `tableIds` / `bandedRangeIds` / `filterViewIds` / `slicerIds`, each a list) —
  capture them so a create-then-populate flow stays one batch.
- **Unguarded and powerful** — confirm before running, and prefer a typed command when one exists
  (the typed paths auto-build masks, capture ids, and resolve A1 for you).

## Pointers

- Stop one tier down for anything routine: `basic.md` (read → write → verify) and `intermediate.md`
  (conditional formatting, data validation, base `structure` actions, `data-ops`, `dimensions`,
  `comments`, `read-many`, `export`).
- For the structural-read envelope these objects round-trip through, see `intermediate.md`'s
  `structure --action read` section.
- `gsheets <cmd> --help` is the authoritative, always-current source for the exact flags of any
  command.
