"""argparse CLI adapter (DESIGN §7.2).

The ONLY module importing ``argparse``. One subcommand per core function (flags map 1:1),
plus the adapter-only ``auth login|status`` subcommand (the only place interactive OAuth
consent is allowed). Global ``--json``; a :class:`~gsheets.core.errors.SheetsError` is caught
at the top of :func:`main` and rendered to stderr (or as an ``ok:false`` JSON envelope), exit 1.

THIN over core: every Sheets subcommand resolves ``services`` once, calls the matching core
function with flags mapped 1:1 to its args, and prints the returned dict — either verbatim as
JSON (``--json``) or as a terse readable rendering. No Sheets logic lives here.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, auth, core
from .core.errors import SheetsError

# Render modes / input modes / action enums mirrored from core so argparse can validate up
# front (core re-validates and raises SheetsError — these just give nice argparse errors).
_RENDER_CHOICES = ("plain", "unformatted", "formula", "all")
_INPUT_CHOICES = ("user_entered", "raw")
_CF_ACTIONS = ("add", "update", "delete")
_STRUCTURE_ACTIONS = (
    "read",
    "merge",
    "unmerge",
    "add_named",
    "delete_named",
    "protect",
    "unprotect",
    "freeze",
    "tab_color",
    "group",
    "ungroup",
    # v0.2 extension actions (DESIGN §X.3/§X.4/§X.12): tables / banding / filters CRUD +
    # spreadsheet-property setter. Mirrored here for argparse; core re-validates.
    "add_table",
    "update_table",
    "delete_table",
    "add_banding",
    "update_banding",
    "delete_banding",
    "set_basic_filter",
    "clear_basic_filter",
    "add_filter_view",
    "update_filter_view",
    "delete_filter_view",
    # v0.2 §X.16 — slicer write CRUD (add/update/delete).
    "add_slicer",
    "update_slicer",
    "delete_slicer",
    "spreadsheet_props",
)
_MANAGE_ACTIONS = ("add", "delete", "duplicate", "rename", "reorder")
_METADATA_ACTIONS = ("read", "create", "update", "delete")
_CHARTS_ACTIONS = ("create", "update", "delete", "read")
_SCOPES_CHOICES = ("default", "broad")

# v0.2 extension action enums (DESIGN §Extensions) — mirrored from core for nice argparse
# errors; core re-validates and raises SheetsError.
_DATA_OPS_ACTIONS = (
    "find_replace",
    "delete_duplicates",
    "trim_whitespace",
    "sort_range",
    "text_to_columns",
    "auto_fill",
    "copy_paste",
    "cut_paste",
)
_DIMENSIONS_ACTIONS = (
    "insert",
    "delete",
    "move",
    "append",
    "auto_resize",
    "set_props",
    "read",
)
# v0.2 cross-file + export extensions (DESIGN §3.x / §3.3) — mirrored for nice argparse errors;
# core re-validates and raises SheetsError.
_EXPORT_FORMATS = ("pdf", "xlsx", "ods", "csv", "tsv")
_READ_MANY_MODES = ("values", "summary")
_COMMENTS_ACTIONS = ("read", "create", "reply", "resolve", "delete")


# ===========================================================================================
# JSON-flag helpers (used by argument types so bad JSON fails as an argparse error, not later)
# ===========================================================================================


def _json_arg(raw: str | None) -> object | None:
    """Parse a ``--*-json`` flag value (supports ``@file.json``) or return ``None``.

    Used as the argument ``type`` for JSON-bearing flags so a malformed value surfaces as a
    clean ``argparse`` error (exit 2) rather than blowing up mid-dispatch.
    """
    if raw is None:
        return None
    text = raw
    if raw.startswith("@"):
        path = raw[1:]
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            raise argparse.ArgumentTypeError(f"cannot read {path}: {exc}") from exc
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc}") from exc


# ===========================================================================================
# Parser construction
# ===========================================================================================


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with all subcommands (DESIGN §7.2).

    Registers one subcommand per core function (names are the core fn names with hyphens) plus
    the ``auth`` subcommand, and the global ``--json`` flag. Flags map 1:1 to core args.

    Returns:
        The configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        prog="gsheets",
        description=(
            "Read and write Google Sheets — values + formulas, cell formatting/colors, "
            "conditional-format rules, validation, structure — from the command line."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"gsheets {__version__}",
        help="print the gsheets version and exit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the raw core result dict as JSON (default: terse readable text)",
    )
    parser.add_argument(
        "--scopes",
        choices=_SCOPES_CHOICES,
        default=None,
        help="auth scope mode for this invocation (overrides GSHEETS_SCOPES)",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    _add_overview(sub)
    _add_inspect(sub)
    _add_read_values(sub)
    _add_read_conditional_formats(sub)
    _add_write_values(sub)
    _add_append_rows(sub)
    _add_clear(sub)
    _add_format(sub)
    _add_set_conditional_format(sub)
    _add_set_validation(sub)
    _add_structure(sub)
    _add_manage_sheets(sub)
    _add_metadata(sub)
    _add_charts(sub)
    # v0.2 extensions (DESIGN §Extensions): two NEW data tools + one NEW dimensions tool.
    _add_data_ops(sub)
    _add_dimensions(sub)
    _add_comments(sub)
    # v0.2 cross-file + export extensions (DESIGN §3.x / §3.3): export + multi-spreadsheet read.
    _add_export(sub)
    _add_read_many(sub)
    _add_batch(sub)
    _add_auth(sub)

    return parser


def _spreadsheet_id_arg(p: argparse.ArgumentParser) -> None:
    """Add the universal positional ``spreadsheet_id`` (second core arg on every Sheets fn)."""
    p.add_argument("spreadsheet_id", help="target spreadsheet id (<YOUR_SPREADSHEET_ID>)")


# --------------------------------------------------------------------------- read subcommands


def _add_overview(sub) -> None:
    p = sub.add_parser("overview", help="cheap orientation snapshot (no grid data)")
    _spreadsheet_id_arg(p)
    p.set_defaults(func=_cmd_overview, needs_services=True)


def _add_inspect(sub) -> None:
    p = sub.add_parser(
        "inspect",
        help="flagship rich read: values + formulas + both formats + merges + validation",
    )
    _spreadsheet_id_arg(p)
    p.add_argument("range", help="A1 range, e.g. 'Cliff!A1:D20' or 'Cliff'")
    p.add_argument("--compact", action="store_true", help="collapse identical cells into runs")
    p.add_argument(
        "--no-effective",
        dest="include_effective_format",
        action="store_false",
        help="omit effectiveFormat (what renders)",
    )
    p.add_argument(
        "--no-user-entered",
        dest="include_user_entered_format",
        action="store_false",
        help="omit userEnteredFormat (intent)",
    )
    p.add_argument(
        "--no-formulas",
        dest="include_formulas",
        action="store_false",
        help="omit formulas",
    )
    p.add_argument(
        "--no-validation",
        dest="include_validation",
        action="store_false",
        help="omit data validation",
    )
    # v0.2 extensions (DESIGN §X.1/§X.6): opt-in rich-text runs + in-cell links, and pivot
    # definitions. Off by default → zero added token/mask cost on the base call.
    p.add_argument(
        "--rich-text",
        dest="include_rich_text",
        action="store_true",
        help="include per-run rich text (textFormatRuns) + in-cell hyperlinks (#1/#8)",
    )
    p.add_argument(
        "--pivot",
        dest="include_pivot",
        action="store_true",
        help="include pivot-table definitions on anchor cells (#6)",
    )
    p.set_defaults(func=_cmd_inspect, needs_services=True)


def _add_read_values(sub) -> None:
    p = sub.add_parser("read-values", help="values for one or more A1 ranges, with render mode")
    _spreadsheet_id_arg(p)
    p.add_argument("ranges", nargs="+", help="one or more A1 ranges")
    p.add_argument(
        "--render",
        choices=_RENDER_CHOICES,
        default="plain",
        help="plain | unformatted | formula | all (formula+computed side by side)",
    )
    p.set_defaults(func=_cmd_read_values, needs_services=True)


def _add_read_conditional_formats(sub) -> None:
    p = sub.add_parser(
        "read-conditional-formats",
        help="per-sheet conditional-format rules serialized to readable lines",
    )
    _spreadsheet_id_arg(p)
    p.add_argument("--sheet", default=None, help="restrict to one sheet (default: all sheets)")
    p.set_defaults(func=_cmd_read_conditional_formats, needs_services=True)


# --------------------------------------------------------------------------- write subcommands


def _add_write_values(sub) -> None:
    p = sub.add_parser("write-values", help="write/update one or more ranges (USER_ENTERED)")
    _spreadsheet_id_arg(p)
    p.add_argument("range", nargs="?", default=None, help="single-range form: target A1 range")
    p.add_argument(
        "--values-json",
        type=_json_arg,
        default=None,
        help="single-range form: 2D array '[[...]]' (with the RANGE positional)",
    )
    p.add_argument(
        "--data-json",
        type=_json_arg,
        default=None,
        help='multi-range form: \'[{"range":..,"values":..}]\'',
    )
    p.add_argument("--input", choices=_INPUT_CHOICES, default="user_entered")
    p.set_defaults(func=_cmd_write_values, needs_services=True)


def _add_append_rows(sub) -> None:
    p = sub.add_parser("append-rows", help="append rows after a table (INSERT_ROWS, no overwrite)")
    _spreadsheet_id_arg(p)
    p.add_argument("range", help="A1 range identifying the table")
    p.add_argument(
        "--values-json",
        type=_json_arg,
        required=True,
        help="2D array of rows to append, '[[...],[...]]'",
    )
    p.add_argument("--input", choices=_INPUT_CHOICES, default="user_entered")
    p.set_defaults(func=_cmd_append_rows, needs_services=True)


def _add_clear(sub) -> None:
    p = sub.add_parser("clear", help="clear values (optionally formats/validation/notes)")
    _spreadsheet_id_arg(p)
    p.add_argument("ranges", nargs="+", help="one or more A1 ranges to clear")
    p.add_argument(
        "--no-values",
        dest="values",
        action="store_false",
        help="do NOT clear values (clear only the structural flags below)",
    )
    p.add_argument("--formats", action="store_true", help="also clear cell formatting")
    p.add_argument("--validation", action="store_true", help="also clear data validation")
    p.add_argument("--notes", action="store_true", help="also clear cell notes")
    p.set_defaults(func=_cmd_clear, needs_services=True)


def _add_format(sub) -> None:
    p = sub.add_parser("format", help="apply formatting to a range (atomic, auto fields mask)")
    _spreadsheet_id_arg(p)
    p.add_argument("range", help="A1 range to format")
    p.add_argument("--bg", default=None, help="background hex or theme:NAME (e.g. #FFCDD2)")
    p.add_argument("--fg", default=None, help="foreground/text hex or theme:NAME")
    p.add_argument("--bold", dest="bold", action="store_const", const=True, default=None)
    p.add_argument("--no-bold", dest="bold", action="store_const", const=False)
    p.add_argument("--italic", dest="italic", action="store_const", const=True, default=None)
    p.add_argument("--no-italic", dest="italic", action="store_const", const=False)
    p.add_argument(
        "--underline", dest="underline", action="store_const", const=True, default=None
    )
    p.add_argument("--no-underline", dest="underline", action="store_const", const=False)
    p.add_argument(
        "--strike", dest="strikethrough", action="store_const", const=True, default=None
    )
    p.add_argument("--no-strike", dest="strikethrough", action="store_const", const=False)
    p.add_argument("--font-size", dest="fontSize", type=int, default=None)
    p.add_argument("--font-family", dest="fontFamily", default=None)
    p.add_argument("--number", dest="numberFormat", default=None, help="number/date pattern")
    p.add_argument("--halign", default=None, help="LEFT | CENTER | RIGHT")
    p.add_argument("--valign", default=None, help="TOP | MIDDLE | BOTTOM")
    p.add_argument("--wrap", default=None, help="OVERFLOW_CELL | CLIP | WRAP")
    p.add_argument("--note", default=None, help="cell note text")
    p.add_argument(
        "--border",
        action="append",
        default=None,
        metavar="SIDE=STYLE:#hex",
        help="repeatable, e.g. --border top=SOLID:#000000 (SIDE: top/bottom/left/right)",
    )
    p.add_argument(
        "--fmt-json",
        type=_json_arg,
        default=None,
        help="raw flat CellFormat dict (overrides the individual flags)",
    )
    p.set_defaults(func=_cmd_format, needs_services=True)


def _add_set_conditional_format(sub) -> None:
    p = sub.add_parser(
        "set-conditional-format",
        help="add/update/delete a CF rule by positional index (array order = priority)",
    )
    _spreadsheet_id_arg(p)
    p.add_argument("--action", choices=_CF_ACTIONS, default=None, help="single-form action")
    p.add_argument("--sheet", default=None, help="target tab name")
    p.add_argument("--index", type=int, default=None, help="positional index (0 = highest)")
    p.add_argument(
        "--rule", default=None, help="body LINE, e.g. '[Cliff!A2:A100] if NUMBER_GREATER(0) -> bg #C8E6C9'"
    )
    p.add_argument(
        "--rule-json", type=_json_arg, default=None, help="structured {ranges,kind,condition,format}"
    )
    p.add_argument(
        "--rules-json",
        type=_json_arg,
        default=None,
        help='BATCH form: \'[{"action","index","rule"}]\' (sorted high->low in one batch)',
    )
    p.set_defaults(func=_cmd_set_conditional_format, needs_services=True)


def _add_set_validation(sub) -> None:
    p = sub.add_parser("set-validation", help="set/clear data validation on a range")
    _spreadsheet_id_arg(p)
    p.add_argument("range", help="A1 range to validate")
    p.add_argument(
        "--rule-json",
        type=_json_arg,
        default=None,
        help="structured ValidationRule; omit to CLEAR validation",
    )
    p.add_argument("--no-strict", dest="strict", action="store_false", help="allow invalid input")
    p.add_argument(
        "--no-dropdown", dest="show_dropdown", action="store_false", help="hide the in-cell dropdown"
    )
    p.set_defaults(func=_cmd_set_validation, needs_services=True)


def _add_structure(sub) -> None:
    p = sub.add_parser(
        "structure",
        help="read/modify merges, named/protected ranges, frozen rows/cols, tab color, groups",
    )
    _spreadsheet_id_arg(p)
    p.add_argument("--action", choices=_STRUCTURE_ACTIONS, required=True)
    p.add_argument("--sheet", default=None, help="target tab (optional for read; required to mutate)")
    p.add_argument("--range", dest="range", default=None, help="A1 range for the action")
    p.add_argument(
        "--params-json", type=_json_arg, default=None, help="per-action params dict (see --help / docs)"
    )
    p.set_defaults(func=_cmd_structure, needs_services=True)


def _add_manage_sheets(sub) -> None:
    p = sub.add_parser("manage-sheets", help="add/delete/duplicate/rename/reorder tabs")
    _spreadsheet_id_arg(p)
    p.add_argument("--action", choices=_MANAGE_ACTIONS, required=True)
    p.add_argument("--sheet", default=None, help="target tab name (delete/duplicate/rename/reorder)")
    p.add_argument(
        "--params-json",
        type=_json_arg,
        default=None,
        help='per-action params, e.g. \'{"title":"New","rows":100}\'',
    )
    p.set_defaults(func=_cmd_manage_sheets, needs_services=True)


def _add_metadata(sub) -> None:
    p = sub.add_parser("metadata", help="read/write developer metadata (durable anchors)")
    _spreadsheet_id_arg(p)
    p.add_argument("--action", choices=_METADATA_ACTIONS, required=True)
    p.add_argument("--key", default=None, help="metadata key")
    p.add_argument("--value", default=None, help="metadata value")
    p.add_argument(
        "--location-json",
        type=_json_arg,
        default=None,
        help='anchor, e.g. \'{"sheet":"Cliff","dimension":"ROWS","start":10,"end":11}\'',
    )
    p.add_argument("--visibility", default="DOCUMENT", help="DOCUMENT | PROJECT")
    p.add_argument("--metadata-id", dest="metadata_id", type=int, default=None)
    p.set_defaults(func=_cmd_metadata, needs_services=True)


def _add_charts(sub) -> None:
    p = sub.add_parser("charts", help="create/update/delete/read embedded charts")
    _spreadsheet_id_arg(p)
    p.add_argument("--action", choices=_CHARTS_ACTIONS, required=True)
    p.add_argument("--sheet", default=None, help="target tab (read / anchor)")
    p.add_argument("--chart-id", dest="chart_id", type=int, default=None)
    p.add_argument(
        "--spec-json",
        type=_json_arg,
        default=None,
        help='flat chart spec, e.g. \'{"type":"LINE","series":["Cliff!B1:B100"],"domain":"Cliff!A1:A100"}\'',
    )
    p.set_defaults(func=_cmd_charts, needs_services=True)


def _add_data_ops(sub) -> None:
    """NEW v0.2 tool (DESIGN §X.2/§X.11/§X.14/§X.15): the one-request batchUpdate data verbs."""
    p = sub.add_parser(
        "data-ops",
        help="data verbs: find/replace, dedupe, trim, sort, split, fill, copy/cut-paste",
    )
    _spreadsheet_id_arg(p)
    p.add_argument("--action", choices=_DATA_OPS_ACTIONS, required=True)
    p.add_argument(
        "--params-json",
        type=_json_arg,
        default=None,
        help='per-action params, e.g. \'{"find":"foo","replacement":"bar","allSheets":true}\'',
    )
    p.set_defaults(func=_cmd_data_ops, needs_services=True)


def _add_dimensions(sub) -> None:
    """NEW v0.2 tool (DESIGN §X.7/§X.10/§X.13): row/column dimension ops + hidden read."""
    p = sub.add_parser(
        "dimensions",
        help="row/col ops: insert/delete/move/append/auto_resize/set_props, or read hidden",
    )
    _spreadsheet_id_arg(p)
    p.add_argument("--action", choices=_DIMENSIONS_ACTIONS, required=True)
    p.add_argument("--sheet", default=None, help="target tab name (REQUIRED for every action)")
    p.add_argument(
        "--params-json",
        type=_json_arg,
        default=None,
        help='per-action params, e.g. \'{"dimension":"ROWS","start":10,"end":12}\'',
    )
    p.set_defaults(func=_cmd_dimensions, needs_services=True)


def _add_comments(sub) -> None:
    """NEW v0.2 tool (DESIGN §X.5): read/write Drive threaded comments (full CRUD).

    ``--action`` dispatches read (default) / create / reply / resolve / delete. ``delete`` is
    DESTRUCTIVE and requires ``--confirm``. ``--no-resolved``/``--include-deleted`` apply to read.
    """
    p = sub.add_parser(
        "comments",
        help="read/write Drive threaded comments (read/create/reply/resolve/delete)",
    )
    _spreadsheet_id_arg(p)
    p.add_argument(
        "--action",
        choices=_COMMENTS_ACTIONS,
        default="read",
        help="read (default) | create | reply | resolve | delete",
    )
    p.add_argument(
        "--comment-id",
        dest="comment_id",
        default=None,
        help="target comment id (required for reply/resolve/delete)",
    )
    p.add_argument(
        "--content",
        default=None,
        help="comment/reply body (required for create/reply; optional for resolve)",
    )
    p.add_argument(
        "--anchor",
        default=None,
        help="opaque Drive anchor for create (pass-through; never an A1 range)",
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help="required to actually run the DESTRUCTIVE delete action",
    )
    p.add_argument(
        "--no-resolved",
        dest="include_resolved",
        action="store_false",
        help="read: omit resolved comments (default: include them)",
    )
    p.add_argument(
        "--include-deleted",
        dest="include_deleted",
        action="store_true",
        help="read: include deleted comments (Drive includeDeleted)",
    )
    p.set_defaults(func=_cmd_comments, needs_services=True)


def _add_export(sub) -> None:
    """NEW v0.2 tool (DESIGN §3.x): download a spreadsheet to a local file."""
    p = sub.add_parser(
        "export",
        help="download a spreadsheet to a local file (pdf/xlsx/ods whole-book; csv/tsv per-sheet)",
    )
    _spreadsheet_id_arg(p)
    p.add_argument(
        "--format",
        choices=_EXPORT_FORMATS,
        default="pdf",
        help="pdf (default) | xlsx | ods (whole-workbook, Drive scope) | csv | tsv (one sheet)",
    )
    p.add_argument(
        "--path",
        default=None,
        help="output path (defaults to <spreadsheetId>.<format> in the cwd)",
    )
    p.add_argument(
        "--sheet",
        default=None,
        help="sheet to export — REQUIRED for csv/tsv, IGNORED for pdf/xlsx/ods",
    )
    p.set_defaults(func=_cmd_export, needs_services=True)


def _add_read_many(sub) -> None:
    """NEW v0.2 tool (DESIGN §3.3): cross-file values/summary read fan-out."""
    p = sub.add_parser(
        "read-many",
        help="read values or summaries across many spreadsheets (per-file error capture)",
    )
    # NOTE: no spreadsheet_id positional — the ids live inside --requests-json (one per request).
    p.add_argument(
        "--requests-json",
        type=_json_arg,
        required=True,
        help='requests[] \'[{"spreadsheetId":..,"ranges":[..],"render"?:..}]\' (or @file.json)',
    )
    p.add_argument(
        "--mode",
        choices=_READ_MANY_MODES,
        default="values",
        help="values (default; per-request ranges) | summary (cheap orientation, no ranges)",
    )
    p.set_defaults(func=_cmd_read_many, needs_services=True)


def _add_batch(sub) -> None:
    p = sub.add_parser("batch", help="power-user escape hatch: raw batchUpdate requests")
    _spreadsheet_id_arg(p)
    p.add_argument(
        "--requests-json",
        type=_json_arg,
        required=True,
        help="raw ordered requests[] (or @file.json)",
    )
    p.set_defaults(func=_cmd_batch, needs_services=True)


def _add_auth(sub) -> None:
    """The one adapter-only subcommand — touches the auth layer, never core Sheets (DESIGN §7.2)."""
    p = sub.add_parser("auth", help="bootstrap/inspect credentials (login | status)")
    auth_sub = p.add_subparsers(dest="auth_command", metavar="<login|status>")
    auth_sub.required = True

    login = auth_sub.add_parser(
        "login", help="run OAuth consent (or refresh) and persist token.json"
    )
    login.set_defaults(func=_cmd_auth_login, needs_services=False)

    st = auth_sub.add_parser(
        "status", help="report resolved auth mode/scopes/token state (no Sheets call)"
    )
    st.set_defaults(func=_cmd_auth_status, needs_services=False)

    # ``auth`` with no sub falls through to argparse's required-subparser error.
    p.set_defaults(func=None, needs_services=False)


# ===========================================================================================
# Subcommand bodies — each is THIN: build payload from flags, call core, return its dict.
# (services is None for the auth subcommands, which call the auth layer directly.)
# ===========================================================================================


def _cmd_overview(services, args) -> dict:
    return core.overview(services, args.spreadsheet_id)


def _cmd_inspect(services, args) -> dict:
    return core.inspect(
        services,
        args.spreadsheet_id,
        args.range,
        compact=args.compact,
        include_effective_format=args.include_effective_format,
        include_user_entered_format=args.include_user_entered_format,
        include_formulas=args.include_formulas,
        include_validation=args.include_validation,
        include_rich_text=args.include_rich_text,
        include_pivot=args.include_pivot,
    )


def _cmd_read_values(services, args) -> dict:
    return core.read_values(services, args.spreadsheet_id, args.ranges, render=args.render)


def _cmd_read_conditional_formats(services, args) -> dict:
    return core.read_conditional_formats(services, args.spreadsheet_id, args.sheet)


def _cmd_write_values(services, args) -> dict:
    if args.data_json is not None:
        if args.range is not None or args.values_json is not None:
            raise SheetsError(
                "conflicting_args",
                "pass EITHER --data-json OR the RANGE positional + --values-json, not both",
            )
        data = args.data_json
    else:
        if args.range is None or args.values_json is None:
            raise SheetsError(
                "missing_args",
                "single-range form requires the RANGE positional and --values-json "
                "(or use --data-json for the multi-range form)",
            )
        data = [{"range": args.range, "values": args.values_json}]
    return core.write_values(services, args.spreadsheet_id, data, input=args.input)


def _cmd_append_rows(services, args) -> dict:
    return core.append_rows(
        services, args.spreadsheet_id, args.range, args.values_json, input=args.input
    )


def _cmd_clear(services, args) -> dict:
    return core.clear(
        services,
        args.spreadsheet_id,
        args.ranges,
        values=args.values,
        formats=args.formats,
        validation=args.validation,
        notes=args.notes,
    )


# Flat scalar format flags whose argparse ``dest`` matches the flat CellFormat key 1:1.
_FORMAT_SCALAR_DESTS = (
    "bg",
    "fg",
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "fontSize",
    "fontFamily",
    "numberFormat",
    "halign",
    "valign",
    "wrap",
    "note",
)


def _parse_border_flags(borders: list[str] | None) -> dict | None:
    """Parse repeatable ``--border SIDE=STYLE:#hex`` flags into the flat ``borders`` dict.

    e.g. ``["top=SOLID:#000000"]`` -> ``{"top": "SOLID #000000"}`` (the flat CellFormat border
    shape core's ``format`` consumes). Raises :class:`SheetsError` on a malformed token.
    """
    if not borders:
        return None
    out: dict[str, str] = {}
    for raw in borders:
        if "=" not in raw:
            raise SheetsError("bad_border", f"--border must be SIDE=STYLE:#hex, got {raw!r}")
        side, spec = raw.split("=", 1)
        side = side.strip().lower()
        if ":" not in spec:
            raise SheetsError("bad_border", f"--border spec must be STYLE:#hex, got {spec!r}")
        style, hexcolor = spec.split(":", 1)
        out[side] = f"{style.strip().upper()} {hexcolor.strip()}"
    return out


def _cmd_format(services, args) -> dict:
    if args.fmt_json is not None:
        fmt = args.fmt_json
    else:
        fmt = {}
        for dest in _FORMAT_SCALAR_DESTS:
            val = getattr(args, dest, None)
            if val is not None:
                fmt[dest] = val
        borders = _parse_border_flags(args.border)
        if borders is not None:
            fmt["borders"] = borders
    return core.format(services, args.spreadsheet_id, args.range, fmt)


def _cmd_set_conditional_format(services, args) -> dict:
    if args.rule is not None and args.rule_json is not None:
        raise SheetsError(
            "conflicting_args", "pass EITHER --rule (body line) OR --rule-json, not both"
        )
    rule = args.rule if args.rule is not None else args.rule_json
    return core.set_conditional_format(
        services,
        args.spreadsheet_id,
        action=args.action,
        sheet=args.sheet,
        index=args.index,
        rule=rule,
        rules=args.rules_json,
    )


def _cmd_set_validation(services, args) -> dict:
    return core.set_validation(
        services,
        args.spreadsheet_id,
        args.range,
        rule=args.rule_json,
        strict=args.strict,
        show_dropdown=args.show_dropdown,
    )


def _cmd_structure(services, args) -> dict:
    return core.structure(
        services,
        args.spreadsheet_id,
        action=args.action,
        sheet=args.sheet,
        range=args.range,
        params=args.params_json,
    )


def _cmd_manage_sheets(services, args) -> dict:
    return core.manage_sheets(
        services,
        args.spreadsheet_id,
        action=args.action,
        sheet=args.sheet,
        params=args.params_json,
    )


def _cmd_metadata(services, args) -> dict:
    return core.metadata(
        services,
        args.spreadsheet_id,
        action=args.action,
        key=args.key,
        value=args.value,
        location=args.location_json,
        visibility=args.visibility,
        metadata_id=args.metadata_id,
    )


def _cmd_charts(services, args) -> dict:
    return core.charts(
        services,
        args.spreadsheet_id,
        action=args.action,
        sheet=args.sheet,
        chart_id=args.chart_id,
        spec=args.spec_json,
    )


def _cmd_data_ops(services, args) -> dict:
    return core.data_ops(
        services,
        args.spreadsheet_id,
        action=args.action,
        params=args.params_json,
    )


def _cmd_dimensions(services, args) -> dict:
    return core.dimensions(
        services,
        args.spreadsheet_id,
        action=args.action,
        sheet=args.sheet,
        params=args.params_json,
    )


def _cmd_comments(services, args) -> dict:
    # delete is DESTRUCTIVE — require --confirm so a stray `comments ID --action delete
    # --comment-id X` cannot silently remove a comment.
    if args.action == "delete" and not args.confirm:
        raise SheetsError(
            "confirmation_required",
            "comments --action delete is destructive; re-run with --confirm",
        )
    return core.comments(
        services,
        args.spreadsheet_id,
        action=args.action,
        comment_id=args.comment_id,
        content=args.content,
        anchor=args.anchor,
        include_resolved=args.include_resolved,
        include_deleted=args.include_deleted,
    )


def _cmd_export(services, args) -> dict:
    return core.export(
        services,
        args.spreadsheet_id,
        format=args.format,
        path=args.path,
        sheet=args.sheet,
    )


def _cmd_read_many(services, args) -> dict:
    # read-many has NO spreadsheet_id positional — the ids live inside --requests-json. core.
    # read_many takes (services, requests, *, mode).
    return core.read_many(services, args.requests_json, mode=args.mode)


def _cmd_batch(services, args) -> dict:
    return core.batch(services, args.spreadsheet_id, args.requests_json)


def _cmd_auth_login(services, args) -> dict:  # services is None (auth-only path)
    return auth.bootstrap(scopes_mode=args.scopes)


def _cmd_auth_status(services, args) -> dict:  # services is None (auth-only path)
    return auth.status(scopes_mode=args.scopes)


# ===========================================================================================
# Terse text rendering (default; --json prints the raw dict)
# ===========================================================================================


def _render_text(result: dict) -> str:
    """Render a core result dict as terse, readable lines for the default (non-JSON) output.

    A best-effort human view: known rich shapes get a purpose-built rendering; anything else
    falls back to compact key/value lines. ``--json`` always remains available for the exact
    machine shape, so this never needs to be lossless.
    """
    lines: list[str] = []

    # overview
    if "title" in result and "sheets" in result and "namedRanges" in result:
        lines.append(f"{result.get('title', '')}  [{result.get('spreadsheetId', '')}]")
        for s in result.get("sheets", []):
            extras = []
            if s.get("frozenRows"):
                extras.append(f"frozenRows={s['frozenRows']}")
            if s.get("frozenCols"):
                extras.append(f"frozenCols={s['frozenCols']}")
            if s.get("protectedRangeCount"):
                extras.append(f"protected={s['protectedRangeCount']}")
            if s.get("conditionalFormatCount"):
                extras.append(f"cf={s['conditionalFormatCount']}")
            if s.get("tabColor"):
                extras.append(f"tab={s['tabColor']}")
            suffix = ("  " + " ".join(extras)) if extras else ""
            lines.append(
                f"  [{s.get('index')}] {s.get('title')}  "
                f"{s.get('rows')}x{s.get('cols')} (id={s.get('sheetId')}){suffix}"
            )
        for nr in result.get("namedRanges", []):
            lines.append(f"  named: {nr.get('name')} -> {nr.get('range')}")
        return "\n".join(lines)

    # comments (DESIGN §X.5): a top-level Drive-comments list. Each entry carries a terse
    # ``line`` from serialize_comment; fall back to a constructed line if absent.
    if "comments" in result and "sheets" not in result and "cells" not in result:
        comments = result.get("comments", [])
        if not comments:
            return "(no comments)"
        for c in comments:
            lines.append(_render_comment(c))
        return "\n".join(lines)

    # read_conditional_formats / structure(read) multi-sheet envelope
    if "sheets" in result and isinstance(result.get("sheets"), list) and "title" not in result:
        if result.get("namedRanges"):
            for nr in result["namedRanges"]:
                lines.append(f"named: {nr.get('name')} -> {nr.get('range')}")
        for s in result["sheets"]:
            lines.append(f"# {s.get('sheet')} (id={s.get('sheetId')})")
            for r in s.get("rules", []):
                lines.append(f"  [{r.get('index')}] {r.get('line')}")
            for m in s.get("merges", []):
                lines.append(f"  merge: {m}")
            if s.get("frozenRows"):
                lines.append(f"  frozenRows: {s['frozenRows']}")
            if s.get("frozenCols"):
                lines.append(f"  frozenCols: {s['frozenCols']}")
            if s.get("tabColor"):
                lines.append(f"  tabColor: {s['tabColor']}")
            for pr in s.get("protectedRanges", []):
                lines.append(
                    f"  protected: {pr.get('range')} ({pr.get('description') or 'no desc'})"
                )
            for g in s.get("dimensionGroups", []):
                lines.append(
                    f"  group: {g.get('dimension')} {g.get('start')}-{g.get('end')} "
                    f"depth={g.get('depth')}"
                )
        return "\n".join(lines) if lines else "(no structural data)"

    # inspect
    if "cells" in result or "runs" in result:
        header = f"{result.get('sheet')}!{result.get('range')}  " if result.get("sheet") else ""
        lines.append(f"{header}{result.get('rows')}x{result.get('cols')}")
        for m in result.get("merges", []):
            lines.append(f"merge: {m}")
        if "runs" in result:
            for run in result["runs"]:
                lines.append(_render_inspect_run(run))
        else:
            for cell in result.get("cells", []):
                lines.append(_render_inspect_cell(cell))
        return "\n".join(lines)

    # read_values
    if "ranges" in result and "render" in result:
        lines.append(f"render={result.get('render')}")
        for rng in result["ranges"]:
            lines.append(f"# {rng.get('range')}")
            values = rng.get("values", [])
            computed = rng.get("computed")
            for i, row in enumerate(values):
                if computed is not None and i < len(computed):
                    pairs = [
                        f"{v} => {c}" for v, c in zip(row, computed[i])
                    ]
                    lines.append("  " + " | ".join(pairs))
                else:
                    lines.append("  " + " | ".join("" if c is None else str(c) for c in row))
        return "\n".join(lines)

    # dimensions(action="read") (DESIGN §X.7): hidden row/col index lists.
    if "hiddenRows" in result or "hiddenCols" in result:
        sheet = result.get("sheet")
        lines.append(f"hidden ({sheet}):" if sheet else "hidden:")
        hidden_rows = result.get("hiddenRows") or []
        hidden_cols = result.get("hiddenCols") or []
        lines.append(f"  rows: {hidden_rows if hidden_rows else '(none)'}")
        lines.append(f"  cols: {hidden_cols if hidden_cols else '(none)'}")
        return "\n".join(lines)

    # export (DESIGN §3.x): a terse "exported <format> -> <path> (<bytes> bytes)" line.
    if "format" in result and "path" in result and "bytes" in result:
        return (
            f"exported {result.get('format')} -> {result.get('path')} "
            f"({result.get('bytes')} bytes)"
        )

    # read_many (DESIGN §3.3): the cross-file fan-out envelope (mode/count/results).
    if "results" in result and "mode" in result and "count" in result:
        return _render_read_many(result)

    # data_ops / dimensions write summary (DESIGN §X.2/§X.7): a terse one-line action summary.
    if "action" in result and ("sheets" not in result and "cells" not in result):
        summary = _render_action_summary(result)
        if summary is not None:
            return summary

    # Generic fallback: compact key/value lines (skip the always-present ok/spreadsheetId).
    return _render_generic(result)


def _render_inspect_cell(cell: dict) -> str:
    parts = [cell.get("a1", "?")]
    if cell.get("formula"):
        parts.append(f"{cell['formula']} => {cell.get('value', '')}")
    elif cell.get("value") not in (None, ""):
        parts.append(str(cell["value"]))
    if cell.get("validation"):
        parts.append(f"[{cell['validation']}]")
    if cell.get("note"):
        parts.append(f"note={cell['note']!r}")
    # v0.2 (DESIGN §X.1/§X.6): rich-text runs / in-cell hyperlink / pivot, present only when set.
    parts.extend(_inspect_rich_parts(cell))
    return "  " + "  ".join(parts)


def _render_inspect_run(run: dict) -> str:
    parts = [run.get("a1Range", "?")]
    if run.get("formula"):
        parts.append(f"{run['formula']} => {run.get('value', '')}")
    elif run.get("value") not in (None, ""):
        parts.append(str(run["value"]))
    if run.get("validationRule"):
        parts.append("[validation]")
    if run.get("note"):
        parts.append(f"note={run['note']!r}")
    parts.extend(_inspect_rich_parts(run))
    return "  " + "  ".join(parts)


def _inspect_rich_parts(cell: dict) -> list[str]:
    """Terse fragments for the v0.2 per-cell rich shapes (runs/hyperlink/pivot).

    Each is emitted ONLY when the cell carries it (mirroring core's per-cell-only attachment),
    so cells without rich data render exactly as before. ``runs`` summarizes the styled segments,
    ``hyperlink`` shows the in-cell link, and ``pivot`` surfaces the serializer's terse ``line``.
    """
    parts: list[str] = []
    runs = cell.get("runs")
    if runs:
        seg = " + ".join(
            f"{r.get('text', '')!r}[{r.get('start', 0)}{' link ' + r['link'] if r.get('link') else ''}]"
            for r in runs
        )
        parts.append(f"runs: {seg}")
    if cell.get("hyperlink"):
        parts.append(f"link={cell['hyperlink']}")
    pivot = cell.get("pivot")
    if pivot:
        parts.append(pivot.get("line") or f"pivot <- {pivot.get('source', '?')}")
    return parts


def _render_read_many(result: dict) -> str:
    """Render a cross-file ``read_many`` envelope (DESIGN §3.3): one line per result entry.

    Each entry is EITHER a captured failure (``ok:False`` -> ``ERROR <code>: <message>``) or a
    success (a summary entry -> ``<title> (<n> sheet(s))``; a values entry -> ``<n> row(s) across
    <k> range(s)``). The top-level ``ok:true`` does NOT imply every file succeeded, so failures
    are surfaced inline.
    """
    mode = result.get("mode")
    count = result.get("count")
    lines = [f"read-many mode={mode}: {count} result(s)"]
    for entry in result.get("results", []):
        sid = entry.get("spreadsheetId", "?")
        if entry.get("ok") is False:
            err = entry.get("error") or {}
            lines.append(f"  {sid}: ERROR {err.get('code', '?')}: {err.get('message', '')}")
        elif "ranges" in entry:
            ranges = entry.get("ranges", [])
            rows = sum(len(r.get("values") or []) for r in ranges)
            lines.append(f"  {sid}: {rows} row(s) across {len(ranges)} range(s)")
        else:
            title = entry.get("title", "")
            nsheets = len(entry.get("sheets", []))
            lines.append(f"  {sid}: {title or '(untitled)'} ({nsheets} sheet(s))")
    return "\n".join(lines)


def _render_comment(comment: dict) -> str:
    """Render one serialized Drive comment as a terse line (reuses the serializer's ``line``)."""
    line = comment.get("line")
    if line:
        return line
    # Defensive fallback if a serialized comment lacks the precomputed line.
    author = comment.get("author", "unknown")
    content = comment.get("content", "")
    state = "resolved" if comment.get("resolved") else "open"
    cid = comment.get("id", "?")
    return f'comment {cid} by {author}: "{content}" ({state})'


# Per-action summary keys to surface for the data_ops / dimensions write returns (DESIGN
# §X.2/§X.7). Anything not listed falls through to the generic key/value rendering.
_ACTION_SUMMARY_KEYS = (
    "occurrencesChanged",
    "valuesChanged",
    "formulasChanged",
    "rowsChanged",
    "sheetsChanged",
    "duplicatesRemoved",
    "cellsChangedCount",
    "range",
    "source",
    "destination",
    "sheet",
    "dimension",
    "start",
    "end",
    "destinationIndex",
    "length",
    "pixelSize",
    "hiddenByUser",
    "addedRows",
    "addedColumns",
)


def _render_action_summary(result: dict) -> str | None:
    """Render a data_ops / dimensions write result as ``<action>: k=v k=v`` (or None)."""
    action = result.get("action")
    pairs = [
        f"{key}={result[key]}"
        for key in _ACTION_SUMMARY_KEYS
        if key in result
    ]
    if not pairs:
        return None
    return f"{action}: " + " ".join(pairs)


def _render_generic(result: dict) -> str:
    lines: list[str] = []
    for key, val in result.items():
        if key in ("ok", "spreadsheetId"):
            continue
        if isinstance(val, (dict, list)):
            lines.append(f"{key}: {json.dumps(val, ensure_ascii=False)}")
        else:
            lines.append(f"{key}: {val}")
    return "\n".join(lines) if lines else "ok"


# ===========================================================================================
# main()
# ===========================================================================================


def _emit(result: dict, *, as_json: bool, stream=None) -> None:
    """Print a successful core result — verbatim JSON (``--json``) or terse text."""
    out = stream if stream is not None else sys.stdout
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2), file=out)
    else:
        print(_render_text(result), file=out)


def _emit_error(err: SheetsError, *, as_json: bool) -> None:
    """Render a :class:`SheetsError` to stderr — ``ok:false`` JSON envelope or terse line."""
    if as_json:
        payload = {"ok": False, "error": err.to_dict()}
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
    else:
        msg = f"gsheets: error: {err.code}: {err.message}"
        if err.hint:
            msg += f"\n  hint: {err.hint}"
        print(msg, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Console-script entrypoint (``gsheets``): parse, dispatch, render (DESIGN §7.2).

    Parses ``argv``; for Sheets subcommands calls :func:`gsheets.auth.build_services`,
    dispatches to the matching core function, and prints terse text or ``--json``. A
    :class:`SheetsError` is caught here and rendered to stderr (or as the ``ok:false`` JSON
    envelope under ``--json``), returning exit code 1.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (0 on success, 1 on a handled error).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # ``auth`` with no sub-subcommand (defensive — the subparser is marked required).
    func = getattr(args, "func", None)
    if func is None:
        parser.error("a subcommand is required")
        return 2  # pragma: no cover - parser.error raises SystemExit

    try:
        if getattr(args, "needs_services", False):
            services = auth.build_services(scopes_mode=args.scopes)
            result = func(services, args)
        else:
            # auth subcommands: no Sheets handle; they call the auth layer directly.
            result = func(None, args)
    except SheetsError as err:
        _emit_error(err, as_json=args.json)
        return 1

    # ``auth status`` returns ok:False (not a raise) when no creds resolve -> non-zero exit.
    if isinstance(result, dict) and result.get("ok") is False:
        _emit(result, as_json=args.json)
        return 1

    _emit(result, as_json=args.json)
    return 0
