# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Nothing yet.

## [0.2.0] - 2026-06-09

Adds verified feature-gap capabilities on top of the v0.1.0 surface — all additive, with
every base signature and test preserved. The tool surface grows from 15 to 20: new
`data_ops`, `dimensions`, `comments`, `export`, and `read_many` tools, plus mask / flag /
action extensions to existing tools (`inspect`, `structure`, `overview`). `comments` and
the slicer actions on `structure` close the last read-only gaps into full CRUD. Read-side
richness and token efficiency carry through: per-cell rich data is emitted only when
present, complex reads are serialized into terse round-trippable lines, and writes keep
`USER_ENTERED` defaults with auto fields masks and index-safe batches.

### Added
- **Read richness on `inspect` (per-cell, opt-in, zero cost when off):**
  - Rich-text runs — `include_rich_text` surfaces per-character styled segments
    (`textFormatRuns`) flattened into a `runs` list, including a run-level `link` that takes
    precedence over the cell hyperlink (the only way to recover multi-link cells). (#1)
  - Cell hyperlink — the read-only `hyperlink` field attaches as a flat string (folded into
    `include_rich_text`). (#8)
  - Pivot-table definition — `include_pivot` attaches a flattened `pivot` (source, rows /
    columns / values / filters) to the pivot's anchor cell only. Read-only. (#6)
- **Structural reads on `structure(action="read")` (sheet-scoped, emitted only when present):**
  - Native Sheets tables — a `tables` key (name, A1 range, typed columns; a DROPDOWN column
    renders the same `ValidationRule` one-liner `inspect` uses). (#3)
  - Filter views + basic-filter state — `basicFilter` and `filterViews` keys (sort specs +
    per-column criteria, reusing the conditional-format condition serializer). (#4)
  - Banding — a `bandedRanges` key (per-axis header / first / second / footer hex colors). (#9)
  - Slicers — a `slicers` key (title, data range, filtered column, dashboard anchor). (#16)
- **Structural writes as new `structure` actions (full CRUD symmetry with the reads above):**
  - Tables — `add_table` / `update_table` / `delete_table` (captures new `tableId`). (#3)
  - Banding — `add_banding` / `update_banding` / `delete_banding` (captures new
    `bandedRangeId`). (#9)
  - Filters — `set_basic_filter` / `clear_basic_filter` and `add_filter_view` /
    `update_filter_view` / `delete_filter_view` (captures new `filterViewId`). (#4)
  - Slicers — `add_slicer` / `update_slicer` / `delete_slicer` (`add_slicer` takes the data
    range via the top-level `range` or `params.dataRange` and returns the new `slicerId`;
    `update_slicer` / `delete_slicer` take `params.slicerId`). (#16)
  - Spreadsheet properties — `spreadsheet_props` sets `title` / `locale` / `timeZone`
    (spreadsheet-scoped; auto fields mask). (#12)
- **Spreadsheet metadata read on `overview`:** top-level `locale` and `timeZone` (omitted
  when absent) for correct interpretation of dates and numbers. (#12)
- **New `data_ops` tool** — single-request `batchUpdate` data verbs through one dispatch:
  `find_replace` (#2), `delete_duplicates` and `trim_whitespace` (#11), `sort_range`,
  `text_to_columns`, and `auto_fill` (#15), and `copy_paste` / `cut_paste` (#14), each
  returning an action-specific summary (e.g. `find_replace` surfaces
  `occurrencesChanged`/`valuesChanged`/`formulasChanged`).
- **New `dimensions` tool** — row/column operations: `insert` / `delete` / `move` /
  `append` (#7), `auto_resize` (#10), `set_props` for pixel size / `hiddenByUser` (#13), and
  a `read` that reports which rows/columns are hidden (#13).
- **New `comments` tool (full CRUD)** — read/create/reply/resolve/delete on the Drive
  threaded comments of the spreadsheet file, via the Drive API. `read` (default) flattens
  each comment to author/content/timestamps/`resolved`/quoted snippet/replies, paginates,
  and honors `include_resolved`/`include_deleted`; the opaque non-A1 `anchor` is surfaced
  raw at the document level. `create`/`reply`/`resolve`/`delete` are the write actions —
  `resolve` posts a reply carrying `action:resolve` (Drive has no standalone resolve
  endpoint). The required Drive `fields` mask is always sent; raises `drive_unavailable`
  when no Drive scope is present. The MCP tool is a write tool (`readOnlyHint=False`,
  `destructiveHint=True`); the CLI `--action delete` requires `--confirm`. (#5)
- **New `export` tool** — downloads a spreadsheet to a local file. `pdf`/`xlsx`/`ods`
  render the whole workbook server-side via Drive `files.export` (needs a Drive scope, else
  `drive_unavailable`); `csv`/`tsv` serialize a single named `--sheet` from its values
  through the Sheets API (no Drive scope). Read-only against the spreadsheet — it writes a
  local file and returns `{format, mimeType, path, bytes}`. (#18)
- **New `read_many` tool** — fans one values-or-summary read across many spreadsheets in a
  single call (the cross-file analogue of `overview`/`read_values`). Each request names one
  `spreadsheetId`; a bad id (404, permission denied, bad range) is captured as a per-file
  `{ok:false, error}` entry instead of aborting the batch, so a top-level `ok:true` does not
  mean every file succeeded. Read-only. CLI: ids and ranges live inside `--requests-json`
  (no positional id, no `--ranges`); the global `--json` flag precedes the subcommand. (#19)
- **New pure-core serializer / dispatch modules** (boundary-pure, golden-master tested
  where they serialize): `richtext`, `pivot`, `tables`, `filters`, `banding`, `slicers`,
  `comments`, `dataops`, `dimensions`, `export`, `multiread`. Five new top-level core
  functions (`data_ops`, `dimensions`, `comments`, `export`, `read_many`) are re-exported
  from `gsheets.core`.
- **Adapter + model parity:** new Pydantic mirror models (`TextRun`, `Pivot*`, `Table`,
  `BasicFilter`, `FilterView`, `Banding`, `Slicer`, `Comment`, `CommentReply`,
  `DataOpsResult`, `DimensionsResult`, `CommentsResult`, `ExportResult`, `ReadManyResult`)
  and extensions to `Cell` / `Run` / `SheetStructure` / `OverviewResult`; five new MCP tools
  (`sheets_data_ops`, `sheets_dimensions`, `sheets_comments`, `sheets_export`,
  `sheets_read_many`) plus the `include_rich_text` / `include_pivot` kwargs on
  `sheets_inspect`; five new CLI subcommands (`data-ops`, `dimensions`, `comments`,
  `export`, `read-many`) plus `--rich-text` / `--pivot` flags on `inspect`. `comments` adds
  the `--action {read,create,reply,resolve,delete}` write surface (`--confirm` gates
  delete); `structure` adds the `add_slicer` / `update_slicer` / `delete_slicer` actions.
  The global `--json` flag is defined on the top-level parser, so it precedes the
  subcommand (`gsheets --json read-many …`).

### Changed
- `structure(action="read")` now requests `sheets.rowGroups` + `sheets.columnGroups` for the
  dimension-groups read (there is no single `dimensionGroups` field on a `Sheet`); the
  flattened `dimensionGroups` output key is unchanged.

### Fixed
- `data_ops(action="delete_duplicates")` now reports the correct count: it reads Google's real
  `DeleteDuplicatesResponse.duplicatesRemovedCount` reply field (it previously read a
  non-existent `duplicatesRemoved`, so the surfaced `duplicatesRemoved` was always `0`). Caught
  by the new live round-trip; the unit fixture now mocks the real field name to guard it.
- Slicer anchor read: a `GridCoordinate` omits `rowIndex`/`columnIndex` when they are `0`, so a
  top-left or row-0 slicer anchor previously lost its `row`/`col`. The anchor now defaults absent
  indices to `0`, so it always reads back as `{sheet, row, col}` and the terse line always renders
  (e.g. `@ Sheet!E1` for an anchor the API returned as `{columnIndex: 4}` with `rowIndex` omitted).

### Notes
- Connected Sheets / data sources (`dataSourceTable`, `dataSourceFormula`, spreadsheet
  `dataSources`) are intentionally **not** given a typed tool (low signal, heavy schema);
  they remain readable through the `batch` / raw `spreadsheets.get` escape hatch. (#17)
- The `tests_boundary_guard` pytest marker is now registered to silence its
  `PytestUnknownMarkWarning`.
- The v0.2 capabilities are exercised by live round-trip integration tests
  (`tests/integration/test_live_v02.py`, `@pytest.mark.live`): each new write verb is written
  and read back, and each new read is exercised against a real spreadsheet. They are gated on
  `GSHEETS_LIVE=1` + `GSHEETS_TEST_SPREADSHEET_ID` (never a committed/Production id) and skip in
  a normal `pytest` run.

## [0.1.0] - 2026-06-09

Initial release: a pure Google Sheets core library wrapped by two thin adapters — an
MCP server (`google-sheets-mcp`) and a CLI (`gsheets`) with a bundled `SKILL.md` — over
shared code, with read-side richness as the thesis.

### Added
- **Pure core library** (`gsheets.core`) with a 15-function surface, importing only the
  standard library plus `googleapiclient` / `google.auth*` (zero MCP/CLI/transport/
  pydantic imports).
- **Read-side richness:**
  - `overview` — cheap orientation snapshot (title, tabs, dimensions, frozen rows/cols,
    protected-range and conditional-format counts, named ranges) with no grid data.
  - `inspect` — flagship rich read combining values + formulas + `userEnteredFormat` +
    `effectiveFormat` + merges + data validation over a tight `fields` mask (never
    `includeGridData`), with an optional compact mode that collapses identical cells into
    rectangular runs.
  - `read_values` — values for one or more ranges with render modes `plain` /
    `unformatted` / `formula` / `all` (formula + computed side by side, index-aligned).
  - `read_conditional_formats` — per-sheet conditional-format rules serialized to terse,
    round-trippable readable lines (the priority feature).
- **Write surface with safe defaults:**
  - `write_values` (`USER_ENTERED` default, multi-range in one call), `append_rows`
    (`INSERT_ROWS`, no overwrite), and `clear` (values, optionally formats / validation /
    notes).
  - `format` — apply background, font, number/date pattern, alignment, wrap, padding,
    borders, and notes atomically, with the `fields` mask auto-built from the payload.
  - `set_conditional_format` — add / update / delete boolean and gradient rules by
    positional index, with an index-shift-safe batch form (mutations ordered high → low).
  - `set_validation` — set / clear structured data-validation rules that round-trip from
    `inspect`.
- **Structure, sheets, metadata, charts, and an escape hatch:**
  - `structure` — read/modify merges, named ranges, protected ranges, frozen rows/cols,
    tab color, and dimension groups through one structural interface with a shape-stable
    multi-sheet envelope.
  - `manage_sheets` — add / delete / duplicate / rename / reorder tabs, returning new ids.
  - `metadata` — read/write developer metadata for durable row/column/sheet anchors.
  - `charts` — create / update / delete embedded charts (read returns metadata only in
    v1).
  - `batch` — raw `batchUpdate` escape hatch.
- **Conditional-format serialization grammar** — a locked, body-only line grammar for
  boolean and gradient rules, with a golden-mastered round-trip (rule → line → parse →
  rule → line).
- **Shared core helpers** — A1 ↔ `GridRange` conversion, hex ↔ `ColorStyle`, auto
  `fields`-mask construction with a locked atomic-leaf set, Google `CellFormat`
  flattening, and jagged-array normalization.
- **Auth layer** (`gsheets.auth`) — credential resolution with Service Account / OAuth
  2.0 Desktop / ADC precedence, least-privilege scopes by default, env-var-only
  configuration, and token caching. Interactive consent is a CLI-only path.
- **MCP adapter** (`mcp_server.py`) — FastMCP stdio server registering one tool per core
  function with `ToolAnnotations`, an `ENABLED_TOOLS` allowlist, masked error details, a
  single tool-error envelope, and a lifespan that builds the service once and fails
  cleanly to stderr when no usable credentials exist.
- **CLI adapter** (`cli.py`) — `argparse` CLI with one subcommand per core function
  (flags mapping 1:1 to core kwargs), a global `--json` flag, and an auth-only
  `auth login | status` subcommand.
- **Pydantic mirror models** (`models.py`, adapter-side only) for MCP structured output,
  mirroring each core return dict field-for-field with a terse text rendering.
- **Bundled skill** — `skill/SKILL.md` wrapping the `gsheets` CLI, with reference docs
  under `skill/references/` (commands, reading, writing).
- **Tests** — unit suite against a mocked Sheets service with golden-master fixtures for
  the serializers, plus a subprocess **boundary-guard** test enforcing the pure-core
  invariant, and opt-in live integration tests gated on `GSHEETS_LIVE` /
  `GSHEETS_TEST_SPREADSHEET_ID`.
- **Project docs** — `README.md`, `CONTRIBUTING.md`, `docs/ARCHITECTURE.md`, issue and
  pull-request templates.

### Security
- No credentials, real spreadsheet IDs, or personal information are committed; all real
  values are supplied via environment variables / local config at runtime, and docs use
  the `<YOUR_SPREADSHEET_ID>` placeholder.
- Error hints are generic by default and never leak the operator's account email unless
  an opt-in verbose mode is enabled.

[Unreleased]: https://github.com/colindmurray/google-sheets-mcp-and-skill/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/colindmurray/google-sheets-mcp-and-skill/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/colindmurray/google-sheets-mcp-and-skill/releases/tag/v0.1.0
