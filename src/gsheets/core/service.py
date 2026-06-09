"""The authed service handle that core receives as its first parameter (DESIGN §2.4).

:class:`SheetsServices` is a frozen dataclass built by the ``auth`` layer and handed to
every core function. Core never resolves credentials, never reads env auth vars, and never
imports ``fastmcp``/``Context``. No logic, no I/O lives here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SheetsServices:
    """An authed handle to the Google APIs that core operates against.

    Attributes:
        sheets: A ``googleapiclient`` Resource from ``build("sheets", "v4", ...)``.
        drive: A ``build("drive", "v3", ...)`` Resource, or ``None`` if the drive scope
            is absent.
        account_email: Best-effort authenticated account email, used ONLY for verbose-mode
            error hints (``GSHEETS_VERBOSE_ERRORS``); never required and never leaked in
            default pass-through error text (DESIGN §6).
    """

    sheets: object
    drive: object | None = None
    account_email: str | None = None
