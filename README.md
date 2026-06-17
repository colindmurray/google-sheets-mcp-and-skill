# google-sheets-mcp-and-skill

[![CI](https://github.com/colindmurray/google-sheets-mcp-and-skill/actions/workflows/ci.yml/badge.svg)](https://github.com/colindmurray/google-sheets-mcp-and-skill/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/google-sheets-mcp-and-skill)](https://pypi.org/project/google-sheets-mcp-and-skill/)
[![Python](https://img.shields.io/pypi/pyversions/google-sheets-mcp-and-skill)](https://pypi.org/project/google-sheets-mcp-and-skill/)

Read a Google Sheet the way it actually works — formulas, real colors, conditional-format rules, native tables, filter state, in-cell rich text, and the comments humans left on it — then write it back safely, or export the whole thing to PDF, Excel, or CSV. Render any read as text, JSON, JSONL, CSV, TSV, or Markdown. One core library, exposed as both an MCP server and a CLI (with a bundled skill).

## The problem

A serious spreadsheet is rarely just a grid of values. It's a small application: derived columns built from formulas, cells that change color from conditional-format rules, merged headers, dropdowns, native tables with typed columns, filter views that hide half the rows, frozen panes. The values you can see are the *output*; the logic that produces them lives in the formulas, the formatting, the conditional-format rules, and the structure around them.

To understand such a sheet before changing it, whether you're an AI agent or a person, you have to read that logic losslessly. A screenshot tells you a cell is red, but not *why* it's red, what formula feeds it, which rule would turn it green, or that the table is filtered so the row you're about to edit isn't the row you're looking at. "What's the value in C7?" is the easy question. "What formula computes C7, which conditional-format rule sets its background and at what threshold, and is this column part of a named table?" is the one that matters, and the one most tooling can't answer.

That gap is the whole point of this project. The thesis is **read-side richness**, built on three reads: values *and* the formulas behind them; the format a cell *actually renders* (`effectiveFormat`, which already folds in conditional-format results) alongside the author's intent (`userEnteredFormat`); and every conditional-format rule serialized to one terse, readable, round-trippable line. The comparison table below carries the rest of the surface (tables, filters, rich text, pivots, banding, comments, export). Everything goes through tight field masks so it stays cheap enough to put in an LLM's context, with rich data attached per-cell only when actually present, so a plain sheet costs nothing extra. And because every read shape can be written back, an agent can audit a sheet, propose a change, apply it, and re-read to confirm.

## How it compares

This was built after surveying the Google Sheets MCP servers and skills people actually use. The four below are the ones worth comparing against, chosen by traction (star counts verified with `gh` on 2026-06-09):

- **[xing5/mcp-google-sheets](https://github.com/xing5/mcp-google-sheets)** — 900★ — the recommended dedicated Sheets MCP (listed in `awesome-mcp-servers`, on Homebrew and PyPI).
- **[taylorwilsdon/google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp)** — 2,639★ — the most-starred Workspace MCP and the closest analogue in shape (it ships a server, a CLI, and a bundled skill).
- **[a-bonus/google-docs-mcp](https://github.com/a-bonus/google-docs-mcp)** — 563★ — the densest Sheets feature surface of the bunch (native tables, comments, CF round-trip, charts).
- **[gemini-cli-extensions/workspace](https://github.com/gemini-cli-extensions/workspace)** — 585★ — Google's official Workspace extension, listed in `google/mcp`. Its Sheets surface is read-only.

Legend: ✅ first-class · ⚠️ partial, indirect, or build-only · ❌ absent. A raw `batchUpdate` pass-through (xing5's `batch_update`, this project's `sheets_batch`) can reach most of the API by hand-built JSON; it is not counted toward any capability row, and neither is a raw `includeGridData` dump (xing5's `get_sheet_data`) — rows grade structured, purpose-built reads and writes only.

| Capability | xing5 | taylorwilsdon | a-bonus | gemini (official) | **this** |
|---|:---:|:---:|:---:|:---:|:---:|
| Read formulas **side by side** with computed values | ⚠️ separate call | ⚠️ flag | ⚠️ flag | ❌ | ✅ |
| Write formulas (`USER_ENTERED`, not inert `RAW`) | ✅ | ✅ | ✅ | ❌ read-only | ✅ |
| Read cell formatting + colors (flattened, **effective *and* user-entered**) | ❌ raw blob only | ❌ write-only | ⚠️ single format, no split | ❌ | ✅ |
| **Read conditional-format rules** (terse round-trippable lines) | ❌ | ⚠️ summary lines (bg/fg-only rule model) | ✅ structured, no round-trip grammar | ❌ | ✅ |
| Write conditional-format rules (index-safe) | ❌ | ✅ | ✅ | ❌ | ✅ |
| Data validation **read *and* write** (round-trip) | ❌ | ❌ | ⚠️ write-only dropdown | ❌ | ✅ |
| Native Sheets **Tables** (typed-column read + CRUD) | ❌ | ⚠️ list + append | ✅ | ❌ | ✅ |
| **Per-run rich text + in-cell hyperlinks** (read) | ❌ | ⚠️ hyperlink URLs only (incl. run-level links), no styled runs | ❌ | ❌ | ✅ |
| **Filter views + basic-filter state** (read) | ❌ | ❌ | ❌ | ❌ | ✅ |
| **Pivot definitions** (read) | ❌ | ❌ | ❌ | ❌ | ✅ |
| **Banding + slicers (read *and* write)** | ❌ | ❌ | ❌ | ❌ | ✅ |
| **Drive threaded comments** (full CRUD) | ❌ | ⚠️ list/create/reply/resolve, no delete | ✅ | ❌ | ✅ |
| **Export to PDF / XLSX / ODS / CSV / TSV** | ❌ | ⚠️ Drive download-URL export (XLSX/PDF/CSV) | ⚠️ generic Drive export (PDF/CSV/XLSX) | ❌ | ✅ |
| **Multi-spreadsheet batch reads** | ✅ | ❌ | ❌ | ❌ | ✅ |
| Embedded charts (create / update / delete + read) | ⚠️ add only | ❌ | ⚠️ insert/delete | ❌ | ✅ (read = metadata list) |
| Developer metadata (durable anchors) | ❌ | ❌ | ❌ | ❌ | ✅ |
| Data verbs (find/replace, dedupe, sort, paste-type) | ⚠️ find only | ⚠️ move rows | ❌ (find/replace is Docs-only) | ❌ | ✅ |
| Pure core, no transport coupling (CLI-able) | ⚠️ ctx-coupled | ⚠️ CLI is a proxy to the running server | ⚠️ | n/a | ✅ |
| Ships **MCP server + CLI + bundled skill** | MCP only | MCP + proxy CLI + skill | MCP only | MCP + read-only skill | ✅ MCP + CLI + skill |
| Auth models | SA + OAuth + ADC | OAuth 2.0/2.1 + SA (DWD) | OAuth + SA | (Google account) | SA + OAuth + ADC, least-privilege default |

Where each falls short:

- **xing5** can't read formatting without pulling the raw `includeGridData` blob, and has no structured conditional-format read — the rules surface only inside that raw dump. Flattened formats (effective *and* user-entered) plus a terse CF line grammar are exactly what it lacks.
- **taylorwilsdon** is strong on CF *write* and has table-append and dimension ops, but its conditional-format model is background/text color only — rules using bold, italic, or number formats can't be expressed and summarize lossily on read — and it never reads cell formats, styled rich-text runs, pivots, filter views, or banding. Its CLI is a network proxy to the running MCP server, not a standalone adapter over shared logic.
- **a-bonus** is the densest Sheets surface and the most direct feature rival: the only competitor here with native tables, comments, and CF round-trip. With comments CRUD and chart writes in this tool, there's no longer a Sheets capability it has that this one doesn't, and it still lacks rich-text runs, the effective-vs-user format split, pivot/filter-view/banding read, structured validation round-trip, and developer-metadata anchors.
- **gemini-cli-extensions/workspace** is Google's official answer, and its Sheets surface is read-only (`getText`/`getRange`/`getMetadata`) — no write, format, CF, tables, or charts. Google ships no managed Sheets MCP, and its official open-source extension can't write or format a sheet.

The official spreadsheet skill in [`anthropics/skills`](https://github.com/anthropics/skills) (the `xlsx` skill) operates on **local Excel files and explicitly excludes Google Sheets**. The largest Google Workspace skill, [`googleworkspace/cli`](https://github.com/googleworkspace/cli)'s `gws-sheets`, has values-only curated helpers (`+append`/`+read`); everything past those is raw REST passthrough, so the agent hand-builds `batchUpdate` JSON with no structured read of formats, CF rules, or tables. Neither reads a Google Sheet with any depth, which is what the bundled `SKILL.md` here is for.

*(One more worth knowing despite fewer stars: [freema/mcp-gsheets](https://github.com/freema/mcp-gsheets) (73★) is the only other server in this survey that reads `basicFilter`, and the strongest small reference for formatting reads.)*

## What it's good at

- **Auditing or documenting a formula- and conditional-formatting-driven sheet** — read the logic instead of guessing from a screenshot.
- **Understanding a sheet you didn't build** — `overview` → `inspect` → `read-conditional-formats` → `structure --action read` gives you formulas, real colors, CF rules, table schemas, filter state, banding, and named/protected ranges in one cheap pass.
- **Reading a whole region in one call** — `describe` folds values, formulas, formats, merges, notes, validation, and structure for a range (or developer-metadata-addressed block) into a single structured read, with a `--max-cells` guard that fails fast before returning an oversized payload.
- **Seeing a column's formula logic without reading every cell** — `formula-patterns` dedupes the formulas down a column into distinct R1C1-relative patterns plus their row spans, so a 10,000-row derived column reads as a handful of patterns.
- **Choosing the output shape the consumer needs** — render any read as text, JSON, JSONL, CSV, TSV, or Markdown (`--format` on the CLI; `output_format` per read tool over MCP), so a value grid can drop straight into a CSV pipeline or a Markdown table.
- **Not editing the wrong row** — read the active filter view and basic filter first, so an agent knows which rows are hidden before it touches anything.
- **Round-tripping conditional-format rules** — read a rule as a readable line, edit the line, write it back at the same priority index.
- **Recovering multi-link cells and styled text** — per-run rich text plus in-cell hyperlinks, the only way to read a cell holding more than one link.
- **Acting on human review** — pull the Drive comments threaded on the file, treat them as the change request, then reply to or resolve them in place.
- **Bulk data hygiene without the escape hatch** — find/replace (regex-aware), de-dupe, trim, sort, split text to columns, auto-fill, and paste-type-aware copy/cut, each a first-class verb.
- **Handing a sheet to a human** — export the whole workbook to PDF, Excel, or ODS, or a single tab to CSV/TSV.
- **Reading across many files at once** — pull values or summaries from a set of spreadsheets in one call, with per-file errors instead of an all-or-nothing failure.

## Two ways to use it

Both paths run the exact same core. Behavior is identical whether you call a tool over MCP or a subcommand in a shell.

> Published on [PyPI](https://pypi.org/project/google-sheets-mcp-and-skill/). Install with `uv` (or `pipx`) as shown below. To run the unreleased `main` instead, replace the package name with `git+https://github.com/colindmurray/google-sheets-mcp-and-skill`.

### A. MCP server

Install with `uv` (this links both the `gsheets` CLI and the `google-sheets-mcp` server):

```sh
uv tool install google-sheets-mcp-and-skill
```

Register it with Claude Code (or any MCP client). The server speaks stdio; pass auth via `--env`:

```sh
claude mcp add google-sheets \
  --env GSHEETS_AUTH_MODE=oauth \
  --env GSHEETS_TOKEN_FILE="$HOME/.config/google-sheets-mcp/token.json" \
  -- uvx --from google-sheets-mcp-and-skill google-sheets-mcp
```

The MCP server needs a **pre-existing, valid or refreshable token**: it never opens a browser consent prompt mid-session, which would hang the JSON-RPC channel. Mint the token once with `gsheets auth login` (see [Authentication](#authentication)). If credentials can't be resolved at startup, the server writes a clear message to stderr and exits non-zero instead of crashing.

The server registers one `sheets_*` tool per core function — the full list is in the [command / tool reference](#command--tool-reference) below. Read tools are annotated `readOnlyHint`; destructive paths carry `destructiveHint`. Set `ENABLED_TOOLS` to a comma-separated allowlist to register only a subset.

The read tools (`sheets_read_values`, `sheets_inspect`, `sheets_describe`, `sheets_formula_patterns`, `sheets_read_many`) take an `output_format` arg to pick the rendering — `read_values` accepts `text`/`json`/`jsonl`/`csv`/`tsv`/`markdown`; the structured reads accept `text`/`json`/`jsonl`/`markdown` (no `csv`/`tsv`). Those same five tools accept `out_path`: an MCP-only escape valve that writes the rendered read to a local file and returns a small handle (path, format, row/col counts, byte size, a short preview) instead of streaming the whole payload back through the model. Paths are resolved and safety-checked — credential-shaped filenames and the config/secrets subtrees are refused, and the parent directory must already exist.

### B. CLI + skill

The same surface as a command-line tool. Install gives you `gsheets`:

```sh
uv tool install google-sheets-mcp-and-skill   # provides `gsheets` and `google-sheets-mcp`
```

Every subcommand maps 1:1 to a core function. A session reading a sheet looks like this:

```sh
# Orient — cheap, no grid data.
$ gsheets overview <YOUR_SPREADSHEET_ID>
Team Budget  [<YOUR_SPREADSHEET_ID>]  (locale=en_US, tz=America/New_York)
  [0] Budget  200x8 (id=0)  frozenRows=1 protected=1 cf=3 tab=#0B8043
  [1] Dashboard  40x12 (id=205)
  named: categories -> Budget!A2:A40

# Read formula AND computed value together.
$ gsheets read-values <YOUR_SPREADSHEET_ID> 'Budget!C1:D2' --render all
render=all
# Budget!C1:D2
  Spent => Spent | Remaining => Remaining
  =SUM(C5:C40) => $1,824.00 | =B2-C2 => $676.00

# Fold a whole region — values, formulas, formats, merges, notes, validation, structure — into one read.
$ gsheets describe <YOUR_SPREADSHEET_ID> 'Budget!A1:E40' --max-cells 5000

# Dedupe a column's formulas into distinct patterns + their row spans.
$ gsheets formula-patterns <YOUR_SPREADSHEET_ID> 'Budget!E2:E40'

# Read the conditional-format rules that color cells dynamically (whole sheet, or scope to a range).
$ gsheets read-conditional-formats <YOUR_SPREADSHEET_ID> --sheet Budget
# Budget (id=0)
  [0] [Budget!D2:D40] if CUSTOM_FORMULA(=$D2<0) -> bg #F4C7C3 bold
  [1] [Budget!C2:C40] if NUMBER_GREATER(500) -> fg #B45309 bold
  [2] [Budget!E2:E40] gradient min=#FFFFFF | max=#0B8043

$ gsheets read-conditional-formats <YOUR_SPREADSHEET_ID> --range 'Budget!D2:D40'  # only rules overlapping this range

# The full structural picture: tables, filters, banding, slicers, protected ranges.
$ gsheets structure <YOUR_SPREADSHEET_ID> --action read --sheet Budget
# Budget (id=0)
  frozenRows: 1
  protected: Budget!A1:E1 (header row)
  table "Expenses" [Budget!A1:E40] cols: Category:TEXT, Status:DROPDOWN(Planned,Paid), Budgeted:CURRENCY, Spent:CURRENCY, Remaining:CURRENCY
  basicFilter [Budget!A1:E40] sort E desc | B: hide Paid
  filterView 88412 "Over budget" [Budget!A1:E40] E: NUMBER_LESS(0)
  banding 412 [Budget!A1:E40] rows: hdr #0B8043 / #FFFFFF / #E6F4EA
  slicer 3 "Status" col 1 [Budget!A1:E40] @ Dashboard!G2

# Read the in-cell rich text and links most tools can't see (per-run; multi-link cells recoverable).
$ gsheets inspect <YOUR_SPREADSHEET_ID> 'Dashboard!A2' --rich-text
Dashboard!A2  1x1
  A2  Policy / Receipts  runs: "Policy"[0:6 fg #1155CC bold link https://example.com/policy] + " / Receipts"[6:17 link https://example.com/receipts]

# Read what the humans asked for.
$ gsheets comments <YOUR_SPREADSHEET_ID>
comment AAABcZwq8jM by Dana Kim: "Can we split Utilities out of Misc?" (open, 1 reply)
```

Add `--json` (before the subcommand) to get the exact machine shape — the raw core dict — for piping to `jq`. `--json` is an alias for the global `--format json`; the full axis is `--format {text,json,jsonl,csv,tsv,markdown}` (default `text`), uniform across subcommands. `csv`/`tsv` only render a rectangular value grid (`read-values`) — on a structured read they raise `format_unsupported`. `read-values` also takes `--major {rows,columns}` and, like `describe`, can address by `--data-filter-json` (an A1, `gridRange`, or `developerMetadataLookup` selector) instead of a positional range.

Writing follows the same read → write → read-back rhythm:

```sh
# Write a live formula (USER_ENTERED: "=B2-C2" becomes a formula, not literal text).
$ gsheets write-values <YOUR_SPREADSHEET_ID> 'Budget!D2' --values-json '[["=B2-C2"]]'
updatedRanges: ["Budget!D2"]
updatedCells: 1
updatedRows: 1
updatedColumns: 1

# Apply formatting; the fields mask is auto-built from exactly the keys you pass.
$ gsheets format <YOUR_SPREADSHEET_ID> 'Budget!D2:D40' --bg '#F4C7C3' --bold --number '$#,##0.00'
range: Budget!D2:D40
appliedFields: userEnteredFormat(backgroundColorStyle,textFormat.bold,numberFormat)

# Add a conditional-format rule from the same readable line you'd read back (index 0 = top priority).
$ gsheets set-conditional-format <YOUR_SPREADSHEET_ID> --action add --sheet Budget --index 0 \
    --rule '[Budget!D2:D40] if CUSTOM_FORMULA(=$D2<0) -> bg #F4C7C3 bold'
action: add
sheet: Budget
index: 0
rule: [Budget!D2:D40] if CUSTOM_FORMULA(=$D2<0) -> bg #F4C7C3 bold

# Reply to a review comment and resolve it.
$ gsheets comments <YOUR_SPREADSHEET_ID> --action reply --comment-id AAABcZwq8jM --content 'Done, new Utilities rows added.'
commentId: AAABcZwq8jM
reply: {"author": "Alex Rivera", "content": "Done, new Utilities rows added."}

$ gsheets comments <YOUR_SPREADSHEET_ID> --action resolve --comment-id AAABcZwq8jM
commentId: AAABcZwq8jM
resolved: True
reply: {"author": "Alex Rivera", "action": "resolve"}

# Export the whole workbook to PDF, or one tab to CSV.
$ gsheets export <YOUR_SPREADSHEET_ID> --format pdf --path ./team-budget.pdf
exported pdf -> ./team-budget.pdf (182044 bytes)

$ gsheets export <YOUR_SPREADSHEET_ID> --format csv --sheet Budget --path ./budget.csv
exported csv -> ./budget.csv (2113 bytes)
```

A fuller CLI walkthrough is in [docs/usage.md](docs/usage.md), and [examples/](examples/) holds runnable recipe scripts.

**Installing the skill.** A bundled `SKILL.md` lives at [`skill/SKILL.md`](skill/SKILL.md), with deeper references under [`skill/references/`](skill/references/). It wraps the `gsheets` CLI for agents that support the skill format — drop the `skill/` directory into your agent's skills location (for Claude Code, copy it to `~/.claude/skills/gsheets/`) and put `gsheets` on `PATH`. The skill teaches the understand → change → escape-hatch workflow and the safe-write defaults; the CLI is the deterministic helper underneath.

## Authentication

Credentials are resolved from **environment variables and local config at runtime**: never hardcoded, never committed. Three sources are supported, least-privilege scopes by default.

Bootstrap a token once, then verify:

```sh
gsheets auth login     # OAuth desktop consent, or refresh/validate an existing token
gsheets auth status    # report resolved mode, scopes, token path, expiry; non-zero if unusable
```

`gsheets auth login` is the only place interactive OAuth consent runs (a CLI path, never the MCP server). Once a token exists, both the CLI and the server use it.

### Sources and precedence

With `GSHEETS_AUTH_MODE=auto` (the default), the first match wins:

1. **Service Account** — if `GSHEETS_SERVICE_ACCOUNT_FILE` is set, or `GOOGLE_APPLICATION_CREDENTIALS` points at a service-account key. Best for headless automation; share each target sheet (or a Drive folder) with the service account's email.
2. **OAuth 2.0 Desktop** — a cached authorized-user token (`GSHEETS_TOKEN_FILE`), or an OAuth desktop-client file (`GSHEETS_OAUTH_CLIENT_FILE`) for first-time consent. A valid/refreshable token refreshes in place without the client file. Simplest for a single personal account.
3. **ADC** — `google.auth.default()` fallback (honors `GOOGLE_APPLICATION_CREDENTIALS`, `gcloud` user creds, GCE/Cloud Run metadata).

Set `GSHEETS_AUTH_MODE` to `service_account`, `oauth`, or `adc` to force a single source.

### Environment variables

| Variable | Meaning | Default |
|---|---|---|
| `GSHEETS_AUTH_MODE` | `service_account` \| `oauth` \| `adc` \| `auto` | `auto` |
| `GSHEETS_SERVICE_ACCOUNT_FILE` | Path to a service-account JSON key | unset |
| `GSHEETS_OAUTH_CLIENT_FILE` | Path to an OAuth **desktop client** secrets file | `~/.config/google-sheets-mcp/credentials.json` |
| `GSHEETS_TOKEN_FILE` | Cached authorized-user token (written after consent) | `~/.config/google-sheets-mcp/token.json` |
| `GOOGLE_APPLICATION_CREDENTIALS` | Standard Google ADC / service-account path | unset |
| `GSHEETS_SCOPES` | `default` (narrow) \| `broad` \| explicit comma-separated list | `default` |
| `GSHEETS_CONFIG_DIR` | Override the default config dir | `~/.config/google-sheets-mcp/` |
| `ENABLED_TOOLS` | (MCP only) comma-separated tool allowlist; empty = all | unset |
| `GSHEETS_VERBOSE_ERRORS` | `1` lets error hints include the authenticated account email (off by default, so it never leaks in pass-through errors) | unset |
| `GSHEETS_MAX_RETRIES` | **Legacy** retry opt-in (retry is now OFF by default, v0.4.0). A value `> 0` enables retry with that many retries and the `exponential_jitter` strategy; `0` keeps retry disabled | unset (off) |
| `GSHEETS_BACKOFF_STRATEGY` | `none` \| `fixed` \| `exponential` \| `exponential_jitter`. Any non-`none` value **enables** retry (the canonical opt-in) | unset (off) |
| `GSHEETS_BACKOFF_MAX_RETRIES` | Retries after the first try (total tries = `1 + N`); canonical name for the legacy `GSHEETS_MAX_RETRIES` | `4` |
| `GSHEETS_BACKOFF_BASE_DELAY` | Base backoff delay in seconds | `0.5` |
| `GSHEETS_BACKOFF_MAX_DELAY` | Per-attempt sleep cap in seconds | `30.0` |
| `GSHEETS_BACKOFF_DEADLINE` | Overall wall-clock cap (seconds) across all sleeps; `<= 0` or `none` means no overall cap | `60.0` |
| `GSHEETS_BACKOFF_HONOR_RETRY_AFTER` | Honor a server `Retry-After` header (`1`/`0`/`true`/`false`) | `true` |
| `GSHEETS_BACKOFF_RETRY_AFTER_CAP` | Cap (seconds) applied to a server `Retry-After` value | `60.0` |
| `GSHEETS_BACKOFF_LOG` | `1` emits one stderr line per retry (diagnostic) | unset (off) |
| `GSHEETS_HTTP_TIMEOUT` | Socket timeout (seconds) for API calls; raise it for very large grid reads | library default |

### Scopes (least-privilege default)

| `GSHEETS_SCOPES` | Scopes granted |
|---|---|
| `default` | `spreadsheets`, `drive.file` (only files this app creates or opens) |
| `broad` | the above **plus** full `drive` (cross-file discovery) |
| explicit list | exactly the comma-separated scopes you pass |

The default deliberately avoids whole-Drive access; opt into `broad` only when you need to discover sheets you didn't create through this tool. Note that `sheets_comments` and PDF/Excel `sheets_export` go through the Drive API: `drive.file` covers files this app created or opened; reading comments on, or exporting, a sheet you only have a link to needs `broad`.

### Scope reconciliation (cached tokens)

A cached token is refreshed against the scopes it was originally granted, never the current `GSHEETS_SCOPES` request (Google rejects a refresh whose scopes aren't a subset of the original consent). If the grant doesn't cover what a call needs, you get an `oauth_scope_insufficient` error telling you to re-run `gsheets auth login`. In short: a token minted with one scope set keeps working across `default`/`broad` requests as long as its grant covers them — the full walkthrough is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

### Retry & backoff

**Retry is OFF by default (since v0.4.0).** A 429 or 5xx fails fast unless you opt in — a deliberate breaking change from the old behavior of 4 automatic retries on every call. Opt in per call, on either adapter, or via env vars; the three styles are mutually exclusive.

- **CLI.** A one-shot catch-all preset, `--default-backoff-strategy` (full-jitter exponential backoff, 4 retries, a 60s overall deadline), OR full granular control: `--retries N`, `--backoff {none,fixed,exponential,exponential-jitter}`, `--retry-base-delay S`, `--retry-max-delay S`, `--retry-deadline S` (`<= 0` ⇒ no overall cap), `--retry-after-cap S`, and `--honor-retry-after` / `--no-honor-retry-after`. `--no-retry` forces fail-fast (overrides any `GSHEETS_BACKOFF_*` env). The preset, `--no-retry`, and the granular flags are mutually exclusive (a conflict is a clean `backoff_flags_conflict` error). These are **global** flags, so they go before the subcommand.
- **MCP.** Every tool (read and write) takes an optional per-call `retry` object: omit it for no retry (fail fast), set `preset: "default"` for the sensible exponential-jitter backoff, set `preset: "off"` to force disable, or set granular fields (`strategy`, `max_retries`, `base_delay`, `max_delay`, `deadline`, `honor_retry_after`, `retry_after_cap`). `preset` and the granular fields are mutually exclusive.
- **Env.** `GSHEETS_BACKOFF_STRATEGY` (any non-`none` value enables retry) plus the `GSHEETS_BACKOFF_*` knobs above; the legacy `GSHEETS_MAX_RETRIES > 0` still enables retry. With no explicit per-call retry config, a call resolves its policy from these env vars (and stays off if none enable it).

When retry is enabled and a call still fails after exhausting it, the structured error carries `retries` and `waitedMs` so you can see how hard it tried. Retry smooths transient bursts, but it cannot conjure quota that is already spent — **batching is still the real quota fix**: pass many ranges to one `read-values` (one `batchGet`), read many files with one `read-many`, and prefer `export` for bulk value dumps.

## Command / tool reference

Each CLI subcommand maps 1:1 to a core function and to the matching `sheets_*` MCP tool.

| CLI command | MCP tool | What it does |
|---|---|---|
| `overview` | `sheets_overview` | Cheap orientation: title, locale/timeZone, tabs, sizes, frozen panes, per-sheet protected/conditional-format **counts**, named ranges. No grid data. Call this first. |
| `inspect` | `sheets_inspect` | Rich read of a range: per-cell values + formulas + userEntered & effective formats + merges + notes + structured validation. `--rich-text` adds per-run styled text and in-cell links; `--pivot` adds pivot definitions on anchor cells; `--compact` collapses repeats into rectangular runs. |
| `describe` | `sheets_describe` | One-call region read: folds values, formulas, formats, merges, notes, validation, and the structure overlapping the range into a single structured result. Address by range(s) or by developer-metadata-addressed block (`--data-filter-json`); `--max-cells` fails fast before returning an oversized payload. |
| `formula-patterns` | `sheets_formula_patterns` | Dedupe a column's formulas into distinct R1C1-relative patterns and their row spans (column-major), so a long derived column reads as a few patterns instead of thousands of cells. `--no-sample` skips the sampled computed-value pass. |
| `read-values` | `sheets_read_values` | Plain values for one or more ranges; `--render` = `plain` \| `unformatted` \| `formula` \| `all` (formula + computed side by side). `--major rows\|columns` flips the orientation; `--data-filter-json` addresses by selector instead of A1. |
| `read-conditional-formats` | `sheets_read_conditional_formats` | Conditional-format rules serialized to readable lines, each with its positional `index`. Scope to one sheet (`--sheet`) or just the rules overlapping a range (`--range`). The read this project was built around; most Sheets tooling can't read these rules at all. |
| `read-many` | `sheets_read_many` | Read values or summaries across **several spreadsheets** in one call; a bad id becomes a per-file error instead of failing the batch. |
| `comments` | `sheets_comments` | Drive threaded comments — **read, create, reply, resolve, delete** (`--action`). Uses the Drive API. |
| `export` | `sheets_export` | Download the workbook to PDF / XLSX / ODS (whole file, via Drive), or a single tab to CSV / TSV. Writes a local file and reports the path + byte count. |
| `write-values` | `sheets_write_values` | Write/update one or more ranges in one call. `USER_ENTERED` by default, so formulas stay live. |
| `append-rows` | `sheets_append_rows` | Append rows after a table's last row (`INSERT_ROWS`, never overwrites). |
| `clear` | `sheets_clear` | Clear values, and optionally formats / validation / notes, from ranges. |
| `format` | `sheets_format` | Apply fill, font, number/date pattern, alignment, wrap, padding, borders, and notes atomically; field mask auto-built from the payload. |
| `set-conditional-format` | `sheets_set_conditional_format` | Add / update / delete a boolean or gradient rule by positional index; the batch form mutates several rules index-safe in one call. |
| `set-validation` | `sheets_set_validation` | Set or clear data validation (dropdowns, number/date/text/custom-formula); round-trips with `inspect`. |
| `structure` | `sheets_structure` | Read or modify merges, named/protected ranges, frozen panes, tab color, row/column groups — **and** read native tables, basic filter, filter views, banding, slicers, with CRUD for tables, banding, filters, and slicers, plus spreadsheet props (title/locale/timeZone). |
| `manage-sheets` | `sheets_manage_sheets` | Add / delete / duplicate / rename / reorder tabs; returns new sheet ids. |
| `metadata` | `sheets_metadata` | Read / write developer metadata: durable anchors that survive row inserts, unlike A1. |
| `dimensions` | `sheets_dimensions` | Row/column ops: insert / delete / move / append / auto-resize / set pixel-size or hidden; plus a read action returning which rows/cols are hidden. |
| `data-ops` | `sheets_data_ops` | Data verbs: find/replace (regex-aware), delete-duplicates, trim-whitespace, sort-range, text-to-columns, auto-fill, and paste-type-aware copy/cut-paste. |
| `charts` | `sheets_charts` | Create / update / delete / list embedded charts (read returns chart metadata). |
| `batch` | `sheets_batch` | Escape hatch: a raw ordered list of `batchUpdate` requests, for anything the typed tools don't cover. |
| `auth login` / `auth status` | — (CLI only) | Bootstrap / inspect credentials. |

Run `gsheets <command> --help` for the exact, current flags of any subcommand — that's the authoritative source.

A few behaviors worth knowing:

- **Writes default to `USER_ENTERED`.** Strings starting with `=` become live formulas; `5%` / `$3` parse like typed input. Pass `--input raw` (or `input="raw"`) to store text verbatim.
- **Conditional-format rules are addressed by positional index** (0 = highest priority; there's no stable rule id). When mutating several rules in separate calls, go high index → low, or use the batch form, which orders them for you.
- **Format / clear / structure / dimension writes auto-build their field mask** from the payload, so a partial update never wipes unspecified subfields.
- **Rich reads are opt-in and per-cell.** `--rich-text` (runs + hyperlinks) and `--pivot` add fields to the mask only when set, and attach data only to cells that have it — a plain sheet pays nothing.
- **Output rendering is its own axis.** The CLI's global `--format {text,json,jsonl,csv,tsv,markdown}` (and the read tools' MCP `output_format`) wraps the same result in a different shape; `csv`/`tsv` only apply to a rectangular value grid (`read-values`) and raise `format_unsupported` on a structured read. This is independent of `read-values --render` (which picks values vs. formulas) and of `export --format` (which picks an export backend: PDF/XLSX/ODS via Drive, CSV/TSV per-tab).
- **Reads can address by selector, not just A1.** `read-values` and `describe` accept a data filter — an `a1`, `gridRange`, or `developerMetadataLookup` selector — so a metadata-anchored block survives row inserts that would move its A1 address.
- **Anything writable is readable back.** Read a rule, validation, format, table, banding, filter, or slicer, edit it, write it back. The remaining read-only surfaces are deliberate v1 scope: charts read returns metadata (write the full spec via `batch`); pivot definitions and rich-text runs/hyperlinks are read-only; Connected Sheets / data sources stay batch-only.

## Build from source

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```sh
uv sync                 # create the venv and install deps (incl. the dev dependency group)
uv run pytest           # run the test suite
uv run gsheets --help   # run the CLI from the working tree
```

The architecture is a pure core (`src/gsheets/core/`, zero MCP/CLI/transport imports) plus an auth layer, wrapped by two thin adapters — `mcp_server.py` (FastMCP) and `cli.py` (argparse). Each cohesive read or serialization concern lives in its own pure module (`condformat`, `richtext`, `tables`, `filters`, `banding`, `pivot`, `comments`, `dataops`, `dimensions`, `slicers`, `export`, `multiread`, and friends). A subprocess-level boundary test asserts that importing `gsheets.core` never pulls in `fastmcp`, `mcp`, `argparse`, or `pydantic`.

The full design — the layer diagram, the conditional-format line grammar, and the boundary test that enforces the pure core — is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Contributing

Issues and pull requests are welcome. The design contract and module layout are documented in the source; when changing a public signature, update every caller and the tests together. New serializers are golden-master tested (terse line + round-trip) in the same style as the conditional-format reader. Run `uv run pytest` before opening a PR. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

## License

MIT. See [LICENSE](LICENSE).
