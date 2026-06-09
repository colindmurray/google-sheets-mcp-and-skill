"""FastMCP stdio adapter (DESIGN §7.1).

The ONLY module importing ``fastmcp``/``mcp`` AND ``gsheets.models``. Registers one tool per
core function (one-line bodies), with ToolAnnotations per the §7.1 table, an ``ENABLED_TOOLS``
allowlist, ``mask_error_details=True``, and a ``to_tool_error`` envelope. The lifespan builds
:class:`SheetsServices` once and CATCHES build failure -> clear stderr message (no interactive
consent at startup). ``main()`` runs the stdio server. NEVER prints to stdout (JSON-RPC channel).
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from . import auth
from .core.errors import SheetsError


def to_tool_error(err: SheetsError) -> ToolError:
    """Format a :class:`SheetsError` into the canonical terse MCP ``ToolError`` (DESIGN §6.2).

    The server runs with ``mask_error_details=True``, so curated ``ToolError`` text passes
    through to the client while unexpected exceptions surface generically. The 403 hint stays
    generic by default (no operator email).

    Args:
        err: The core error.

    Returns:
        A ``ToolError`` carrying the canonical terse string.
    """
    raise NotImplementedError


@asynccontextmanager
async def lifespan(server: "FastMCP"):
    """Build :class:`SheetsServices` once for the server's lifetime (DESIGN §7.1).

    Calls :func:`gsheets.auth.build_services` (no interactive consent at MCP startup). On
    failure, writes a clear actionable message to stderr and exits non-zero rather than crashing
    the JSON-RPC channel with a raw stack trace. Tools pull the handle from
    ``ctx.request_context.lifespan_context``; core never sees ``Context``.

    Args:
        server: The owning :class:`FastMCP` instance.

    Yields:
        A context object exposing the built ``services``.
    """
    raise NotImplementedError
    yield  # pragma: no cover - unreachable stub marker; keeps this an async generator


#: The FastMCP server instance. Tools are registered against it as the adapter is built out.
mcp = FastMCP(name="google-sheets-mcp", lifespan=lifespan, mask_error_details=True)


def main() -> None:
    """Console-script entrypoint (``google-sheets-mcp``): run the stdio server (DESIGN §7.1)."""
    raise NotImplementedError


# Silence unused-import lints for symbols the tool bodies will use as they land.
_ = (auth, sys)
