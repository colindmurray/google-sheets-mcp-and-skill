"""google-sheets-mcp-and-skill — best-in-class Google Sheets integration for AI tools.

Top-level package marker. Defines ``__version__`` only. Importing ``gsheets`` re-exports
NOTHING transport-bound (no ``fastmcp``/``mcp``/``argparse``/``pydantic``) so ``import gsheets``
stays cheap and boundary-clean (DESIGN §1).

The public surface lives in :mod:`gsheets.core` (the pure library) and the two thin
adapters :mod:`gsheets.cli` and :mod:`gsheets.mcp_server`.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
