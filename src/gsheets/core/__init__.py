"""Pure Google Sheets core library — the shared logic both adapters drive (DESIGN §1, §3).

Re-exports the 15 public core functions so callers can do
``from gsheets.core import overview, inspect, ...``.

PURE boundary (enforced; lint-checked, DESIGN §1): this package and its modules import ONLY
stdlib + ``googleapiclient``/``google.auth*``. They must NEVER import ``fastmcp``, ``mcp``,
``argparse``, ``pydantic``, or ``gsheets.models``. Importing ``gsheets.core`` must not drag
any transport/CLI/pydantic module into ``sys.modules``.
"""

from __future__ import annotations

from .batch import batch
from .charts import charts
from .formatting import format
from .reads import inspect, overview, read_conditional_formats
from .rules import set_conditional_format, set_validation
from .structure import manage_sheets, metadata, structure
from .values import append_rows, clear, read_values, write_values

__all__ = [
    "overview",
    "inspect",
    "read_values",
    "read_conditional_formats",
    "write_values",
    "append_rows",
    "clear",
    "format",
    "set_conditional_format",
    "set_validation",
    "structure",
    "manage_sheets",
    "metadata",
    "charts",
    "batch",
]
