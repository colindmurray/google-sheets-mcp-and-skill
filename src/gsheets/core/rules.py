"""Conditional-format and data-validation writes + validation (de)serialization (DESIGN §3.3).

Houses ``set_conditional_format`` / ``set_validation`` plus the validation round-trip helpers
``validation_to_rule`` / ``rule_to_validation`` that give the ``inspect`` <-> ``set_validation``
structured symmetry (DESIGN §3.1).
"""

from __future__ import annotations

from .service import SheetsServices


def set_conditional_format(
    services: SheetsServices,
    spreadsheet_id: str,
    *,
    action: str,
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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


def rule_to_validation(rule: dict, *, strict: bool = True, show_dropdown: bool = True) -> dict:
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
    raise NotImplementedError
