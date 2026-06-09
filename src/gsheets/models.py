"""Pydantic models mirroring each core return dict, field-for-field (DESIGN §3.1, §7.1).

ADAPTER-SIDE ONLY: ``gsheets.core`` / ``gsheets.auth`` must NEVER import this module
(boundary rule, DESIGN §1). These models give the MCP adapter its ``outputSchema`` /
``structuredContent``; a terse ``__str__`` / ``terse`` property provides the token-efficient
``content`` text (DESIGN §3.1, research §4/§6). Models are mechanical mirrors — adding a core
field means adding a model field, never reshaping.

Every result model derives from :class:`_Result`, which:

* carries ``ok: bool = True`` (every core success dict carries ``ok``);
* sets ``model_config = {"extra": "allow"}`` so a model never lags a core dict if a new key
  lands before the model is updated (the documented fields below are still pinned + typed so
  ``outputSchema`` stays self-documenting);
* renders a terse, token-efficient one/few-line summary via :pyattr:`terse` and ``__str__``.

Shared sub-models (:class:`CellFormat`, :class:`Cell`, :class:`ValidationRule`, :class:`Run`,
:class:`CFRule`, :class:`SheetCFRules`, :class:`SheetStructure`) and the multi-sheet envelope
shape are reused across result models, matching the core dicts exactly.

The :class:`TypedDict` params/spec/location helpers at the bottom (e.g. :class:`StructureParams`,
:class:`ChartSpec`) document the per-action ``params`` keys the MCP adapter can spell out in its
input schema (DESIGN §3.3); they are advisory shapes, not enforced (core validates).
"""

from __future__ import annotations

from typing import Any, Literal, Optional, TypedDict

from pydantic import BaseModel


class _Sub(BaseModel):
    """Base for shared SUB-models (nested shapes inside a result, DESIGN §3.1).

    A sub-model is a fragment of a core dict (a cell, a run, a per-sheet entry), NOT a
    top-level result — so it carries no ``ok`` flag. ``extra="allow"`` keeps it from lagging a
    core dict if a nested key is added before the model is updated.
    """

    model_config = {"extra": "allow"}


class _Result(_Sub):
    """Base for every top-level result model: typed, permissive mirror of a core dict.

    Adds the ``ok`` flag every core success dict carries (errors are raised, never returned —
    DESIGN §6). Concrete result models pin the exact documented fields (typed) for a
    self-documenting ``outputSchema``. Subclasses override :pyattr:`terse` to render the
    token-efficient ``content`` summary (DESIGN §3.1, research §6).
    """

    ok: bool = True

    @property
    def terse(self) -> str:
        """A token-efficient one/few-line summary for the MCP ``content`` block.

        Subclasses override this with a domain-specific rendering; the base falls back to the
        class name plus ``ok`` so a model is never content-less.
        """
        return f"{type(self).__name__}(ok={self.ok})"

    def __str__(self) -> str:  # pragma: no cover - thin delegate to ``terse``
        return self.terse


# --- Shared sub-models -----------------------------------------------------------------


class CellFormat(_Sub):
    """Flattened ``userEnteredFormat`` OR ``effectiveFormat`` (DESIGN §3.1). All keys optional.

    Mirrors ``core.flatten.flatten_cell_format`` output: colors are top-level hex strings,
    text styles are lifted, ``numberFormat`` is the pattern with ``numberFormatType`` alongside,
    borders are ``"<style> <hex>"`` per side. Unset keys are omitted (token efficiency), so every
    field is optional and ``extra`` is allowed for any forward-added key.
    """

    bg: Optional[str] = None
    fg: Optional[str] = None
    bold: Optional[bool] = None
    italic: Optional[bool] = None
    underline: Optional[bool] = None
    strikethrough: Optional[bool] = None
    fontSize: Optional[float] = None
    fontFamily: Optional[str] = None
    numberFormat: Optional[str] = None
    numberFormatType: Optional[str] = None
    halign: Optional[str] = None
    valign: Optional[str] = None
    wrap: Optional[str] = None
    borders: Optional[dict[str, str]] = None
    padding: Optional[dict[str, int]] = None
    textRotation: Optional[dict[str, Any]] = None


class ValidationRule(_Sub):
    """Structured data-validation rule that round-trips into ``set_validation`` (DESIGN §3.1).

    The SAME dict ``inspect`` surfaces under a cell's ``validationRule`` and that
    ``set_validation(rule=...)`` accepts. ``ok`` is irrelevant here (this is a sub-shape, not a
    result), so it is left at its inherited default and never rendered.
    """

    type: Optional[str] = None
    values: Optional[list[Any]] = None
    source: Optional[str] = None
    strict: Optional[bool] = None
    showDropdown: Optional[bool] = None


class Cell(_Sub):
    """One cell in non-compact ``inspect`` output (DESIGN §3.1 ``Cell``).

    ``a1`` is always present; ``value``/``formula``/formats/``note``/``validation`` are present
    only when set (empty cells are emitted as a bare ``{"a1": ...}`` so the rectangle is padded).
    """

    a1: Optional[str] = None
    value: Optional[Any] = None
    formula: Optional[str] = None
    userEnteredFormat: Optional[CellFormat] = None
    effectiveFormat: Optional[CellFormat] = None
    note: Optional[str] = None
    validation: Optional[str] = None
    validationRule: Optional[ValidationRule] = None


class Run(_Sub):
    """One rectangular RLE run in compact ``inspect`` output (DESIGN §3.3 run shape).

    A maximal rectangle of cells whose value/formula/format/note/validationRule are identical.
    ``a1Range`` is a bare ``"A1:B2"`` (no sheet prefix, always ``lo:hi`` even for a 1x1 run).
    ``note``/``validationRule`` are present only when set.
    """

    a1Range: Optional[str] = None
    value: Optional[Any] = None
    formula: Optional[str] = None
    format: Optional[CellFormat] = None
    note: Optional[str] = None
    validationRule: Optional[ValidationRule] = None


class OverviewSheet(_Sub):
    """One per-sheet row in ``overview`` (DESIGN §3.3 overview)."""

    sheetId: Optional[int] = None
    title: Optional[str] = None
    index: Optional[int] = None
    type: Optional[str] = None
    rows: Optional[int] = None
    cols: Optional[int] = None
    frozenRows: Optional[int] = None
    frozenCols: Optional[int] = None
    tabColor: Optional[str] = None
    protectedRangeCount: Optional[int] = None
    conditionalFormatCount: Optional[int] = None


class NamedRange(_Sub):
    """A spreadsheet-scoped named range (``overview`` / ``structure`` read) (DESIGN §3.3)."""

    name: Optional[str] = None
    namedRangeId: Optional[str] = None
    range: Optional[str] = None


class ProtectedRange(_Sub):
    """A per-sheet protected range in ``structure`` read (DESIGN §3.3)."""

    protectedRangeId: Optional[int] = None
    range: Optional[str] = None
    description: Optional[str] = None
    editors: Optional[list[str]] = None
    warningOnly: Optional[bool] = None


class DimensionGroup(_Sub):
    """A per-sheet row/column dimension group in ``structure`` read (DESIGN §3.3)."""

    dimension: Optional[str] = None
    start: Optional[int] = None
    end: Optional[int] = None
    depth: Optional[int] = None
    collapsed: Optional[bool] = None


class SheetStructure(_Sub):
    """One per-sheet entry in the ``structure(action="read")`` envelope (DESIGN §3.3)."""

    sheet: Optional[str] = None
    sheetId: Optional[int] = None
    merges: Optional[list[str]] = None
    frozenRows: Optional[int] = None
    frozenCols: Optional[int] = None
    tabColor: Optional[str] = None
    protectedRanges: Optional[list[ProtectedRange]] = None
    dimensionGroups: Optional[list[DimensionGroup]] = None


class CFCondition(_Sub):
    """A boolean-rule condition inside a serialized CF rule (DESIGN §3.3/§4)."""

    type: Optional[str] = None
    values: Optional[list[Any]] = None


class CFRule(_Sub):
    """One serialized conditional-format rule (DESIGN §3.3 ``read_conditional_formats``).

    Carries the positional ``index`` (the only addressing source of truth), the body-only
    ``line`` (human/AI-facing), the structured ``ranges``/``kind``/``format`` for round-trip,
    and EITHER ``condition`` (boolean) OR ``stops`` (gradient) per ``kind``.
    """

    index: Optional[int] = None
    line: Optional[str] = None
    ranges: Optional[list[str]] = None
    kind: Optional[str] = None
    condition: Optional[CFCondition] = None
    stops: Optional[list[dict[str, Any]]] = None
    format: Optional[CellFormat] = None


class SheetCFRules(_Sub):
    """One per-sheet entry in the ``read_conditional_formats`` envelope (DESIGN §3.3)."""

    sheet: Optional[str] = None
    sheetId: Optional[int] = None
    rules: Optional[list[CFRule]] = None


class ValueRange(_Sub):
    """One range entry in ``read_values`` output (DESIGN §3.3 read_values).

    ``computed`` is present only when ``render="all"`` (the FORMATTED pass, index-aligned with
    ``values``).
    """

    range: Optional[str] = None
    values: Optional[list[list[Any]]] = None
    computed: Optional[list[list[Any]]] = None


class SheetRef(_Sub):
    """A ``{sheetId, title, index}`` reference returned by ``manage_sheets`` (DESIGN §3.3)."""

    sheetId: Optional[int] = None
    title: Optional[str] = None
    index: Optional[int] = None


class AppendUpdates(_Sub):
    """The ``updates`` sub-object of ``append_rows`` (DESIGN §3.3)."""

    updatedRange: Optional[str] = None
    updatedRows: Optional[int] = None
    updatedCells: Optional[int] = None


class MetadataEntry(_Sub):
    """One developer-metadata entry in ``metadata`` output (DESIGN §3.3)."""

    metadataId: Optional[int] = None
    key: Optional[str] = None
    value: Optional[str] = None
    visibility: Optional[str] = None
    location: Optional[dict[str, Any]] = None


class ChartMeta(_Sub):
    """One chart's v1 metadata in ``charts(action="read")`` (DESIGN §3.3, v1 scope)."""

    chartId: Optional[int] = None
    title: Optional[str] = None
    type: Optional[str] = None
    anchor: Optional[dict[str, Any]] = None


class CFMutationResult(_Sub):
    """One applied mutation in the ``set_conditional_format`` batch form (DESIGN §3.3)."""

    action: Optional[str] = None
    index: Optional[int] = None
    rule: Optional[str] = None


class NewIds(_Sub):
    """Captured reply ids from ``batch`` (DESIGN §3.3/§5.4). Every bucket is always a list."""

    sheetIds: list[int] = []
    chartIds: list[int] = []
    namedRangeIds: list[str] = []
    protectedRangeIds: list[int] = []
    metadataIds: list[int] = []


# --- Per-core-function result models ---------------------------------------------------


class OverviewResult(_Result):
    """Mirror of ``core.overview`` (DESIGN §3.3)."""

    spreadsheetId: Optional[str] = None
    title: Optional[str] = None
    sheets: list[OverviewSheet] = []
    namedRanges: list[NamedRange] = []

    @property
    def terse(self) -> str:
        head = f"{self.title or '(untitled)'} [{self.spreadsheetId}]"
        lines = [head]
        for s in self.sheets:
            extras = []
            if s.protectedRangeCount:
                extras.append(f"{s.protectedRangeCount} protected")
            if s.conditionalFormatCount:
                extras.append(f"{s.conditionalFormatCount} CF")
            suffix = f" ({', '.join(extras)})" if extras else ""
            lines.append(
                f"  - {s.title}: {s.rows}x{s.cols}, "
                f"frozen {s.frozenRows}r/{s.frozenCols}c{suffix}"
            )
        if self.namedRanges:
            names = ", ".join(n.name or "?" for n in self.namedRanges)
            lines.append(f"  named ranges: {names}")
        return "\n".join(lines)


class InspectResult(_Result):
    """Mirror of ``core.inspect`` (DESIGN §3.3).

    Carries ``cells`` (non-compact) OR ``runs`` (compact) per the ``compact`` flag; both are
    optional so the model mirrors whichever the core dict produced.
    """

    spreadsheetId: Optional[str] = None
    sheet: Optional[str] = None
    range: Optional[str] = None
    rows: Optional[int] = None
    cols: Optional[int] = None
    cells: Optional[list[Cell]] = None
    runs: Optional[list[Run]] = None
    merges: list[str] = []
    compact: bool = False

    @property
    def terse(self) -> str:
        head = f"{self.range} ({self.rows}x{self.cols})"
        if self.compact and self.runs is not None:
            lines = [f"{head} — {len(self.runs)} runs"]
            for r in self.runs:
                val = r.formula if r.formula else r.value
                lines.append(f"  {r.a1Range}: {val!r}")
            if self.merges:
                lines.append(f"  merges: {', '.join(self.merges)}")
            return "\n".join(lines)
        cells = self.cells or []
        lines = [f"{head} — {len(cells)} cells"]
        for c in cells:
            if c.value is None and c.formula is None:
                continue
            val = c.formula if c.formula else c.value
            lines.append(f"  {c.a1}: {val!r}")
        if self.merges:
            lines.append(f"  merges: {', '.join(self.merges)}")
        return "\n".join(lines)


class ReadValuesResult(_Result):
    """Mirror of ``core.read_values`` (DESIGN §3.3)."""

    spreadsheetId: Optional[str] = None
    render: Optional[str] = None
    ranges: list[ValueRange] = []

    @property
    def terse(self) -> str:
        lines = [f"read_values render={self.render}"]
        for vr in self.ranges:
            n = len(vr.values or [])
            lines.append(f"  {vr.range}: {n} rows")
        return "\n".join(lines)


class ConditionalFormatReport(_Result):
    """Mirror of ``core.read_conditional_formats`` — multi-sheet envelope (DESIGN §3.3)."""

    spreadsheetId: Optional[str] = None
    sheets: list[SheetCFRules] = []

    @property
    def terse(self) -> str:
        lines: list[str] = []
        for s in self.sheets:
            rules = s.rules or []
            lines.append(f"[{s.sheet}] {len(rules)} rule(s)")
            for r in rules:
                lines.append(f"  {r.index}: {r.line}")
        return "\n".join(lines) if lines else "no conditional-format rules"


class WriteValuesResult(_Result):
    """Mirror of ``core.write_values`` (DESIGN §3.3)."""

    spreadsheetId: Optional[str] = None
    updatedRanges: list[str] = []
    updatedCells: Optional[int] = None
    updatedRows: Optional[int] = None
    updatedColumns: Optional[int] = None

    @property
    def terse(self) -> str:
        return (
            f"wrote {self.updatedCells} cell(s) across "
            f"{', '.join(self.updatedRanges) or '(none)'}"
        )


class AppendResult(_Result):
    """Mirror of ``core.append_rows`` (DESIGN §3.3)."""

    spreadsheetId: Optional[str] = None
    updates: Optional[AppendUpdates] = None
    tableRange: Optional[str] = None

    @property
    def terse(self) -> str:
        u = self.updates
        if u is None:
            return "appended rows"
        return f"appended {u.updatedRows} row(s) to {u.updatedRange}"


class ClearResult(_Result):
    """Mirror of ``core.clear`` (DESIGN §3.3)."""

    spreadsheetId: Optional[str] = None
    clearedRanges: list[str] = []
    cleared: dict[str, bool] = {}

    @property
    def terse(self) -> str:
        what = ", ".join(k for k, v in self.cleared.items() if v) or "nothing"
        return f"cleared {what} on {', '.join(self.clearedRanges) or '(none)'}"


class FormatResult(_Result):
    """Mirror of ``core.format`` (DESIGN §3.3)."""

    spreadsheetId: Optional[str] = None
    range: Optional[str] = None
    appliedFields: Optional[str] = None

    @property
    def terse(self) -> str:
        return f"formatted {self.range}: {self.appliedFields}"


class SetConditionalFormatResult(_Result):
    """Mirror of ``core.set_conditional_format`` (single + batch forms) (DESIGN §3.3).

    Single form carries ``action``/``sheet``/``index``/``rule``; batch form carries
    ``results``. Both are optional so the model mirrors whichever shape core returned.
    """

    spreadsheetId: Optional[str] = None
    action: Optional[str] = None
    sheet: Optional[str] = None
    index: Optional[int] = None
    rule: Optional[str] = None
    results: Optional[list[CFMutationResult]] = None

    @property
    def terse(self) -> str:
        if self.results is not None:
            lines = [f"applied {len(self.results)} CF mutation(s) (high->low)"]
            for r in self.results:
                suffix = f": {r.rule}" if r.rule else ""
                lines.append(f"  {r.action} @ {r.index}{suffix}")
            return "\n".join(lines)
        suffix = f": {self.rule}" if self.rule else ""
        return f"{self.action} CF @ index {self.index} on {self.sheet}{suffix}"


class SetValidationResult(_Result):
    """Mirror of ``core.set_validation`` (DESIGN §3.3)."""

    spreadsheetId: Optional[str] = None
    range: Optional[str] = None
    validation: Optional[str] = None
    validationRule: Optional[ValidationRule] = None

    @property
    def terse(self) -> str:
        if self.validation is None:
            return f"cleared validation on {self.range}"
        return f"set validation on {self.range}: {self.validation}"


class StructureResult(_Result):
    """Mirror of ``core.structure`` — shape-stable multi-sheet read envelope OR a mutate ack.

    Read returns top-level ``namedRanges`` + a ``sheets`` list; mutate returns
    ``action`` + the affected ids/ranges. All fields are optional so one model mirrors both
    shapes (DESIGN §3.3 — shape-stable, never object-vs-list).
    """

    spreadsheetId: Optional[str] = None
    # read shape
    namedRanges: Optional[list[NamedRange]] = None
    sheets: Optional[list[SheetStructure]] = None
    # mutate shape (any subset, by action)
    action: Optional[str] = None
    sheet: Optional[str] = None
    range: Optional[str] = None
    mergeType: Optional[str] = None
    name: Optional[str] = None
    namedRangeId: Optional[str] = None
    protectedRangeId: Optional[int] = None
    tabColor: Optional[str] = None
    frozenRows: Optional[int] = None
    frozenCols: Optional[int] = None
    dimension: Optional[str] = None
    start: Optional[int] = None
    end: Optional[int] = None

    @property
    def terse(self) -> str:
        if self.action is None and self.sheets is not None:
            lines: list[str] = []
            if self.namedRanges:
                lines.append(
                    "named: " + ", ".join(n.name or "?" for n in self.namedRanges)
                )
            for s in self.sheets:
                bits = [f"frozen {s.frozenRows}r/{s.frozenCols}c"]
                if s.merges:
                    bits.append(f"{len(s.merges)} merge(s)")
                if s.protectedRanges:
                    bits.append(f"{len(s.protectedRanges)} protected")
                if s.dimensionGroups:
                    bits.append(f"{len(s.dimensionGroups)} group(s)")
                lines.append(f"[{s.sheet}] {', '.join(bits)}")
            return "\n".join(lines) if lines else "no structure"
        return f"structure {self.action} ok"


class ManageSheetsResult(_Result):
    """Mirror of ``core.manage_sheets`` (DESIGN §3.3)."""

    spreadsheetId: Optional[str] = None
    action: Optional[str] = None
    sheet: Optional[SheetRef] = None

    @property
    def terse(self) -> str:
        s = self.sheet
        label = (s.title if s else None) or (s.sheetId if s else None)
        return f"{self.action} sheet {label}"


class MetadataResult(_Result):
    """Mirror of ``core.metadata`` (DESIGN §3.3)."""

    spreadsheetId: Optional[str] = None
    action: Optional[str] = None
    metadata: list[MetadataEntry] = []

    @property
    def terse(self) -> str:
        lines = [f"metadata {self.action}: {len(self.metadata)} entry(ies)"]
        for m in self.metadata:
            lines.append(f"  #{m.metadataId} {m.key}={m.value!r}")
        return "\n".join(lines)


class ChartsResult(_Result):
    """Mirror of ``core.charts`` (DESIGN §3.3, v1 scope).

    Create/update/delete carry ``chartId``; read carries ``charts`` (metadata only).
    """

    spreadsheetId: Optional[str] = None
    action: Optional[str] = None
    chartId: Optional[int] = None
    charts: Optional[list[ChartMeta]] = None

    @property
    def terse(self) -> str:
        if self.charts is not None:
            lines = [f"charts read: {len(self.charts)} chart(s)"]
            for c in self.charts:
                lines.append(f"  #{c.chartId} {c.title or '(untitled)'} [{c.type}]")
            return "\n".join(lines)
        return f"charts {self.action}: chartId={self.chartId}"


class BatchResult(_Result):
    """Mirror of ``core.batch`` (DESIGN §3.3)."""

    spreadsheetId: Optional[str] = None
    replies: list[Any] = []
    newIds: Optional[NewIds] = None

    @property
    def terse(self) -> str:
        ids = self.newIds
        new = ""
        if ids is not None:
            captured = []
            if ids.sheetIds:
                captured.append(f"sheets={ids.sheetIds}")
            if ids.chartIds:
                captured.append(f"charts={ids.chartIds}")
            if ids.namedRangeIds:
                captured.append(f"named={ids.namedRangeIds}")
            if ids.protectedRangeIds:
                captured.append(f"protected={ids.protectedRangeIds}")
            if ids.metadataIds:
                captured.append(f"metadata={ids.metadataIds}")
            if captured:
                new = " | newIds: " + ", ".join(captured)
        return f"batch: {len(self.replies)} reply(ies){new}"


# --- core-fn -> model registry + wrapper -----------------------------------------------

#: Maps a core function NAME to its mirror result model. The MCP adapter uses this (or the
#: per-tool annotation) to wrap a core dict; keeping it here means one place to update when a
#: core function/model pair changes.
RESULT_MODELS: dict[str, type[_Result]] = {
    "overview": OverviewResult,
    "inspect": InspectResult,
    "read_values": ReadValuesResult,
    "read_conditional_formats": ConditionalFormatReport,
    "write_values": WriteValuesResult,
    "append_rows": AppendResult,
    "clear": ClearResult,
    "format": FormatResult,
    "set_conditional_format": SetConditionalFormatResult,
    "set_validation": SetValidationResult,
    "structure": StructureResult,
    "manage_sheets": ManageSheetsResult,
    "metadata": MetadataResult,
    "charts": ChartsResult,
    "batch": BatchResult,
}


def to_model(model_cls: type[BaseModel], data: dict[str, Any]) -> BaseModel:
    """Wrap a core return dict in its mirror model (mechanical, no reshaping).

    The single boundary helper the MCP adapter calls: it validates the plain core dict into the
    given model so FastMCP can emit ``structuredContent`` (the typed payload) and ``content``
    (the model's terse summary). No reshaping happens — a model is a field-for-field mirror, and
    ``extra="allow"`` means an as-yet-unmodeled key still round-trips.

    Args:
        model_cls: The target result model class.
        data: The plain dict a core function returned.

    Returns:
        A populated model instance for MCP ``structuredContent`` / ``content``.
    """
    return model_cls.model_validate(data)


def model_for(core_fn_name: str, data: dict[str, Any]) -> _Result:
    """Wrap ``data`` in the mirror model registered for ``core_fn_name`` (DESIGN §7.1).

    Convenience over :func:`to_model` for the adapter's one-line tool bodies.

    Args:
        core_fn_name: A key in :data:`RESULT_MODELS` (a core function name).
        data: The plain dict that core function returned.

    Returns:
        The populated mirror model.

    Raises:
        KeyError: if ``core_fn_name`` is not a known core function.
    """
    return to_model(RESULT_MODELS[core_fn_name], data)  # type: ignore[return-value]


# --- Per-action TypedDicts for MCP input schemas (advisory; core validates) ------------
# These spell out the per-action ``params``/``location``/``spec`` keys (DESIGN §3.3) so the MCP
# adapter CAN surface them in a tool's input schema. They are NOT enforced here — core remains
# the single validator of action-specific keys (it raises ``unknown_param`` on a typo).


class StructureParams(TypedDict, total=False):
    """Union of all ``structure`` per-action ``params`` keys (DESIGN §3.3 table)."""

    mergeType: Literal["MERGE_ALL", "MERGE_COLUMNS", "MERGE_ROWS"]
    name: str
    namedRangeId: str
    description: str
    editors: list[str]
    warningOnly: bool
    rows: int
    cols: int
    color: str
    dimension: Literal["ROWS", "COLUMNS"]
    start: int
    end: int


class ManageSheetsParams(TypedDict, total=False):
    """Union of all ``manage_sheets`` per-action ``params`` keys (DESIGN §3.3)."""

    title: str
    index: int
    rows: int
    cols: int
    newName: str
    newIndex: int


class MetadataLocation(TypedDict, total=False):
    """A ``metadata`` ``location`` anchor (dimension / whole-sheet / spreadsheet) (DESIGN §3.3)."""

    sheet: str
    dimension: Literal["ROWS", "COLUMNS"]
    start: int
    end: int


class ChartAnchor(TypedDict, total=False):
    """The ``{sheet,row,col}`` anchor of a chart ``spec`` (DESIGN §3.3 charts)."""

    sheet: str
    row: int
    col: int


class ChartSpec(TypedDict, total=False):
    """The locked minimal flat chart ``spec`` (DESIGN §3.3 charts, v1 scope)."""

    title: str
    type: Literal["LINE", "COLUMN", "BAR", "PIE", "SCATTER", "AREA"]
    series: list[str]
    domain: str
    anchor: ChartAnchor


class WriteValuesItem(TypedDict, total=False):
    """One ``{range, values}`` item for ``write_values`` ``data`` (DESIGN §3.3)."""

    range: str
    values: list[list[Any]]


class CFBatchItem(TypedDict, total=False):
    """One ``{action, index, rule}`` item for the ``set_conditional_format`` batch (DESIGN §3.3)."""

    action: Literal["add", "update", "delete"]
    index: int
    rule: Any  # body line (str) OR structured rule dict


__all__ = [
    # per-core-fn result models
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
    # shared sub-models
    "CellFormat",
    "Cell",
    "ValidationRule",
    "Run",
    "OverviewSheet",
    "NamedRange",
    "ProtectedRange",
    "DimensionGroup",
    "SheetStructure",
    "CFCondition",
    "CFRule",
    "SheetCFRules",
    "ValueRange",
    "SheetRef",
    "AppendUpdates",
    "MetadataEntry",
    "ChartMeta",
    "CFMutationResult",
    "NewIds",
    # helpers + registry
    "to_model",
    "model_for",
    "RESULT_MODELS",
    # advisory input-schema TypedDicts
    "StructureParams",
    "ManageSheetsParams",
    "MetadataLocation",
    "ChartAnchor",
    "ChartSpec",
    "WriteValuesItem",
    "CFBatchItem",
]
