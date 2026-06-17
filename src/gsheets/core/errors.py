"""Single error envelope for core (DESIGN §6).

Core raises exactly one exception type (:class:`SheetsError`); it NEVER returns an error
dict. :func:`classify_google_error` maps a ``googleapiclient`` ``HttpError`` to a
:class:`SheetsError` carrying ``status``/``reason``/``hint``.

Privacy: the 403 hint is GENERIC by default and MUST NOT embed the operator's account email;
the concrete email may appear only when ``GSHEETS_VERBOSE_ERRORS`` is set (DESIGN §6.1).

This module is PURE core: stdlib only. It must NEVER import ``fastmcp``, ``mcp``,
``argparse``, ``pydantic``, or ``gsheets.models`` (DESIGN §1 boundary).
"""

from __future__ import annotations

import json
import os


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
        retries: Retries performed before this error was surfaced (ISSUES.md #25); ``None`` when
            retry was off / not applicable. Lets a caller see a multi-minute backoff wasn't silent.
        waited_ms: Cumulative backoff sleep (ms) across those retries (ISSUES.md #25); ``None``
            when retry was off / not applicable.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        reason: str | None = None,
        hint: str | None = None,
        retries: int | None = None,
        waited_ms: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status = status
        self.reason = reason
        self.hint = hint
        self.retries = retries
        self.waited_ms = waited_ms
        super().__init__(f"{code}: {message}")

    def __repr__(self) -> str:  # pragma: no cover - convenience only
        return (
            f"SheetsError(code={self.code!r}, message={self.message!r}, "
            f"status={self.status!r}, reason={self.reason!r}, hint={self.hint!r}, "
            f"retries={self.retries!r}, waited_ms={self.waited_ms!r})"
        )

    def to_dict(self) -> dict:
        """Return the CLI ``ok:false`` error payload (DESIGN §6.2).

        The CLI adapter wraps this under ``{"ok": false, "error": {...}}``; keys with a
        ``None`` value are omitted so the envelope stays terse. The retry telemetry
        (``retries``/``waitedMs``, ISSUES.md #25) is likewise included only when present.
        """
        out: dict[str, object] = {"code": self.code, "message": self.message}
        if self.status is not None:
            out["status"] = self.status
        if self.reason is not None:
            out["reason"] = self.reason
        if self.hint is not None:
            out["hint"] = self.hint
        if self.retries is not None:
            out["retries"] = self.retries
        if self.waited_ms is not None:
            out["waitedMs"] = self.waited_ms
        return out


# Map HTTP status -> generic, actionable hint. The 403 hint is deliberately GENERIC and
# never embeds the operator email by default (DESIGN §6.1); the email is appended only in
# verbose mode (see ``_verbose_errors_enabled``).
_HINT_BY_STATUS: dict[int, str] = {
    400: "check the request — bad range/payload; the API message above has specifics",
    401: "credentials missing or expired — run `gsheets auth login` to mint a fresh token",
    403: "share the sheet with the authenticated account, or check that the granted "
    "OAuth scopes cover this operation",
    404: "check the spreadsheet id / sheet name — the spreadsheet or sheet was not found",
    429: "rate limit / quota exceeded — N parallel callers share one per-user quota; batch "
    "many ranges into one read_values call and reduce read RPM, then retry with backoff",
    500: "transient Google server error — retry the request",
    503: "Google Sheets is temporarily unavailable — retry the request",
}

_DEFAULT_HINT = "see the API message above for details"


def _verbose_errors_enabled() -> bool:
    """True when ``GSHEETS_VERBOSE_ERRORS`` is set to a truthy value.

    Only then may a hint include the authenticated account email (DESIGN §6.1). Off by
    default so the operator email never leaks in masked/pass-through error text.
    """
    val = os.environ.get("GSHEETS_VERBOSE_ERRORS")
    if val is None:
        return False
    return val.strip().lower() not in ("", "0", "false", "no", "off")


def _decode_content(http_error: object) -> dict | None:
    """Best-effort decode of an ``HttpError``'s JSON content to a dict.

    Returns the parsed ``dict`` (typically ``{"error": {...}}``) or ``None`` when the body
    is absent / not JSON / not an object. Never raises.
    """
    content = getattr(http_error, "content", None)
    if content is None:
        return None
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            return None
    if not isinstance(content, str):
        return None
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _extract_status(http_error: object, error_obj: dict | None) -> int | None:
    """Extract the integer HTTP status from the ``HttpError`` (or its JSON body)."""
    # Prefer the live response object (set by googleapiclient on real errors).
    resp = getattr(http_error, "resp", None)
    status = getattr(resp, "status", None)
    if status is not None:
        try:
            return int(status)
        except (TypeError, ValueError):
            pass
    # ``status_code`` property mirrors ``resp.status`` on real HttpErrors.
    status = getattr(http_error, "status_code", None)
    if status is not None:
        try:
            return int(status)
        except (TypeError, ValueError):
            pass
    # Fall back to the JSON body's ``error.code``.
    if error_obj is not None:
        code = error_obj.get("code")
        if code is not None:
            try:
                return int(code)
            except (TypeError, ValueError):
                pass
    return None


def _extract_reason(error_obj: dict | None) -> str | None:
    """Extract Google's machine ``reason`` code (e.g. ``"PERMISSION_DENIED"``).

    Google Sheets errors expose the canonical reason as ``error.status``
    (``PERMISSION_DENIED``, ``NOT_FOUND``, ``INVALID_ARGUMENT``, …). Older/per-error
    detail lives in ``error.errors[].reason`` (``forbidden``, ``notFound``, …); use that as
    a fallback. This is distinct from ``HttpError.reason`` (the human message).
    """
    if not error_obj:
        return None
    status = error_obj.get("status")
    if isinstance(status, str) and status:
        return status
    errors = error_obj.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            reason = first.get("reason")
            if isinstance(reason, str) and reason:
                return reason
    return None


def _extract_message(http_error: object, error_obj: dict | None) -> str:
    """Extract the human-readable API message for the error."""
    if error_obj is not None:
        msg = error_obj.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    # ``HttpError.reason`` already holds the parsed ``error.message`` for real errors.
    reason = getattr(http_error, "reason", None)
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    text = str(http_error).strip()
    return text or "Google API request failed"


def classify_google_error(
    http_error: object, *, account_email: str | None = None
) -> SheetsError:
    """Map a ``googleapiclient.errors.HttpError`` to a :class:`SheetsError`.

    Extracts the HTTP ``status`` and Google ``reason`` code and attaches an actionable
    ``hint`` (e.g. 403 PERMISSION_DENIED → "share the sheet with the authenticated
    account"; 404 → "check the spreadsheet id / sheet name"; 400 → echo the API message).

    The 403 hint is GENERIC by default and NEVER embeds the operator email. The optional
    ``account_email`` is appended to the hint ONLY when ``GSHEETS_VERBOSE_ERRORS`` is set
    (DESIGN §6.1); in the default (masked / pass-through) path the email is dropped so it
    cannot leak through ``ToolError`` text to an MCP client.

    Args:
        http_error: The raised ``HttpError`` instance (any object exposing ``resp.status``
            and ``content`` is tolerated; missing fields degrade gracefully).
        account_email: Best-effort authenticated account email from ``SheetsServices``.
            Used only in verbose mode and only for permission (403/401) hints.

    Returns:
        A :class:`SheetsError` with ``code="google_api_error"`` and populated
        ``status``/``reason``/``hint``.
    """
    data = _decode_content(http_error)
    error_obj = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error_obj, dict):
        error_obj = None

    status = _extract_status(http_error, error_obj)
    reason = _extract_reason(error_obj)
    message = _extract_message(http_error, error_obj)

    hint = _HINT_BY_STATUS.get(status, _DEFAULT_HINT) if status is not None else _DEFAULT_HINT

    # Email is verbose-only and only meaningful for permission/credential failures.
    if (
        account_email
        and status in (401, 403)
        and _verbose_errors_enabled()
    ):
        hint = f"{hint} (authenticated as {account_email})"

    # Retry telemetry (ISSUES.md #25): execute_with_retry annotates the raised HttpError with how
    # many retries it performed and how long it slept before giving up. Surface them so a caller
    # can see a multi-minute backoff wasn't silent. Best-effort int coercion; absent -> None.
    retries = _retry_int(getattr(http_error, "_gsheets_retry_attempts", None))
    waited_ms = _retry_int(getattr(http_error, "_gsheets_retry_waited_ms", None))

    return SheetsError(
        "google_api_error",
        message,
        status=status,
        reason=reason,
        hint=hint,
        retries=retries,
        waited_ms=waited_ms,
    )


def _retry_int(value: object) -> int | None:
    """Coerce a retry-telemetry attribute to an int, or ``None`` (absent / unparseable)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Transport / unexpected-error classification (DESIGN §6 — nothing leaks raw).
#
# Core's per-call ``try/except HttpError`` only catches API-level errors. A
# transport-level failure (socket timeout, DNS/connection error, a failed OAuth
# token refresh) is NOT an ``HttpError`` and would otherwise bubble up as a raw
# traceback (CLI) or a bare masked "Error calling tool" (MCP). These helpers map
# such failures to a coded :class:`SheetsError` so BOTH adapters surface a
# structured ``{code, message, hint}`` envelope instead.
# ---------------------------------------------------------------------------


def _is_connection_failure(exc: BaseException) -> bool:
    """True when ``exc`` (or its cause chain) looks like a connectivity/DNS/timeout failure.

    Recognizes stdlib socket/timeout/connection errors directly, plus google-auth's
    ``TransportError`` wrapper and any ``requests``/``urllib3`` connection error reachable
    through ``__cause__`` (a failed token refresh wraps the underlying connection error).
    """
    import socket

    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, (socket.timeout, socket.gaierror, TimeoutError, ConnectionError)):
            return True
        name = type(cur).__name__
        if name in ("TransportError", "ConnectTimeout", "ReadTimeout",
                    "NewConnectionError", "MaxRetryError", "SSLError"):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def classify_transport_error(exc: BaseException) -> SheetsError | None:
    """Map a transport-level exception to a :class:`SheetsError`, or ``None`` if unrecognized.

    Covers the failure modes a plain ``except HttpError`` misses: a socket read timeout on a
    large grid read (``network_timeout``), and a DNS/connection failure reaching Google
    (``network_error``). Returns ``None`` for anything that is not transport-shaped so the
    caller can fall back to a generic ``internal_error``.
    """
    import socket
    import ssl

    if isinstance(exc, (socket.timeout, TimeoutError)):
        return SheetsError(
            "network_timeout",
            f"the request to Google timed out: {exc}",
            hint="the read may be large or the network slow — retry, or narrow the range "
            "(set GSHEETS_HTTP_TIMEOUT to raise the socket timeout)",
        )
    if isinstance(exc, (socket.gaierror, ConnectionError, ssl.SSLError)) or _is_connection_failure(
        exc
    ):
        return SheetsError(
            "network_error",
            f"could not reach Google's servers: {exc}",
            hint="check network connectivity / DNS to googleapis.com and retry "
            "(a cached, still-valid token is unaffected by a transient outage)",
        )
    if isinstance(exc, OSError):
        return SheetsError(
            "network_error",
            f"I/O error during the API call: {exc}",
            hint="check network connectivity and retry",
        )
    return None


def to_sheets_error(exc: BaseException) -> SheetsError:
    """Coerce ANY exception into a :class:`SheetsError` (the adapter catch-all envelope).

    Precedence: an already-built :class:`SheetsError` passes through unchanged; a Google
    ``HttpError`` is classified by :func:`classify_google_error`; a transport failure by
    :func:`classify_transport_error`; everything else becomes a generic ``internal_error`` that
    still carries the exception type + message (never a bare, contextless string). This is what
    lets both adapters turn an unexpected per-call failure into a structured error instead of a
    raw traceback / masked "Error calling tool" (DESIGN §6.2).
    """
    if isinstance(exc, SheetsError):
        return exc
    try:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            return classify_google_error(exc)
    except Exception:  # pragma: no cover - googleapiclient always present at runtime
        pass
    transport = classify_transport_error(exc)
    if transport is not None:
        return transport
    return SheetsError(
        "internal_error",
        f"{type(exc).__name__}: {exc}",
        hint="an unexpected error occurred while calling Google — this is likely a bug; "
        "re-run with GSHEETS_VERBOSE_ERRORS=1 for more detail",
    )
