"""Pydantic models mirroring each core return dict, field-for-field (DESIGN §3.1, §7.1).

ADAPTER-SIDE ONLY: ``gsheets.core`` / ``gsheets.auth`` must NEVER import this module
(boundary rule, DESIGN §1). These models give the MCP adapter its ``outputSchema`` /
``structuredContent``; a terse ``__str__``/``terse`` field provides the token-efficient
``content`` text. Models are mechanical mirrors — adding a core field means adding a model
field, never reshaping.

Shared sub-models (:class:`CellFormat`, :class:`Cell`, :class:`ValidationRule`) and the
``structure`` / ``read_conditional_formats`` multi-sheet envelope are reused across result
models.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class _Result(BaseModel):
    """Base for every result model: permissive mirror of a core dict.

    ``model_config`` allows extra keys so a model never lags a core dict during build-out;
    concrete result models pin the exact fields as the implementation lands.
    """

    model_config = {"extra": "allow"}

    ok: bool = True


# --- Shared sub-models -----------------------------------------------------------------


class CellFormat(_Result):
    """Flattened ``userEnteredFormat`` OR ``effectiveFormat`` (DESIGN §3.1). All keys optional."""


class ValidationRule(_Result):
    """Structured data-validation rule that round-trips into ``set_validation`` (DESIGN §3.1)."""


class Cell(_Result):
    """One cell in ``inspect`` output (value + optional formula/formats/note/validation)."""


# --- Per-core-function result models ---------------------------------------------------


class OverviewResult(_Result):
    """Mirror of ``core.overview`` (DESIGN §3.3)."""


class InspectResult(_Result):
    """Mirror of ``core.inspect`` (DESIGN §3.3)."""


class ReadValuesResult(_Result):
    """Mirror of ``core.read_values`` (DESIGN §3.3)."""


class ConditionalFormatReport(_Result):
    """Mirror of ``core.read_conditional_formats`` — multi-sheet envelope (DESIGN §3.3)."""


class WriteValuesResult(_Result):
    """Mirror of ``core.write_values`` (DESIGN §3.3)."""


class AppendResult(_Result):
    """Mirror of ``core.append_rows`` (DESIGN §3.3)."""


class ClearResult(_Result):
    """Mirror of ``core.clear`` (DESIGN §3.3)."""


class FormatResult(_Result):
    """Mirror of ``core.format`` (DESIGN §3.3)."""


class SetConditionalFormatResult(_Result):
    """Mirror of ``core.set_conditional_format`` (single + batch forms) (DESIGN §3.3)."""


class SetValidationResult(_Result):
    """Mirror of ``core.set_validation`` (DESIGN §3.3)."""


class StructureResult(_Result):
    """Mirror of ``core.structure`` — shape-stable multi-sheet envelope (DESIGN §3.3)."""


class ManageSheetsResult(_Result):
    """Mirror of ``core.manage_sheets`` (DESIGN §3.3)."""


class MetadataResult(_Result):
    """Mirror of ``core.metadata`` (DESIGN §3.3)."""


class ChartsResult(_Result):
    """Mirror of ``core.charts`` (DESIGN §3.3)."""


class BatchResult(_Result):
    """Mirror of ``core.batch`` (DESIGN §3.3)."""


def to_model(model_cls: type[BaseModel], data: dict[str, Any]) -> BaseModel:
    """Wrap a core return dict in its mirror model (mechanical, no reshaping).

    Args:
        model_cls: The target result model class.
        data: The plain dict a core function returned.

    Returns:
        A populated model instance for MCP ``structuredContent``.
    """
    raise NotImplementedError


__all__ = [
    "OverviewResult",
    "InspectResult",
    "ReadValuesResult",
    "ConditionalFormatReport",
    "WriteValuesResult",
    "AppendResult",
    "ClearResult",
    "FormatResult",
    "SetConditionalFormatResult",
    "SetValidationResult",
    "StructureResult",
    "ManageSheetsResult",
    "MetadataResult",
    "ChartsResult",
    "BatchResult",
    "CellFormat",
    "Cell",
    "ValidationRule",
]
