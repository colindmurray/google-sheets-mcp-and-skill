# google-sheets-mcp-and-skill

Read a Google Sheet the way it actually works — formulas, real colors, and conditional-format rules — and write it back safely. One core library, an MCP server, and a CLI + skill.

## The problem

A serious spreadsheet is rarely just a grid of values. It's a small application: derived columns built from formulas, cells that change color based on conditional-format rules, merged headers, dropdowns, frozen panes. The values you can see are the *output*; the logic that produces them lives in the formulas, the formatting, and the conditional-format rules.

If you want an AI (or yourself) to understand such a sheet before changing it, you have to read that logic losslessly. A screenshot tells you a cell is red, but not *why* it's red, what formula feeds it, or what rule would turn it green. "What's the value in C7?" is the easy question; "what formula computes C7, and which conditional-format rule decides its background, and at what threshold?" is the one that matters — and it's the one most tooling can't answer.

That's the gap this project fills. The thesis is **read-side richness**: surface values *and* the formulas behind them, the format a cell *actually renders* (`effectiveFormat`, which already includes conditional-format results) alongside the author's intent (`userEnteredFormat`), and every conditional-format rule serialized into a terse, readable, round-trippable line. All of it through tight field masks so it stays cheap enough to put in an LLM's context. And because every read shape can be written back, an AI can audit a sheet, propose a change, apply it, and re-read to confirm.

## How it compares

This was built after surveying four existing Google Sheets MCP servers. They're useful, but each has a gap that makes reading a formatting-driven sheet painful. The headline differences:

| Capability | dudegladiator | freema | Prajapdh | xing5 | **this** |
|---|---|---|---|---|---|
| Read formulas (+ computed value side by side) | no | yes | no | yes | **yes** |
| Write formulas (USER_ENTERED, not RAW) | yes | yes | **no — RAW only** | partial | **yes** |
| Read cell formatting (structured, compact) | no | yes | no | raw blob | **yes** |
| Write cell formatting (atomic, incl. borders) | partial | borders split out | no | partial | **yes** |
| **Read conditional-format rules** | no | yes | no | **no** | **yes — serialized lines** |
| Write conditional-format rules | no | add only | no | partial | **yes (add/update/delete, index-safe)** |
| Data validation (read **and** write) | no | read only | no | partial | **yes** |
| Named / protected ranges (CRUD) | no | no | partial | partial | **yes** |
| Developer metadata (durable anchors) | no | no | no | no | **yes** |
| Pure core, no transport coupling (CLI-able) | high | output layer coupled | partial | ctx-coupled | **yes, by design** |
| Auth models | SA | SA | OAuth | SA+OAuth+ADC | **SA+OAuth+ADC, least-privilege default** |

The facts worth calling out, stated plainly:

- **xing5** cannot read conditional formatting at all — that's the headline gap. It's otherwise a solid server (good tool annotations, an `ENABLED_TOOLS` allowlist, a `batch` escape hatch — all of which are borrowed here).
- **Prajapdh** hardcodes `valueInputOption=RAW`, which silently turns any formula you write into inert literal text. Writing `=SUM(A:A)` stores the *string*, not a formula. This tool defaults to `USER_ENTERED`.
- **freema** is the strongest reference and the only one that reads both formatting and conditional formatting — but it has read/write asymmetry (data validation is read-only; conditional formatting is add-only), splits borders out of the format path, and its output layer is coupled to the transport. The fix here is full CRUD symmetry: anything you can write, you can read back.
- **dudegladiator** has the cleanest architecture (a pure client with zero MCP imports). That decoupling is the model followed here, which is what makes shipping a CLI essentially free.

So this isn't a claim that the others are bad — it's that none of them lets you read a sheet's conditional-format rules *and* write them back *and* drive the whole thing from either an MCP client or a shell.

## What it's good at

- **Auditing or documenting a formula- and conditional-formatting-driven sheet** — read the logic instead of guessing from a screenshot.
- **Building a formula map of a live sheet** — pull the formulas of derived columns and write them up; the source of truth is the sheet itself.
- **Round-tripping conditional-format rules** — read a rule as a readable line, edit the line, write it straight back at the same priority index.
- **AI edits that respect existing formatting** — auto-built field masks mean a partial format update touches only the keys you pass and never clobbers the rest.
- **Token-efficient reads for LLM context** — never `includeGridData`, always a tight `fields` mask, optional compact (run-length) reads, flattened Google objects.

## Two ways to use it

Both paths run the exact same core. Behavior is identical whether you call a tool over MCP or a subcommand in a shell.

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

The MCP server requires a **pre-existing, valid or refreshable token** — it never pops a browser consent prompt mid-session (that would hang the JSON-RPC channel). Mint the token once with `gsheets auth login` (see Authentication). If credentials can't be resolved at startup, the server writes a clear message to stderr and exits non-zero instead of crashing.

The tools (one per core function, names prefixed `sheets_`):

| Tool | What it does |
|---|---|
| `sheets_overview` | Cheap orientation: title, tabs, sizes, frozen panes, per-sheet protected/conditional-format **counts**, named ranges. No grid data. Call this first. |
| `sheets_inspect` | Flagship rich read of a range: per-cell values + formulas + userEntered & effective formats + merges + notes + structured validation. `compact=true` collapses repeats into rectangular runs. |
| `sheets_read_values` | Plain values for one or more ranges; `render` = `plain` \| `unformatted` \| `formula` \| `all` (formula + computed side by side). |
| `sheets_read_conditional_formats` | Conditional-format rules serialized to readable lines, each with its positional `index`. The differentiating read. |
| `sheets_write_values` | Write/update one or more ranges in one call. USER_ENTERED by default (formulas stay live). |
| `sheets_append_rows` | Append rows after a table's last row (`INSERT_ROWS`, never overwrites). |
| `sheets_clear` | Clear values, and optionally formats / validation / notes, from ranges. |
| `sheets_format` | Apply fill, font, number/date pattern, alignment, wrap, padding, borders, and notes atomically; field mask auto-built from the payload. |
| `sheets_set_conditional_format` | Add / update / delete a boolean or gradient rule by positional index; batch form mutates several rules index-safe in one call. |
| `sheets_set_validation` | Set or clear data validation (dropdowns, number/date/text/custom-formula); round-trips with `inspect`. |
| `sheets_structure` | Read or modify merges, named ranges, protected ranges, frozen rows/cols, tab color, and row/column groups through one interface. |
| `sheets_manage_sheets` | Add / delete / duplicate / rename / reorder tabs; returns new sheet ids. |
| `sheets_metadata` | Read / write developer metadata — durable anchors that survive row inserts, unlike A1. |
| `sheets_charts` | Create / update / delete / list embedded charts (read returns chart metadata only in v1). |
| `sheets_batch` | Escape hatch: a raw ordered list of `batchUpdate` requests, for anything the typed tools don't cover. |

Read tools are annotated `readOnlyHint`; destructive paths carry `destructiveHint`. Set `ENABLED_TOOLS` to a comma-separated allowlist to register only a subset.

### B. CLI + skill

The same surface as a command-line tool. Install just the CLI with `uv`:

```sh
uv tool install google-sheets-mcp-and-skill   # provides `gsheets`
```

Every subcommand maps 1:1 to a core function. A session reading a sheet looks like this:

```sh
# Orient — cheap, no grid data.
$ gsheets overview <YOUR_SPREADSHEET_ID>
Workout Tracker  [<YOUR_SPREADSHEET_ID>]
  [0] Cliff  1000x86 (id=0)  frozenRows=1 frozenCols=2 protected=1 cf=12 tab=#4285F4
  [1] WEEK-TEMPLATES  1000x40 (id=18)
  named: config -> Cliff!AS986:AS1000

# Read formula AND computed value together.
$ gsheets read-values <YOUR_SPREADSHEET_ID> 'Cliff!A1:B2' --render all
render=all
# Cliff!A1:B2
  Set => Set | =SUM(B:B) => 1234
  1 => 1 | 0 => 0

# The differentiator: read the conditional-format rules that color cells dynamically.
$ gsheets read-conditional-formats <YOUR_SPREADSHEET_ID> --sheet Cliff
# Cliff (id=0)
  [0] [Cliff!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold
  [1] [Cliff!C2:C100] if NUMBER_GREATER(0) -> fg #1B5E20 bold
  [2] [Cliff!G2:G100] gradient min=#FFFFFF | max=#1A73E8
```

Add `--json` to any command to get the exact machine shape (the raw core dict) for piping to `jq`.

Writing follows the same read → write → read-back rhythm:

```sh
# Write a live formula (USER_ENTERED — "=SUM(B:B)" becomes a formula, not literal text).
$ gsheets write-values <YOUR_SPREADSHEET_ID> 'Cliff!A1' --values-json '[["=SUM(B:B)"]]'
updatedRanges: ["Cliff!A1"]
updatedCells: 1

# Apply formatting; the fields mask is auto-built from exactly the keys you pass.
$ gsheets format <YOUR_SPREADSHEET_ID> 'Cliff!A1:A10' --bg '#FFCDD2' --bold --number '0.00%'
range: Cliff!A1:A10
appliedFields: userEnteredFormat(backgroundColorStyle,textFormat.bold,numberFormat)

# Add a conditional-format rule from the same readable line you'd read back (index 0 = top priority).
$ gsheets set-conditional-format <YOUR_SPREADSHEET_ID> --action add --sheet Cliff --index 0 \
    --rule '[Cliff!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold'
action: add
index: 0
rule: [Cliff!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold
```

**Installing the skill.** A bundled `SKILL.md` lives at [`skill/SKILL.md`](skill/SKILL.md) (with deeper references under `skill/references/`). It wraps the `gsheets` CLI for agents that support the skill format — drop the `skill/` directory into your agent's skills location (for Claude Code, copy it to `~/.claude/skills/gsheets/`) and make sure `gsheets` is on `PATH`. The skill teaches the understand → change → escape-hatch workflow and the safe-write defaults; the CLI is the deterministic helper underneath it.

## Authentication

Credentials are resolved from **environment variables / local config at runtime** — never hardcoded, never committed. Three sources are supported, with least-privilege scopes by default.

Bootstrap a token once, then verify:

```sh
gsheets auth login     # OAuth desktop consent, or refresh/validate an existing token
gsheets auth status    # report resolved mode, scopes, token path, expiry; non-zero if unusable
```

`gsheets auth login` is the only place interactive OAuth consent runs — it's a CLI path, never the MCP server. Once a token exists, both the CLI and the MCP server use it.

### Sources and precedence

With `GSHEETS_AUTH_MODE=auto` (the default), the first match wins:

1. **Service Account** — if `GSHEETS_SERVICE_ACCOUNT_FILE` is set, or `GOOGLE_APPLICATION_CREDENTIALS` points at a service-account key. Best for headless automation; share each target sheet (or a Drive folder) with the service account's email.
2. **OAuth 2.0 Desktop** — a cached authorized-user token (`GSHEETS_TOKEN_FILE`), or an OAuth desktop-client file (`GSHEETS_OAUTH_CLIENT_FILE`) for first-time consent. A valid/refreshable token refreshes in place without needing the client file again. Simplest for a single personal account.
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
| `GSHEETS_VERBOSE_ERRORS` | `1` allows error hints to include the authenticated account email (off by default, so it never leaks in pass-through errors) | unset |

### Scopes (least-privilege default)

| `GSHEETS_SCOPES` | Scopes granted |
|---|---|
| `default` | `spreadsheets`, `drive.file` (only files this app creates/opens) |
| `broad` | the above **plus** full `drive` (cross-file discovery) |
| explicit list | exactly the comma-separated scopes you pass |

The default deliberately avoids whole-Drive access; opt into `broad` only when you need to discover sheets you didn't create through this tool.

### Scope reconciliation (cached tokens)

When a cached OAuth token is loaded, it is **refreshed against the scopes it was originally granted** — never against whatever `GSHEETS_SCOPES` currently asks for. This matters because Google's refresh-grant rejects a refresh whose scope list isn't a subset of the original consent with `invalid_scope`. For example, a token consented with the broad `drive` scope would otherwise fail a refresh that requests the narrow `drive.file`, even though `drive` is functionally broader. To avoid that, the resolver:

1. loads the token without forcing the requested scope list onto it (so the refresh re-grants against the token's own scopes), then
2. checks that the token's granted scopes **cover** what the current request needs (the broad `drive` scope is treated as covering `drive.file`).

If the granted scopes don't cover the request, you get a clear `oauth_scope_insufficient` error telling you to re-run `gsheets auth login` with the scopes you need (e.g. `GSHEETS_SCOPES=broad`). In short: a token minted with one scope set keeps working across `default`/`broad` requests as long as its grant covers them — you don't have to re-consent just because the requested mode changed.

## Command / tool reference

Each CLI subcommand maps 1:1 to a core function and to the matching `sheets_*` MCP tool.

| CLI subcommand | MCP tool | Purpose |
|---|---|---|
| `overview` | `sheets_overview` | Orientation snapshot, no grid data |
| `inspect` | `sheets_inspect` | Values + formulas + both formats + merges + validation |
| `read-values` | `sheets_read_values` | Values with render mode (`--render all` = formula + computed) |
| `read-conditional-formats` | `sheets_read_conditional_formats` | CF rules as readable lines (`--sheet`) |
| `write-values` | `sheets_write_values` | Write/update ranges (USER_ENTERED default) |
| `append-rows` | `sheets_append_rows` | Append after a table (no overwrite) |
| `clear` | `sheets_clear` | Clear values / formats / validation / notes |
| `format` | `sheets_format` | Atomic formatting incl. borders + notes |
| `set-conditional-format` | `sheets_set_conditional_format` | Add/update/delete CF rules by index |
| `set-validation` | `sheets_set_validation` | Set/clear data validation |
| `structure` | `sheets_structure` | Merges, named/protected ranges, frozen panes, tab color, groups |
| `manage-sheets` | `sheets_manage_sheets` | Add/delete/duplicate/rename/reorder tabs |
| `metadata` | `sheets_metadata` | Developer metadata (durable anchors) |
| `charts` | `sheets_charts` | Embedded charts (read = metadata only) |
| `batch` | `sheets_batch` | Raw `batchUpdate` escape hatch |
| `auth login` / `auth status` | — (CLI only) | Bootstrap / inspect credentials |

Run `gsheets <command> --help` for the exact, current flags of any subcommand — that's the authoritative source.

A few behaviors worth knowing:

- **Writes default to `USER_ENTERED`.** Strings starting with `=` become live formulas; `5%` / `$3` parse like typed input. Pass `--input raw` (or `input="raw"`) to store text verbatim.
- **Conditional-format rules are addressed by positional index** (0 = highest priority; there is no stable rule id). When mutating several rules in separate calls, go high index → low, or use the batch form which orders them for you.
- **Format/clear/structure writes auto-build their field mask** from the payload, so a partial update never wipes unspecified subfields.
- **Anything writable is readable back.** Read a rule, validation, or format, edit it, write it back. (The one v1 exception: charts read returns metadata only.)

## Build from source

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```sh
uv sync                 # create the venv and install deps (incl. dev extras)
uv run pytest           # run the test suite
uv run gsheets --help   # run the CLI from the working tree
```

The architecture is a pure core (`src/gsheets/core/`, zero MCP/CLI/transport imports) plus an auth layer, wrapped by two thin adapters — `mcp_server.py` (FastMCP) and `cli.py` (argparse). A subprocess-level boundary test asserts that importing `gsheets.core` never pulls in `fastmcp`, `mcp`, `argparse`, or `pydantic`.

## Contributing

Issues and pull requests are welcome. The design contract and module layout are documented in the source; when changing a public signature, update every caller and the tests together. Run `uv run pytest` before opening a PR.

## License

MIT. See [LICENSE](LICENSE).
