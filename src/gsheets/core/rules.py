"""Conditional-format and data-validation writes + validation (de)serialization (DESIGN §3.3).

Houses ``set_conditional_format`` / ``set_validation`` plus the validation round-trip helpers
``validation_to_rule`` / ``rule_to_validation`` that give the ``inspect`` <-> ``set_validation``
structured symmetry (DESIGN §3.1).

PURE core module: imports only stdlib + ``googleapiclient`` + sibling core modules. It must
NEVER import ``fastmcp``, ``mcp``, ``argparse``, ``pydantic``, or ``gsheets.models``
(DESIGN §1 boundary).
"""

from __future__ import annotations

from googleapiclient.errors import HttpError

from . import condformat
from .addressing import a1_to_gridrange
from .errors import SheetsError, classify_google_error
from .service import SheetsServices

# --------------------------------------------------------------------------------------
# Conditional formatting
# --------------------------------------------------------------------------------------

_CF_ACTIONS = ("add", "update", "delete")


def set_conditional_format(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    action: str | None = None,
    sheet: str | None = None,
    index: int | None = None,
    rule: str | dict | None = None,
    rules: list[dict] | None = None,
) -> dict:
    """Add/update/delete a conditional-format rule by positional index (DESIGN §3.3, §4).

    Array order IS priority (index 0 = highest); there is no ``priority`` field. ``rule``
    accepts EITHER a readable body line (parsed via the §4 grammar — the line carries no
    index) OR a structured ``{ranges, kind, condition, format}`` dict. The target index comes
    SOLELY from the ``index`` kwarg.

    For multiple safe mutations in one shot, pass ``rules`` (a list of
    ``{"action", "index", "rule"}`` items); core sorts them by ``index`` DESCENDING and emits
    one ``batchUpdate`` so earlier mutations never shift later targets. Passing both ``rules``
    and any of ``action``/``index``/``rule`` raises ``SheetsError("conflicting_args")``.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        action: ``"add"`` | ``"update"`` | ``"delete"`` (single form).
        sheet: Target tab name (for the single form).
        index: Positional index — required for update/delete; insert position for add.
        rule: Body line OR structured rule dict (single form).
        rules: BATCH form — list of ``{"action", "index", "rule"}`` items.

    Returns:
        Single form: ``{"ok": True, "spreadsheetId": ..., "action": ..., "sheet": ...,
        "index": ..., "rule": ...}``. Batch form: ``{"ok": True, "spreadsheetId": ...,
        "results": [...]}`` in applied (high->low) order.
    """
    if rules is not None:
        if action is not None or index is not None or rule is not None:
            raise SheetsError(
                "conflicting_args",
                "pass EITHER the batch `rules` list OR single-form "
                "`action`/`index`/`rule`, not both",
            )
        return _set_conditional_format_batch(services, spreadsheet_id, sheet, rules)

    if action is None:
        raise SheetsError(
            "missing_action",
            "set_conditional_format requires `action` (or the batch `rules` list)",
        )
    return _set_conditional_format_single(
        services, spreadsheet_id, action, sheet, index, rule
    )


def _set_conditional_format_single(
    services: SheetsServices,
    spreadsheet_id: str,
    action: str,
    sheet: str | None,
    index: int | None,
    rule: str | dict | None,
) -> dict:
    """Build + apply ONE conditional-format mutation (single form)."""
    request, serialized_line, resolved_index = _build_cf_request(
        services, spreadsheet_id, action, sheet, index, rule
    )

    _execute_batch_update(services, spreadsheet_id, [request])

    out: dict = {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "action": action,
        "sheet": sheet,
        "index": resolved_index,
    }
    if serialized_line is not None:
        out["rule"] = serialized_line
    return out


def _set_conditional_format_batch(
    services: SheetsServices,
    spreadsheet_id: str,
    sheet: str | None,
    rules: list[dict],
) -> dict:
    """Build + apply MANY conditional-format mutations in one batch, sorted high->low.

    Each item is ``{"action", "index", "rule"}`` (``rule`` omitted for delete). Core sorts
    by ``index`` DESCENDING so earlier mutations never shift later targets, then emits one
    ``batchUpdate``. The returned ``results`` are in applied (high->low) order.
    """
    if not isinstance(rules, list) or not rules:
        raise SheetsError("empty_payload", "`rules` must be a non-empty list of items")

    prepared: list[dict] = []
    for i, item in enumerate(rules):
        if not isinstance(item, dict):
            raise SheetsError(
                "bad_rule", f"rules[{i}] must be a dict with action/index/rule"
            )
        item_action = item.get("action")
        item_index = item.get("index")
        item_rule = item.get("rule")
        item_sheet = item.get("sheet", sheet)

        request, serialized_line, resolved_index = _build_cf_request(
            services, spreadsheet_id, item_action, item_sheet, item_index, item_rule
        )
        prepared.append(
            {
                "request": request,
                "action": item_action,
                "index": resolved_index,
                "sheet": item_sheet,
                "rule_line": serialized_line,
            }
        )

    # Sort by index DESCENDING so each mutation does not shift the array position of a
    # later (lower-index) target. ``add`` uses its index as an insert position; sorting it
    # alongside delete/update high->low is still index-shift-safe within one batch.
    prepared.sort(key=lambda p: p["index"], reverse=True)

    _execute_batch_update(services, spreadsheet_id, [p["request"] for p in prepared])

    results: list[dict] = []
    for p in prepared:
        entry: dict = {"action": p["action"], "index": p["index"]}
        if p["rule_line"] is not None:
            entry["rule"] = p["rule_line"]
        results.append(entry)

    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "results": results,
    }


def _build_cf_request(
    services: SheetsServices,
    spreadsheet_id: str,
    action: str | None,
    sheet: str | None,
    index: int | None,
    rule: str | dict | None,
) -> tuple[dict, str | None, int]:
    """Build a single CF ``batchUpdate`` request from one mutation's args.

    Returns ``(request, serialized_line_or_None, resolved_index)``. ``serialized_line`` is the
    canonical body-only line for add/update (``None`` for delete). Resolves the rule's A1
    ranges to ``GridRange`` dicts (this module owns the ``SheetsServices`` handle, DESIGN §5.2)
    and validates the index per action.
    """
    if action not in _CF_ACTIONS:
        raise SheetsError(
            "bad_action",
            f"unknown conditional-format action {action!r}; "
            "expected 'add', 'update', or 'delete'",
        )

    resolved_index = _require_index(action, index)

    if action == "delete":
        # ``deleteConditionalFormatRule`` addresses one sheet's ``conditionalFormats[]``
        # array by ``(sheetId, index)``; both are required. The sheet comes from the
        # ``sheet`` name (delete carries no rule whose ranges could supply it).
        sheet_id = _resolve_sheet_id(services, spreadsheet_id, sheet)
        if sheet_id is None:
            raise SheetsError(
                "missing_sheet",
                "conditional-format `delete` requires a `sheet` to identify the rule array",
            )
        return (
            {"deleteConditionalFormatRule": {"index": resolved_index, "sheetId": sheet_id}},
            None,
            resolved_index,
        )

    # add / update both need a rule. Normalize to a Google ConditionalFormatRule whose
    # ranges are A1 strings, then resolve those ranges to GridRange dicts.
    if rule is None:
        raise SheetsError(
            "missing_rule", f"conditional-format action {action!r} requires a `rule`"
        )

    google_rule, serialized_line = _normalize_rule(rule)
    resolved_rule = _resolve_rule_ranges(services, spreadsheet_id, google_rule)

    if action == "add":
        # ``addConditionalFormatRule`` infers the target sheet from the rule's ranges; only
        # ``rule`` + insert ``index`` are sent.
        request = {
            "addConditionalFormatRule": {
                "rule": resolved_rule,
                "index": resolved_index,
            }
        }
    else:  # update
        # ``updateConditionalFormatRule`` addresses ``(sheetId, index)``; the sheet comes
        # from the explicit ``sheet`` name when given, else from the rule's first resolved
        # range (all ranges in a CF rule share one sheet).
        sheet_id = _resolve_sheet_id(services, spreadsheet_id, sheet)
        if sheet_id is None:
            sheet_id = _sheet_id_from_resolved_rule(resolved_rule)
        request = {
            "updateConditionalFormatRule": {
                "index": resolved_index,
                "sheetId": sheet_id,
                "rule": resolved_rule,
            }
        }
    return request, serialized_line, resolved_index


def _resolve_sheet_id(
    services: SheetsServices, spreadsheet_id: str, sheet: str | None
) -> int | None:
    """Resolve a sheet NAME to its ``sheetId`` (or ``None`` when no name is given).

    Reuses ``a1_to_gridrange`` (which resolves sheet name -> sheetId via the cached
    ``spreadsheets.get``) against a bare-sheet A1 reference so this module never duplicates
    the resolution logic owned by ``core_addressing``.
    """
    if sheet is None:
        return None
    # Quote the bare name so addressing treats it as a SHEET reference, not an A1 cell
    # (e.g. a sheet literally named "Sheet1" must not be parsed as cell Sheet1). A literal
    # single quote inside the name is doubled per the A1 quoting rules.
    quoted = "'" + sheet.replace("'", "''") + "'"
    grid = a1_to_gridrange(services, spreadsheet_id, quoted)
    return grid.get("sheetId")


def _sheet_id_from_resolved_rule(resolved_rule: dict) -> int:
    """Pull the ``sheetId`` from the first resolved ``GridRange`` of a rule."""
    ranges = resolved_rule.get("ranges") or []
    for gr in ranges:
        if isinstance(gr, dict) and gr.get("sheetId") is not None:
            return gr["sheetId"]
    raise SheetsError(
        "missing_sheet",
        "conditional-format `update` needs a `sheet` (or a rule whose ranges name one)",
    )


def _require_index(action: str, index: int | None) -> int:
    """Validate + normalize the positional index for a CF mutation.

    ``update``/``delete`` require an explicit index; ``add`` defaults to ``0`` (highest
    priority, prepend) when omitted, per the design's insert-position semantics.
    """
    if index is None:
        if action == "add":
            return 0
        raise SheetsError(
            "missing_index",
            f"conditional-format action {action!r} requires an `index`",
        )
    if isinstance(index, bool) or not isinstance(index, int):
        raise SheetsError("bad_index", f"index must be an int, got {index!r}")
    if index < 0:
        raise SheetsError("bad_index", f"index must be >= 0, got {index}")
    return index


def _normalize_rule(rule: str | dict) -> tuple[dict, str]:
    """Normalize a body line OR structured rule dict to a Google ``ConditionalFormatRule``.

    Returns ``(google_rule, serialized_line)`` where ``google_rule`` keeps A1-string ranges
    (the caller resolves them to ``GridRange`` dicts) and ``serialized_line`` is the canonical
    body-only line (round-trippable, DESIGN §4.4).
    """
    if isinstance(rule, str):
        parsed = condformat.parse_rule_line(rule)
        google_rule = condformat.build_google_rule(parsed)
    elif isinstance(rule, dict):
        # A caller may pass a structured ``{ranges, kind, condition/stops, format}`` dict OR
        # an already-built Google rule (``{ranges, booleanRule/gradientRule}``). Detect the
        # latter and pass it through; otherwise build from the structured dict.
        if "booleanRule" in rule or "gradientRule" in rule:
            google_rule = dict(rule)
        else:
            google_rule = condformat.build_google_rule(rule)
    else:
        raise SheetsError(
            "bad_rule",
            f"rule must be a body line (str) or structured dict, got "
            f"{type(rule).__name__}",
        )

    serialized_line = condformat.serialize_rule(google_rule)
    return google_rule, serialized_line


def _resolve_rule_ranges(
    services: SheetsServices, spreadsheet_id: str, google_rule: dict
) -> dict:
    """Return a copy of ``google_rule`` whose A1-string ``ranges`` become ``GridRange`` dicts."""
    resolved = dict(google_rule)
    ranges = google_rule.get("ranges")
    if not ranges:
        raise SheetsError("bad_rule", "conditional-format rule has no ranges")
    grid_ranges: list[dict] = []
    for a1 in ranges:
        if isinstance(a1, dict):
            # Already a GridRange (caller pre-resolved); pass through.
            grid_ranges.append(a1)
        else:
            grid_ranges.append(a1_to_gridrange(services, spreadsheet_id, a1))
    resolved["ranges"] = grid_ranges
    return resolved


def _execute_batch_update(
    services: SheetsServices, spreadsheet_id: str, requests: list[dict]
) -> dict:
    """Issue one ``spreadsheets.batchUpdate`` for the prepared requests, classifying errors."""
    try:
        return (
            services.sheets.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests},
            )
            .execute()
        )
    except HttpError as exc:
        raise classify_google_error(exc, account_email=services.account_email) from exc


# --------------------------------------------------------------------------------------
# Data validation
# --------------------------------------------------------------------------------------

# Validation condition types whose Google ``BooleanCondition.values`` are a single
# range-formula (``=Sheet!A1:A10``); surfaced in the structured shape as ``source`` rather
# than ``values`` so the round-trip is symmetric and human-friendly.
_RANGE_VALIDATION_TYPES = frozenset({"ONE_OF_RANGE"})

# Validation condition types that carry NO values (checkbox / blank-style).
_NO_VALUE_VALIDATION_TYPES = frozenset({"BOOLEAN", "NOT_BLANK", "BLANK"})


def set_validation(
    services: SheetsServices,
    spreadsheet_id: str,
    range: str,
    *,
    rule: dict | None = None,
    strict: bool = True,
    show_dropdown: bool = True,
) -> dict:
    """Set or clear data validation on a range (DESIGN §3.3).

    ``rule`` is the structured ``ValidationRule`` shape (e.g.
    ``{"type": "ONE_OF_LIST", "values": ["Yes", "No"]}``); ``None`` clears validation.
    ``strict``/``show_dropdown`` may also be carried inside ``rule`` (kwargs win on conflict).
    Maps to ``setDataValidation``. The same ``ValidationRule`` dict is surfaced by ``inspect``
    under each cell's ``validationRule`` key, so it round-trips unchanged.

    Args:
        services: The authed handle.
        spreadsheet_id: Target spreadsheet id.
        range: A1 range to validate.
        rule: Structured ``ValidationRule`` dict, or ``None`` to clear.
        strict: Reject invalid input (default ``True``).
        show_dropdown: Show the in-cell dropdown (default ``True``).

    Returns:
        ``{"ok": True, "spreadsheetId": ..., "range": ..., "validation": ...,
        "validationRule": {...}}``.
    """
    grid_range = a1_to_gridrange(services, spreadsheet_id, range)

    if rule is None:
        # Clear: setDataValidation with no ``rule`` removes validation on the range.
        request: dict = {"setDataValidation": {"range": grid_range}}
        google_rule = None
        normalized = None
        terse = None
    else:
        if not isinstance(rule, dict):
            raise SheetsError(
                "bad_validation",
                f"validation rule must be a dict, got {type(rule).__name__}",
            )
        google_rule = rule_to_validation(rule, strict=strict, show_dropdown=show_dropdown)
        # Read the structured shape back from the Google rule so the returned
        # ``validationRule`` is exactly what ``inspect`` would surface (full round-trip).
        normalized = validation_to_rule(google_rule)
        terse = _validation_one_liner(normalized)
        request = {
            "setDataValidation": {"range": grid_range, "rule": google_rule}
        }

    _execute_batch_update(services, spreadsheet_id, [request])

    return {
        "ok": True,
        "spreadsheetId": spreadsheet_id,
        "range": range,
        "validation": terse,
        "validationRule": normalized,
    }


def rule_to_validation(
    rule: dict, *, strict: bool = True, show_dropdown: bool = True
) -> dict:
    """Convert a structured ``ValidationRule`` to a Google ``DataValidationRule``.

    The write side of the validation round-trip (DESIGN §3.1). Inverse of
    :func:`validation_to_rule`. ``strict``/``show_dropdown`` may be overridden by keys inside
    ``rule`` per the kwargs-win rule documented on :func:`set_validation`.

    Args:
        rule: A structured ``ValidationRule`` dict.
        strict: Reject invalid input.
        show_dropdown: Show the in-cell dropdown.

    Returns:
        A Google ``DataValidationRule`` dict for ``setDataValidation``.
    """
    if not isinstance(rule, dict):
        raise SheetsError(
            "bad_validation", f"validation rule must be a dict, got {type(rule).__name__}"
        )
    rule_type = rule.get("type")
    if not isinstance(rule_type, str) or not rule_type:
        raise SheetsError("bad_validation", "validation rule requires a `type`")

    condition: dict = {"type": rule_type}
    values = _validation_condition_values(rule, rule_type)
    if values:
        condition["values"] = values

    google: dict = {"condition": condition}

    # ``strict`` / ``showDropdown`` resolution (DESIGN §3.3 — "kwargs win on conflict"). The
    # rule may carry ``strict``/``showDropdown``; the kwargs override. Since the locked
    # signature gives the kwargs a default of ``True`` (so an explicit kwarg is
    # indistinguishable from the default), we honor an explicit ``False`` from EITHER source:
    # a non-default kwarg always applies, and when the kwarg is left at its ``True`` default
    # the rule's own key (if present) is honored. This is what makes the
    # ``validation_to_rule`` -> ``set_validation(rule=...)`` round-trip preserve a non-strict /
    # no-dropdown rule.
    google["strict"] = bool(_resolve_flag(strict, rule.get("strict")))
    # Google's field for the in-cell dropdown is ``showCustomUi``.
    google["showCustomUi"] = bool(_resolve_flag(show_dropdown, rule.get("showDropdown")))
    return google


def _resolve_flag(kwarg_value: bool, rule_value: object) -> bool:
    """Resolve a strict/dropdown flag: a non-default kwarg wins; else the rule's key.

    The public kwargs default to ``True``. When the kwarg is ``False`` it was explicitly set
    (the only non-default) and wins. When the kwarg is ``True`` (its default) we cannot tell
    explicit-True from default-True, so we defer to the rule's own value when it carries one.
    """
    if kwarg_value is False:
        return False
    if isinstance(rule_value, bool):
        return rule_value
    return bool(kwarg_value)


def _validation_condition_values(rule: dict, rule_type: str) -> list[dict]:
    """Build the Google ``BooleanCondition.values`` for a structured validation rule."""
    if rule_type in _NO_VALUE_VALIDATION_TYPES:
        return []

    if rule_type in _RANGE_VALIDATION_TYPES:
        source = rule.get("source")
        if source is None:
            # Tolerate a ``values`` carrier too (first element is the range).
            vals = rule.get("values")
            if isinstance(vals, (list, tuple)) and vals:
                source = vals[0]
        if not isinstance(source, str) or not source.strip():
            raise SheetsError(
                "bad_validation",
                f"{rule_type} validation requires a `source` range string",
            )
        formula = source.strip()
        if not formula.startswith("="):
            formula = "=" + formula
        return [{"userEnteredValue": formula}]

    # Generic value-bearing types (ONE_OF_LIST, NUMBER_BETWEEN, CUSTOM_FORMULA, date/text...).
    raw = rule.get("values")
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise SheetsError(
            "bad_validation", f"{rule_type} validation `values` must be a list"
        )
    out: list[dict] = []
    for v in raw:
        out.append({"userEnteredValue": _scalar_to_str(v)})
    return out


def validation_to_rule(google_validation: dict) -> dict:
    """Convert a Google ``DataValidationRule`` to the structured ``ValidationRule`` shape.

    The read side of the validation round-trip (DESIGN §3.1): feeds the ``validationRule`` key
    on each ``inspect`` cell. Inverse of :func:`rule_to_validation`.

    Args:
        google_validation: A Google ``DataValidationRule`` dict.

    Returns:
        A structured ``ValidationRule`` dict (e.g.
        ``{"type": "ONE_OF_LIST", "values": [...], "strict": ..., "showDropdown": ...}``).
    """
    if not isinstance(google_validation, dict):
        raise SheetsError(
            "bad_validation",
            f"DataValidationRule must be a dict, got {type(google_validation).__name__}",
        )
    condition = google_validation.get("condition")
    if not isinstance(condition, dict):
        raise SheetsError("bad_validation", "DataValidationRule has no condition")
    rule_type = condition.get("type")
    if not isinstance(rule_type, str) or not rule_type:
        raise SheetsError("bad_validation", "validation condition has no type")

    out: dict = {"type": rule_type}

    raw_values = condition.get("values") or []
    extracted = [_condition_value_to_scalar(v) for v in raw_values]

    if rule_type in _RANGE_VALIDATION_TYPES:
        if extracted:
            source = extracted[0]
            # Strip the leading "=" so ``source`` reads as a plain range (it round-trips
            # back through rule_to_validation which re-adds the "=").
            if isinstance(source, str) and source.startswith("="):
                source = source[1:]
            out["source"] = source
    elif rule_type in _NO_VALUE_VALIDATION_TYPES:
        pass  # checkbox / blank: no values
    else:
        if extracted:
            out["values"] = extracted

    # ``strict`` defaults True when Google omits it (Google omits ``strict`` when False on
    # some payloads, but the documented default behaviour for setDataValidation reads back
    # the field; surface what Google reports, defaulting to True to mirror the write default).
    out["strict"] = bool(google_validation.get("strict", True))
    out["showDropdown"] = bool(google_validation.get("showCustomUi", True))
    return out


def _validation_one_liner(rule: dict) -> str:
    """Render the terse, token-cheap human one-liner for a structured ``ValidationRule``.

    Examples: ``"ONE_OF_LIST(Yes,No)"``, ``"ONE_OF_RANGE(Cliff!Z1:Z10)"``, ``"BOOLEAN"``,
    ``"NUMBER_BETWEEN(0,100)"``, ``"CUSTOM_FORMULA(=ISNUMBER(A1))"``.
    """
    rule_type = rule.get("type", "")
    if rule_type in _RANGE_VALIDATION_TYPES:
        source = rule.get("source")
        return f"{rule_type}({source})" if source is not None else rule_type
    if rule_type in _NO_VALUE_VALIDATION_TYPES:
        return rule_type
    values = rule.get("values")
    if values:
        return f"{rule_type}({','.join(_scalar_to_str(v) for v in values)})"
    return rule_type


def _condition_value_to_scalar(value: object) -> object:
    """Extract a scalar from a Google ``BooleanCondition.values[]`` entry.

    Each entry is typically ``{"userEnteredValue": "..."}`` (or ``{"relativeDate": "..."}``).
    Numbers stay as the raw string Google supplies (we never guess their numeric type).
    """
    if isinstance(value, dict):
        if "userEnteredValue" in value:
            return value["userEnteredValue"]
        if "relativeDate" in value:
            return value["relativeDate"]
        # Unknown variant: best-effort first value.
        return next(iter(value.values()), "")
    return value


def _scalar_to_str(value: object) -> str:
    """Stringify a scalar for a Google ``userEnteredValue`` (ints stay bare, no ``.0``)."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
