"""Single error envelope for core (DESIGN §6).

Core raises exactly one exception type (:class:`SheetsError`); it NEVER returns an error
dict. :func:`classify_google_error` maps a ``googleapiclient`` ``HttpError`` to a
:class:`SheetsError` carrying ``status``/``reason``/``hint``.

Privacy: the 403 hint is GENERIC by default and MUST NOT embed the operator's account email;
the concrete email may appear only when ``GSHEETS_VERBOSE_ERRORS`` is set (DESIGN §6.1).
"""

from __future__ import annotations


class SheetsError(Exception):
    """The one exception type core raises.

    ``str(e)`` renders as ``"<code>: <message>"``. The structured fields
    (``status``/``reason``/``hint``) are consumed by the adapters to build their error
    envelopes (MCP ``ToolError`` / CLI ``ok:false`` JSON).

    Args:
        code: Short machine code (e.g. ``"bad_range"``, ``"empty_payload"``,
            ``"google_api_error"``).
        message: Human-readable description.
        status: HTTP status when derived from a Google error.
        reason: Google API ``reason`` string when available.
        hint: Actionable next step (generic for 403 by default — no email).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        reason: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status = status
        self.reason = reason
        self.hint = hint
        super().__init__(f"{code}: {message}")


def classify_google_error(http_error: object) -> SheetsError:
    """Map a ``googleapiclient.errors.HttpError`` to a :class:`SheetsError`.

    Extracts the HTTP ``status`` and Google ``reason`` and attaches an actionable ``hint``
    (e.g. 403 PERMISSION_DENIED → "share the sheet with the authenticated account"; 404 →
    "check the spreadsheet id / sheet name"; 400 → echo the API message). The 403 hint is
    generic by default and never embeds the operator email (DESIGN §6.1).

    Args:
        http_error: The raised ``HttpError`` instance.

    Returns:
        A :class:`SheetsError` with ``code="google_api_error"`` and populated
        ``status``/``reason``/``hint``.
    """
    raise NotImplementedError
