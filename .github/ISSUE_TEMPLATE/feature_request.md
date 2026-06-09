---
name: Feature request
about: Propose a new capability or improvement
title: "[Feature] "
labels: enhancement
assignees: ''
---

<!--
Thanks for proposing an improvement. Before you submit:
- Search existing issues to avoid duplicates.
- NEVER include real credentials or a real spreadsheet ID. Use <YOUR_SPREADSHEET_ID>.
- Skim docs/ARCHITECTURE.md and CONTRIBUTING.md — new behavior must fit the
  pure-core / thin-adapter design and map 1:1 to BOTH adapters.
-->

## The problem / use case

What are you trying to do with a spreadsheet that you can't (cleanly) do today? Describe
the workflow, not just the API call.

## Proposed solution

What would you like to see? If it is a new tool, sketch it:

- **Core function name** (and signature, if you have one in mind):
- **What it reads or writes:**
- **Return shape** (remember: anything writable should be readable back — CRUD symmetry):
- **MCP tool name** (`sheets_…`) **and CLI subcommand** (`…`):

## Read-side richness?

This project's thesis is rich, token-efficient, round-trippable **reads**. Does this
proposal improve a read (values/formulas/formatting/conditional-format/validation/
structure)? If so, say how it stays token-efficient (tight `fields` mask, flattened
output, optional compact mode).

## Alternatives considered

Including: can the existing `batch` escape hatch already do this? If so, why is a typed
tool worth adding?

## Design fit checklist

- [ ] Fits the pure-core / thin-adapter boundary (logic in core, one-line adapter bodies)
- [ ] Maps 1:1 to both the MCP tool and the CLI subcommand
- [ ] Preserves CRUD symmetry (writable ⇒ readable back) where applicable
- [ ] Token-efficient by default (no `includeGridData`; tight `fields` mask)
- [ ] No real IDs / credentials / personal info anywhere in the proposal

## Additional context

Examples (using `<YOUR_SPREADSHEET_ID>`), references to the Google Sheets API surface,
prior art in other tools, or anything else that helps.
