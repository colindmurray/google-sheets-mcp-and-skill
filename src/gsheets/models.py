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
    # v0.2 rich reads (DESIGN §X.1/§X.6): present per-cell ONLY when the cell carries them.
    runs: Optional[list[TextRun]] = None
    hyperlink: Optional[str] = None
    pivot: Optional[Pivot] = None


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
    # v0.2 rich reads (DESIGN §X.1/§X.6): cells differing in these never merge into one run, so a
    # surviving run carries them ONLY when its cells all share them. Present only when set.
    runs: Optional[list[TextRun]] = None
    hyperlink: Optional[str] = None
    pivot: Optional[Pivot] = None


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
    # v0.2 sheet-scoped structural reads (DESIGN §X.3/§X.4/§X.9/§X.16); each present only when
    # the sheet carries it. ``basicFilter`` is a single object (or null); the rest are lists.
    tables: Optional[list["Table"]] = None
    basicFilter: Optional["BasicFilter"] = None
    filterViews: Optional[list["FilterView"]] = None
    bandedRanges: Optional[list["Banding"]] = None
    slicers: Optional[list["Slicer"]] = None


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


# --- v0.2 extension sub-models (DESIGN §Extensions / §X.0) ------------------------------
# These mirror the new pure-core serializers (richtext/pivot/tables/filters/banding/slicers/
# comments). Each serializer emits a flat, omit-when-absent dict that ALSO carries a terse
# ``line`` (condformat-style) for the human/AI-facing rendering; ``extra="allow"`` tolerates the
# ``line`` key while the documented structured fields stay pinned + typed for ``outputSchema``.


class TextRun(_Sub):
    """One styled char-range segment of a cell's ``textFormatRuns`` (DESIGN §X.0a).

    Mirrors ``core.richtext.serialize_text_runs`` per-run output: ``start`` is the 0-based char
    offset, ``text`` is the substring ``[start..nextStart)``, ``format`` is the flattened
    run-level ``textFormat`` subset (only when styled), ``link`` is the run-level link URI (which
    TAKES PRECEDENCE over a cell-level ``hyperlink``). ``format``/``link`` are present only when
    set (omit-when-absent).
    """

    start: Optional[int] = None
    text: Optional[str] = None
    format: Optional[CellFormat] = None
    link: Optional[str] = None


class PivotField(_Sub):
    """One pivot ROW or COLUMN grouping (DESIGN §X.0b ``rows``/``columns`` entry).

    Mirrors ``core.pivot``'s flattened ``PivotGroup``: ``field`` is the source column's display
    name (resolved from a header/data-source ref when present), ``sourceColumnOffset`` is its
    0-based offset into the pivot ``source``, ``showTotals``/``sortOrder`` are present only when
    set.
    """

    field: Optional[str] = None
    sourceColumnOffset: Optional[int] = None
    showTotals: Optional[bool] = None
    sortOrder: Optional[str] = None


class PivotValue(_Sub):
    """One pivot VALUE / aggregation (DESIGN §X.0b ``values`` entry).

    Mirrors ``core.pivot``'s flattened ``PivotValue``: ``name`` is the value's display label,
    ``sourceColumnOffset`` its 0-based source offset, ``summarize`` the ``summarizeFunction``
    (e.g. ``SUM``). ``field``/``summarize`` are present only when known.
    """

    name: Optional[str] = None
    sourceColumnOffset: Optional[int] = None
    field: Optional[str] = None
    summarize: Optional[str] = None


class PivotFilter(_Sub):
    """One flattened pivot filter criterion (DESIGN §X.0b ``filters`` entry).

    Mirrors ``core.pivot``'s flattened ``criteria`` map entry: keyed by ``sourceColumnOffset``
    with the normalized ``visibleValues`` it constrains to.
    """

    sourceColumnOffset: Optional[int] = None
    visibleValues: Optional[list[Any]] = None


class Pivot(_Sub):
    """A flattened pivot-table definition, attached to the anchor cell only (DESIGN §X.0b).

    Mirrors ``core.pivot.serialize_pivot``: ``source`` is the data ``GridRange`` resolved to A1,
    ``rows``/``columns`` are :class:`PivotField` lists, ``values`` a :class:`PivotValue` list,
    ``filters`` a :class:`PivotFilter` list, ``valueLayout`` is ``HORIZONTAL``/``VERTICAL``. The
    serializer also carries a terse ``line`` (kept by ``extra="allow"``).
    """

    source: Optional[str] = None
    rows: Optional[list[PivotField]] = None
    columns: Optional[list[PivotField]] = None
    values: Optional[list[PivotValue]] = None
    filters: Optional[list[PivotFilter]] = None
    valueLayout: Optional[str] = None
    line: Optional[str] = None


class TableColumn(_Sub):
    """One native-table column (DESIGN §X.0c ``columns`` entry).

    Mirrors ``core.tables.serialize_table``'s per-column dict: ``name``, ``type`` (the
    ``columnType`` enum TEXT|DOUBLE|CURRENCY|PERCENT|DATE|TIME|DATETIME|DROPDOWN|CHECKBOX|
    SMART_CHIP|RATING), and ``validation`` (a DROPDOWN column's ``ValidationRule`` one-liner,
    present only when set).
    """

    name: Optional[str] = None
    type: Optional[str] = None
    validation: Optional[str] = None


class Table(_Sub):
    """A native Table (``Table``) read shape (DESIGN §X.0c).

    Mirrors ``core.tables.serialize_table``: ``tableId``/``name``, ``range`` (the table's
    ``GridRange`` resolved to A1), and a :class:`TableColumn` list. A terse ``line`` rides along
    (kept by ``extra="allow"``).
    """

    tableId: Optional[str] = None
    name: Optional[str] = None
    range: Optional[str] = None
    columns: Optional[list[TableColumn]] = None
    line: Optional[str] = None


class SortSpec(_Sub):
    """One flattened sort spec inside a basic filter / filter view (DESIGN §X.0d).

    Mirrors ``core.filters``'s flattened ``SortSpec``: ``col`` is the column LETTER (offset → A1
    col), ``order`` is ``ASCENDING``/``DESCENDING``.
    """

    col: Optional[str] = None
    order: Optional[str] = None


class FilterCriterion(_Sub):
    """One flattened per-column filter criterion (DESIGN §X.0d ``criteria`` entry).

    Mirrors ``core.filters``'s flattened ``FilterCriteria``: ``col`` is the column LETTER,
    ``hidden``/``visible`` are the normalized hidden/visible value lists (present only when set),
    ``condition`` is the ``BooleanCondition`` serialized via the SAME condformat condition
    serializer (present only when set).
    """

    col: Optional[str] = None
    hidden: Optional[list[Any]] = None
    visible: Optional[list[Any]] = None
    condition: Optional[str] = None


class BasicFilter(_Sub):
    """A sheet's single basic filter, or ``null`` when none (DESIGN §X.0d).

    Mirrors ``core.filters.serialize_basic_filter``: ``range`` (A1), ``sorted`` (:class:`SortSpec`
    list), ``criteria`` (:class:`FilterCriterion` list). A terse ``line`` rides along.
    """

    range: Optional[str] = None
    sorted: Optional[list[SortSpec]] = None
    criteria: Optional[list[FilterCriterion]] = None
    line: Optional[str] = None


class FilterView(_Sub):
    """One saved filter view (DESIGN §X.0d ``filterViews`` entry).

    Mirrors ``core.filters.serialize_filter_view``: ``filterViewId``/``title``, ``range`` (A1),
    ``sorted`` + ``criteria`` (as in :class:`BasicFilter`). A terse ``line`` rides along.
    """

    filterViewId: Optional[int] = None
    title: Optional[str] = None
    range: Optional[str] = None
    sorted: Optional[list[SortSpec]] = None
    criteria: Optional[list[FilterCriterion]] = None
    line: Optional[str] = None


class BandingStyle(_Sub):
    """The per-axis band colors of a banded range (DESIGN §X.0e ``rowBanding``/``columnBanding``).

    Mirrors ``core.banding``'s flattened band: each slot is a flattened hex (or ``null`` when the
    band has no color for that slot). ``header``/``footer`` are the header/footer band colors;
    ``first``/``second`` alternate the body rows/columns.
    """

    header: Optional[str] = None
    first: Optional[str] = None
    second: Optional[str] = None
    footer: Optional[str] = None


class Banding(_Sub):
    """One banded range (``bandedRanges`` entry) (DESIGN §X.0e).

    Mirrors ``core.banding.serialize_banding``: ``bandedRangeId``, ``range`` (A1), and the
    per-axis :class:`BandingStyle` (``rowBanding`` and/or ``columnBanding``, each ``null`` when
    absent). A terse ``line`` rides along.
    """

    bandedRangeId: Optional[int] = None
    range: Optional[str] = None
    rowBanding: Optional[BandingStyle] = None
    columnBanding: Optional[BandingStyle] = None
    line: Optional[str] = None


class SlicerAnchor(_Sub):
    """A slicer's overlay anchor cell (DESIGN §X.0f ``anchor``)."""

    sheet: Optional[str] = None
    row: Optional[int] = None
    col: Optional[int] = None


class Slicer(_Sub):
    """One slicer (``slicers`` entry) (DESIGN §X.0f).

    Mirrors ``core.slicers.serialize_slicer``: ``slicerId``/``title``, ``range`` (the filtered
    data range in A1), ``columnIndex`` (the column the slicer filters), ``anchor`` (overlay
    position), and ``criteria`` (a one-liner when set). A terse ``line`` rides along.
    """

    slicerId: Optional[int] = None
    title: Optional[str] = None
    range: Optional[str] = None
    columnIndex: Optional[int] = None
    anchor: Optional[SlicerAnchor] = None
    criteria: Optional[str] = None
    line: Optional[str] = None


class CommentReply(_Sub):
    """One reply on a Drive comment (DESIGN §X.0g ``replies`` entry).

    Mirrors ``core.comments``'s flattened reply: ``author`` (display name), ``content``, and an
    optional ``action`` (e.g. ``resolve``). All present only when set.
    """

    author: Optional[str] = None
    content: Optional[str] = None
    action: Optional[str] = None


class Comment(_Sub):
    """One Drive comment on the spreadsheet file (DESIGN §X.0g).

    Mirrors ``core.comments.serialize_comment``: ``id``, ``author`` (display name), ``content``,
    ``created``/``modified`` timestamps, ``resolved`` flag, ``quoted`` snippet (when any),
    ``anchorRaw`` (the OPAQUE document-level anchor — NEVER an A1 range), and a
    :class:`CommentReply` list. A terse ``line`` rides along.
    """

    id: Optional[str] = None
    author: Optional[str] = None
    content: Optional[str] = None
    created: Optional[str] = None
    modified: Optional[str] = None
    resolved: Optional[bool] = None
    quoted: Optional[str] = None
    anchorRaw: Optional[str] = None
    replies: Optional[list[CommentReply]] = None
    line: Optional[str] = None


# --- Per-core-function result models ---------------------------------------------------


class OverviewResult(_Result):
    """Mirror of ``core.overview`` (DESIGN §3.3)."""

    spreadsheetId: Optional[str] = None
    title: Optional[str] = None
    # v0.2 spreadsheet-level locale/timeZone (DESIGN §X.12); present only when set.
    locale: Optional[str] = None
    timeZone: Optional[str] = None
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


class DataOpsResult(_Result):
    """Mirror of ``core.data_ops`` (DESIGN §X.2/§X.11/§X.14/§X.15).

    One dispatch fn over the one-request ``batchUpdate`` data verbs; the return is action-specific
    so every summary field below is optional and ``extra="allow"`` carries any verb-specific key
    not pinned here. ``action`` is always present; the scope (``range`` / ``source`` + ``destination``
    / ``sheet`` / ``allSheets``) and the count summary depend on the verb:

    * ``find_replace`` → ``occurrencesChanged``/``valuesChanged``/``formulasChanged``/
      ``rowsChanged``/``sheetsChanged`` (+ the chosen scope);
    * ``delete_duplicates`` → ``duplicatesRemoved``;
    * ``trim_whitespace``/``text_to_columns`` → ``cellsChangedCount``;
    * ``sort_range`` → echoed ``specs``;
    * ``copy_paste``/``cut_paste``/``auto_fill`` → ``source``/``destination`` (or ``range``).
    """

    spreadsheetId: Optional[str] = None
    action: Optional[str] = None
    # scope (mutually-exclusive across verbs)
    range: Optional[str] = None
    sheet: Optional[str] = None
    allSheets: Optional[bool] = None
    source: Optional[str] = None
    destination: Optional[str] = None
    specs: Optional[list[dict[str, Any]]] = None
    # action-specific count summaries (present per verb)
    occurrencesChanged: Optional[int] = None
    valuesChanged: Optional[int] = None
    formulasChanged: Optional[int] = None
    rowsChanged: Optional[int] = None
    sheetsChanged: Optional[int] = None
    duplicatesRemoved: Optional[int] = None
    cellsChangedCount: Optional[int] = None

    @property
    def terse(self) -> str:
        scope = self.range or (
            f"{self.source} -> {self.destination}"
            if self.source and self.destination
            else (self.sheet or ("all sheets" if self.allSheets else None))
        )
        counts: list[str] = []
        for label, val in (
            ("occurrences", self.occurrencesChanged),
            ("values", self.valuesChanged),
            ("formulas", self.formulasChanged),
            ("rows", self.rowsChanged),
            ("sheets", self.sheetsChanged),
            ("duplicates", self.duplicatesRemoved),
            ("cells", self.cellsChangedCount),
        ):
            if val:
                counts.append(f"{val} {label}")
        head = f"data_ops {self.action}"
        if scope:
            head += f" on {scope}"
        return f"{head}: {', '.join(counts)}" if counts else head


class DimensionsResult(_Result):
    """Mirror of ``core.dimensions`` (DESIGN §X.7/§X.10/§X.13).

    Row/column ops on one tab. ``action``/``sheet`` are always present on a write; the echoed
    geometry (``dimension``/``start``/``end`` + ``destinationIndex``/``length``/``pixelSize``/
    ``hiddenByUser``) depends on the verb. The ``read`` action returns ``hiddenRows``/``hiddenCols``
    (absolute 0-based indices) instead. All fields optional so one model mirrors every verb.
    """

    spreadsheetId: Optional[str] = None
    action: Optional[str] = None
    sheet: Optional[str] = None
    dimension: Optional[str] = None
    start: Optional[int] = None
    end: Optional[int] = None
    destinationIndex: Optional[int] = None
    length: Optional[int] = None
    pixelSize: Optional[int] = None
    hiddenByUser: Optional[bool] = None
    # read shape
    hiddenRows: Optional[list[int]] = None
    hiddenCols: Optional[list[int]] = None

    @property
    def terse(self) -> str:
        if self.action == "read":
            nr = len(self.hiddenRows or [])
            nc = len(self.hiddenCols or [])
            return f"dimensions read [{self.sheet}]: {nr} hidden row(s), {nc} hidden col(s)"
        span = ""
        if self.dimension is not None:
            span = f" {self.dimension}"
            if self.start is not None and self.end is not None:
                span += f"[{self.start}:{self.end}]"
            elif self.length is not None:
                span += f" x{self.length}"
        extra = ""
        if self.destinationIndex is not None:
            extra = f" -> {self.destinationIndex}"
        return f"dimensions {self.action}{span} on {self.sheet}{extra}"


class CommentsResult(_Result):
    """Mirror of ``core.comments`` (DESIGN §X.5, read-only v1).

    Drive-API comments on the spreadsheet file (NOT the Sheets API). ``comments`` is a flat list
    of :class:`Comment` (each with flattened author, ``quoted`` snippet, opaque ``anchorRaw``, and
    replies).
    """

    spreadsheetId: Optional[str] = None
    comments: list[Comment] = []

    @property
    def terse(self) -> str:
        if not self.comments:
            return "no comments"
        lines = [f"{len(self.comments)} comment(s)"]
        for c in self.comments:
            state = "resolved" if c.resolved else "open"
            n = len(c.replies or [])
            reply = f", {n} reply(ies)" if n else ""
            lines.append(
                f"  {c.id} by {c.author or '?'}: {(c.content or '')!r} ({state}{reply})"
            )
        return "\n".join(lines)


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
    # v0.2 extension top-level core fns (DESIGN §Extensions).
    "data_ops": DataOpsResult,
    "dimensions": DimensionsResult,
    "comments": CommentsResult,
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


class DataOpsParams(TypedDict, total=False):
    """Union of all ``data_ops`` per-action ``params`` keys (DESIGN §X.2/§X.11/§X.14/§X.15).

    Advisory shape (core validates strictly; an unknown key → ``unknown_param``). The valid
    subset depends on ``action`` — e.g. ``find_replace`` uses ``find``/``replacement``/scope,
    ``copy_paste`` uses ``source``/``destination``/``pasteType``.
    """

    find: str
    replacement: str
    searchByRegex: bool
    matchCase: bool
    matchEntireCell: bool
    includeFormulas: bool
    range: str
    sheet: str
    allSheets: bool
    comparisonColumns: list[str]
    specs: list[dict[str, str]]
    delimiter: str
    delimiterType: Literal[
        "COMMA", "SEMICOLON", "PERIOD", "SPACE", "CUSTOM", "AUTODETECT"
    ]
    useAlternateSeries: bool
    source: str
    destination: str
    pasteType: Literal[
        "PASTE_NORMAL",
        "PASTE_VALUES",
        "PASTE_FORMAT",
        "PASTE_FORMULA",
        "PASTE_NO_BORDERS",
        "PASTE_CONDITIONAL_FORMATTING",
        "PASTE_DATA_VALIDATION",
    ]
    pasteOrientation: Literal["NORMAL", "TRANSPOSE"]


class DimensionsParams(TypedDict, total=False):
    """Union of all ``dimensions`` per-action ``params`` keys (DESIGN §X.7/§X.10/§X.13).

    Advisory shape (core validates strictly). Every op targets one tab (the ``sheet`` arg);
    indices are 0-based half-open.
    """

    dimension: Literal["ROWS", "COLUMNS"]
    start: int
    end: int
    inheritFromBefore: bool
    destinationIndex: int
    length: int
    pixelSize: int
    hiddenByUser: bool
    range: str


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
    # v0.2 extension result models (DESIGN §Extensions)
    "DataOpsResult",
    "DimensionsResult",
    "CommentsResult",
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
    # v0.2 extension sub-models (DESIGN §X.0)
    "TextRun",
    "PivotField",
    "PivotValue",
    "PivotFilter",
    "Pivot",
    "TableColumn",
    "Table",
    "SortSpec",
    "FilterCriterion",
    "BasicFilter",
    "FilterView",
    "BandingStyle",
    "Banding",
    "SlicerAnchor",
    "Slicer",
    "CommentReply",
    "Comment",
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
    "DataOpsParams",
    "DimensionsParams",
]
