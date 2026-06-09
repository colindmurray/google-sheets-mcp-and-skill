# google-sheets-mcp-and-skill

Best-in-class Google Sheets integration for AI tools, shipped as **one repo, two
install paths over shared code**:

1. an **MCP server** (`google-sheets-mcp`) for Claude Code / Claude Desktop / any MCP client, and
2. a **CLI** (`gsheets`) plus a bundled **SKILL.md** so it can be packaged as an AI skill.

Both adapters are thin wrappers over a single **pure core library** (`gsheets.core`) —
no duplicated Google Sheets logic, identical behavior from either entrypoint.

## Thesis: read-side richness

The differentiator is *reading* an existing, heavily-formatted sheet losslessly:

- **values + formulas** side by side (`=SUM(A:A) ⇒ 1234`),
- **cell formatting** including `effectiveFormat` colors/fonts/borders/number formats,
- **conditional-format rules** serialized into terse, readable, round-trippable lines.

All reads are token-efficient (tight `fields` masks, never `includeGridData`, optional
compact reads, flattened Google objects). Writes default to `USER_ENTERED` and auto-build
their `fields` mask from the payload. Anything writable is readable back.

## Status

Scaffolding stage. Public signatures are stubbed against the locked design contract; the
implementation lands milestone by milestone (M0 skeleton → M1 read richness → writes →
rules → structure → polish).

## Install (from source)

```sh
uv sync
uv run pytest
```

## Auth (quick start)

Credentials are resolved from environment variables / local config at runtime — never
committed. Supports Service Account, OAuth 2.0 Desktop, and ADC. Bootstrap a token once:

```sh
gsheets auth login     # OAuth desktop consent (or refresh an existing token)
gsheets auth status     # verify resolved mode, scopes, token expiry
```

Then point any command at a spreadsheet:

```sh
gsheets overview <YOUR_SPREADSHEET_ID>
gsheets inspect <YOUR_SPREADSHEET_ID> 'Sheet1!A1:D20'
gsheets read-conditional-formats <YOUR_SPREADSHEET_ID> --sheet Sheet1
```

## License

MIT © 2026 Colin Murray
