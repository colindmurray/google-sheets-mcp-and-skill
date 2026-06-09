<!--
Thanks for contributing! Please fill this out so reviewers can move quickly.
Read CONTRIBUTING.md first if you haven't — especially the pure-core / thin-adapter rule.
NEVER include real credentials, tokens, keys, or a real spreadsheet ID anywhere in this
PR. Use <YOUR_SPREADSHEET_ID> in examples.
-->

## What & why

What does this PR change, and why? Link any related issue (`Closes #123`).

## Type of change

- [ ] Bug fix (non-breaking)
- [ ] New tool / capability (a core function + its model + MCP tool + CLI subcommand)
- [ ] Improvement to an existing tool
- [ ] Docs only
- [ ] Refactor / internal (no behavior change)
- [ ] Breaking change (changes a public core signature or output shape)

## Architecture boundary (must hold)

- [ ] No `fastmcp` / `mcp` / `argparse` / `pydantic` / `gsheets.models` import was added
      to any `core/**` or `auth/**` module.
- [ ] Sheets logic lives in core; adapter tool/subcommand bodies stay essentially
      one-line (resolve services → call core → return/print).
- [ ] `uv run pytest` passes, **including** the subprocess boundary-guard test.

## If this adds or changes a tool

Confirm the 1:1 mapping is complete across all entrypoints:

- [ ] Core function implemented and re-exported from `gsheets/core/__init__.py`
- [ ] Unit test added/updated (with a golden-master fixture if it serializes/parses)
- [ ] Pydantic mirror model in `models.py` (same field names as the core dict)
- [ ] MCP tool registered with correct `ToolAnnotations` + an example-rich docstring
- [ ] CLI subcommand added (flags map 1:1 to core kwargs; supports `--json`)
- [ ] Docs updated (`SKILL.md` command map, `skill/references/*`, `docs/ARCHITECTURE.md`)
- [ ] CRUD symmetry preserved where applicable (writable ⇒ readable back)

## Security & privacy (this repo is public)

- [ ] No credentials, tokens, keys, real spreadsheet IDs, or personal info added to the
      committed tree (`src/`, `tests/`, `docs/`, `README`, `SKILL.md`, `examples/`).
- [ ] Examples use the `<YOUR_SPREADSHEET_ID>` placeholder.
- [ ] No new error message leaks the operator's account email by default.

## Changelog

- [ ] Added an entry under `## [Unreleased]` in `CHANGELOG.md`.

## How I tested

Describe what you ran (e.g. `uv run pytest`, a specific subcommand against a throwaway
sheet via the env-gated live tests). Redact any real ids.

```text

```

## Breaking changes / notes for reviewers

If you changed a public core signature, list every caller you updated (core fn, model,
MCP tool, CLI subcommand, tests, docs) and why the change was necessary. Otherwise,
delete this section.
