"""Pure Google Sheets core library — the shared logic both adapters drive (DESIGN §1, §3).

Re-exports the 20 public core functions so callers can do
``from gsheets.core import overview, inspect, ...``. (15 base, DESIGN §3.3, plus the 5
v0.2 extension top-level fns ``data_ops``/``dimensions``/``comments``/``export``/``read_many``,
DESIGN §Extensions / §3.x.)

PURE boundary (enforced; lint-checked, DESIGN §1): this package and its modules import ONLY
stdlib + ``googleapiclient``/``google.auth*``. They must NEVER import ``fastmcp``, ``mcp``,
``argparse``, ``pydantic``, or ``gsheets.models``. Importing ``gsheets.core`` must not drag
any transport/CLI/pydantic module into ``sys.modules``.
"""

from __future__ import annotations

from .batch import batch
from .charts import charts
from .comments import comments
from .dataops import data_ops
from .dimensions import dimensions
from .export import export
from .formatting import format
from .multiread import read_many
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
    # v0.2 extensions (DESIGN §Extensions): three NEW top-level core fns.
    "data_ops",
    "dimensions",
    "comments",
    # v0.2 cross-file + export extensions (DESIGN §3.x / §3.3): two NEW top-level core fns.
    "export",
    "read_many",
]
