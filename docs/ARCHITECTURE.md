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
                │  values · reads · formatting · rules · structure ·   │
                │  charts · batch   +   helpers:                       │
                │  addressing · colors · fieldsmask · flatten ·        │
                │  condformat · errors · service                       │
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
| `addressing` | A1 ↔ `GridRange` and sheet-name → `sheetId` (per-call cached). |
| `colors` | hex ↔ `ColorStyle` (`rgbColor` / `themeColor`); reads flatten to hex. |
| `fieldsmask` | `build_fields_mask(payload)` — the minimal `fields` mask covering exactly the keys present. |
| `flatten` | `flatten_cell_format()` — Google's nested `CellFormat` → flat shape. |
| `condformat` | (de)serialize conditional-format rules ↔ readable lines (see grammar below). |

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

Fifteen core functions, each exposed as one MCP tool and one CLI subcommand. The
understanding path is `overview → inspect → read_conditional_formats`; the change path is
the writers; the raw escape hatch is presented last.

| Core fn | What it does | Kind |
|---|---|---|
| `overview` | Cheap orientation snapshot: title, tabs (dimensions, frozen, counts), named ranges. No grid data. | read |
| `inspect` | Flagship rich read: values + formulas + both formats + merges + validation over a tight `fields` mask; optional compact runs. | read |
| `read_values` | Values for one/more ranges with a render mode (`plain` / `unformatted` / `formula` / `all`). | read |
| `read_conditional_formats` | Per-sheet conditional-format rules serialized to readable lines (the priority feature). | read |
| `write_values` | Write/update one or more ranges; `USER_ENTERED` default; multi-range in one call. | write |
| `append_rows` | Append after the last row of a table (`INSERT_ROWS`, no overwrite). | write |
| `clear` | Clear values, and optionally formats / validation / notes, from ranges. | write |
| `format` | Apply cell formatting (background, font, number/date pattern, alignment, wrap, padding, borders, note) atomically with an auto fields mask. | write |
| `set_conditional_format` | Add / update / delete a boolean or gradient rule by positional index; index-shift-safe batch form. | write |
| `set_validation` | Set / clear data validation on a range (structured rule, round-trips from `inspect`). | write |
| `structure` | Read or modify merges, named ranges, protected ranges, frozen rows/cols, tab color, dimension groups — one structural interface. | read/write |
| `manage_sheets` | Add / delete / duplicate / rename / reorder tabs; returns new ids. | write |
| `metadata` | Read / write developer metadata for durable row/column/sheet anchors. | write |
| `charts` | Create / update / delete / read embedded charts (read returns metadata only in v1). | write |
| `batch` | Power-user escape hatch: a raw ordered list of `batchUpdate` requests. | write |

The structure read and the conditional-format read share a **shape-stable multi-sheet
envelope**: top-level spreadsheet-scoped fields plus a `sheets: [...]` list that is
always a list (one entry for one sheet, every tab when unscoped), so consumers never fork
on object-vs-list.

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

## Addressing and color conventions

- **`GridRange` is 0-based, half-open** (`startRowIndex` inclusive, `endRowIndex`
  exclusive); **A1 is 1-based, inclusive**. The conversion is centralized in
  `addressing` so it is done once and correctly. Unbounded ranges (`A:A`, `2:2`, whole
  sheet) map by omitting the corresponding indices.
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
