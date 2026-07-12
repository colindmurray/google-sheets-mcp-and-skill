"""Grouped serializer subpackage (DESIGN §X — read-side richness serializers).

Holds serializers that render a Google API sub-object to a terse, round-trippable read shape.
Currently ships :mod:`gsheets.core.serializers.chips` (smart-chip ``chipRuns`` reads, T2.2).

PURE leaf serializers: each module here imports only stdlib + sibling core leaves. It must
NEVER import ``fastmcp``/``mcp``/``argparse``/``pydantic``/``gsheets.models`` (DESIGN §1 boundary).
"""
