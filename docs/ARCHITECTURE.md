# Architecture

This document describes how `google-sheets-mcp-and-skill` is structured and why. It is
the public-facing version of the project's design contract. If you are contributing
code, read this alongside [`CONTRIBUTING.md`](../CONTRIBUTING.md).

## Thesis

An AI understands an existing, heavily-formatted spreadsheet primarily by **reading**
it: its formulas, its cell formatting, and especially its **conditional-format rules**.
Screenshots are lossy; reading the actual formulas + formats + rules is lossless and is
the highest-signal way to build a correct mental model of a live sheet. So the design
bias is **read-side richness**, delivered token-efficiently, with full CRUD symmetry
(anything writable is readable back).

Five invariants follow from that thesis and are not negotiable:

1. **Shared pure core, two thin adapters.** One implementation of every behavior; the
   MCP server and the CLI map 1:1 to it; behavior is identical from either entrypoint.
2. **Read-side richness.** Values **and** formulas side by side; both
   `userEnteredFormat` (intent) and `effectiveFormat` (what renders, including
   conditional results); conditional-format rules serialized to terse, round-trippable
   lines.
3. **Token efficiency.** Never `includeGridData`. Always `ranges[]` + a tight `fields`
   mask. Offer compact reads. Flatten Google's nested objects.
4. **Full CRUD symmetry.** If you can write it, you can read it back.
5. **Safe write defaults.** `USER_ENTERED` by default; every formatting/properties write
   auto-builds its `fields` mask from the payload.

---

## Layers

```
                ┌──────────────────────┐      ┌──────────────────────┐
                │   MCP adapter        │      │   CLI adapter        │
                │   mcp_server.py      │      │   cli.py             │
                │   (FastMCP, stdio)   │      │   (argparse)         │
                │   + models.py        │      │                      │
                └──────────┬───────────┘      └──────────┬───────────┘
                           │  one-line tool/subcommand bodies
                           │  (resolve services → call core → return)
                           ▼                             ▼
                ┌─────────────────────────────────────────────────────┐
                │                  PURE CORE  (gsheets.core)           │
                │  values · reads · formula_patterns · formatting ·    │
                │  rules · structure · charts · batch · data_ops ·     │
                │  dimensions · comments · export · multiread          │
                │    +   helpers:                                      │
                │  addressing · colors · fieldsmask · flatten ·        │
                │  condformat · dataselector · errors · service ·      │
                │  format · paths · retry   +                          │
                │                          serializers:                │
                │  richtext · pivot · tables · filters · banding ·     │
                │  slicers · comments                                  │
                │                                                      │
                │  ZERO imports of fastmcp / mcp / argparse / pydantic │
                └───────────────────────┬─────────────────────────────┘
                                        │ receives a SheetsServices handle
                                        ▼
                ┌─────────────────────────────────────────────────────┐
                │                  AUTH  (gsheets.auth)                │
                │  resolve credentials (SA · OAuth desktop · ADC) →    │
                │  build_services() → SheetsServices                   │
                │  reads ONLY env vars; least-privilege scopes         │
                └───────────────────────┬─────────────────────────────┘
                                        ▼
                          Google Sheets API v4  (+ optional Drive v3)
```

### Core (`gsheets.core`) — pure

The core is a pure library. It imports **only** the standard library plus
`googleapiclient` / `google.auth*`. It must never import `fastmcp`, `mcp`, `argparse`,
`pydantic`, or `gsheets.models`. This boundary is enforced by a test that runs in a
fresh subprocess and asserts `import gsheets.core` (and `import gsheets.auth`) pulls none
of those into `sys.modules`.

Every core function:

- takes `services: SheetsServices` (the authed handle) as its first parameter and
  `spreadsheet_id` as its second;
- accepts A1 ranges everywhere and resolves sheet name → `sheetId` and A1 → `GridRange`
  internally — callers never fetch a `sheetId` first;
- returns plain JSON-serializable `dict` / `list`, with `"ok": True` on success;
- raises `SheetsError` on failure (it never returns an error dict).

Helper modules with disjoint responsibilities back the function surface:

| Helper | Responsibility |
|---|---|
| `service` | `SheetsServices` — the frozen authed handle (sheets resource, optional drive resource, optional account email). |
| `errors` | `SheetsError` + `classify_google_error()` — maps a Google `HttpError` to a coded, hinted error. |
| `addressing` | A1 ↔ `GridRange` and sheet-name → `sheetId` (per-call cached); `gridranges_intersect()` — the geometric overlap test `describe` uses to scope conditional-format rules to a requested range. |
| `colors` | hex ↔ `ColorStyle` (`rgbColor` / `themeColor`); reads flatten to hex. |
| `fieldsmask` | `build_fields_mask(payload)` — the minimal `fields` mask covering exactly the keys present. |
| `flatten` | `flatten_cell_format()` — Google's nested `CellFormat` → flat shape. |
| `condformat` | (de)serialize conditional-format rules ↔ readable lines (see grammar below). |
| `dataselector` | `build_data_filters()` — validate + resolve the `data_filters` selector grammar (metadata-/grid-/A1-addressed reads); see below. |
| `format` | `render()` / `render_grid` / `render_kv` / `render_addressed` — the shared pure output-format layer (json/jsonl/csv/tsv/markdown + address-keyed sparse rendering); see below. |
| `paths` | `resolve_out_path()` + `write_file_handle()` — the MCP `out_path` safety check and file-output handle; see below. |
| `retry` | `RetryPolicy` (the frozen policy dataclass) + `execute_with_retry()` + a `_ACTIVE_POLICY` contextvar with `current_policy()` / `activate()` — the pure retry/backoff mechanism, off by default; see below. |

Seven additional pure serializer modules (added in v0.2) flatten the richer read surface
into the same terse, structured, round-trippable line style. Each takes already-resolved
A1 strings (the owning read function resolves `GridRange → A1` first, mirroring the
condformat boundary) and returns plain dicts carrying a `line` field:

| Serializer | Responsibility |
|---|---|
| `richtext` | `serialize_text_runs()` — per-run styled segments (`textFormatRuns`) + in-cell links; `text_runs_line()` renders the terse `runs A1: …` line. |
| `pivot` | `serialize_pivot()` — flatten a `PivotTable` definition (source, rows/cols/values/filters) into a flat dict + terse line. |
| `tables` | `serialize_table()` + `build_{add,update,delete}_table_request()` — native Sheets `Table` read shape and the write requests. |
| `filters` | `serialize_basic_filter()` / `serialize_filter_view()` + `build_*` filter requests — basic-filter and filter-view state and writes. |
| `banding` | `serialize_banding()` + `build_{add,update,delete}_banding_request()` — alternating-color band ranges. |
| `slicers` | `serialize_slicer()` — slicer read shape — plus `build_{add,update,delete}_slicer_request()` for the slicer write CRUD. |
| `comments` | `serialize_comment()` — flatten a Drive `Comment` (author, content, replies); the `comments` core function (full CRUD over the Drive API) lives here too. |

Five additional top-level core functions live in their own modules and stay boundary-pure:

- `data_ops` (`core/dataops.py`) and `dimensions` (`core/dimensions.py`) are single-dispatch
  wrappers over the one-request `batchUpdate` data and dimension verbs.
- `comments` (`core/comments.py`) is full CRUD over Drive threaded comments (it does not touch
  the Sheets API).
- `export` (`core/export.py`) downloads a workbook (Drive) or a single sheet (Sheets) to a local
  file.
- `read_many` (`core/multiread.py`) fans a read across many spreadsheets, capturing per-file
  errors.

One boundary-sensitive detail lives in `export`. The Drive-backed formats stream bytes down with
`MediaIoBaseDownload` from `googleapiclient.http`, which transitively imports `httplib2` →
`argparse` — a module the pure-core guard forbids from a clean `import gsheets.core`. Since
`export` is re-exported from `gsheets.core`, a top-level import would leak `argparse` into the
package import, so `MediaIoBaseDownload` is bound lazily inside `_export_via_drive` (the
module-level name stays a monkeypatch seam for tests). It is the only place core imports anything
beyond module top.

### Auth (`gsheets.auth`) — credential resolution

The auth layer is the only place credentials are resolved. It reads **only** environment
variables (never hardcoded paths or IDs), supports three credential sources with a
least-privilege scope default, builds the Google API service objects, and hands a
`SheetsServices` to core. Core never resolves credentials itself.

Resolution order (under `auto`): **Service Account** → **OAuth 2.0 Desktop** → **ADC**.
The OAuth desktop path distinguishes two states: a present, refreshable token (no client
file needed) versus first-time consent (client file required). Interactive consent is a
**CLI-only** path; the MCP server never runs a browser prompt during its stdio lifespan
and instead requires a pre-existing valid/refreshable token.

Scopes default to the narrowest that work (`spreadsheets` + `drive.file`), with an
opt-in broad mode.

**Scope reconciliation for cached OAuth tokens.** A cached token is refreshed against
the scopes it was **originally granted**, never against whatever `GSHEETS_SCOPES`
currently asks for. Google's refresh grant rejects (with `invalid_scope`) any refresh
whose scope list isn't a subset of the original consent, so a token consented with the
broad `drive` scope would fail a refresh that requested the narrow `drive.file` — even
though `drive` is functionally broader. The resolver therefore (1) loads the token
without forcing the requested scope list onto it, letting the refresh re-grant against
the token's own scopes, then (2) checks that the granted scopes **cover** what the
current request needs (broad `drive` covers `drive.file`). If the grant doesn't cover
the request, the call fails with a clear `oauth_scope_insufficient` error telling the
user to re-run `gsheets auth login` with the scopes they need.

### Adapters — thin

Two adapters wrap the core, each mapping 1:1 to the core function surface:

- **MCP server** (`mcp_server.py`) — a FastMCP stdio server. It is the **only** module
  that imports `fastmcp` / `mcp` and the **only** one that imports `gsheets.models`. It
  builds `SheetsServices` once in its lifespan, registers one tool per core function
  (one-line bodies), attaches `ToolAnnotations`, honors an `ENABLED_TOOLS` allowlist, and
  surfaces errors through a single tool-error envelope. It never prints to stdout (that
  is the JSON-RPC channel).
- **CLI** (`cli.py`) — an `argparse` adapter. It is the **only** module that imports
  `argparse`. It exposes one subcommand per core function (flags map 1:1 to core kwargs),
  a global `--json`, and one auth-only `auth login | status` subcommand that touches the
  auth layer (not the Sheets core). It catches `SheetsError` at the top of `main()` and
  prints a clean envelope to stderr.

`models.py` (adapter-side only) holds Pydantic models that mirror each core return dict
field-for-field, giving the MCP server its output schema / structured content plus a
terse text rendering. The models are mechanical mirrors — adding a core field means
adding a model field, never reshaping.

---

## Data flow

A typical read (`inspect`) flows like this:

1. The user invokes a tool (MCP) or subcommand (CLI) with an A1 range and flags.
2. The adapter resolves the shared `SheetsServices` and calls the matching core function
   in one line.
3. Core resolves the sheet name / A1 range to the internal `GridRange`, issues a single
   `spreadsheets.get` with a **tight `fields` mask** (never `includeGridData`), trimmed
   further by the include flags.
4. Core **flattens** the nested Google response (colors → hex, `textFormat.bold` →
   `bold`, number format → pattern + type, borders → `"<style> <hex>"`), pads jagged
   arrays to a rectangle, and — when `compact=True` — collapses identical cells into
   rectangular runs.
5. Core returns a plain dict with `"ok": True`.
6. The MCP adapter wraps it in the mirror Pydantic model (structured content + terse
   text); the CLI prints it as JSON (`--json`) or terse text.

A typical write (`format`) flows the same way in reverse: the adapter passes a flat
payload to core; core translates flat → Google request shape, **auto-builds the `fields`
mask from the payload** (so unspecified subfields are never wiped and the write is never
a silent no-op), and issues a single `batchUpdate`. Writes default to `USER_ENTERED` so
formulas are interpreted rather than stored as literal text.

Two write-side subtleties worth knowing:

- **Auto fields mask.** A formatting/properties write must carry an exact `fields` mask
  or it no-ops or wipes unspecified subfields. Core derives the mask from the payload.
  Some Google sub-objects are **atomic leaves** — masked at the parent, never recursed
  into (`*ColorStyle`, `numberFormat`, `padding`, `textRotation`). `textFormat` is *not*
  atomic (its children mask individually, e.g. `textFormat.bold`).
- **Conditional-format addressing.** A conditional-format rule has no stable id; its
  position in the per-sheet `conditionalFormats[]` array **is** its priority (index 0 =
  highest). Writes address a rule by positional index. When several rule mutations are
  issued in one batch, core orders them **high index → low** so earlier edits do not
  shift the array position of later targets.

---

## The function surface

Twenty-two core functions, each exposed as one MCP tool and one CLI subcommand (the CLI adds an
auth-only `auth` subcommand that has no core function). The understanding path is
`overview → describe → inspect → read_conditional_formats` (`describe` is the unified one-call
region read that subsumes the latter three for a single region), with `formula_patterns` as the
token-cheap "what's the formula logic across this wide grid" read; the change path is the writers;
the raw escape hatch is presented last.

| Core fn | What it does | Kind |
|---|---|---|
| `overview` | Cheap orientation snapshot: title, tabs (dimensions, frozen, counts), named ranges, spreadsheet `locale`/`timeZone`. No grid data. | read |
| `inspect` | The primary rich read: values + formulas + both formats + merges + validation over a tight `fields` mask; optional compact runs; opt-in rich-text runs + in-cell links (`include_rich_text`) and pivot-table definitions (`include_pivot`). | read |
| `describe` | The unified "understand a region" read: ONE `spreadsheets.get(includeGridData=True)` over a tight union mask returns, per requested range, the cells (reusing `inspect`'s flatten), the sheet's merges, the conditional-format rules **intersecting** that range (range-scoped CF, via `addressing.gridranges_intersect`), its tables / banding / protected ranges (reusing the `structure` serializers), and a validation summary. Multi-range and multi-sheet; collapses 3-4 reads into one. No cache. | read |
| `formula_patterns` | Collapse a region's REPEATED formulas to the distinct templates per column: reads only formulas (column-major, no computed bloat), dedupes each column to its templates with relative row refs normalized to `{r}` / `{r±k}`, the row span(s) each covers, and (by default) one sample computed value. A column that does not reduce cleanly is emitted VERBATIM with `reduced=false`; `read_values(render="formula")` stays the lossless ground truth. A bounded / whole-column range returns exactly **one entry per requested column** — trailing all-blank columns (which the API omits) are padded as `{reduced:true, templates:[]}`; only an unbounded-column range (whole-row / whole-sheet) keeps the data-extent count (ISSUES.md #16). Lossy-but-honest, token-cheap on a wide grid. | read |
| `read_values` | Values for one/more ranges with a render mode (`plain` / `unformatted` / `formula` / `all`). `diff_only` sparsifies the `render="all"` `computed` matrix against `values` (drops static-cell duplication); `max_cells` fails fast with `result_too_large` instead of blowing the caller's token cap. | read |
| `read_conditional_formats` | Per-sheet conditional-format rules serialized to readable lines (the priority feature). | read |
| `write_values` | Write/update one or more ranges; `USER_ENTERED` default; multi-range in one call. | write |
| `append_rows` | Append after the last row of a table (`INSERT_ROWS`, no overwrite). | write |
| `clear` | Clear values, and optionally formats / validation / notes, from ranges. | write |
| `format` | Apply cell formatting (background, font, number/date pattern, alignment, wrap, padding, borders, note) atomically with an auto fields mask. | write |
| `set_conditional_format` | Add / update / delete a boolean or gradient rule by positional index; index-shift-safe batch form. | write |
| `set_validation` | Set / clear data validation on a range (structured rule, round-trips from `inspect`). | write |
| `structure` | Read or modify merges, named ranges, protected ranges, frozen rows/cols, tab color, dimension groups — plus the v0.2 reads (native tables, basic filter, filter views, banding, slicers) and writes (table / banding / filter / slicer CRUD, spreadsheet `title`/`locale`/`timeZone`). One structural interface. | read/write |
| `manage_sheets` | Add / delete / duplicate / rename / reorder tabs; returns new ids. | write |
| `metadata` | Read / write developer metadata for durable row/column/sheet anchors. | read/write |
| `charts` | Create / update / delete / read embedded charts (read returns metadata only in v1). | read/write |
| `data_ops` | Single-request data verbs: `find_replace`, `delete_duplicates`, `trim_whitespace`, `sort_range`, `text_to_columns`, `auto_fill`, `copy_paste`, `cut_paste`. | write |
| `dimensions` | Row/column ops: `insert` / `delete` / `move` / `append`, `auto_resize`, `set_props` (height/width/hide), and `read` (which rows/cols are hidden). | read/write |
| `comments` | Drive threaded comments on the spreadsheet file (author, content, replies, resolved state). Full CRUD via an `action` dispatch (`read` / `create` / `reply` / `resolve` / `delete`); uses the Drive API, not the Sheets API. | read/write |
| `export` | Download the workbook (`pdf` / `xlsx` / `ods`, via Drive `files.export`) or one sheet (`csv` / `tsv`, serialized locally from values) to a local file. | read |
| `read_many` | Fan a values or summary read across many spreadsheets; a bad id is captured as a per-file `{ok: false, error}` entry instead of failing the batch. | read |
| `batch` | Power-user escape hatch: a raw ordered list of `batchUpdate` requests. | write |

The structure read and the conditional-format read share a **shape-stable multi-sheet
envelope**: top-level spreadsheet-scoped fields plus a `sheets: [...]` list that is
always a list (one entry for one sheet, every tab when unscoped), so consumers never fork
on object-vs-list. The v0.2 structural reads (`tables`, `basicFilter`, `filterViews`,
`bandedRanges`, `slicers`) ride that same per-sheet envelope as additional sheet-scoped
keys, emitted only when present.

---

## Conditional-format serialization grammar

The headline read feature serializes each conditional-format rule's **body** into one
terse, human- and AI-readable line that round-trips back into a write. The serialized
line is the rule body **only** — it carries no index. The positional index (priority)
lives separately in the structured output and is supplied separately on write, so there
is a single source of index truth.

### Grammar (EBNF-ish)

```
line          := "[" rangelist "] " body
rangelist     := a1range ("," a1range)*
body          := boolean_body | gradient_body
boolean_body  := "if " condition " -> " format
gradient_body := "gradient " gradstop (" | " gradstop)*
condition     := COND_TYPE [ "(" arg ("," arg)* ")" ]    # args verbatim; formulas kept exact incl. leading "="
format        := fmt_token (" " fmt_token)*              # space-separated; canonical order: bg, fg, text-styles, number, align, wrap
gradstop      := minmax_stop | mid_stop
minmax_stop   := ("min" | "max") "=" hexColor            # MIN/MAX interpolation type; NO value
mid_stop      := "mid:" interp "=" hexColor              # midpoint; carries an explicit value
interp        := "num:" number | "pct:" number | "pctile:" number   # -> NUMBER | PERCENT | PERCENTILE
fmt_token     := "bg " hex | "fg " hex | "bold" | "italic" | "underline" | "strike"
               | "num " pattern | "halign " H | "valign " V | "wrap " W
```

Notes:

- `COND_TYPE` is the Google `BooleanCondition.type` verbatim (`CUSTOM_FORMULA`,
  `NUMBER_GREATER`, `NUMBER_BETWEEN`, `TEXT_CONTAINS`, `TEXT_EQ`, `BLANK`, `NOT_BLANK`,
  `DATE_AFTER`, `ONE_OF_LIST`, …). Args map to the condition's `values[]`.
- Colors render as 6-digit uppercase hex (`#FFCDD2`); theme colors render as
  `theme:ACCENT1`.
- **Gradient stops are keyed by slot, exactly one `=` per stop.** A gradient rule has at
  most three slots: `min` → minpoint, `mid` → midpoint, `max` → maxpoint. `min` / `max`
  carry the implicit interpolation type `MIN` / `MAX` and never take a value (the slot
  keyword *is* the type). Only `mid` carries an explicit value, written
  `mid:<interp>=<hex>` where `<interp>` is `num:<n>` → `NUMBER`, `pct:<n>` → `PERCENT`,
  or `pctile:<n>` → `PERCENTILE`. Attaching an interp to `min` / `max` is invalid. Stops
  are joined by `" | "` and ordered `min`, `mid`, `max` (absent slots omitted).

### Examples — boolean

```
[Sheet1!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold
[Sheet1!C2:C100] if NUMBER_GREATER(0) -> fg #1B5E20 bold
[Sheet1!D2:D100,Sheet1!F2:F100] if TEXT_CONTAINS(done) -> bg #C8E6C9
[Sheet1!E2:E100] if BLANK -> bg #ECEFF1 italic
```

### Examples — gradient

```
[Sheet1!G2:G100] gradient min=#FFFFFF | max=#1A73E8
[Sheet1!H2:H100] gradient min=#F44336 | mid:num:50=#FFEB3B | max=#4CAF50
[Sheet1!I2:I100] gradient min=#FFFFFF | mid:pct:50=#FFEB3B | max=#1A73E8
```

### Round-trip contract

`parse_rule_line(line)` returns a structured `{ranges, kind, condition, format}` dict
with no index; `serialize_rule(build_google_rule(parsed))` reproduces the input body line
exactly, up to canonical formatting (uppercase hex, canonical fmt-token order, canonical
gradient-slot order `min | mid | max`). A read line can be edited and written straight
back via `set_conditional_format`, with the target index passed separately. This
round-trip is golden-mastered in the test suite for the boolean case and the canonical
gradient case.

---

## v0.2 capabilities (richer reads, then files and cross-file)

The conditional-format line style generalizes. Every richer read added in v0.2 follows the
same discipline: **flattened hex colors**, a terse `[range] kind …` (or `kind … [range]`)
`line` for humans/AI, the structured fields alongside the line for round-trip, and
**omit-when-absent** so a sparse value stays token-cheap. Each serializer takes
already-resolved A1 strings; the owning read function resolves `GridRange → A1` first.
Per-cell rich data (runs, hyperlink, pivot) is attached **only to a cell that actually
carries it** — never emitted as an empty placeholder. The slicer and comment serializers gained
write builders in v0.2 (`structure` slicer CRUD; `comments` action CRUD). The last two
subsections (`export`, `read_many`) are not serializers — they are the file-output and
cross-file capabilities, documented here for completeness.

### Rich-text runs + cell hyperlink (`richtext`)

`inspect(include_rich_text=True)` adds `textFormatRuns` and `hyperlink` to the per-cell
mask. A cell with multiple styled segments gains a `runs` list; a cell with a single link
gains a flat `hyperlink`. A run carries its 0-based `start`, the substring `text`, a
flattened text-format subset, and a run-level `link` (which **takes precedence** over the
cell-level `hyperlink` — multi-link cells are recoverable only through the per-run links).

```jsonc
// per run, attached to a cell only when textFormatRuns are present
{ "start": 0, "text": "Click here", "format": { "bold": true, "fg": "#1155CC" },
  "link": "https://example.com" }
```

```
# terse line (one per cell with runs)
runs A1: "Click here"[0:10 fg #1155CC bold link https://example.com] + " then plain"[10:21]
```

Each segment is `"<text>"[<start>:<end> <fmt-tokens> link <uri>]`, fmt-tokens in the same
canonical order as the condformat format tokens. The flat `hyperlink` is omitted when a
cell holds multiple links (those live in the runs).

### Pivot-table definition (`pivot`)

`inspect(include_pivot=True)` adds `pivotTable` to the mask; only the **anchor (top-left)
cell** carries the definition, so `pivot` is attached only there. The `source` GridRange
is resolved to A1; rows/columns/values/filters are flattened.

```jsonc
{ "source": "Data!A1:F500",
  "rows":    [ { "field": "Region", "sourceColumnOffset": 0, "sortOrder": "ASCENDING" } ],
  "columns": [ { "field": "Quarter", "sourceColumnOffset": 2 } ],
  "values":  [ { "name": "Sum of Sales", "sourceColumnOffset": 4, "summarize": "SUM" } ],
  "filters": [ { "sourceColumnOffset": 1, "visibleValues": ["X", "Y"] } ],
  "valueLayout": "HORIZONTAL" }
```

```
pivot <- Data!A1:F500 | rows: Region | cols: Quarter | values: SUM(Sales)
```

A value renders as `SUMMARIZE(name)` (e.g. `SUM(Sales)`); each segment is omitted when its
slot is empty. Pivots are read-only — writing one stays in the `batch` escape hatch.

### Native tables (`tables`)

A `Table` (the 2024-GA native Sheets table) serializes to its name, A1 range, and typed
columns. A `DROPDOWN` column's data-validation rule renders with the **same**
`ValidationRule` one-liner `inspect` surfaces for cell validation.

```jsonc
{ "tableId": "abc", "name": "Sales", "range": "Sheet1!A1:F500",
  "columns": [ { "name": "Region", "type": "TEXT" },
               { "name": "Status", "type": "DROPDOWN", "validation": "ONE_OF_LIST(Open,Closed)" } ] }
```

```
table "Sales" [Sheet1!A1:F500] cols: Region:TEXT, Status:DROPDOWN(Open,Closed)
```

A column's flattened `type` key (Google's API field is `columnType`) is one of `TEXT | DOUBLE | CURRENCY | PERCENT | DATE | TIME | DATETIME |
DROPDOWN | CHECKBOX | SMART_CHIP | RATING`. Tables are full-CRUD via the `structure`
actions `add_table` / `update_table` / `delete_table`.

### Filter views and basic filter (`filters`)

A sheet's `basicFilter` (at most one) and its `filterViews` (an array) flatten to a sort
list plus a per-column criterion list. The column is rendered as a **letter** (`B`),
`hidden` normalizes `hiddenValues` / `visibleValues`, and a criterion's condition reuses
the **same** condition serializer as the condformat grammar.

```jsonc
// basicFilter (one per sheet, or null)
{ "range": "Sheet1!A1:F500", "sorted": [ { "col": "C", "order": "ASCENDING" } ],
  "criteria": [ { "col": "B", "hidden": ["Closed"], "condition": "NUMBER_GREATER(0)" } ] }
// filterView (array per sheet)
{ "filterViewId": 123, "title": "Open only", "range": "Sheet1!A1:F500",
  "criteria": [ { "col": "B", "hidden": ["Closed"] } ] }
```

```
basicFilter [Sheet1!A1:F500] sort C asc | B: hide Closed, NUMBER_GREATER(0)
filterView 123 "Open only" [Sheet1!A1:F500] B: hide Closed
```

Writable via the `structure` actions `set_basic_filter` / `clear_basic_filter` and
`add_filter_view` / `update_filter_view` / `delete_filter_view`.

### Banding (`banding`)

An alternating-color `BandedRange` flattens to its id, A1 range, and per-axis band colors
(header / first / second / footer), each as a hex string.

```jsonc
{ "bandedRangeId": 7, "range": "Sheet1!A1:F500",
  "rowBanding": { "header": "#4285F4", "first": "#FFFFFF", "second": "#E8F0FE", "footer": null },
  "columnBanding": null }
```

```
banding 7 [Sheet1!A1:F500] rows: hdr #4285F4 / #FFFFFF / #E8F0FE
```

Full-CRUD via the `structure` actions `add_banding` / `update_banding` / `delete_banding`.

### Slicers (`slicers`)

A `Slicer` flattens to its id, title, data range, filtered column index, dashboard anchor,
and a terse criterion.

```jsonc
{ "slicerId": 4, "title": "Region", "range": "Data!A1:F500", "columnIndex": 0,
  "anchor": { "sheet": "Dash", "row": 0, "col": 8 }, "criteria": "ONE_OF_LIST(...)" }
```

```
slicer 4 "Region" col 0 [Data!A1:F500] @ Dash!I1
```

The anchor reads back as `{sheet, row, col}` and renders as `@ <Sheet>!<cell>`. A 0-valued
index is meaningful (top row / first column), so a top-left, row-0 anchor still renders (e.g.
`@ Sheet!E1`); an index absent from the response is treated as 0.

Full-CRUD via the `structure` actions `add_slicer` / `update_slicer` / `delete_slicer`.
`add_slicer` takes the data range as the top-level A1 `range` (or `params.dataRange`) plus a
required single-cell `anchor`, and returns the new `slicerId` from the `addSlicer` reply.
`update_slicer` / `delete_slicer` address the slicer by `slicerId` in `params`; `delete_slicer`
maps to `deleteEmbeddedObject` since slicers share the embedded-object id space.

### Drive comments (`comments`)

The `comments` tool reads and writes threaded comments on the spreadsheet **file** via the
Drive API (not the Sheets API — Sheets has no comment surface). An `action` dispatch (default
`read`) selects the operation: `read` paginates `comments.list`; `create` posts a top-level
comment; `reply` posts a reply; `resolve` resolves a comment by posting a reply with
`action="resolve"` (Drive has no standalone resolve endpoint); `delete` removes a comment (the
destructive action — the CLI requires `--confirm`). Each comment flattens its author display
name, content, timestamps, `resolved` state, an optional quoted snippet, and its replies (each
with an optional `action` such as `resolve`).

```jsonc
{ "id": "AAAA", "author": "Jane Doe", "content": "please verify Q3",
  "created": "2026-05-01T…", "modified": "2026-05-02T…", "resolved": false,
  "quoted": "1234",
  "replies": [ { "author": "Bob", "content": "done", "action": "resolve" } ] }
```

```
comment AAAA by Jane Doe: "please verify Q3" (open, 1 reply)
```

A Sheets comment `anchor` is an **opaque, document-type-specific** string with no documented
A1 mapping, so it is surfaced raw under `anchorRaw` at the document level only — it is never
claimed to map to a cell. The Drive `comments.list` call **requires** an explicit `fields`
mask (omitting it errors), so core always sends one and paginates. Every action requires Drive;
without it (no Drive scope), core raises `SheetsError("drive_unavailable")` with a hint to
enable a Drive scope.

### Export (`export`)

`export` is not a serializer — it writes a local file and returns
`{format, mimeType, path, bytes}`. Two backends, chosen by `format`:

- `pdf` / `xlsx` / `ods` — whole-workbook export via Drive `files.export`. Google renders the
  workbook server-side; the bytes stream down with `MediaIoBaseDownload` (the lazy import above).
  Requires a Drive scope; the `sheet` arg is ignored. No Drive service → `drive_unavailable`.
- `csv` / `tsv` — one sheet, serialized locally from `read_values(render="plain")` (Drive's csv
  export only emits the first sheet, so we never use it). The serialization itself is delegated to
  the shared output-format layer (`format.render_grid`, below), so `export`'s on-disk bytes are
  byte-identical to a CLI `--format csv` pipe / an MCP file output. Sheets scope only; `sheet` is
  required → `missing_sheet` if omitted.

The MCP tool is annotated as a **write** tool with `destructiveHint=True`: it mutates no
spreadsheet, but it writes to the local filesystem and silently overwrites an existing file
at `path`.

### Output-format layer (`format`)

`core/format.py` is a pure stdlib helper (`csv` · `io` · `json`) that serializes a core result
dict to a string in one shared place, so both adapters and `export` produce byte-identical output.
`render(result, fmt)` covers the data formats — `json` (pretty `json.dumps`), `jsonl` (one
`{range,row}` record per row for `read_values`, one list element per line otherwise), `csv` /
`tsv` (the rectangular value grid via the stdlib `csv` module, RFC-4180 `\r\n`; a single range is
clean CSV, multiple ranges emit one `# range: <A1>` block each), and `markdown` (below). A tabular
format (`csv`/`tsv`) requested on a structured result raises `SheetsError("format_unsupported")` —
the agent learns to use a value read. `text` is **not** handled here: it stays the adapters'
existing terse renderer (the CLI text renderer / the Pydantic model render), which differs by
adapter and predates this layer.

The adapters wire it identically: the CLI promotes `--json` to a global `--format
{text,json,jsonl,csv,tsv,markdown}` (`--json` is a permanent alias for `--format json`); the MCP
read tools take `output_format` (the rectangular-values `read_values` offers every format, the
structured reads offer `text`/`json`/`jsonl`/`markdown` — markdown's KV form fits any shape, but
csv/tsv need a grid). For a data format the MCP tool returns the rendered string as a content-only
`ToolResult` (no `structured_content`). The five `out_path`-capable read tools are registered with
`output_schema=None` precisely so the MCP lowlevel server does **not** require structured output:
under any non-None schema a content-only result is rejected as "outputSchema defined but no
structured output returned" (ISSUES.md #19/#21), so suppressing the derived schema is what lets the
csv/tsv/jsonl/markdown string body — and the `out_path` handle below — flow through unchanged. The
normal text/json path still returns the mirror model, which FastMCP serializes into
`structuredContent` regardless.

The CLI's piped output uses the **same canonical newline convention** as `render()`/`out_path`: the
data formats (`jsonl`/`csv`/`tsv`/`markdown`) are written **verbatim** (`render()` is already
self-terminating — csv/tsv end in `\r\n`, jsonl in `\n`), so CLI-piped bytes are byte-for-byte equal
to the `out_path` file and the MCP no-`out_path` string. Only the human views `text`/`json` go
through `print()` and so keep a friendly trailing newline (ISSUES.md #20/#22).

#### Markdown (SPEC §6, D-MD)

`markdown` renders a **GitHub-flavored table** over a rectangular value grid, and **key/value
lines** for a structured (non-tabular) result — so `markdown` "just works" on any read (a table
where there is a grid, a record view otherwise) and both adapters call `render` with one body, never
branching on shape. The table is a deliberate **custom** renderer, not `tabulate`: `tabulate`
escapes neither an embedded `|` (it reads as a column separator and corrupts the row) nor an
embedded newline (it splits one row across two physical lines), so a cell carrying either is
silently mangled. The custom escaper maps `\` → `\\`, an embedded newline → the two-char `\n`, and
`|` → `\|`, keeping every row on one physical line with no unescaped pipe, so the table is
unambiguous and reverses cleanly. A multi-range value read emits one `### range: <A1>` heading per
block. `render_kv(result)` is the key/value form: one `field: value` line per record (the result's
primary record list, e.g. `comments`; else the whole dict), the same collision-resistant newline
escaping, blocks separated by a blank line; a nested list/dict value is JSON-encoded compactly so it
stays on one line. The MCP file-output handle (`out_path`) carries `markdown` like the other data
formats; `markdown` on a structured read never errors (it falls back to KV), unlike `csv`/`tsv`.

#### Address-keyed rendering for sparse data (SPEC §4.4)

A dense numeric grid reads best as a **rectangle + range anchor** (csv/json — one row per line,
position carries meaning). **Sparse** data — a formula read, conditional-format/note reads, the
`diff_only` computed holes — reads best as an **inverted index**: one `"<A1>: <body>"` line per
non-empty cell, with empty cells dropped entirely. `core/format.py` owns this in pure core:
`render_addressed(cells)` / `addressed_records(cells)` turn a per-cell list into address-keyed
lines / records, and `cells_from_value_grid(range_a1, values)` + `render_sparse_values(result)`
expand a `read_values` rectangle to absolute A1 (anchored at the requested range's top-left, parsed
from the A1 string — no `sheetId` resolution) so a formula read renders address-keyed. The CLI text
renderer uses `render_sparse_values` for `read_values --render formula`; `formula_patterns` is
itself an address-keyed read (per-column templates keyed by `col` + row span). Dense reads keep the
rectangle — the choice is per shape, not global.

### File-output escape valve (`out_path`, MCP-only) (`paths`)

The CLI pipes its rendered output (`> file`, `| pandas`); the MCP tool's output lands in the
agent's context, so for a large read the dominant cost is dumping the grid into the conversation.
The five read MCP tools that go through `_call_formatted` — `sheets_read_values`, `sheets_inspect`,
`sheets_describe`, `sheets_formula_patterns`, `sheets_read_many` — take an optional `out_path`. When
set, the adapter writes `render(result, output_format)` to that local file (utf-8) and returns a
small **handle** — `{ok, path, format, rows, cols, bytes, preview}`, with `preview` the first ~5 rows
(csv/tsv) or records (jsonl/json) — *instead of* the payload. It is the same shared `render()` plus a
file write, so the file is byte-identical to a CLI `--format` pipe. `out_path` is the **only**
sanctioned MCP-specific parameter (the CLI doesn't need it — its stdout pipes);
`output_format="text"` has no file representation, so under `out_path` it resolves to `json` (the
universal structured serializer).

The path safety and handle construction live in **pure core** (`core/paths.py`, stdlib `fnmatch` ·
`os` · `pathlib`): `resolve_out_path` resolves a relative path against the cwd, **errors**
(`bad_out_path`) if the parent directory does not exist (it never `mkdir`s — the agent named the
file; core does not invent directory trees), and **hard-refuses** any path under
`~/.config/google-sheets-mcp/` or `~/.secrets/`, or whose basename matches a credential glob
(`*token*.json`, `gcp-oauth.keys.json`, `service-account*.json`, `credentials.json`, `*.pem`,
`.env`, `.env.*`) — so a read can never clobber credentials. `write_file_handle` then renders the
result, writes it utf-8, and builds the `{ok, path, format, rows, cols, bytes, preview}` handle
(`rows`/`cols` describe the value grid for csv/tsv; for jsonl/json `rows` is the record count and
`cols` is 0; `preview` is capped at the first ~5 rows/records). These tools keep `readOnlyHint=True`
(the local write is a caller-named, opt-in side effect that modifies no spreadsheet/remote state;
the side effect is documented in each tool's docstring — decision D-ANNOT).

### Read across files (`read_many`)

`read_many` fans one read across many spreadsheets in a single call. It takes a list of request
dicts, each naming a `spreadsheetId`; `mode` picks the per-file read — `summary` runs `overview`,
`values` runs `read_values` over that request's `ranges` (with an optional per-request `render`).
Validation of the request list is up front (a malformed batch is a caller bug), but a live Google
failure on one id (404, permission denied, bad range) is **caught per file** and recorded as a
`{spreadsheetId, ok: false, error}` entry — the other files still read. It is read-only by design:
the Sheets API has no cross-file atomic write, so a multi-file mutation could only half-apply.

### Data-filter selectors (`dataselector`)

`read_values`, `describe`, and the per-request `values` mode of `read_many` accept a `data_filters`
list as an **alternative to literal `ranges`** — the durable, position-independent way to address a
region (a `metadata`-tagged block survives row/column inserts that would shift an A1 range). Core
enforces **exactly one of `ranges` / `data_filters`** per call; the CLI surfaces the selector list as
`--data-filter-json` (with `@file.json` support), the MCP as a `data_filters: list[dict]` arg. The
grammar lives in pure core (`core/dataselector.py`); each selector is **exactly one** of:

```jsonc
{ "a1": "Sheet1!A1:B10" }                                   // resolved A1 → GridRange inside core
{ "gridRange": { "sheetId": 0, "startRowIndex": 0, ... } }  // a raw Google GridRange, passed through
{ "developerMetadataLookup": { "metadataKey": "block:totals" } }  // matches a metadata-tagged block
```

`build_data_filters()` validates the list and resolves each selector; the `a1` form reuses the same
A1 → `GridRange` resolution as the literal-`ranges` path, while `gridRange` / `developerMetadataLookup`
pass through verbatim (the `developerMetadataLookup` shape is the one `metadata` already uses for its
CRUD). An empty list, a non-list, or a selector carrying none / more than one of the three keys raises
`SheetsError("bad_data_filters")` with an example hint. CLI `ranges` positionals are `nargs="*"` and
collapse to `None` when empty, so `--data-filter-json` can be the addressing path without colliding
with a positional range.

### Retry / backoff (`retry`) — pure mechanism, off by default

Retry/backoff (ISSUES.md #25) is **off by default** as of v0.4.0 (a breaking change from the old
always-on 4 automatic retries): a 429/5xx now fails fast unless the caller opts in, per call, on
either adapter. The mechanism is pure and lives entirely in `core/retry.py` — no `googleapiclient`
import at module top, so the boundary guard stays green (the auth layer imports it transport-clean).

There is **no central `.execute()` wrapper in core** (32 call sites); the single chokepoint is the
auth-layer `requestBuilder`, built once per process/lifespan. So per-call configuration flows through
a **contextvar** the builder reads at `.execute()` time, not through every core signature:

- **`RetryPolicy`** is a frozen dataclass holding the full policy — `enabled` (default `False`),
  `strategy` (`none` / `fixed` / `exponential` / `exponential_jitter`), `max_retries`, `base_delay`,
  `max_delay`, `total_deadline` (overall wall-clock cap; `None` = no cap), `honor_retry_after`,
  `retry_after_cap`, the retryable `retry_statuses`, and `retry_rate_limit_403`. Constructors:
  `RetryPolicy.DISABLED` (the true off — one attempt, exceptions propagate), `default_preset()` (the
  sensible catch-all: enabled exponential-jitter, 4 retries, 0.5s base, 30s per-attempt cap, 60s
  overall deadline), and `from_env(**overrides)` (reads `GSHEETS_BACKOFF_*` env, then applies explicit
  overrides; stays DISABLED unless retry is explicitly enabled). `next_delay()` and `is_retryable()`
  are the pure decision logic; `next_delay` accepts an injectable rng so jitter is deterministic in tests.
- **`current_policy()` / `activate(policy)`** read and set the `_ACTIVE_POLICY` contextvar. Both
  adapters wrap the core call in `activate(...)` so the policy is visible to `.execute()` deep in core.
- **`execute_with_retry(call, policy=None, …)`** is the loop: if the policy is disabled it returns
  `call()` once; otherwise it retries on a retryable status, sleeping `next_delay()` (honoring a server
  `Retry-After` when present), capping each sleep at `max_delay`, and giving up if the next sleep would
  breach `total_deadline`. On the final raise it annotates the exception with `_gsheets_retry_attempts`
  / `_gsheets_retry_waited_ms`, which `classify_google_error()` reads onto the `SheetsError` as the
  optional `retries` / `waited_ms` fields (surfaced in the error dict as `retries` / `waitedMs`, only
  when present).

**Auth wiring.** `auth/__init__.py`'s `_make_request_builder()` returns a `_RetryingHttpRequest` whose
`execute()` defers to `core.retry.execute_with_retry(lambda: super().execute(num_retries=0), …)` —
`num_retries=0` turns off googleapiclient's built-in retry so our loop is the only retrier. The builder
reads the active policy via `current_policy()`; it no longer reads any retry env var itself. A stderr
diagnostic line per retry is gated by `GSHEETS_BACKOFF_LOG`.

**Adapters.** The CLI exposes global flags (`--default-backoff-strategy` preset, `--no-retry`, and the
granular `--retries` / `--backoff` / `--retry-*` / `--honor-retry-after`), resolves them to one
`RetryPolicy` in `main()` (mutually-exclusive; a conflict raises `backoff_flags_conflict`), and wraps
dispatch in `activate(policy)`. The MCP server adds a per-call `retry: RetryParams` arg (an MCP-only
mirror model in `models.py`, never in core) to every tool — omit for no retry, `preset:"default"` for
the preset, `preset:"off"` to force disable, or granular fields (mutually exclusive with `preset`); it
resolves to a `RetryPolicy` and `activate(...)`s it inside the sync `_call` body so the contextvar is
set under FastMCP's thread offload.

**Env vars** (read **only** inside `from_env`, mirroring how `errors.py` reads `GSHEETS_VERBOSE_ERRORS`
— config, not credentials, so it is allowed in core): `GSHEETS_BACKOFF_STRATEGY` (a non-`none` value
enables retry), `GSHEETS_BACKOFF_MAX_RETRIES` (canonical; legacy alias `GSHEETS_MAX_RETRIES`, still
honored — `> 0` enables, `0` disables), `GSHEETS_BACKOFF_BASE_DELAY`, `GSHEETS_BACKOFF_MAX_DELAY`,
`GSHEETS_BACKOFF_DEADLINE` (`<= 0` / `none` = no overall cap), `GSHEETS_BACKOFF_HONOR_RETRY_AFTER`,
`GSHEETS_BACKOFF_RETRY_AFTER_CAP`, and `GSHEETS_BACKOFF_LOG`. Parse failures fall back to field
defaults, never crash.

---

## Addressing and color conventions

- **`GridRange` is 0-based, half-open** (`startRowIndex` inclusive, `endRowIndex`
  exclusive); **A1 is 1-based, inclusive**. The conversion is centralized in
  `addressing` so it is done once and correctly. Unbounded ranges (`A:A`, `2:2`, whole
  sheet) map by omitting the corresponding indices. **Half-open ranges map each endpoint
  independently** (Google semantics): `A2:A` is column A *from row 2 down*
  (`startRowIndex` set, `endRowIndex` omitted), `A:A5` is column A *down to row 5*
  (`endRowIndex` set, `startRowIndex` omitted) — a partial bound is never silently widened
  to the whole column/row (which would clobber row 1 / column A on a write), and these
  forms round-trip through `gridrange_to_a1` unchanged.
- **Writes always use `ColorStyle`** (`rgbColor` / `themeColor`), never the deprecated
  flat `Color`. **Reads flatten to a hex string.** Channel rounding is `round(channel *
  255)`.

---

## Error handling

Core raises a single exception type, `SheetsError(code, message, status?, reason?,
hint?)`, and never returns an error dict. `classify_google_error()` maps a Google
`HttpError` to a `SheetsError` with an actionable, **generic** hint (e.g. a permission
error suggests sharing the sheet with the authenticated account). Hints do not embed the
operator's account email by default — that is gated behind an opt-in verbose mode so a
public/masked deployment never leaks it.

Each adapter has one envelope:

- **MCP** raises a curated `ToolError` (the server runs with masked error details, so
  unexpected exceptions surface generically while curated messages pass through);
- **CLI** catches the `SheetsError` at the top of `main()` and prints a terse stderr
  line, or a structured `{"ok": false, "error": {…}}` object under `--json`, with exit
  code 1.

---

## Security and privacy

This project is public. Credentials and real spreadsheet IDs come **only** from
environment variables / local config at runtime — never the committed tree. All docs and
examples use the placeholder `<YOUR_SPREADSHEET_ID>`. See
[`CONTRIBUTING.md`](../CONTRIBUTING.md#security-and-privacy-this-repo-is-public) for the
full rules.
