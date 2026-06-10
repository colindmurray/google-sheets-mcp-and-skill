# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Google Sheets integration for AI tools: **one pure core library** (`gsheets.core`) wrapped by **two thin adapters** — an MCP server (`google-sheets-mcp`) and a CLI (`gsheets`) — plus a bundled skill (`skill/SKILL.md`). The differentiating thesis is **read-side richness**: losslessly reading a sheet's formulas + cell formatting + conditional-format rules in a compact, structured, token-efficient form, with full CRUD symmetry (anything writable is readable back).

Python 3.11+, managed with `uv`, `src/` layout.

## Commands

```sh
uv sync                                       # create .venv and install package (editable) + dev deps (pytest)
uv run pytest                                 # full unit suite — mocked, no network, no creds; live tests auto-deselect
uv run pytest tests/unit/test_condformat.py   # one file
uv run pytest -k fieldsmask                    # by keyword
uv run pytest --cov=gsheets                    # with coverage
uv run gsheets --help                          # run the CLI from the working tree
uv run python -c "import gsheets.mcp_server"   # verify the MCP server module imports (it has no --help; it's a stdio server)
```

CI (`.github/workflows/ci.yml`) runs `uv run pytest` on Python 3.11/3.12/3.13. A PR must be green before merge.

**Live integration tests** (`tests/integration/`) are opt-in and skipped by default. They are gated on `GSHEETS_LIVE=1` **and** `GSHEETS_TEST_SPREADSHEET_ID`, and carry a hashed denylist guard so they can never run against a Production sheet. Never point them at a real/production spreadsheet.

## The architecture rule you MUST preserve

This is the single most important rule; the whole project is built on it.

> **A pure core, wrapped by two thin adapters that map 1:1 to the core. Behavior is identical from either entrypoint. There is no duplicated Sheets logic.**

1. **The core is pure.** `src/gsheets/core/**` and `src/gsheets/auth/**` import **only** the standard library plus `googleapiclient` / `google.auth*`. They must **never** import `fastmcp`, `mcp`, `argparse`, `pydantic`, or `gsheets.models`. This is enforced by `tests/unit/test_boundary_guard.py`, which runs in a fresh subprocess and asserts `import gsheets.core` (and `gsheets.auth`) drags none of those into `sys.modules`. **If you break the boundary, this test fails** — it must pass before any merge.
   - The one deliberate exception: `core/export.py` binds `MediaIoBaseDownload` lazily inside `_export_via_drive` (a top-level import would leak `argparse` via httplib2). Keep that import lazy.
2. **The adapters are thin.** `mcp_server.py` is the **only** module importing `fastmcp`/`mcp` and `gsheets.models`. `cli.py` is the **only** module importing `argparse`. Each tool/subcommand body is essentially one line: resolve `services`, call the matching core function, return/print its result. If you find yourself parsing a `GridRange`, building a fields mask, or serializing a rule inside an adapter — stop, that belongs in core.

Core function contract: first param is `services: SheetsServices`, second is `spreadsheet_id: str`; accept A1 ranges everywhere and resolve sheet-name→`sheetId` / A1→`GridRange` *inside* core; return a plain JSON-serializable dict carrying `"ok": True`; raise `SheetsError` on failure (never return an error dict).

## Layout

```
src/gsheets/
  core/          PURE library — one cohesive concern per module:
                   functions:   values, reads, formatting, rules, structure, charts, batch,
                                 dataops, dimensions, comments, export, multiread
                   helpers:     addressing (A1↔GridRange), colors (hex↔ColorStyle),
                                 fieldsmask (auto-build fields mask from payload), flatten,
                                 condformat, errors (SheetsError + classify_google_error), service
                   serializers: richtext, pivot, tables, filters, banding, slicers, comments
                   __init__.py re-exports the 20 public core functions
  auth/          Credential resolution (Service Account / OAuth desktop / ADC). Reads ONLY env vars,
                 least-privilege scopes. Builds SheetsServices and hands it to core. The ONLY place
                 credentials resolve and the ONLY place interactive OAuth consent runs (CLI path only).
  mcp_server.py  FastMCP stdio adapter: one tool per core fn, ToolAnnotations, ENABLED_TOOLS allowlist.
  cli.py         argparse adapter: one subcommand per core fn, global --json.
  models.py      Adapter-side Pydantic mirror models (one per core return dict). MCP-only.
```

20 core functions, each exposed as exactly one `sheets_*` MCP tool and one CLI subcommand (CLI adds an auth-only `auth login|status`). The understanding path is `overview → inspect → read_conditional_formats`; writers follow; the raw `batch` escape hatch is last.

## Adding a tool (touches a predictable set of files — keep both adapters in lockstep)

1. Design + implement the core function in the right `core/` module; re-export it from `core/__init__.py`. Reuse shared helpers (`addressing`, `colors`, `fieldsmask`, `flatten`, `condformat`) — never hand-write a fields mask or duplicate sibling logic.
2. Unit test at `tests/unit/test_<module>.py` against the `mock_sheets_service` fixture. If it serializes/parses, add a golden-master fixture under `tests/unit/golden/` and a round-trip assertion. **If you change a serializer's output, update its golden file in the same change.**
3. Add the Pydantic mirror model in `models.py` (mechanical mirror — same field names, plus a terse rendering; never reshape).
4. Register the MCP tool in `mcp_server.py` (one-line body, correct `ToolAnnotations` — `readOnlyHint` for reads, `destructiveHint` on destructive paths, `openWorldHint=True`, tag `{"read"}`/`{"write"}`; example-rich docstring using `<YOUR_SPREADSHEET_ID>`; honor `ENABLED_TOOLS`).
5. Add the CLI subcommand in `cli.py` (subcommand name = core fn name with hyphens; flags map 1:1 to core kwargs; supports `--json`).
6. Document in `skill/SKILL.md` + `skill/references/*.md` and `docs/ARCHITECTURE.md`'s function table.

## Sheets API gotchas baked into core (easy to get silently wrong)

- Writes default to **`USER_ENTERED`** so `=`-strings become live formulas; `RAW` stores them as inert text. Don't hardcode RAW.
- Never `includeGridData`; always `ranges[]` + a tight `fields` mask. Rich reads (`--rich-text`, `--pivot`) add to the mask only when requested, and attach per-cell data only to cells that have it.
- Every formatting/properties write **auto-builds its `fields` mask from the payload** (`fieldsmask.build_fields_mask`) so a partial update never wipes unspecified subfields. Some Google sub-objects are atomic leaves (`*ColorStyle`, `numberFormat`, `padding`, `textRotation`) — masked at the parent; `textFormat` is *not* atomic.
- **Conditional-format rules have no stable id** — their positional index in the per-sheet array *is* their priority (index 0 = highest). Address by index; when mutating several in one batch, core orders high→low so earlier edits don't shift later targets.
- `GridRange` is 0-based half-open; A1 is 1-based inclusive — the conversion is centralized in `addressing`.
- Reads flatten colors to hex; writes always use `ColorStyle` (`rgbColor`/`themeColor`), never the deprecated flat `Color`.
- Values reads omit empty trailing rows/cols — core pads jagged arrays to a rectangle.

The conditional-format **line grammar** (the headline read feature — each rule's body serialized to one terse, round-trippable line) and all v0.2 serializer shapes are fully specified in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Read it before touching `condformat` or any serializer.

## Security / privacy (this repo is public)

Credentials and real spreadsheet IDs come **only** from env vars / local config at runtime — never the committed tree. All docs, examples, and MCP tool docstrings use the placeholder `<YOUR_SPREADSHEET_ID>`. Auth env vars are `GSHEETS_*` (see README "Authentication") — config lives under `~/.config/google-sheets-mcp/`, outside any repo.

## Further reading

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — full design contract: layer diagram, CF grammar, serializer shapes, the boundary test rationale.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev workflow, the boundary rule, the add-a-tool checklist in full.
- `internal/` holds historical planning/research docs (`task.md`, `DESIGN.md`, research/). Note `internal/CLAUDE.md` is **stale** — it predates the build and describes the repo as greenfield; ignore it.
