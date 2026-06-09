# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Nothing yet.

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

[Unreleased]: https://github.com/colindmurray/google-sheets-mcp-and-skill/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/colindmurray/google-sheets-mcp-and-skill/releases/tag/v0.1.0
