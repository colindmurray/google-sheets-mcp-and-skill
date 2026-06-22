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
import dataclasses
import json
import sys

from . import __version__, auth, core
from .core import addressing as addressing_mod
from .core import retry as retry_mod
from .core.errors import SheetsError, to_sheets_error
from .core.format import render as render_format
from .core.format import render_sparse_values
from .core.richtext import text_runs_line

# Global --format choices. ``text`` (the default) is the existing terse renderer; the data
# formats are serialized by the shared core layer (``gsheets.core.format.render``). ``--json``
# stays a permanent documented alias for ``--format json`` (SPEC §1.3, D-FMTFLAG).
_FORMAT_CHOICES = ("text", "json", "jsonl", "csv", "tsv", "markdown")

# Render modes / input modes / action enums mirrored from core so argparse can validate up
# front (core re-validates and raises SheetsError — these just give nice argparse errors).
_RENDER_CHOICES = ("plain", "unformatted", "formula", "all")
_MAJOR_CHOICES = ("rows", "columns")
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

# Global retry/backoff strategy choices (ISSUES.md #25). The hyphenated ``exponential-jitter`` is
# the user-facing spelling; it maps to the underscore ``exponential_jitter`` core strategy name in
# :func:`_resolve_retry_policy`. Retry is OFF by default (v0.4.0 breaking default change).
_BACKOFF_CHOICES = ("none", "fixed", "exponential", "exponential-jitter")

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
        "--format",
        dest="output_format",
        choices=_FORMAT_CHOICES,
        default=None,
        help="output format: text (default, terse) | json | jsonl | csv | tsv | markdown "
        "(csv/tsv need a rectangular value read, e.g. read-values; markdown renders a table for "
        "a value grid, key/value lines for a structured read)",
    )
    parser.add_argument(
        "--json",
        dest="json",
        action="store_true",
        help="alias for --format json (emit the raw core result dict as pretty JSON)",
    )
    parser.add_argument(
        "--scopes",
        choices=_SCOPES_CHOICES,
        default=None,
        help="auth scope mode for this invocation (overrides GSHEETS_SCOPES)",
    )

    # --------------------------------------------------------------- retry / backoff (ISSUES.md #25)
    #
    # Global flags resolved ONCE in main() into a core.retry.RetryPolicy, then activated around the
    # whole build_services + dispatch + render block so the auth-layer request builder reads it at
    # .execute() time. Retry is OFF BY DEFAULT (v0.4.0): a 429/5xx fails fast unless the caller opts
    # in. Three shapes, mutually exclusive: the one-shot preset (--default-backoff-strategy), explicit
    # fail-fast (--no-retry), or granular control (--retries / --backoff / --retry-*). The actual
    # mutual-exclusion validation lives in _resolve_retry_policy (a SheetsError, not an argparse one,
    # so it renders as the same ok:false envelope every other handled failure produces).
    retry_group = parser.add_argument_group(
        "retry/backoff",
        "Retry on transient 429/5xx errors. OFF BY DEFAULT — a 429/5xx fails fast unless you opt "
        "in. Use --default-backoff-strategy for the sensible preset, or set granular flags; the "
        "preset and granular flags are mutually exclusive, and --no-retry forces fail-fast.",
    )
    retry_group.add_argument(
        "--default-backoff-strategy",
        dest="default_backoff_strategy",
        action="store_true",
        help="enable the sensible preset (full-jitter exponential backoff, 4 retries, 60s "
        "deadline); mutually exclusive with --no-retry and the granular flags",
    )
    retry_group.add_argument(
        "--no-retry",
        dest="no_retry",
        action="store_true",
        help="force retry OFF (explicit fail-fast; overrides any GSHEETS_BACKOFF_* env)",
    )
    retry_group.add_argument(
        "--retries",
        dest="retries",
        type=int,
        default=None,
        metavar="N",
        help="granular: number of retries AFTER the first try (total tries = 1 + N)",
    )
    retry_group.add_argument(
        "--backoff",
        dest="backoff",
        choices=_BACKOFF_CHOICES,
        default=None,
        help="granular: backoff strategy (default preset uses exponential-jitter)",
    )
    retry_group.add_argument(
        "--retry-base-delay",
        dest="retry_base_delay",
        type=float,
        default=None,
        metavar="S",
        help="granular: base seconds for the per-attempt delay",
    )
    retry_group.add_argument(
        "--retry-max-delay",
        dest="retry_max_delay",
        type=float,
        default=None,
        metavar="S",
        help="granular: per-attempt sleep cap (seconds)",
    )
    retry_group.add_argument(
        "--retry-deadline",
        dest="retry_deadline",
        type=float,
        default=None,
        metavar="S",
        help="granular: overall wall-clock cap across all sleeps (<= 0 means no cap)",
    )
    retry_group.add_argument(
        "--retry-after-cap",
        dest="retry_after_cap",
        type=float,
        default=None,
        metavar="S",
        help="granular: cap (seconds) applied to a server Retry-After header value",
    )
    retry_group.add_argument(
        "--honor-retry-after",
        dest="honor_retry_after",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="granular: honor a server Retry-After header (use --no-honor-retry-after to ignore it)",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    _add_overview(sub)
    _add_inspect(sub)
    _add_describe(sub)
    _add_formula_patterns(sub)
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
        help="include per-run rich text (textFormatRuns) + in-cell hyperlinks",
    )
    p.add_argument(
        "--pivot",
        dest="include_pivot",
        action="store_true",
        help="include pivot-table definitions on anchor cells",
    )
    p.set_defaults(func=_cmd_inspect, needs_services=True)


def _add_describe(sub) -> None:
    """NEW v0.3 tool (SPEC §3): the "understand a region" verb — one-call merged region view."""
    p = sub.add_parser(
        "describe",
        help="one-call merged region view: cells + structure + range-scoped CF, per range",
    )
    _spreadsheet_id_arg(p)
    # ``ranges`` is OPTIONAL (nargs="*") because --data-filter-json is an alternative addressing
    # path (SPEC §6 P2); core enforces "exactly one of ranges / data_filters".
    p.add_argument(
        "ranges",
        nargs="*",
        help="one or more A1 ranges (multi-sheet allowed; omit when using --data-filter-json)",
    )
    p.add_argument(
        "--data-filter-json",
        type=_json_arg,
        default=None,
        help="symbolic addressing INSTEAD of positional ranges: a JSON list of selectors, "
        'e.g. \'[{"a1":"Sheet1!A1:B10"},{"developerMetadataLookup":{"metadataKey":"block:totals"}}]\' '
        "(or @file.json). Mutually exclusive with the ranges positional.",
    )
    p.add_argument(
        "--max-cells",
        type=int,
        default=None,
        help="fail with result_too_large if the regions exceed this many cells "
        "(default: unlimited)",
    )
    p.set_defaults(func=_cmd_describe, needs_services=True)


def _add_formula_patterns(sub) -> None:
    """NEW v0.3 tool (SPEC §4): collapse a column's repeated formulas to distinct templates."""
    p = sub.add_parser(
        "formula-patterns",
        help="collapse repeated formulas per column to distinct {r}-normalized templates",
    )
    _spreadsheet_id_arg(p)
    p.add_argument("ranges", nargs="+", help="one or more A1 ranges (multi-sheet allowed)")
    p.add_argument(
        "--no-sample",
        dest="sample",
        action="store_false",
        help="skip the sample computed value (no second FORMATTED pass)",
    )
    p.set_defaults(func=_cmd_formula_patterns, needs_services=True)


def _add_read_values(sub) -> None:
    p = sub.add_parser("read-values", help="values for one or more A1 ranges, with render mode")
    _spreadsheet_id_arg(p)
    # ``ranges`` is OPTIONAL (nargs="*") because --data-filter-json is an alternative addressing
    # path (SPEC §6 P2); core enforces "exactly one of ranges / data_filters".
    p.add_argument(
        "ranges",
        nargs="*",
        help="one or more A1 ranges (multi-sheet allowed; omit when using --data-filter-json)",
    )
    p.add_argument(
        "--render",
        choices=_RENDER_CHOICES,
        default="plain",
        help="plain | unformatted | formula | all (formula+computed side by side)",
    )
    p.add_argument(
        "--major",
        choices=_MAJOR_CHOICES,
        default="rows",
        help="rows (default) | columns — Google majorDimension; columns suits uniform "
        "helper columns (each inner list is one column)",
    )
    p.add_argument(
        "--data-filter-json",
        type=_json_arg,
        default=None,
        help="symbolic addressing INSTEAD of positional ranges: a JSON list of selectors, "
        'e.g. \'[{"a1":"Sheet1!A1:B10"},{"developerMetadataLookup":{"metadataKey":"block:totals"}}]\' '
        "(or @file.json). Mutually exclusive with the ranges positional.",
    )
    p.add_argument(
        "--diff-only",
        action="store_true",
        help="render=all only: null out computed cells equal to values (drops a static "
        "sheet's duplicate computed matrix; a null hole means computed==values)",
    )
    p.add_argument(
        "--max-cells",
        type=int,
        default=None,
        help="fail with result_too_large if the read exceeds this many cells "
        "(default: unlimited; prefer 'export' csv/tsv for bulk value dumps)",
    )
    p.set_defaults(func=_cmd_read_values, needs_services=True)


def _add_read_conditional_formats(sub) -> None:
    p = sub.add_parser(
        "read-conditional-formats",
        help="per-sheet conditional-format rules serialized to readable lines",
    )
    _spreadsheet_id_arg(p)
    p.add_argument("--sheet", default=None, help="restrict to one sheet (default: all sheets)")
    p.add_argument(
        "--range",
        default=None,
        help="restrict to rules INTERSECTING this A1 range (on its own sheet); "
        "mutually exclusive with --sheet",
    )
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
    p.add_argument(
        "--number-format-type",
        dest="numberFormatType",
        default=None,
        help="number format TYPE (NUMBER | CURRENCY | PERCENT | DATE | TIME | TEXT | SCIENTIFIC)",
    )
    p.add_argument("--halign", default=None, help="LEFT | CENTER | RIGHT")
    p.add_argument("--valign", default=None, help="TOP | MIDDLE | BOTTOM")
    p.add_argument("--wrap", default=None, help="OVERFLOW_CELL | CLIP | WRAP")
    p.add_argument("--note", default=None, help="cell note text")
    p.add_argument(
        "--padding",
        dest="padding",
        type=_json_arg,
        default=None,
        metavar="JSON",
        help='cell padding dict, e.g. \'{"top":2,"right":3,"bottom":2,"left":3}\'',
    )
    p.add_argument(
        "--text-rotation",
        dest="textRotation",
        type=_json_arg,
        default=None,
        metavar="JSON",
        help='text rotation dict, e.g. \'{"angle":45}\' or \'{"vertical":true}\'',
    )
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
        help='requests[] \'[{"spreadsheetId":..,"ranges":[..],"render"?:..,"major"?:..}]\' '
        '(or @file.json). In values mode each item needs EITHER "ranges" OR "data_filters" '
        '(symbolic selectors).',
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


def _cmd_describe(services, args) -> dict:
    return core.describe(
        services,
        args.spreadsheet_id,
        args.ranges or None,
        max_cells=args.max_cells,
        data_filters=args.data_filter_json,
    )


def _cmd_formula_patterns(services, args) -> dict:
    return core.formula_patterns(
        services,
        args.spreadsheet_id,
        args.ranges,
        sample=args.sample,
    )


def _cmd_read_values(services, args) -> dict:
    # ``ranges`` is the positional (possibly empty when --data-filter-json is used); core enforces
    # the "exactly one of ranges / data_filters" contract, so the body stays a thin pass-through.
    return core.read_values(
        services,
        args.spreadsheet_id,
        args.ranges or None,
        render=args.render,
        major=args.major,
        data_filters=args.data_filter_json,
        diff_only=args.diff_only,
        max_cells=args.max_cells,
    )


def _cmd_read_conditional_formats(services, args) -> dict:
    return core.read_conditional_formats(
        services, args.spreadsheet_id, args.sheet, range=args.range
    )


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
    "numberFormatType",
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
        # padding / textRotation are structured atomic leaves (not flat scalars), so they come in
        # as already-parsed JSON dicts ({top,right,bottom,left} / {angle}|{vertical}).
        if getattr(args, "padding", None) is not None:
            fmt["padding"] = args.padding
        if getattr(args, "textRotation", None) is not None:
            fmt["textRotation"] = args.textRotation
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
        meta = []
        if result.get("locale"):
            meta.append(f"locale={result['locale']}")
        if result.get("timeZone"):
            meta.append(f"tz={result['timeZone']}")
        suffix = f"  ({', '.join(meta)})" if meta else ""
        lines.append(f"{result.get('title', '')}  [{result.get('spreadsheetId', '')}]{suffix}")
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

    # describe (SPEC §3): the one-call merged region view. ``regions`` is the distinctive key.
    if "regions" in result and isinstance(result.get("regions"), list):
        return _render_describe(result)

    # formula_patterns (SPEC §4): per-column distinct templates. ``columns`` is the distinctive key.
    if "columns" in result and isinstance(result.get("columns"), list):
        return _render_formula_patterns(result)

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
            # v0.2 structural objects: each serializer precomputes a terse ``line``.
            for t in s.get("tables", []):
                lines.append(f"  {t.get('line')}")
            basic_filter = s.get("basicFilter")
            if basic_filter:
                lines.append(f"  {basic_filter.get('line')}")
            for fv in s.get("filterViews", []):
                lines.append(f"  {fv.get('line')}")
            for b in s.get("bandedRanges", []):
                lines.append(f"  {b.get('line')}")
            for sl in s.get("slicers", []):
                lines.append(f"  {sl.get('line')}")
        return "\n".join(lines) if lines else "(no structural data)"

    # inspect
    if "cells" in result or "runs" in result:
        # Core returns ``range`` already sheet-qualified; only prepend the sheet when it isn't.
        rng = str(result.get("range") or "")
        if rng and "!" not in rng and result.get("sheet"):
            rng = f"{result['sheet']}!{rng}"
        header = f"{rng}  " if rng else ""
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
        # A formula read is SPARSE — address-keyed lines ("C5: =SUM(...)") read better than a dense
        # rectangle (SPEC §4.4). The address-keying (anchor → absolute A1) lives in core.format so
        # both adapters share it; dense numeric grids (plain/unformatted/all) keep the rectangle.
        if result.get("render") == "formula":
            addressed = render_sparse_values(result)
            return addressed if addressed else f"render={result.get('render')}\n(no formulas)"
        lines.append(f"render={result.get('render')}")
        for rng in result["ranges"]:
            lines.append(f"# {rng.get('range')}")
            values = rng.get("values", [])
            computed = rng.get("computed")
            for i, row in enumerate(values):
                if computed is not None and i < len(computed):
                    # A null computed cell (diff_only) means "same as values" — show just the
                    # value rather than a misleading "v => None".
                    pairs = [
                        str(v) if c is None else f"{v} => {c}"
                        for v, c in zip(row, computed[i])
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
    # Single-form set_conditional_format also carries "action" but is the only result with
    # "index"/"rule" — let it fall through to the generic renderer so neither field is dropped.
    if (
        "action" in result
        and "sheets" not in result
        and "cells" not in result
        and "index" not in result
        and "rule" not in result
    ):
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
        # Use the canonical core renderer (offsets, style tokens, links) and drop its
        # ``runs <a1>: `` prefix — the cell line already leads with the address.
        a1 = cell.get("a1") or cell.get("a1Range") or "?"
        full = text_runs_line(a1, runs)
        parts.append("runs: " + full[len(f"runs {a1}: "):])
    if cell.get("hyperlink"):
        parts.append(f"link={cell['hyperlink']}")
    pivot = cell.get("pivot")
    if pivot:
        parts.append(pivot.get("line") or f"pivot <- {pivot.get('source', '?')}")
    return parts


def _render_describe(result: dict) -> str:
    """Render a ``describe`` region view (SPEC §3): one block per requested range.

    Each region leads with its A1 range, then the per-cell lines (reusing the inspect cell
    renderer), then the structural facets that intersect/scope it — merges, the range-scoped
    conditional-format rules (with their priority index), tables/banding/protected ranges (each
    serializer precomputes a terse ``line``), and a one-line validation summary.
    """
    regions = result.get("regions", [])
    if not regions:
        return "(no regions)"
    lines: list[str] = []
    for region in regions:
        lines.append(f"# {region.get('range')}")
        for m in region.get("merges", []):
            lines.append(f"  merge: {m}")
        for cell in region.get("cells", []):
            lines.append(_render_inspect_cell(cell))
        for r in region.get("conditionalFormats", []):
            lines.append(f"  CF [{r.get('index')}]: {r.get('line')}")
        for t in region.get("tables", []):
            lines.append(f"  {t.get('line')}")
        for b in region.get("bandedRanges", []):
            lines.append(f"  {b.get('line')}")
        for pr in region.get("protectedRanges", []):
            lines.append(
                f"  protected: {pr.get('range')} ({pr.get('description') or 'no desc'})"
            )
        vs = region.get("validationSummary") or {}
        if vs.get("cells"):
            rules = ", ".join(vs.get("rules", []))
            lines.append(f"  validation: {vs['cells']} cell(s) [{rules}]")
    return "\n".join(lines)


def _render_formula_patterns(result: dict) -> str:
    """Render ``formula_patterns`` (SPEC §4.2): the column header on the first template line.

    Subsequent templates align under the header; each line is
    ``"<col-or-pad>  <formula>  rows <lo:hi>  (<cells>)  [<a1> -> <value>]"``. A literal-only
    column shows ``"(no formulas)"``; a non-reducible column appends a ``"(verbatim — not reduced)"``
    marker so the lossy fallback is explicit.
    """
    columns = result.get("columns", [])
    if not columns:
        return "(no formula columns)"
    lines: list[str] = []
    for col in columns:
        label = col.get("col") or "?"
        templates = col.get("templates") or []
        if not templates:
            lines.append(f"{label}  (no formulas)")
            continue
        indent = " " * len(label)
        for i, t in enumerate(templates):
            head = label if i == 0 else indent
            bits = [f"{head}  {t.get('formula')}", f"rows {t.get('rows')}", f"({t.get('cells')})"]
            sample = t.get("sample")
            if sample and sample.get("a1") is not None:
                bits.append(f"{sample.get('a1')} -> {sample.get('value')}")
            lines.append("  ".join(bits))
        if col.get("reduced") is False:
            lines.append(f"{indent}  (verbatim — not reduced)")
    return "\n".join(lines)


def _render_read_many(result: dict) -> str:
    """Render a cross-file ``read_many`` envelope (DESIGN §3.3): one line per result entry.

    Each entry is EITHER a captured failure (``ok:False`` -> ``ERROR <code>: <message>``) or a
    success (a summary entry -> ``<title> (<n> sheet(s))``; a values entry -> ``<n> row(s) across
    <k> range(s)``). The top-level ``ok:true`` does NOT imply every file succeeded, so failures
    are surfaced inline.
    """
    mode = result.get("mode")
    count = result.get("count")
    head = f"read-many mode={mode}: {count} result(s)"
    if result.get("partialFailure"):
        head += f" — PARTIAL FAILURE: {result.get('failed')} of {count} failed"
    lines = [head]
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


def _resolve_format(args) -> str:
    """Resolve the effective output format from ``--format`` / the ``--json`` alias (SPEC §1.3).

    ``--format`` wins when set; otherwise ``--json`` maps to ``"json"`` (its permanent alias,
    D-FMTFLAG); otherwise the default ``"text"``. Passing ``--json`` together with an explicit
    ``--format`` that is NOT ``json`` is a conflict (so a stray combination fails loudly rather
    than silently picking one).
    """
    fmt = getattr(args, "output_format", None)
    as_json = getattr(args, "json", False)
    if fmt is None:
        return "json" if as_json else "text"
    if as_json and fmt != "json":
        raise SheetsError(
            "conflicting_args",
            f"--json is an alias for --format json; it conflicts with --format {fmt}",
        )
    return fmt


def _resolve_retry_policy(args) -> "retry_mod.RetryPolicy":
    """Resolve the global retry flags into a :class:`~gsheets.core.retry.RetryPolicy` (ISSUES.md #25).

    Retry is OFF BY DEFAULT (v0.4.0): with no retry flags this returns ``from_env()`` (itself
    disabled unless a ``GSHEETS_BACKOFF_*`` env var explicitly enables it). The three opt-in shapes
    are mutually exclusive — the one-shot preset (``--default-backoff-strategy``), explicit fail-fast
    (``--no-retry``), and granular control (``--retries`` / ``--backoff`` / ``--retry-*`` /
    ``--honor-retry-after``):

    - ``--no-retry`` -> :data:`RetryPolicy.DISABLED` (overrides env);
    - ``--default-backoff-strategy`` -> :meth:`RetryPolicy.default_preset`;
    - any granular flag -> ``from_env(enabled=True, <granular overrides>)`` (env defaults the
      unspecified fields; the explicit flags win);
    - nothing -> ``from_env()``.

    A conflicting combination raises ``SheetsError("backoff_flags_conflict", …)`` — caught in
    :func:`main` and rendered as the same ``ok:false`` envelope every other handled failure produces.
    """
    default_preset = bool(getattr(args, "default_backoff_strategy", False))
    no_retry = bool(getattr(args, "no_retry", False))

    # The granular flags: a value of None (or False for a store_true) means "not provided".
    # --backoff maps the hyphenated user spelling to the underscore core strategy name. The deadline
    # flag is captured separately because "<= 0 => no overall cap" is a value (total_deadline=None)
    # that from_env cannot express via an override (it drops None overrides) — so we apply it after.
    backoff = getattr(args, "backoff", None)
    strategy = backoff.replace("-", "_") if backoff is not None else None
    deadline_flag = getattr(args, "retry_deadline", None)
    granular = {
        "max_retries": getattr(args, "retries", None),
        "strategy": strategy,
        "base_delay": getattr(args, "retry_base_delay", None),
        "max_delay": getattr(args, "retry_max_delay", None),
        "retry_after_cap": getattr(args, "retry_after_cap", None),
        "honor_retry_after": getattr(args, "honor_retry_after", None),
    }
    has_granular = (
        any(v is not None for v in granular.values()) or deadline_flag is not None
    )

    # Mutual exclusion: pick exactly one shape (the preset, --no-retry, or granular), or none.
    if default_preset and no_retry:
        raise SheetsError(
            "backoff_flags_conflict",
            "--default-backoff-strategy and --no-retry are mutually exclusive",
            hint="pick exactly one: the preset, --no-retry, or granular flags",
        )
    if default_preset and has_granular:
        raise SheetsError(
            "backoff_flags_conflict",
            "--default-backoff-strategy cannot be combined with granular retry flags "
            "(--retries / --backoff / --retry-*)",
            hint="use either the preset OR granular flags, not both",
        )
    if no_retry and has_granular:
        raise SheetsError(
            "backoff_flags_conflict",
            "--no-retry cannot be combined with granular retry flags "
            "(--retries / --backoff / --retry-*)",
            hint="--no-retry forces fail-fast; drop the granular flags to use it",
        )

    if no_retry:
        return retry_mod.RetryPolicy.DISABLED
    if default_preset:
        return retry_mod.RetryPolicy.default_preset()
    if has_granular:
        # Granular flags explicitly enable retry; from_env fills the unspecified fields (a None
        # override is ignored, so only the provided flags win). The deadline is applied last via
        # replace() because "<= 0 => no overall cap" is total_deadline=None — a value from_env's
        # override path can't carry.
        overrides = {k: v for k, v in granular.items() if v is not None}
        policy = retry_mod.RetryPolicy.from_env(enabled=True, **overrides)
        if deadline_flag is not None:
            policy = dataclasses.replace(
                policy, total_deadline=(deadline_flag if deadline_flag > 0 else None)
            )
        return policy
    return retry_mod.RetryPolicy.from_env()


def _emit(result: dict, *, fmt: str, stream=None) -> None:
    """Print a successful core result in ``fmt`` (SPEC §1.3).

    ``text`` and ``json`` are HUMAN/interactive views: they go through ``print()`` so each gets a
    friendly trailing newline. The data formats (``jsonl``/``csv``/``tsv``/``markdown``) are MACHINE
    payloads: ``core.format.render`` is already self-terminating for them (csv/tsv end in the csv
    module's ``\\r\\n``; jsonl ends in ``\\n``), so they are written VERBATIM with ``out.write`` — no
    second terminator. That makes CLI-piped bytes byte-identical to ``render()``, to the MCP
    ``out_path`` file, and to the MCP no-out_path data-format string (ISSUES.md #20/#22). Adding a
    ``print()`` newline here is exactly what produced the extra trailing blank line on piped csv.
    """
    out = stream if stream is not None else sys.stdout
    if fmt == "text":
        print(_render_text(result), file=out)
    elif fmt == "json":
        # Human/interactive view: pretty-printed with a friendly trailing newline.
        print(render_format(result, "json"), file=out)
    else:
        # Machine payloads (jsonl/csv/tsv/markdown): render() is self-terminating, so write it
        # verbatim — NO extra newline — to preserve byte-equality with out_path / MCP / export.
        # render() raises format_unsupported on a structured result asked for csv/tsv (caught in
        # main() -> structured error envelope).
        out.write(render_format(result, fmt))


def _emit_error(err: SheetsError, *, fmt: str) -> None:
    """Render a :class:`SheetsError` to stderr — ``ok:false`` JSON envelope or terse line.

    Non-text formats all surface the error as the structured ``ok:false`` JSON envelope (csv/tsv
    of an error makes no sense); ``text`` keeps the terse two-line form.
    """
    if fmt == "text":
        msg = f"gsheets: error: {err.code}: {err.message}"
        if err.hint:
            msg += f"\n  hint: {err.hint}"
        print(msg, file=sys.stderr)
    else:
        payload = {"ok": False, "error": err.to_dict()}
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)


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

    # Resolve the effective output format up front. A --json/--format conflict is itself a
    # SheetsError; surface it as an error envelope (default to text framing for that message).
    try:
        fmt = _resolve_format(args)
    except SheetsError as err:
        _emit_error(err, fmt="text")
        return 1

    try:
        # Resolve the per-invocation retry policy from the global backoff flags (ISSUES.md #25).
        # Retry is OFF BY DEFAULT (v0.4.0); a conflicting flag combination raises a
        # SheetsError("backoff_flags_conflict", …) caught below. The policy is then activated around
        # the WHOLE build_services + dispatch + render block so the auth-layer request builder reads
        # it via current_policy() at .execute() time (there is no central .execute() wrapper in core).
        policy = _resolve_retry_policy(args)
        # Wrap the dispatch in a sheet-index cache scope (ISSUES.md #27) — same chokepoint as the
        # retry policy — so every subcommand that resolves multiple A1<->GridRanges (inspect over
        # merges, overview/describe over named ranges, a multi-series charts create, …) fetches the
        # sheet list ONCE instead of once per element. Nested inside retry; the two contextvars are
        # independent. auth subcommands pass services=None and never touch addressing (harmless).
        with retry_mod.activate(policy), addressing_mod.sheet_index_cache():
            if getattr(args, "needs_services", False):
                services = auth.build_services(scopes_mode=args.scopes)
                result = func(services, args)
            else:
                # auth subcommands: no Sheets handle; they call the auth layer directly.
                result = func(None, args)
            # ``auth status`` returns ok:False (not a raise) when no creds resolve -> non-zero exit.
            if isinstance(result, dict) and result.get("ok") is False:
                _emit(result, fmt=fmt)
                return 1
            # Rendering can fail (e.g. csv/tsv on a structured result -> format_unsupported); keep it
            # inside the guard so it surfaces as the SAME structured error envelope, not a traceback.
            _emit(result, fmt=fmt)
    except SheetsError as err:
        _emit_error(err, fmt=fmt)
        return 1
    except Exception as exc:  # noqa: BLE001 - never surface a raw traceback to the user
        # A transport-level failure (socket read timeout on a big inspect, DNS/connection error,
        # a failed token refresh) is NOT a SheetsError and would otherwise bubble up as a raw
        # Python traceback (ISSUES.md #9b). Coerce it to the SAME structured envelope every other
        # failure produces.
        _emit_error(to_sheets_error(exc), fmt=fmt)
        return 1

    return 0
