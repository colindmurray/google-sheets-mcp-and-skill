---
name: Bug report
about: Report something that is broken or behaves incorrectly
title: "[Bug] "
labels: bug
assignees: ''
---

<!--
Thanks for filing a bug. Before you submit:
- Search existing issues to avoid duplicates.
- NEVER paste real credentials, tokens, service-account keys, or a real spreadsheet ID.
  Use <YOUR_SPREADSHEET_ID> as a placeholder and redact any personal data.
-->

## What happened

A clear, concise description of the bug.

## What you expected

What you expected to happen instead.

## Entrypoint

Which adapter were you using? (the behavior should be identical from either)

- [ ] CLI (`gsheets`)
- [ ] MCP server (`google-sheets-mcp`)
- [ ] Calling `gsheets.core` directly

## Which tool / subcommand

e.g. `inspect`, `read-conditional-formats`, `format`, `set-conditional-format`, …

## Steps to reproduce

A minimal reproduction. Redact real IDs.

```sh
# Example — replace <YOUR_SPREADSHEET_ID> with a placeholder, never a real id
gsheets inspect <YOUR_SPREADSHEET_ID> 'Sheet1!A1:D20' --compact
```

## Actual output / error

Paste the command output or error envelope. Redact any account email or real id.

```text

```

## Environment

- Package version (`gsheets --version` or the installed version):
- Python version (`python --version`):
- OS:
- Auth mode (`GSHEETS_AUTH_MODE`): `service_account` / `oauth` / `adc` / `auto`
- Installed via: `uv sync` from source / `uv tool install` / `uvx` / other

## Additional context

Anything else that helps — a redacted snippet of the sheet, related issues, a guess at
the cause. If you can share a minimal redacted golden-master JSON that triggers it, that
is ideal.
