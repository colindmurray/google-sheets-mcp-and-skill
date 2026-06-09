# Contributing to google-sheets-mcp-and-skill

Thanks for your interest in contributing. This project is a best-in-class Google
Sheets integration for AI tools, shipped as **one repo, two install paths over
shared code**: an MCP server (`google-sheets-mcp`) and a CLI (`gsheets`) plus a
bundled `SKILL.md`. Both adapters are thin wrappers over a single **pure core
library** (`gsheets.core`).

Most of this document is about one rule — the pure-core / thin-adapter boundary —
because preserving it is what keeps the two entrypoints behaving identically with
zero duplicated Sheets logic. Read [The architecture rule you MUST
preserve](#the-architecture-rule-you-must-preserve) before writing any code, and
see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design.

---

## Table of contents

- [Code of conduct](#code-of-conduct)
- [Dev setup with uv](#dev-setup-with-uv)
- [Running tests](#running-tests)
- [The architecture rule you MUST preserve](#the-architecture-rule-you-must-preserve)
- [How to add a new tool to BOTH adapters](#how-to-add-a-new-tool-to-both-adapters)
- [Code style](#code-style)
- [Security and privacy (this repo is public)](#security-and-privacy-this-repo-is-public)
- [Live tests against a real spreadsheet](#live-tests-against-a-real-spreadsheet)
- [Commits and pull requests](#commits-and-pull-requests)

---

## Code of conduct

Be kind, assume good faith, and keep discussion technical. We follow the spirit of
the [Contributor Covenant](https://www.contributor-covenant.org/). Harassment of any
kind is not tolerated.

---

## Dev setup with uv

This project uses [`uv`](https://docs.astral.sh/uv/). You need Python **3.11+**.

```sh
# 1. Clone your fork
git clone https://github.com/<you>/google-sheets-mcp-and-skill.git
cd google-sheets-mcp-and-skill

# 2. Create the virtualenv and install the package (editable) + dev extras
uv sync --extra dev

# 3. Verify the two console scripts are wired up
uv run gsheets --help
uv run google-sheets-mcp --help   # MCP stdio server; Ctrl-C to stop
```

`uv sync` reads `pyproject.toml` and `uv.lock`, creates `.venv/`, and installs the
package as an editable `src`-layout install. Run any tool through `uv run …` so it
uses the project environment. No global installs are required for development.

Notes:

- The **CLI** (`gsheets`) uses only stdlib `argparse` — no runtime dependency.
- The **MCP server** (`google-sheets-mcp`) uses `fastmcp` and `pydantic`. These are
  *adapter-side only*; the core never imports them (see the boundary rule below).
- Auth credentials are never committed. They come from environment variables / local
  config at runtime (see [Live tests](#live-tests-against-a-real-spreadsheet)). You
  do **not** need credentials to run the unit suite — it runs entirely against a
  mocked Sheets service.

---

## Running tests

The whole suite runs against a **mocked** Google Sheets service — no network, no
credentials, fully deterministic.

```sh
uv run pytest                     # full unit suite (live tests auto-deselect)
uv run pytest tests/unit/test_condformat.py    # one file
uv run pytest -k fieldsmask       # by keyword
uv run pytest --cov=gsheets       # with coverage
```

The suite includes:

- **Unit tests** (`tests/unit/`) for every core/auth module, with **golden-master**
  JSON fixtures under `tests/unit/golden/` for the serializers (conditional-format
  serialize + body-only round-trip, `flatten_cell_format`, `build_fields_mask`
  including the atomic-leaf cases, `a1_to_gridrange`/`gridrange_to_a1`, color
  hex↔ColorStyle, and more). If you change a serializer's output, update the matching
  golden file in the same PR and explain why in the description.
- **A boundary-guard test** (`tests/unit/test_boundary_guard.py`) that runs in a
  **fresh subprocess** and asserts `import gsheets.core` / `import gsheets.auth` does
  not drag any of `{argparse, fastmcp, mcp, pydantic}` into `sys.modules`. This is the
  enforcement mechanism for the architecture rule below. **If you break the boundary,
  this test fails.** It must pass before any PR merges.
- **Live integration tests** (`tests/integration/`) — opt-in, see [Live
  tests](#live-tests-against-a-real-spreadsheet). They are skipped by default.

CI runs `uv run pytest` on the supported Python versions. A PR must be green before
review.

---

## The architecture rule you MUST preserve

This is the single most important contributor rule. The whole project is built on it.

> **A pure core, wrapped by two thin adapters that map 1:1 to the core. Behavior is
> identical from either entrypoint. There is no duplicated Sheets logic.**

Concretely:

### 1. The core is pure

`src/gsheets/core/**` and `src/gsheets/auth/**` import **only** the standard library
plus `googleapiclient` / `google.auth*`. They must **never** import:

- `fastmcp` or `mcp` (MCP transport),
- `argparse` (CLI transport),
- `pydantic` **or** `gsheets.models` (the Pydantic models are adapter-side only).

A `from gsheets import models` (or `from gsheets.models import …`) anywhere under
`core/**` or `auth/**` is a boundary violation and will fail the boundary-guard test.

Core functions:

- take `services: SheetsServices` (the authed handle) as their **first** parameter and
  never resolve credentials, read auth env vars, or touch a transport `Context`;
- return plain JSON-serializable `dict` / `list` (every success dict carries
  `"ok": True`);
- raise `SheetsError` on failure — they never return an error dict.

### 2. The adapters are thin

- `src/gsheets/mcp_server.py` is the **only** module that imports `fastmcp` / `mcp`
  **and** the only one that imports `gsheets.models`.
- `src/gsheets/cli.py` is the **only** module that imports `argparse`.
- Each adapter tool/subcommand body is **essentially one line**: resolve `services`,
  call the matching core function, return/print its result. No Sheets logic lives in an
  adapter. If you find yourself parsing a `GridRange`, building a fields mask, or
  serializing a rule inside an adapter — stop, that belongs in core.

### 3. Why it matters

This is what makes the CLI skill-packageable and guarantees the MCP server and CLI
behave identically: there is exactly one implementation of every behavior. Putting
logic in an adapter silently forks behavior between the two entrypoints — the exact
failure mode this project exists to avoid.

If a change seems to require transport/CLI knowledge inside core, it almost certainly
belongs in the adapter, or the core function's signature needs to grow a plain
parameter. Ask in an issue before bending the boundary.

---

## How to add a new tool to BOTH adapters

A "tool" is a core function exposed as **one MCP tool and one CLI subcommand**. Because
of the 1:1 rule, adding one touches a predictable set of files. Follow this checklist so
the two entrypoints stay in lockstep.

1. **Design the core function first.** Decide its name, signature, and return shape.
   First param is always `services: SheetsServices`; `spreadsheet_id: str` is second.
   Accept A1 ranges everywhere (resolve to `GridRange` *inside* core). Return a plain
   dict with `"ok": True`. Raise `SheetsError` on failure. Reuse the shared helpers —
   `addressing` (A1 ↔ GridRange), `colors`, `fieldsmask` (auto-build the `fields` mask
   from the payload — never hand-write one), `flatten`, `condformat`. Do not duplicate
   logic that already exists in a sibling module.

2. **Implement it in the right core module** under `src/gsheets/core/` (values, reads,
   formatting, rules, structure, charts, batch …) and **re-export it** from
   `src/gsheets/core/__init__.py` so `from gsheets.core import <fn>` works.

3. **Write the unit test** at `tests/unit/test_<module>.py` against the
   `mock_sheets_service` fixture. If the function serializes/parses anything, add a
   golden-master fixture under `tests/unit/golden/` and a round-trip assertion.

4. **Add the Pydantic mirror model** in `src/gsheets/models.py` — a **mechanical
   mirror** of the return dict, same field names, plus a terse `__str__` / `terse`
   field for the token-efficient MCP `content`. Adding a core field means adding a model
   field; never reshape.

5. **Register the MCP tool** in `src/gsheets/mcp_server.py`: a one-line body calling the
   core function, the right `ToolAnnotations` (`readOnlyHint` for reads;
   `destructiveHint` on destructive paths; `openWorldHint=True` on every tool; tag
   `{"read"}` or `{"write"}`), and an example-rich docstring whose examples use
   `<YOUR_SPREADSHEET_ID>`. Honor the `ENABLED_TOOLS` allowlist.

6. **Add the CLI subcommand** in `src/gsheets/cli.py`: a subparser whose flags map 1:1
   to the core kwargs (subcommand name = core fn name with hyphens), and a one-line
   dispatch to the core function. Support the global `--json`.

7. **Document it** in `skill/SKILL.md`'s command map and the relevant
   `skill/references/*.md`, and add it to `docs/ARCHITECTURE.md`'s function table if it
   is a new capability.

8. **Run `uv run pytest`** — including the boundary-guard test — and make sure the new
   model/tool/subcommand all reference the same field names.

A good PR for a new tool is reviewable as: *one core function + its test + its model +
its MCP registration + its CLI subcommand + its docs*, all with matching field names.

### Adding a capability without a new top-level tool

Not every new capability needs a new core function. Several tools are **multi-action
dispatchers** (`structure`, `data_ops`, `dimensions`, `metadata`, `manage_sheets`,
`charts`), and the cheaper, more cohesive way to add a single-request capability is often a
**new action on an existing tool** rather than a whole new tool. Prefer this when the
capability is a natural sibling of what the tool already does:

- A new structural read/write (tables, banding, filters, slicers, spreadsheet properties)
  belongs on `structure` as a new action and/or a new sheet-scoped read key — not a new
  tool.
- A new single-request `batchUpdate` data verb belongs on `data_ops`; a new row/column op
  belongs on `dimensions`.

To add an action: register it in the tool's `_*_ACTIONS` set, add its allowed `params` keys
to the `_*_PARAMS` table (unknown keys must raise `SheetsError("unknown_param")`), add its
handler to the dispatch table, and — if it creates an object whose id is returned in
`replies[]` — extend the reply-id capture spec. The MCP docstring and Pydantic input schema
enumerate the new action and its `params`; the CLI passes `--params-json`. No new
subcommand/tool is needed.

### Where serialization logic lives

If your capability **reads** a complex Google object (a table, a filter, a banded range, a
pivot, rich-text runs, a comment), put the flatten/serialize logic in a **new, single-purpose
pure-core serializer module** under `src/gsheets/core/` (see `richtext`, `pivot`, `tables`,
`filters`, `banding`, `slicers`, `comments` for the pattern), not inline in the read
function. A serializer takes **already-resolved A1 strings** (the owning read function
resolves `GridRange → A1` first, mirroring the condformat boundary), returns a plain dict
that carries a terse `line` field in the established style, **flattens colors to hex**, and
**omits absent keys**. Golden-master its output (and its round-trip, if it round-trips). Keep
per-cell rich data attached **only to cells that carry it** — never an empty placeholder.

Keeping new logic in its own module keeps the boundary-guard green and keeps each PR's edits
non-overlapping: ideally one new capability touches one new module plus the one existing file
that registers it.

---

## Code style

- **Python 3.11+**, type hints on public signatures, `from __future__ import
  annotations` at the top of modules.
- Module-level docstrings explain the *why*; reference the design section the module
  implements (e.g. "DESIGN §5.1") where it clarifies intent.
- Prefer small, single-responsibility helper modules with disjoint ownership (this is
  what lets pieces be tested in isolation).
- 4-space indent, descriptive names, no one-letter variables except trivial loop
  indices.
- Keep imports boundary-clean: never add a transport/CLI/pydantic import to a core or
  auth module.
- Errors are raised as `SheetsError(code, message, …)` in core; adapters translate them
  to their own envelope. Use the existing error codes where one fits
  (`empty_payload`, `bad_range`, `unknown_action`, `unknown_param`, `missing_sheet`,
  `conflicting_args`, …).
- No `print()` in the MCP server's stdout path (it is the JSON-RPC channel) — log to
  stderr.

If a formatter/linter config is added to the repo, run it before pushing. Until then,
match the style of surrounding code.

---

## Security and privacy (this repo is public)

This repository is public. **Never** hardcode or commit:

- credentials (OAuth client secrets, service-account keys, tokens),
- real spreadsheet IDs,
- personal information (real email addresses, names tied to data, file paths under a
  home directory that reveal identity).

Rules:

- Real IDs and credentials come **only** from environment variables / local config at
  runtime — never the committed tree (`src/`, `tests/`, `docs/`, `README`, `SKILL.md`,
  `examples/`).
- Use the placeholder `<YOUR_SPREADSHEET_ID>` in all docs and examples.
- The `.gitignore` already blocks common credential filenames (`token.json`,
  `service-account*.json`, `credentials.json`, `gcp-oauth.keys.json`, `.env*`). Do not
  remove those entries.
- Error messages must not leak the operator's account email by default (it is gated
  behind `GSHEETS_VERBOSE_ERRORS`). Keep new error hints generic.

If you accidentally commit a secret, treat it as compromised: rotate it and tell a
maintainer — scrubbing history is not enough.

---

## Live tests against a real spreadsheet

The unit suite never touches the network. The **live integration tests**
(`tests/integration/`, marked `@pytest.mark.live`) exercise a real authed read/write
round-trip and are **opt-in**. They are controlled entirely by environment variables so
nothing real is ever committed:

| Env var | Purpose |
|---|---|
| `GSHEETS_LIVE` | Set to `1` to enable the live tests (otherwise they skip). |
| `GSHEETS_TEST_SPREADSHEET_ID` | A **throwaway** spreadsheet id to test against — never Production, never committed. |
| `GSHEETS_AUTH_MODE` | `service_account` \| `oauth` \| `adc` \| `auto` (default `auto`). |
| `GSHEETS_SERVICE_ACCOUNT_FILE` | Path to a service-account JSON key (if using SA). |
| `GSHEETS_OAUTH_CLIENT_FILE` | Path to an OAuth desktop client JSON (only needed for first-time consent). |
| `GSHEETS_TOKEN_FILE` | Path to the cached authorized-user token. |
| `GSHEETS_SCOPES` | `default` (narrow) \| `broad` \| explicit comma list. |
| `GSHEETS_TEST_WRITE_RANGE` | A clobberable A1 range (e.g. `Scratch!A1:B2`) for the write round-trip. Unset ⇒ the write test is skipped and only the read coverage runs. |
| `GSHEETS_PRODUCTION_DENYLIST` | Optional comma-separated list of **your own** Production ids to refuse, in addition to the built-in guard. Read at runtime; never committed. |

Bootstrap a token once, then run the live suite:

```sh
# One-time: mint/refresh a token via the OAuth desktop flow (CLI-only path)
uv run gsheets auth login
uv run gsheets auth status        # verify resolved mode, scopes, token expiry

# Run the live tests against a throwaway sheet you own
export GSHEETS_LIVE=1
export GSHEETS_TEST_SPREADSHEET_ID=<your-throwaway-sheet-id>
uv run pytest -m live
```

Guidelines:

- **Use a scratch spreadsheet you own and don't care about.** The write tests confine
  themselves to a scratch range and clean up after themselves, but treat the target as
  disposable.
- The live tests are belt-and-suspenders guarded against running against a known
  Production id. Because this repo is public, that guard stores **no plaintext id** — it
  pins known-Production ids by a salted one-way hash and also honors any ids you list in
  `GSHEETS_PRODUCTION_DENYLIST` at runtime. The real safeguard, though, is *you* pointing
  `GSHEETS_TEST_SPREADSHEET_ID` at a throwaway.
- Do **not** commit a token, key file, or spreadsheet id. The env vars are read at
  runtime only.
- CI does not run live tests (no credentials in CI). A PR is expected to keep the
  mocked unit suite green; reviewers may run the live tests locally for changes that
  touch real API behavior.

---

## Commits and pull requests

- Branch off `main`; keep PRs focused (one tool/capability or one fix per PR where
  practical).
- Make sure `uv run pytest` is green (including the boundary-guard test) before opening
  the PR.
- Update docs in the same PR as the code: `SKILL.md` / `skill/references/*` for
  user-facing behavior, `docs/ARCHITECTURE.md` for design changes, and `CHANGELOG.md`
  under the `Unreleased` section.
- Use the PR template; explain *what* changed and *why*, and confirm no real
  IDs/credentials/personal info were added.
- If you must change a public core signature that other modules depend on, change it
  consistently across all callers (core function, model, MCP tool, CLI subcommand,
  tests, docs) and call it out explicitly in the PR description.

We appreciate every contribution — bug reports, docs fixes, and new tools alike.
