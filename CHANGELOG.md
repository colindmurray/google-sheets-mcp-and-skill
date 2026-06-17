# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Nothing yet.

## [0.4.2] - 2026-06-17

The v0.4.1 performance fix, generalized. An audit of every core module found the same
one-network-call-per-range pathology in **9 more functions** — most importantly `overview`,
`inspect`, and `describe` (the three primary read tools). All are fixed at once.

### Fixed
- **The N+1 sheet-index pattern affected far more than conditional formats.** Any operation that
  resolved more than one A1↔`GridRange` — `overview` over named ranges, `inspect` over merges,
  `describe` over regions, a multi-series `charts` create, a multi-range `set_conditional_format`,
  developer-metadata reads, value `clear` with structural payloads, `formula_patterns` over multiple
  ranges, `read_values`/`describe` with `data_filters` — called the sheet-index `spreadsheets.get`
  **once per element** with no active cache. On a merge- or rule-heavy sheet this meant minutes of
  wall-clock and per-user-quota exhaustion, exactly like the v0.4.1 conditional-format case.
- **Fixed systemically at the adapter boundary.** Both adapters now open a single
  `addressing.sheet_index_cache()` scope around the whole core dispatch — the same chokepoint where
  the retry policy is activated (CLI `main`, MCP `_call` / `_call_formatted`). The sheet index is
  therefore fetched **once per tool call / CLI invocation** no matter which core function runs, and
  any future core function inherits the fix automatically. The cache is per-operation (torn down
  when the call returns), so it can never serve a stale sheet title across operations and never
  affects cell data, formats, or rules (which are not cached at all). Audited cache-safe across
  every write path (no write re-resolves the sheet list after mutating structure in the same call).
- `sheet_index_cache()` is now **re-entrant**: the per-function scopes already in
  `read_conditional_formats` / `structure` (which keep direct library callers fast) reuse the outer
  adapter scope instead of forcing a redundant refetch. Output is unchanged everywhere; not a
  breaking change.

## [0.4.1] - 2026-06-17

A performance fix: reading conditional formats (or full structure) on a large, rule-heavy sheet
was pathologically slow — a 54-rule tab took **~5m21s**. It now returns in **~1s**, byte-identical.

### Fixed
- **`read_conditional_formats` / `structure(action="read")` made one network call per range.** Each
  rule's (and each merge's / named range's) `sheetId`→A1 conversion went through `gridrange_to_a1`,
  which called the sheet-index `spreadsheets.get` **once per range with no caching** — so a 54-rule
  sheet fired ~55 sequential API calls (minutes of wall-clock, and enough requests to exhaust the
  per-user read quota and trigger 429s/timeouts). The sheet index is now cached for the duration of
  a single read (the "per-call cached" behavior the docstrings already promised but never
  implemented), scoped per-operation so it can never serve stale sheet titles. Both reads also scope
  their underlying `spreadsheets.get` to the requested sheet/range via `ranges` instead of loading
  every tab's model. Result: a 54-rule conditional-format read dropped from ~5m21s to ~1s (≈54 API
  calls → 2). Output is unchanged; not a breaking change.

## [0.4.0] - 2026-06-17

A breaking default change: automatic retry/backoff is now **OFF by default**, replacing the prior
always-on 4 retries on every call. Retry becomes opt-in and per-call configurable on both adapters,
with the same parameter structure, plus error-side observability for when an enabled retry still
gives up.

### Changed
- **BREAKING: retry/backoff is now OFF by default.** Previously every API call carried 4 automatic
  retries with randomized exponential backoff on 429 / 5xx / rate-limit; now a 429/5xx **fails fast**
  unless the caller opts in. Opt in via the CLI's global `--default-backoff-strategy` (a one-shot
  catch-all preset: full-jitter exponential, 4 retries, 60s overall deadline) or the granular global
  flags (`--retries`, `--backoff {none,fixed,exponential,exponential-jitter}`, `--retry-base-delay`,
  `--retry-max-delay`, `--retry-deadline`, `--retry-after-cap`, `--honor-retry-after` /
  `--no-honor-retry-after`); the MCP per-call `retry` object (`{"preset":"default"}` or granular
  fields); the new `GSHEETS_BACKOFF_*` env vars (`GSHEETS_BACKOFF_STRATEGY`,
  `GSHEETS_BACKOFF_MAX_RETRIES`, `GSHEETS_BACKOFF_BASE_DELAY`, `GSHEETS_BACKOFF_MAX_DELAY`,
  `GSHEETS_BACKOFF_DEADLINE`, `GSHEETS_BACKOFF_HONOR_RETRY_AFTER`, `GSHEETS_BACKOFF_RETRY_AFTER_CAP`,
  `GSHEETS_BACKOFF_LOG`); or the **legacy** `GSHEETS_MAX_RETRIES` (now an opt-in alias — a value
  `> 0` enables retry with that many retries and the `exponential_jitter` strategy; `0` keeps it
  off). The CLI's `--no-retry` forces explicit fail-fast (overrides env). The preset, `--no-retry`,
  and the granular flags are mutually exclusive (a conflict is a clean `backoff_flags_conflict` /
  `backoff_params_conflict` error). Batching remains the real quota fix; retry only smooths bursts.

### Added
- **Per-call configurable retry policy on both adapters.** A new pure-core `retry` module
  (`core/retry.py`) holds a frozen `RetryPolicy` (strategy, retry budget, base/max delay, overall
  deadline, `Retry-After` honoring + cap, retryable statuses incl. opt-in rate-limited 403), a
  `_ACTIVE_POLICY` contextvar with `current_policy()` / `activate()`, and the `execute_with_retry`
  loop. The single chokepoint is the auth-layer request builder, which reads the active policy at
  `.execute()` time (it no longer reads any retry env var and turns googleapiclient's built-in retry
  off). The CLI resolves its global flags to one policy and activates it around dispatch; the MCP
  server adds a per-call `retry` (`models.RetryParams`, MCP-only) to every tool and activates the
  resolved policy inside the sync call body.
- **Retry observability on the structured error.** When an enabled retry exhausts and the call still
  fails, `SheetsError` carries optional `retries` / `waited_ms` fields, surfaced in the error dict as
  `retries` / `waitedMs` (present only when non-null), so a caller can see how hard the call tried.

## [0.3.1] - 2026-06-17

Patch release: three correctness fixes to the v0.3 read/output surface. Behavior is otherwise
identical; no signatures changed.

### Fixed
- **MCP file-output / data-format reads no longer fail with an output-validation error
  (#19/#21).** The five `out_path`-capable read tools (`sheets_read_values`, `sheets_inspect`,
  `sheets_describe`, `sheets_formula_patterns`, `sheets_read_many`) raised "Output validation
  error: outputSchema defined but no structured output returned" in any schema-enforcing MCP
  client whenever `out_path` was set, or whenever a data format (`csv`/`tsv`/`jsonl`/`markdown`)
  was requested without `out_path` — the file was written but the call reported failure and the
  handle (or rendered string) was lost. These tools are now registered with `output_schema=None`,
  so a content-only result flows through unchanged; the normal text/json read still emits
  `structuredContent` for its mirror model.
- **`formula-patterns` now returns one entry per requested column (#16).** A bounded or
  whole-column range (e.g. `A1:CH75`, `A:CH`) previously returned a content-dependent number of
  `columns[]` entries because the Sheets API trims trailing all-blank columns from the response.
  Trailing columns are now padded to the requested A1 width with the empty
  `{reduced: true, templates: []}` shape, so the count is deterministic. Inherently
  unbounded-column ranges (whole-row / whole-sheet) keep the data-extent count.
- **CLI piped output is byte-identical to the MCP `out_path` file for the data formats
  (#20/#22).** The CLI added an extra trailing newline (a visible blank line for csv) to
  `jsonl`/`csv`/`tsv`/`markdown` output. Those formats are now written verbatim through the
  already-self-terminating shared renderer, so CLI-piped bytes match the `out_path` file, the MCP
  no-`out_path` string, and `export`. The human views (`text`/`json`) keep their friendly trailing
  newline.

## [0.3.0] - 2026-06-16

Additive on top of v0.2.0; every base signature and test preserved. The core surface grows
from 20 to 22 functions with two new structure-aware reads (`describe`, `formula_patterns`).
Output formatting becomes a uniform multi-format axis (text / json / jsonl / csv / tsv /
markdown) over one shared pure renderer, and the MCP read tools gain a file-output escape
valve.

### Added
- **New `describe` core fn / `sheets_describe` tool / `describe` CLI subcommand** — a
  structure-aware read that summarizes one or more ranges (or a `data_filters` selection).
  Optional `ranges` (CLI nargs `*`; `None` ⇒ whole spreadsheet), `--data-filter-json` /
  MCP `data_filters`, and `--max-cells` / MCP `max_cells` (fails `result_too_large` before
  returning an over-cap payload). Core enforces exactly one of `ranges` / `data_filters`.
  Read-only.
- **New `formula_patterns` core fn / `sheets_formula_patterns` tool / `formula-patterns`
  CLI subcommand** — clusters a range's formulas into shared patterns. Required `ranges`
  (CLI nargs `+`), `--no-sample` (CLI) / `sample` (MCP, default `True`) to skip the second
  FORMATTED pass. Always reads FORMULA, column-major; exposes no major axis. Read-only.
- **`data_filters` selector grammar** (`core/dataselector.py`) — each selector is exactly one
  of `{"a1": "Sheet1!A1:B10"}` / `{"gridRange": {...}}` /
  `{"developerMetadataLookup": {"metadataKey": "block:totals"}}`. Used by `read_values`,
  `describe`, and per-request inside `read_many`. CLI `--data-filter-json` (with
  `@file.json` support); MCP `data_filters: list[dict]`. Invalid selector → `bad_data_filters`.
- **`read_values` reach extensions** — `--data-filter-json` / `data_filters` as an
  alternative addressing path (exactly one of `ranges` / `data_filters`), `--major` (CLI) /
  `major_dimension` (MCP) `rows` | `columns` (wire/core/result key is `major`; `rows` is
  Google's default and is omitted on the wire), `--diff-only` / `diff_only` (`render="all"`
  only — nulls out computed cells equal to their values), and `--max-cells` / `max_cells`.
- **Range-scoped conditional-format reads** — `read_conditional_formats` gains a `--range`
  flag, mutually exclusive with `--sheet` (the range carries its own sheet; passing both
  raises `conflicting_args`).
- **Multi-format output as a uniform axis** over one shared pure renderer
  (`core.format.render`, supporting `json` / `jsonl` / `csv` / `tsv` / `markdown`; `text` is
  the adapters' own terse renderer):
  - **CLI** — one global `--format {text,json,jsonl,csv,tsv,markdown}` (default `text`)
    applied to every subcommand; `--json` is a permanent alias for `--format json` (an
    explicit conflicting `--format` raises `conflicting_args`). csv/tsv on a structured
    (non-grid) result raises `format_unsupported`.
  - **MCP** — a per-tool `output_format` arg on the read tools that render through
    `_call_formatted`. `sheets_read_values` accepts the full `ValueFormat` (text / json /
    jsonl / csv / tsv / markdown); `sheets_inspect`, `sheets_describe`,
    `sheets_formula_patterns`, and `sheets_read_many` accept `StructuredFormat` (text / json
    / jsonl / markdown — no csv/tsv).
- **MCP `out_path` file-output handle** — an MCP-only arg (not a CLI flag, not a core param)
  on the five `_call_formatted` read tools (`sheets_read_values`, `sheets_inspect`,
  `sheets_describe`, `sheets_formula_patterns`, `sheets_read_many`). When set, the rendered
  read is written utf-8 to a local file and a small handle
  (`{ok, path, format, rows, cols, bytes, preview}`, preview capped at 5) is returned instead
  of the payload (`text` resolves to `json`). The path is resolved + safety-checked in pure
  core (`core/paths.py`): the parent dir must already exist (never `mkdir`), and
  credential-shaped basenames plus the `~/.config/google-sheets-mcp/` and `~/.secrets/`
  subtrees are hard-refused with `bad_out_path` before any write. The spreadsheet is never
  modified.

### Changed
- The package now reports version `0.3.0`. The pyproject description and keywords were
  refreshed for the v0.3 surface (`describe`, `formula-patterns`, multi-format output).
- Output formatting is centralized in the pure `core.format.render`; both adapters drive it
  (CLI `--format`/`--json`, MCP `output_format`). There is no `output_format` parameter on any
  core function — output remains strictly adapter-side.
- Adapter + model parity: the two new core functions get matching MCP tools and CLI
  subcommands, and `models.py`'s `RESULT_MODELS` registry maps all 22 names to mirror models.
  The new read tools carry `readOnlyHint=True, idempotentHint=True` and `tags={"read"}`.

## [0.2.0] - 2026-06-09

All additive on top of v0.1.0; every base signature and test preserved. The tool surface
grows from 15 to 20: new `data_ops`, `dimensions`, `comments`, `export`, and `read_many`
tools, plus mask / flag / action extensions to existing tools (`inspect`, `structure`,
`overview`). `comments` and the slicer actions on `structure` close the last read-only
gaps into full CRUD.

### Added
- **Read richness on `inspect` (per-cell, opt-in, zero cost when off):**
  - Rich-text runs — `include_rich_text` surfaces per-character styled segments
    (`textFormatRuns`) flattened into a `runs` list, including a run-level `link` that takes
    precedence over the cell hyperlink (the only way to recover multi-link cells).
  - Cell hyperlink — the read-only `hyperlink` field attaches as a flat string (folded into
    `include_rich_text`).
  - Pivot-table definition — `include_pivot` attaches a flattened `pivot` (source, rows /
    columns / values / filters) to the pivot's anchor cell only. Read-only.
- **Structural reads on `structure(action="read")` (sheet-scoped, emitted only when present):**
  - Native Sheets tables — a `tables` key (name, A1 range, typed columns; a DROPDOWN column
    renders the same `ValidationRule` one-liner `inspect` uses).
  - Filter views + basic-filter state — `basicFilter` and `filterViews` keys (sort specs +
    per-column criteria, reusing the conditional-format condition serializer).
  - Banding — a `bandedRanges` key (per-axis header / first / second / footer hex colors).
  - Slicers — a `slicers` key (title, data range, filtered column, dashboard anchor).
- **Structural writes as new `structure` actions (full CRUD symmetry with the reads above):**
  - Tables — `add_table` / `update_table` / `delete_table` (captures new `tableId`).
  - Banding — `add_banding` / `update_banding` / `delete_banding` (captures new
    `bandedRangeId`).
  - Filters — `set_basic_filter` / `clear_basic_filter` and `add_filter_view` /
    `update_filter_view` / `delete_filter_view` (captures new `filterViewId`).
  - Slicers — `add_slicer` / `update_slicer` / `delete_slicer` (`add_slicer` takes the data
    range via the top-level `range` or `params.dataRange` and returns the new `slicerId`;
    `update_slicer` / `delete_slicer` take `params.slicerId`).
  - Spreadsheet properties — `spreadsheet_props` sets `title` / `locale` / `timeZone`
    (spreadsheet-scoped; auto fields mask).
- **Spreadsheet metadata read on `overview`:** top-level `locale` and `timeZone` (omitted
  when absent) for correct interpretation of dates and numbers.
- **New `data_ops` tool** — single-request `batchUpdate` data verbs through one dispatch:
  `find_replace`, `delete_duplicates`, `trim_whitespace`, `sort_range`,
  `text_to_columns`, `auto_fill`, and `copy_paste` / `cut_paste`, each returning an
  action-specific summary (e.g. `find_replace` surfaces
  `occurrencesChanged`/`valuesChanged`/`formulasChanged`).
- **New `dimensions` tool** — row/column operations: `insert` / `delete` / `move` /
  `append`, `auto_resize`, `set_props` for pixel size / `hiddenByUser`, and
  a `read` that reports which rows/columns are hidden.
- **New `comments` tool (full CRUD)** — read/create/reply/resolve/delete on the Drive
  threaded comments of the spreadsheet file, via the Drive API. `read` (default) flattens
  each comment to author/content/timestamps/`resolved`/quoted snippet/replies, paginates,
  and honors `include_resolved`/`include_deleted`; the opaque non-A1 `anchor` is surfaced
  raw at the document level. `create`/`reply`/`resolve`/`delete` are the write actions —
  `resolve` posts a reply carrying `action:resolve` (Drive has no standalone resolve
  endpoint). The required Drive `fields` mask is always sent; raises `drive_unavailable`
  when no Drive scope is present. The MCP tool is a write tool (`readOnlyHint=False`,
  `destructiveHint=True`); the CLI `--action delete` requires `--confirm`.
- **New `export` tool** — downloads a spreadsheet to a local file. `pdf`/`xlsx`/`ods`
  render the whole workbook server-side via Drive `files.export` (needs a Drive scope, else
  `drive_unavailable`); `csv`/`tsv` serialize a single named `--sheet` from its values
  through the Sheets API (no Drive scope). Read-only against the spreadsheet — it writes a
  local file and returns `{format, mimeType, path, bytes}`.
- **New `read_many` tool** — fans one values-or-summary read across many spreadsheets in a
  single call (the cross-file analogue of `overview`/`read_values`). Each request names one
  `spreadsheetId`; a bad id (404, permission denied, bad range) is captured as a per-file
  `{ok:false, error}` entry instead of aborting the batch, so a top-level `ok:true` does not
  mean every file succeeded. Read-only. CLI: ids and ranges live inside `--requests-json`
  (no positional id, no `--ranges`); the global `--json` flag precedes the subcommand.
- **New pure-core serializer / dispatch modules** (boundary-pure, golden-master tested
  where they serialize): `richtext`, `pivot`, `tables`, `filters`, `banding`, `slicers`,
  `comments`, `dataops`, `dimensions`, `export`, `multiread`. Five new top-level core
  functions (`data_ops`, `dimensions`, `comments`, `export`, `read_many`) are re-exported
  from `gsheets.core`.
- **Adapter + model parity:** each new core function gets a matching MCP tool and CLI
  subcommand. New Pydantic mirror models (`TextRun`, `Pivot*`, `Table`, `BasicFilter`,
  `FilterView`, `Banding`, `Slicer`, `Comment`, `CommentReply`, `DataOpsResult`,
  `DimensionsResult`, `CommentsResult`, `ExportResult`, `ReadManyResult`) and extensions
  to `Cell` / `Run` / `SheetStructure` / `OverviewResult`; the rich-text and pivot reads
  surface on `inspect` as `include_rich_text` / `include_pivot` kwargs (CLI:
  `--rich-text` / `--pivot`).
- **CI** — a GitHub Actions workflow runs the mocked test suite (including the
  boundary-guard test) on Python 3.11 / 3.12 / 3.13.

### Changed
- Re-tiered the bundled skill's reference docs into `skill/references/basic.md` (~80% of tasks),
  `intermediate.md` (~15%), and `advanced.md` (~5%) for progressive disclosure; `SKILL.md` now
  routes to the right tier on demand. Replaces the former `commands.md` / `reading.md` /
  `writing.md`.
- The CLI gained a top-level `--version` flag (`gsheets --version`).
- `structure(action="read")` now requests `sheets.rowGroups` + `sheets.columnGroups` for the
  dimension-groups read (there is no single `dimensionGroups` field on a `Sheet`); the
  flattened `dimensionGroups` output key is unchanged.
- MCP self-documentation overhaul: the server now ships server-level instructions, tool
  schemas carry `Literal` enums and per-argument `Field` descriptions, and tool
  descriptions were cut ~46%. `sheets_read_many` no longer takes an unused
  `spreadsheet_id` argument.
- The default-text CLI renderer caught up with the v0.2 reads: `structure --action read`
  now renders the `tables` / `basicFilter` / `filterViews` / `bandedRanges` / `slicers`
  terse lines, `inspect` renders rich-text runs through the canonical `text_runs_line`
  form, and `overview` shows `(locale=…, tz=…)` on the title line.
- Dev dependencies moved from `[project.optional-dependencies]` to PEP 735
  `[dependency-groups]`, so a plain `uv sync` installs them.

### Fixed
- `data_ops(action="delete_duplicates")` now reports the correct count: it reads Google's real
  `DeleteDuplicatesResponse.duplicatesRemovedCount` reply field (it previously read a
  non-existent `duplicatesRemoved`, so the surfaced `duplicatesRemoved` was always `0`). Caught
  by the new live round-trip; the unit fixture now mocks the real field name to guard it.
- Slicer anchor read: a `GridCoordinate` omits `rowIndex`/`columnIndex` when they are `0`, so a
  top-left or row-0 slicer anchor previously lost its `row`/`col`. The anchor now defaults absent
  indices to `0`, so it always reads back as `{sheet, row, col}` and the terse line always renders
  (e.g. `@ Sheet!E1` for an anchor the API returned as `{columnIndex: 4}` with `rowIndex` omitted).
- MCP tool annotations now match behavior: `sheets_export` was annotated read-only but
  overwrites local files, so it is now a write tool with `destructiveHint=True`;
  `sheets_set_validation` carries `destructiveHint=True` (`rule=None` clears the rule);
  `sheets_structure` and `sheets_metadata` declare `idempotentHint=False`.
- The `inspect` text header no longer duplicates the sheet prefix (`Dash!Dash!A1`): core
  returns `range` already sheet-qualified, and the renderer prepended the sheet again.
- The single-form `set-conditional-format` text output no longer collapses to
  `add: sheet=…`: the result was routed through the data-ops action summary, which dropped
  the `index` and `rule` fields. It now renders all four fields (`action`, `sheet`,
  `index`, `rule`).

### Notes
- Connected Sheets / data sources (`dataSourceTable`, `dataSourceFormula`, spreadsheet
  `dataSources`) are intentionally **not** given a typed tool (low signal, heavy schema);
  they remain readable through the `batch` / raw `spreadsheets.get` escape hatch.
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
  under `skill/references/`.
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

[Unreleased]: https://github.com/colindmurray/google-sheets-mcp-and-skill/compare/v0.4.2...HEAD
[0.4.2]: https://github.com/colindmurray/google-sheets-mcp-and-skill/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/colindmurray/google-sheets-mcp-and-skill/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/colindmurray/google-sheets-mcp-and-skill/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/colindmurray/google-sheets-mcp-and-skill/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/colindmurray/google-sheets-mcp-and-skill/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/colindmurray/google-sheets-mcp-and-skill/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/colindmurray/google-sheets-mcp-and-skill/releases/tag/v0.1.0
