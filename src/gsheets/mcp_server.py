"""FastMCP stdio adapter (DESIGN §7.1).

The ONLY module importing ``fastmcp``/``mcp`` AND ``gsheets.models``. Registers one tool per
core function (one-line bodies), with ToolAnnotations per the §7.1 table, an ``ENABLED_TOOLS``
allowlist, ``mask_error_details=True``, and a ``to_tool_error`` envelope. The lifespan builds
:class:`SheetsServices` once and CATCHES build failure -> clear stderr message (no interactive
consent at startup). ``main()`` runs the stdio server. NEVER prints to stdout (JSON-RPC channel).

Boundary discipline (DESIGN §1): this adapter validates args, pulls the shared ``services``
handle out of the lifespan context, calls the matching PURE core function, and shapes the
result into a Pydantic mirror model. It contains ZERO Sheets logic — every tool body is one
line through :func:`_call`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from . import auth, models
from .core import (
    append_rows as _append_rows,
    batch as _batch,
    charts as _charts,
    clear as _clear,
    comments as _comments,
    data_ops as _data_ops,
    dimensions as _dimensions,
    format as _format,
    inspect as _inspect,
    manage_sheets as _manage_sheets,
    metadata as _metadata,
    overview as _overview,
    read_conditional_formats as _read_conditional_formats,
    read_values as _read_values,
    set_conditional_format as _set_conditional_format,
    set_validation as _set_validation,
    structure as _structure,
    write_values as _write_values,
)
from .core.errors import SheetsError
from .core.service import SheetsServices


# --------------------------------------------------------------------------- error envelope


def to_tool_error(err: SheetsError) -> ToolError:
    """Format a :class:`SheetsError` into the canonical terse MCP ``ToolError`` (DESIGN §6.2).

    The server runs with ``mask_error_details=True``, so curated ``ToolError`` text passes
    through to the client while unexpected exceptions surface generically. The 403 hint stays
    generic by default (no operator email — DESIGN §6.1).

    Two shapes per DESIGN §6.2:
      * Google API failures (``status`` present) ->
        ``"google_api_error: 403 PERMISSION_DENIED — share the sheet with the auth principal"``.
      * Validation failures (no ``status``) -> ``"<code>: <message>"``.

    Args:
        err: The core error.

    Returns:
        A ``ToolError`` carrying the canonical terse string. (Callers ``raise`` the result.)
    """
    if err.status is not None:
        head = f"{err.code}: {err.status}"
        if err.reason:
            head = f"{head} {err.reason}"
        msg = head if err.message in (None, "") else f"{head} {err.message}"
        if err.hint:
            msg = f"{msg} — {err.hint}"
        return ToolError(msg)

    msg = f"{err.code}: {err.message}"
    if err.hint:
        msg = f"{msg} — {err.hint}"
    return ToolError(msg)


# --------------------------------------------------------------------------- lifespan / ctx


@dataclass
class AppCtx:
    """Per-server lifespan context exposing the shared authed handle (DESIGN §7.1)."""

    services: SheetsServices


@asynccontextmanager
async def lifespan(server: "FastMCP") -> AsyncIterator[AppCtx]:
    """Build :class:`SheetsServices` once for the server's lifetime (DESIGN §7.1).

    Calls :func:`gsheets.auth.build_services` (steady-state; NO interactive consent at MCP
    startup — the server requires a pre-existing valid/refreshable token, §2.2). On failure,
    writes a clear actionable message to stderr and exits non-zero rather than crashing the
    JSON-RPC channel with a raw stack trace. Tools pull the handle from
    ``ctx.request_context.lifespan_context.services``; core never sees ``Context``.

    Args:
        server: The owning :class:`FastMCP` instance.

    Yields:
        An :class:`AppCtx` exposing the built ``services``.
    """
    try:
        services = auth.build_services()
    except SheetsError as exc:
        print(
            "google-sheets-mcp: no usable credentials — "
            f"{exc.code}: {exc.message}"
            + (f" ({exc.hint})" if exc.hint else "")
            + "\ngoogle-sheets-mcp: run `gsheets auth login` to mint a token first "
            "(see README Setup).",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1) from exc
    except Exception as exc:  # noqa: BLE001 - any startup failure must be a clean stderr exit
        print(
            f"google-sheets-mcp: failed to initialize Google Sheets services: {exc}\n"
            "google-sheets-mcp: run `gsheets auth login` / check GSHEETS_* env vars "
            "(see README Setup).",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1) from exc

    try:
        yield AppCtx(services=services)
    finally:
        pass  # googleapiclient Resources need no explicit close


#: The FastMCP server instance. Tools register against it below (filtered by ``ENABLED_TOOLS``).
mcp = FastMCP(name="google-sheets-mcp", lifespan=lifespan, mask_error_details=True)


def _services(ctx: Context) -> SheetsServices:
    """Pull the shared :class:`SheetsServices` out of the lifespan context (adapter glue)."""
    return ctx.request_context.lifespan_context.services


def _call(
    model_cls: type[BaseModel],
    fn: Callable[..., dict],
    *args: Any,
    **kwargs: Any,
) -> BaseModel:
    """Run one core function and shape its result for MCP (the entire tool body).

    Maps the single core exception (:class:`SheetsError`) to the canonical ``ToolError``
    envelope so every tool surfaces failures identically. On success, wraps the plain core
    dict in its mechanical Pydantic mirror so FastMCP emits ``structuredContent`` + a terse
    ``content`` block (DESIGN §3.1, §7.1). Contains no Sheets logic.

    Args:
        model_cls: The mirror result model for this core function.
        fn: The pure core function.
        *args: ``services`` followed by the core positional args.
        **kwargs: The core keyword args.

    Returns:
        A populated ``model_cls`` instance.

    Raises:
        ToolError: Built from the raised :class:`SheetsError` (curated, passes through masking).
    """
    try:
        result = fn(*args, **kwargs)
    except SheetsError as exc:
        raise to_tool_error(exc) from exc
    return model_cls(**result)


# --------------------------------------------------------------------------- registration


#: Comma-separated allowlist; empty/unset => every tool is registered (DESIGN §7.1, §2.1).
ENABLED_TOOLS: set[str] = {
    t.strip() for t in os.environ.get("ENABLED_TOOLS", "").split(",") if t.strip()
}


def register(*, annotations: ToolAnnotations, tags: set[str]):
    """Register a function as an MCP tool unless ``ENABLED_TOOLS`` excludes it (DESIGN §7.1, §8).

    Mirrors the ``xing5`` reference: an env-var allowlist read at registration time. When the
    set is non-empty and the tool name is not in it, the function is left unregistered — not
    advertised in ``list_tools`` and not callable.

    Args:
        annotations: The :class:`ToolAnnotations` (read/destructive/idempotent/open-world hints).
        tags: ``{"read"}`` or ``{"write"}`` for tag-based client scoping.

    Returns:
        A decorator that conditionally calls ``mcp.tool``.
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        if ENABLED_TOOLS and fn.__name__ not in ENABLED_TOOLS:
            return fn
        return mcp.tool(annotations=annotations, tags=tags)(fn)

    return deco


# --------------------------------------------------------------------------- READ tools


@register(
    annotations=ToolAnnotations(
        title="Spreadsheet overview",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
    tags={"read"},
)
def sheets_overview(spreadsheet_id: str, ctx: Context) -> models.OverviewResult:
    """Get a cheap orientation snapshot of a spreadsheet — NO grid data.

    Use this FIRST when you encounter a spreadsheet: it lists every tab with its dimensions,
    frozen rows/cols, tab color, and the COUNT of protected ranges and conditional-format
    rules, plus all named ranges. It never pulls cell contents, so it is safe to call on a
    huge sheet. Drill in afterward with ``sheets_inspect`` / ``sheets_read_conditional_formats``.

    Args:
        spreadsheet_id: The spreadsheet ID, e.g. "<YOUR_SPREADSHEET_ID>".

    Returns:
        ``{ok, spreadsheetId, title, sheets: [{sheetId, title, index, type, rows, cols,
        frozenRows, frozenCols, tabColor, protectedRangeCount, conditionalFormatCount}],
        namedRanges: [{name, range, namedRangeId}]}``.
    """
    return _call(models.OverviewResult, _overview, _services(ctx), spreadsheet_id)


@register(
    annotations=ToolAnnotations(
        title="Inspect a range (values + formulas + formats)",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
    tags={"read"},
)
def sheets_inspect(
    spreadsheet_id: str,
    range: str,
    ctx: Context,
    compact: bool = False,
    include_effective_format: bool = True,
    include_user_entered_format: bool = True,
    include_formulas: bool = True,
    include_validation: bool = True,
    include_rich_text: bool = False,
    include_pivot: bool = False,
) -> models.InspectResult:
    """Read a range RICHLY: values AND formulas, both userEntered & effective formats, merges,
    notes, and structured data-validation — in one call, with a tight fields mask.

    This is the flagship read. ``effectiveFormat`` shows what actually renders (including the
    result of conditional formatting); ``userEnteredFormat`` shows the author's intent.
    ``validationRule`` round-trips straight back into ``sheets_set_validation``. Set
    ``compact=True`` to collapse identical cells into rectangular runs (big token savings on
    repetitive blocks); set the ``include_*`` flags to False to trim the payload further. Opt
    into ``include_rich_text=True`` to surface per-cell rich-text runs + hyperlinks (segments
    styled differently within one cell, plus the cell's link), and ``include_pivot=True`` to
    surface a pivot-table definition on its anchor cell — both off by default (zero token cost).

    Args:
        spreadsheet_id: The spreadsheet ID, e.g. "<YOUR_SPREADSHEET_ID>".
        range: An A1 range, e.g. "Sheet1!A1:D20" (or a whole tab "Sheet1").
        compact: Collapse identical cells into rectangular ``runs`` and drop empties.
        include_effective_format: Include the rendered (effective) per-cell format.
        include_user_entered_format: Include the author-intent (userEntered) format.
        include_formulas: Include each cell's ``formula`` when it has one.
        include_validation: Include the terse ``validation`` line + structured ``validationRule``.
        include_rich_text: Attach ``runs`` (per-character styled segments) + ``hyperlink`` to a
            cell ONLY when it has them (off by default — adds ``textFormatRuns``/``hyperlink`` to
            the read mask).
        include_pivot: Attach a flattened ``pivot`` definition to a pivot table's anchor cell
            ONLY when present (off by default — adds ``pivotTable`` to the read mask).

    Returns:
        ``{ok, spreadsheetId, sheet, range, rows, cols, cells:[{a1,value,formula,
        userEnteredFormat,effectiveFormat,note,validation,validationRule,runs,hyperlink,pivot}],
        merges, compact}`` (``cells`` becomes ``runs`` when ``compact=True``; ``runs``/
        ``hyperlink``/``pivot`` present per-cell only when set).
    """
    return _call(
        models.InspectResult,
        _inspect,
        _services(ctx),
        spreadsheet_id,
        range,
        compact=compact,
        include_effective_format=include_effective_format,
        include_user_entered_format=include_user_entered_format,
        include_formulas=include_formulas,
        include_validation=include_validation,
        include_rich_text=include_rich_text,
        include_pivot=include_pivot,
    )


@register(
    annotations=ToolAnnotations(
        title="Read values",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
    tags={"read"},
)
def sheets_read_values(
    spreadsheet_id: str,
    ranges: list[str],
    ctx: Context,
    render: str = "plain",
) -> models.ReadValuesResult:
    """Read plain values for one or more A1 ranges, with a selectable render mode.

    Use this for fast bulk value reads when you do NOT need formats. ``render`` controls what
    each cell yields: "plain" (formatted display strings), "unformatted" (raw typed values),
    "formula" (the formula source, literals passed through), or "all" (formula + computed
    side-by-side, index-aligned). For rich per-cell formats/notes/validation, use
    ``sheets_inspect`` instead.

    Args:
        spreadsheet_id: The spreadsheet ID, e.g. "<YOUR_SPREADSHEET_ID>".
        ranges: One or more A1 ranges, e.g. ["Sheet1!A1:B10", "Sheet1!D:D"].
        render: "plain" | "unformatted" | "formula" | "all".

    Returns:
        ``{ok, spreadsheetId, render, ranges:[{range, values:[[...]], computed:[[...]]}]}``
        (``computed`` present only when ``render="all"``; rows padded to a rectangle).
    """
    return _call(
        models.ReadValuesResult, _read_values, _services(ctx), spreadsheet_id, ranges, render=render
    )


@register(
    annotations=ToolAnnotations(
        title="Read conditional-format rules",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
    tags={"read"},
)
def sheets_read_conditional_formats(
    spreadsheet_id: str,
    ctx: Context,
    sheet: str | None = None,
) -> models.ConditionalFormatReport:
    """Read a sheet's conditional-formatting RULES, serialized to terse readable lines.

    Use this to learn a sheet's color/highlight LOGIC before editing it — e.g. why a column
    goes red. Each rule renders as one body line plus structured fields, and carries its
    positional ``index`` (0 = highest priority; array order IS the priority — there is no
    separate priority field). Pass that ``index`` to ``sheets_set_conditional_format`` to
    update/delete the exact rule.

    Args:
        spreadsheet_id: The spreadsheet ID, e.g. "<YOUR_SPREADSHEET_ID>".
        sheet: Optional tab name; omit to scan every tab.

    Returns:
        ``{ok, spreadsheetId, sheets:[{sheet, sheetId, rules:[{index, line, ranges, kind,
        condition, format}]}]}``, where ``line`` looks like
        "[Sheet1!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold".
    """
    return _call(
        models.ConditionalFormatReport,
        _read_conditional_formats,
        _services(ctx),
        spreadsheet_id,
        sheet,
    )


@register(
    annotations=ToolAnnotations(
        title="Read comments (Drive)",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
    tags={"read"},
)
def sheets_comments(
    spreadsheet_id: str,
    ctx: Context,
    include_resolved: bool = True,
    include_deleted: bool = False,
) -> models.CommentsResult:
    """Read the threaded COMMENTS on a spreadsheet — author, text, resolved state, and replies.

    Comments live on the Drive file, not the Sheets grid, so this uses the Drive API (requires a
    Drive scope; if none is granted you get a clear ``drive_unavailable`` error — re-run with
    ``GSHEETS_SCOPES=broad``). Each comment flattens to author/content/created/modified, a
    ``resolved`` flag, an optional ``quoted`` snippet, and its ``replies``. The Drive ``anchor``
    is opaque and document-specific — surfaced raw as ``anchorRaw``, never mapped to a cell.

    Args:
        spreadsheet_id: The spreadsheet ID, e.g. "<YOUR_SPREADSHEET_ID>".
        include_resolved: Include resolved comments (default True); set False to see only open ones.
        include_deleted: Include deleted comments (default False).

    Returns:
        ``{ok, spreadsheetId, comments:[{id, author, content, created, modified, resolved,
        quoted, anchorRaw, replies:[{author, content, action}], line}]}``, where ``line`` looks
        like 'comment AAAA by Jane Doe: "please verify Q3" (open, 1 reply)'.
    """
    return _call(
        models.CommentsResult,
        _comments,
        _services(ctx),
        spreadsheet_id,
        include_resolved=include_resolved,
        include_deleted=include_deleted,
    )


# --------------------------------------------------------------------------- WRITE tools


@register(
    annotations=ToolAnnotations(
        title="Write values",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    tags={"write"},
)
def sheets_write_values(
    spreadsheet_id: str,
    data: list[dict],
    ctx: Context,
    input: str = "user_entered",
) -> models.WriteValuesResult:
    """Write/update one or more ranges in a single call. USER_ENTERED by default.

    With ``input="user_entered"`` (the default), strings beginning with "=" become live
    formulas and "5%"/"$3" parse like typed input — this is almost always what you want. Use
    ``input="raw"`` to store strings verbatim. This OVERWRITES the target cells; to add rows
    without overwriting, use ``sheets_append_rows``.

    Args:
        spreadsheet_id: The spreadsheet ID, e.g. "<YOUR_SPREADSHEET_ID>".
        data: ``[{"range": "Sheet1!A1", "values": [["=SUM(B:B)", 1]]}, ...]``.
        input: "user_entered" (parse like a user types) | "raw" (store verbatim).

    Returns:
        ``{ok, spreadsheetId, updatedRanges, updatedCells, updatedRows, updatedColumns}``.
    """
    return _call(
        models.WriteValuesResult, _write_values, _services(ctx), spreadsheet_id, data, input=input
    )


@register(
    annotations=ToolAnnotations(
        title="Append rows",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
    tags={"write"},
)
def sheets_append_rows(
    spreadsheet_id: str,
    range: str,
    values: list[list],
    ctx: Context,
    input: str = "user_entered",
) -> models.AppendResult:
    """Append rows AFTER the last row of a table — never overwrites existing data.

    Point ``range`` at any cell inside (or the header of) the table; Sheets finds the table's
    extent and inserts the new rows below it (INSERT_ROWS). NOT idempotent: each call adds more
    rows, so do not retry blindly. USER_ENTERED by default (formulas/percent parse).

    Args:
        spreadsheet_id: The spreadsheet ID, e.g. "<YOUR_SPREADSHEET_ID>".
        range: An A1 range anchoring the table, e.g. "Sheet1!A1".
        values: Row-major values to append, e.g. [["2026-06-09", 42], ["2026-06-10", 7]].
        input: "user_entered" | "raw".

    Returns:
        ``{ok, spreadsheetId, updates:{updatedRange, updatedRows, updatedCells}, tableRange}``.
    """
    return _call(
        models.AppendResult,
        _append_rows,
        _services(ctx),
        spreadsheet_id,
        range,
        values,
        input=input,
    )


@register(
    annotations=ToolAnnotations(
        title="Clear ranges",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
    tags={"write"},
)
def sheets_clear(
    spreadsheet_id: str,
    ranges: list[str],
    ctx: Context,
    values: bool = True,
    formats: bool = False,
    validation: bool = False,
    notes: bool = False,
) -> models.ClearResult:
    """Clear content from A1 ranges — values, and optionally formats / validation / notes.

    DESTRUCTIVE: by default this wipes the VALUES in the ranges. Enable the other flags to also
    strip formatting, data-validation rules, or cell notes. Clearing values only is a fast
    ``batchClear``; clearing the others uses an auto-masked ``updateCells`` so unspecified
    attributes are preserved. Confirm with the user before clearing important data.

    Args:
        spreadsheet_id: The spreadsheet ID, e.g. "<YOUR_SPREADSHEET_ID>".
        ranges: A1 ranges to clear, e.g. ["Sheet1!A2:D100"].
        values: Clear cell values (default True).
        formats: Also clear formatting.
        validation: Also clear data-validation rules.
        notes: Also clear cell notes.

    Returns:
        ``{ok, spreadsheetId, clearedRanges, cleared:{values, formats, validation, notes}}``.
    """
    return _call(
        models.ClearResult,
        _clear,
        _services(ctx),
        spreadsheet_id,
        ranges,
        values=values,
        formats=formats,
        validation=validation,
        notes=notes,
    )


@register(
    annotations=ToolAnnotations(
        title="Format a range",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    tags={"write"},
)
def sheets_format(spreadsheet_id: str, range: str, fmt: dict, ctx: Context) -> models.FormatResult:
    """Apply formatting to a range atomically: fill, font, number/date pattern, align, wrap,
    padding, borders, and cell note — all in ONE all-or-nothing batch.

    Pass the flat ``fmt`` keys (the same shape ``sheets_inspect`` reads back). Only the keys you
    include are touched — the fields mask is auto-built from the payload, so unspecified
    attributes are never wiped. Borders apply alongside the fill in a single batchUpdate (no
    partial-failure). Idempotent: re-applying the same ``fmt`` is a no-op.

    Args:
        spreadsheet_id: The spreadsheet ID, e.g. "<YOUR_SPREADSHEET_ID>".
        range: An A1 range, e.g. "Sheet1!A1:A10".
        fmt: Flat CellFormat, e.g. {"bg": "#FFCDD2", "bold": true, "numberFormat": "0.00%",
            "halign": "CENTER", "padding": {"top": 2, "left": 3},
            "borders": {"top": "SOLID #000000"}, "note": "reviewed"}.

    Returns:
        ``{ok, spreadsheetId, range, appliedFields}`` (the exact fields mask that was written).
    """
    return _call(models.FormatResult, _format, _services(ctx), spreadsheet_id, range, fmt)


@register(
    annotations=ToolAnnotations(
        title="Set conditional-format rule",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
    tags={"write"},
)
def sheets_set_conditional_format(
    spreadsheet_id: str,
    ctx: Context,
    action: str | None = None,
    sheet: str | None = None,
    index: int | None = None,
    rule: str | dict | None = None,
    rules: list[dict] | None = None,
) -> models.SetConditionalFormatResult:
    """Add, update, or delete a conditional-format rule by positional index.

    Rules are addressed by ``index`` in the sheet's rule array (0 = highest priority); there is
    NO separate priority field. ``rule`` accepts either a readable body line (e.g.
    "[Sheet1!A2:A100] if CUSTOM_FORMULA(=$B2>10) -> bg #FFCDD2 bold") OR a structured
    ``{ranges, kind, condition, format}`` dict — the line never carries an index; ``index``
    comes only from the kwarg.

    To mutate SEVERAL rules safely, pass ``rules`` (a list of ``{"action","index","rule"}``
    items, ``rule`` omitted for delete): core sorts them high index -> low and applies them in
    ONE batch so earlier edits never shift later targets. If you issue multiple SINGLE calls
    instead, order them high index -> low yourself (or re-read indices between calls). Supplying
    both ``rules`` and the single-form args raises an error.

    Args:
        spreadsheet_id: The spreadsheet ID, e.g. "<YOUR_SPREADSHEET_ID>".
        action: "add" | "update" | "delete" (single form; omit when using ``rules``).
        sheet: Tab name (single form).
        index: Target index for update/delete; insert position for add.
        rule: A body line or a structured rule dict (single form).
        rules: Batch form, e.g. [{"action":"delete","index":5}, {"action":"update","index":2,
            "rule":"..."}].

    Returns:
        Single: ``{ok, spreadsheetId, action, sheet, index, rule}``. Batch:
        ``{ok, spreadsheetId, results:[{action, index, rule}, ...]}`` (in applied high->low order).
    """
    return _call(
        models.SetConditionalFormatResult,
        _set_conditional_format,
        _services(ctx),
        spreadsheet_id,
        action=action,
        sheet=sheet,
        index=index,
        rule=rule,
        rules=rules,
    )


@register(
    annotations=ToolAnnotations(
        title="Set data validation",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    tags={"write"},
)
def sheets_set_validation(
    spreadsheet_id: str,
    range: str,
    ctx: Context,
    rule: dict | None = None,
    strict: bool = True,
    show_dropdown: bool = True,
) -> models.SetValidationResult:
    """Set (or clear) data-validation on a range — dropdowns, number/date/text/formula rules.

    Pass the structured ``rule`` shape that ``sheets_inspect`` reads back under each cell's
    ``validationRule`` (full round-trip). Omit ``rule`` (None) to CLEAR validation on the range.
    ``strict`` rejects invalid input; ``show_dropdown`` shows the chip for list rules. Idempotent.

    Args:
        spreadsheet_id: The spreadsheet ID, e.g. "<YOUR_SPREADSHEET_ID>".
        range: An A1 range, e.g. "Sheet1!A2:A100".
        rule: A ValidationRule, e.g. {"type": "ONE_OF_LIST", "values": ["Yes", "No"]},
            {"type": "ONE_OF_RANGE", "source": "Sheet1!Z1:Z10"}, {"type": "BOOLEAN"},
            {"type": "NUMBER_BETWEEN", "values": [0, 100]},
            {"type": "CUSTOM_FORMULA", "values": ["=ISNUMBER(A1)"]}. None => clear.
        strict: Reject invalid input (default True).
        show_dropdown: Show the dropdown chip for list rules (default True).

    Returns:
        ``{ok, spreadsheetId, range, validation, validationRule}``.
    """
    return _call(
        models.SetValidationResult,
        _set_validation,
        _services(ctx),
        spreadsheet_id,
        range,
        rule=rule,
        strict=strict,
        show_dropdown=show_dropdown,
    )


@register(
    annotations=ToolAnnotations(
        title="Read or modify structure",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
    tags={"write"},
)
def sheets_structure(
    spreadsheet_id: str,
    action: str,
    ctx: Context,
    sheet: str | None = None,
    range: str | None = None,
    params: dict | None = None,
) -> models.StructureResult:
    """Read OR modify a spreadsheet's structure: merges, named/protected ranges, frozen
    rows/cols, tab color, row/column groups, native Tables, banding, basic filter & filter
    views, and spreadsheet-level properties (title/locale/timeZone).

    ``action="read"`` returns the full structural picture (``sheet`` optional — omit for every
    tab; ``sheets`` is always a list). Each per-sheet entry also carries ``tables``,
    ``basicFilter``, ``filterViews``, ``bandedRanges``, and ``slicers`` (each serialized into a
    terse round-trippable struct). The mutating actions target ONE tab and REQUIRE ``sheet``
    (EXCEPT ``spreadsheet_props``, which is spreadsheet-scoped and needs NEITHER ``sheet`` nor
    ``range``). Range-scoped writes (merge/add_named/protect/add_table/add_banding/
    set_basic_filter/add_filter_view) also REQUIRE ``range``. Some paths are destructive
    (unmerge, delete_named, unprotect, delete_table, delete_banding, delete_filter_view,
    clear_basic_filter). Each action consumes only its documented ``params`` keys; unknown keys
    are rejected.

    Args:
        spreadsheet_id: The spreadsheet ID, e.g. "<YOUR_SPREADSHEET_ID>".
        action: "read" | "merge" | "unmerge" | "add_named" | "delete_named" | "protect" |
            "unprotect" | "freeze" | "tab_color" | "group" | "ungroup" |
            "add_table" | "update_table" | "delete_table" |
            "add_banding" | "update_banding" | "delete_banding" |
            "set_basic_filter" | "clear_basic_filter" |
            "add_filter_view" | "update_filter_view" | "delete_filter_view" |
            "spreadsheet_props".
        sheet: Tab name (optional for read; REQUIRED for every mutate EXCEPT spreadsheet_props).
        range: An A1 range (for merge/unmerge/add_named/protect and add_table/add_banding/
            set_basic_filter/add_filter_view; also accepted by update_table/update_banding/
            update_filter_view to re-anchor).
        params: Action-specific, e.g. merge {"mergeType": "MERGE_ALL"}; add_named {"name": "x"};
            protect {"description", "editors", "warningOnly"}; freeze {"rows": 1, "cols": 2};
            tab_color {"color": "#4285F4"}; group/ungroup {"dimension": "ROWS", "start", "end"};
            add_table {"name": "Sales", "columns": [{"name", "type", "validation"?}, ...]} (a
            DROPDOWN column needs a "ONE_OF_LIST(...)" validation); update_table {"tableId",
            "name"?, "columns"?, "range"?}; delete_table {"tableId"};
            add_banding {"rowBanding"?: {"header", "first", "second", "footer"}, "columnBanding"?}
            (hex colors); update_banding {"bandedRangeId", "rowBanding"?, "columnBanding"?,
            "range"?}; delete_banding {"bandedRangeId"};
            set_basic_filter {"sorted"?: [{"col": "C", "order": "ASCENDING"}], "criteria"?:
            [{"col": "B", "hidden"?, "condition"?}]}; clear_basic_filter {};
            add_filter_view {"title", "sorted"?, "criteria"?}; update_filter_view {"filterViewId",
            "title"?, "range"?, "sorted"?, "criteria"?}; delete_filter_view {"filterViewId"};
            spreadsheet_props {"title"?, "locale"?, "timeZone"?} (no sheet/range — auto fields
            mask from the keys you set).

    Returns:
        read -> ``{ok, spreadsheetId, namedRanges, sheets:[{sheet, sheetId, merges, frozenRows,
        frozenCols, tabColor, protectedRanges, dimensionGroups, tables, basicFilter, filterViews,
        bandedRanges, slicers}]}``; mutate -> ``{ok, spreadsheetId, action, ...affected
        ids/ranges}`` — create actions surface the new id (``tableId``/``bandedRangeId``/
        ``filterViewId``), spreadsheet_props echoes the updated properties.
    """
    return _call(
        models.StructureResult,
        _structure,
        _services(ctx),
        spreadsheet_id,
        action=action,
        sheet=sheet,
        range=range,
        params=params,
    )


@register(
    annotations=ToolAnnotations(
        title="Manage sheets (tabs)",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
    tags={"write"},
)
def sheets_manage_sheets(
    spreadsheet_id: str,
    action: str,
    ctx: Context,
    sheet: str | None = None,
    params: dict | None = None,
) -> models.ManageSheetsResult:
    """Add, delete, duplicate, rename, or reorder tabs. Returns new sheet ids.

    ``delete`` is destructive (removes the whole tab). ``sheet`` names the target for
    delete/duplicate/rename/reorder. Each action consumes only its documented ``params`` keys;
    unknown keys are rejected.

    Args:
        spreadsheet_id: The spreadsheet ID, e.g. "<YOUR_SPREADSHEET_ID>".
        action: "add" | "delete" | "duplicate" | "rename" | "reorder".
        sheet: Target tab name (for delete/duplicate/rename/reorder).
        params: add {"title", "index", "rows", "cols"} (optional); duplicate {"newName",
            "newIndex"}; rename {"newName"} (required); reorder {"newIndex"} (required).

    Returns:
        ``{ok, spreadsheetId, action, sheet:{sheetId, title, index}}``.
    """
    return _call(
        models.ManageSheetsResult,
        _manage_sheets,
        _services(ctx),
        spreadsheet_id,
        action=action,
        sheet=sheet,
        params=params,
    )


@register(
    annotations=ToolAnnotations(
        title="Developer metadata",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
    tags={"write"},
)
def sheets_metadata(
    spreadsheet_id: str,
    action: str,
    ctx: Context,
    key: str | None = None,
    value: str | None = None,
    location: dict | None = None,
    visibility: str = "DOCUMENT",
    metadata_id: int | None = None,
) -> models.MetadataResult:
    """Read or write developer METADATA — durable anchors for rows/columns/sheets that survive
    inserts and deletes.

    Use this to tag a row/column/sheet with a stable key/value so you can find it again later
    even after the grid shifts (unlike A1 addresses, which move). ``delete`` removes a metadata
    entry. The anchor is given by ``location``: a dimension range, a whole sheet, or the
    spreadsheet.

    Args:
        spreadsheet_id: The spreadsheet ID, e.g. "<YOUR_SPREADSHEET_ID>".
        action: "read" | "create" | "update" | "delete".
        key: Metadata key (for read/create/update).
        value: Metadata value (for create/update).
        location: Anchor — {"sheet": "Sheet1", "dimension": "ROWS", "start": 10, "end": 11},
            or {"sheet": "Sheet1"} (whole sheet), or {} (spreadsheet).
        visibility: "DOCUMENT" (default) | "PROJECT".
        metadata_id: Target metadata id (for update/delete).

    Returns:
        ``{ok, spreadsheetId, action, metadata:[{metadataId, key, value, visibility, location}]}``.
    """
    return _call(
        models.MetadataResult,
        _metadata,
        _services(ctx),
        spreadsheet_id,
        action=action,
        key=key,
        value=value,
        location=location,
        visibility=visibility,
        metadata_id=metadata_id,
    )


@register(
    annotations=ToolAnnotations(
        title="Data operations",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
    tags={"write"},
)
def sheets_data_ops(
    spreadsheet_id: str,
    action: str,
    ctx: Context,
    params: dict | None = None,
) -> models.DataOpsResult:
    """Run a single bulk DATA operation: find/replace, dedupe, trim, sort, split, fill, or paste.

    One typed entry for the high-value ``batchUpdate`` data verbs (so you needn't drop to
    ``sheets_batch``). Some are destructive: ``delete_duplicates`` removes rows, ``cut_paste``
    moves data, and ``find_replace`` rewrites cells in bulk — confirm scope before running. NOT
    idempotent (each call re-applies). Each action consumes only its documented ``params`` keys;
    unknown keys are rejected. All ranges are A1.

    Args:
        spreadsheet_id: The spreadsheet ID, e.g. "<YOUR_SPREADSHEET_ID>".
        action: "find_replace" | "delete_duplicates" | "trim_whitespace" | "sort_range" |
            "text_to_columns" | "auto_fill" | "copy_paste" | "cut_paste".
        params: Action-specific, e.g. find_replace {"find": "old", "replacement": "new",
            "range": "Sheet1!A:A"} (exactly one scope of range/sheet/allSheets);
            delete_duplicates {"range": "Sheet1!A1:F100", "comparisonColumns": ["A", "B"]};
            sort_range {"range": "Sheet1!A2:F100", "specs": [{"col": "B", "order": "ASCENDING"}]};
            copy_paste {"source": "Sheet1!A1:B2", "destination": "Sheet1!D1", "pasteType":
            "PASTE_VALUES"}.

    Returns:
        ``{ok, spreadsheetId, action, ...verb-specific summary...}`` — e.g. find_replace ->
        ``occurrencesChanged``/``valuesChanged``/``formulasChanged``; delete_duplicates ->
        ``duplicatesRemoved``; trim_whitespace -> ``cellsChangedCount``.
    """
    return _call(
        models.DataOpsResult,
        _data_ops,
        _services(ctx),
        spreadsheet_id,
        action=action,
        params=params,
    )


@register(
    annotations=ToolAnnotations(
        title="Row/column dimensions",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
    tags={"write"},
)
def sheets_dimensions(
    spreadsheet_id: str,
    action: str,
    sheet: str,
    ctx: Context,
    params: dict | None = None,
) -> models.DimensionsResult:
    """Insert, delete, move, append, auto-resize, or set props on ROWS/COLUMNS — or read hidden ones.

    Distinct from ``sheets_manage_sheets`` (which is tab-level); this is the row/column dimension
    surface. ``delete`` is destructive (drops rows/cols and their data). ``read`` is a cheap,
    safe query that returns which rows/cols are hidden. Every action targets ONE tab, so ``sheet``
    is required. All start/end indices are 0-based half-open. Each action consumes only its
    documented ``params`` keys; unknown keys are rejected.

    Args:
        spreadsheet_id: The spreadsheet ID, e.g. "<YOUR_SPREADSHEET_ID>".
        action: "insert" | "delete" | "move" | "append" | "auto_resize" | "set_props" | "read".
        sheet: Target tab name (required for every action).
        params: Action-specific, e.g. insert {"dimension": "ROWS", "start": 5, "end": 8,
            "inheritFromBefore": true}; delete {"dimension": "COLUMNS", "start": 2, "end": 3};
            move {"dimension": "ROWS", "start": 10, "end": 12, "destinationIndex": 4};
            append {"dimension": "ROWS", "length": 100}; auto_resize {"dimension": "COLUMNS"}
            (omit start/end for the whole sheet); set_props {"dimension": "ROWS", "start": 0,
            "end": 1, "pixelSize": 40, "hiddenByUser": true}; read {"range": "Sheet1!A1:F100"}.

    Returns:
        write -> ``{ok, spreadsheetId, action, sheet, ...echoed geometry...}``; read ->
        ``{ok, spreadsheetId, action, sheet, hiddenRows:[idx,...], hiddenCols:[idx,...]}``.
    """
    return _call(
        models.DimensionsResult,
        _dimensions,
        _services(ctx),
        spreadsheet_id,
        action=action,
        sheet=sheet,
        params=params,
    )


@register(
    annotations=ToolAnnotations(
        title="Manage charts",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
    tags={"write"},
)
def sheets_charts(
    spreadsheet_id: str,
    action: str,
    ctx: Context,
    sheet: str | None = None,
    chart_id: int | None = None,
    spec: dict | None = None,
) -> models.ChartsResult:
    """Create, update, delete, or list embedded charts (minimal flat spec).

    ``create``/``update`` take a small flattened ``spec``; ``read`` lists chart METADATA only
    (id/title/type/anchor — not the full chart spec; that round-trip is out of v1 scope, use
    ``sheets_batch`` for full fidelity). ``delete`` is destructive.

    Args:
        spreadsheet_id: The spreadsheet ID, e.g. "<YOUR_SPREADSHEET_ID>".
        action: "create" | "update" | "delete" | "read".
        sheet: Tab name (for read filtering / create anchor resolution).
        chart_id: Target chart id (for update/delete).
        spec: {"title", "type": "LINE"|"COLUMN"|"BAR"|"PIE"|"SCATTER"|"AREA",
            "series": ["Sheet1!B1:B100"], "domain": "Sheet1!A1:A100",
            "anchor": {"sheet": "Sheet1", "row": 0, "col": 5}}.

    Returns:
        create -> ``{ok, spreadsheetId, action, chartId}``; read -> ``{ok, ..., charts:[...]}``.
    """
    return _call(
        models.ChartsResult,
        _charts,
        _services(ctx),
        spreadsheet_id,
        action=action,
        sheet=sheet,
        chart_id=chart_id,
        spec=spec,
    )


@register(
    annotations=ToolAnnotations(
        title="Raw batchUpdate (escape hatch)",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
    tags={"write"},
)
def sheets_batch(spreadsheet_id: str, requests: list[dict], ctx: Context) -> models.BatchResult:
    """Power-user ESCAPE HATCH: send a raw, ordered list of ``spreadsheets.batchUpdate``
    requests. Prefer the typed tools above for everything they cover.

    Use this only when a typed tool cannot express the operation (e.g. full chart specs, exotic
    requests). Each item is a raw Sheets API request object. Returns the raw replies plus any
    newly assigned ids captured from them.

    Args:
        spreadsheet_id: The spreadsheet ID, e.g. "<YOUR_SPREADSHEET_ID>".
        requests: Raw batchUpdate requests, e.g. [{"updateSheetProperties": {...}}, ...].

    Returns:
        ``{ok, spreadsheetId, replies:[...], newIds:{sheetIds, chartIds, namedRangeIds}}``.
    """
    return _call(models.BatchResult, _batch, _services(ctx), spreadsheet_id, requests)


# --------------------------------------------------------------------------- entrypoint


def main() -> None:
    """Console-script entrypoint (``google-sheets-mcp``): run the stdio server (DESIGN §7.1)."""
    mcp.run()
