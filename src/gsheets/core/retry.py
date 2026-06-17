"""Per-call retry / exponential-backoff policy — the pure mechanism (ISSUES.md #25).

Background: backoff used to be ALWAYS ON (#7) — a ``requestBuilder`` set once in the auth
layer handed every Google call a default ``num_retries`` so a 429/5xx was retried with
googleapiclient's built-in randomized exponential backoff. That was great for resilience but
had no off-switch, no caller-facing controls, and no progress visibility (ISSUES.md #25): a
tight-quota ``read-conditional-formats`` could silently retry for ~5–10 min with zero feedback,
and a latency-sensitive caller had no way to fail fast.

This module replaces that with an OFF-BY-DEFAULT, per-call-configurable policy (v0.4.0 — a
breaking default change). The shape:

- :class:`RetryPolicy` — a frozen, immutable dataclass holding the full policy (enable, strategy,
  caps, deadline, ``Retry-After`` handling, retryable status set). Constructors:
  :data:`RetryPolicy.DISABLED` (the true off), :meth:`RetryPolicy.default_preset` (the sensible
  catch-all preset), and :meth:`RetryPolicy.from_env` (env defaults + explicit overrides).
- :func:`execute_with_retry` — the loop. It wraps a zero-arg ``call`` and retries per the active
  policy, duck-typing the HTTP status / Google reason / ``Retry-After`` off the raised exception
  (NO top-level ``googleapiclient.http`` import — that leaks ``argparse`` via httplib2, DESIGN §1).
- a :class:`~contextvars.ContextVar` (:func:`current_policy` / :func:`activate`) — the per-call
  config channel. There is no central ``.execute()`` wrapper in core (32 call sites); the one
  chokepoint is the auth-layer ``requestBuilder`` (built once per process/lifespan). So both
  adapters wrap their core call in :func:`activate`, and the builder reads :func:`current_policy`
  at ``.execute()`` time — the policy flows down without threading a parameter through every call.

PURE core (DESIGN §1): stdlib only. This module must NEVER import ``fastmcp``, ``mcp``,
``argparse``, ``pydantic``, ``gsheets.models``, or ``googleapiclient.http`` at module top. Reading
``GSHEETS_BACKOFF_*`` env vars inside :meth:`RetryPolicy.from_env` mirrors the existing precedent
of ``core/errors.py`` reading ``GSHEETS_VERBOSE_ERRORS`` — this is operator config, not auth
credentials, so it is allowed in core.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import time
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Callable, Optional


# The strategies :meth:`RetryPolicy.next_delay` understands. ``"none"`` yields a zero delay
# (effectively no backoff between attempts); the others scale ``base_delay`` per attempt.
_STRATEGIES = frozenset({"none", "fixed", "exponential", "exponential_jitter"})

# The default retryable status set: the transient server errors + the rate-limit 429. A 403 is
# retried only when its reason is a rate-limit (see :meth:`RetryPolicy.is_retryable`).
_DEFAULT_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Google's rate-limit ``reason`` codes that make a 403 retryable (a permission 403 is NOT).
_RATE_LIMIT_403_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})


@dataclass(frozen=True)
class RetryPolicy:
    """An immutable retry/backoff policy read by :func:`execute_with_retry` (ISSUES.md #25).

    OFF by default: a freshly constructed ``RetryPolicy()`` has ``enabled=False``, so a 429/5xx
    fails fast (exactly one attempt). Retry is opted into explicitly — the preset, a granular
    config, or an env var (see :meth:`from_env`).

    Attributes:
        enabled: Master switch. When ``False`` the loop makes exactly one attempt and propagates.
        strategy: One of ``"none"``, ``"fixed"``, ``"exponential"``, ``"exponential_jitter"``
            (full-jitter exponential backoff; the preset default).
        max_retries: Retries AFTER the first try (total tries = ``1 + max_retries``).
        base_delay: Base seconds for the per-attempt delay (scaled by the strategy).
        max_delay: Per-attempt sleep cap (seconds) — no single backoff sleep exceeds this.
        total_deadline: Overall wall-clock cap (seconds) across all sleeps; ``None`` = no cap.
            If the NEXT sleep would push the cumulative wait past this, the loop gives up and
            re-raises rather than sleeping.
        honor_retry_after: When ``True``, a server-supplied ``Retry-After`` overrides the computed
            backoff for that attempt (bounded by ``retry_after_cap``).
        retry_after_cap: Cap (seconds) applied to a ``Retry-After`` value — Google can return a
            large one; this bounds it.
        retry_statuses: HTTP statuses that are retryable.
        retry_rate_limit_403: When ``True``, also retry a 403 whose reason is a rate-limit code
            (``rateLimitExceeded`` / ``userRateLimitExceeded``).
    """

    enabled: bool = False
    strategy: str = "exponential_jitter"
    max_retries: int = 4
    base_delay: float = 0.5
    max_delay: float = 30.0
    total_deadline: Optional[float] = 60.0
    honor_retry_after: bool = True
    retry_after_cap: float = 60.0
    retry_statuses: frozenset = field(default_factory=lambda: _DEFAULT_RETRY_STATUSES)
    retry_rate_limit_403: bool = True

    # --------------------------------------------------------------- delay / retryability

    def next_delay(self, attempt: int, retry_after: Optional[float], *, rng=random) -> float:
        """Seconds to sleep before retry ``attempt`` (1-based: ``1`` = the first retry).

        When ``honor_retry_after`` is set and the server supplied a ``retry_after``, that value
        (bounded by ``retry_after_cap``) is the base. Otherwise the base comes from the strategy:

        - ``"none"`` → ``0.0`` (no backoff between attempts);
        - ``"fixed"`` → ``base_delay``;
        - ``"exponential"`` → ``base_delay * 2**(attempt-1)``;
        - ``"exponential_jitter"`` → FULL JITTER: ``uniform(0, base_delay * 2**(attempt-1))``.

        The final value is capped at ``max_delay``. ``rng`` is injectable (defaults to the module
        ``random``) so tests can pin the jitter deterministically.
        """
        if self.honor_retry_after and retry_after is not None:
            base = min(retry_after, self.retry_after_cap)
        elif self.strategy == "none":
            base = 0.0
        elif self.strategy == "fixed":
            base = self.base_delay
        elif self.strategy == "exponential":
            base = self.base_delay * (2 ** (attempt - 1))
        else:  # "exponential_jitter" — full jitter over the exponential window.
            base = rng.uniform(0.0, self.base_delay * (2 ** (attempt - 1)))
        return min(base, self.max_delay)

    def is_retryable(self, status: Optional[int], reason: Optional[str]) -> bool:
        """True when an error with this ``status`` / Google ``reason`` is worth retrying.

        Retryable iff ``status`` is in :attr:`retry_statuses`, OR it is a ``403`` whose reason is
        a rate-limit code and :attr:`retry_rate_limit_403` is set. A ``None`` status (e.g. a
        non-HTTP transport exception) is never retryable here.
        """
        if status is None:
            return False
        if status in self.retry_statuses:
            return True
        return (
            status == 403
            and self.retry_rate_limit_403
            and reason in _RATE_LIMIT_403_REASONS
        )

    # ------------------------------------------------------------------- constructors

    @classmethod
    def default_preset(cls) -> "RetryPolicy":
        """The sensible catch-all preset: enabled full-jitter exponential backoff (ISSUES.md #25).

        Mirrors the pre-v0.4 always-on behavior (#7) but bounded by a 60 s overall deadline so a
        call can never silently run for minutes. This is what ``--default-backoff-strategy`` (CLI)
        and ``preset="default"`` (MCP) resolve to.
        """
        return cls(
            enabled=True,
            strategy="exponential_jitter",
            max_retries=4,
            base_delay=0.5,
            max_delay=30.0,
            total_deadline=60.0,
            honor_retry_after=True,
            retry_after_cap=60.0,
        )

    @classmethod
    def from_env(cls, **overrides) -> "RetryPolicy":
        """Resolve a policy from ``GSHEETS_BACKOFF_*`` env vars, then apply explicit overrides.

        Precedence: field defaults < env vars < explicit ``overrides`` (an override wins; a ``None``
        override is ignored so a caller can pass ``None`` to mean "leave at the env/default value").

        ENABLING SEMANTICS (off unless explicitly turned on). The result is :data:`DISABLED` unless
        retry is enabled by ANY of:

        - ``GSHEETS_BACKOFF_STRATEGY`` set to a non-``"none"`` value;
        - a retries-count env var set to an int ``> 0`` (``== 0`` forces DISABLED) — this is the
          canonical ``GSHEETS_BACKOFF_MAX_RETRIES`` or the honored legacy alias ``GSHEETS_MAX_RETRIES``
          (canonical wins when both are set), treated identically so that setting a retry COUNT never
          silently leaves retry off;
        - an override carrying ``enabled=True``.

        A retries-count env var ``> 0`` with no ``GSHEETS_BACKOFF_STRATEGY`` enables retry with that
        many retries and the default ``"exponential_jitter"`` strategy. Parse failures fall back to
        the field default (never crash). ``GSHEETS_BACKOFF_DEADLINE`` of ``<= 0`` or ``"none"``
        means "no overall cap" (``total_deadline=None``).
        """
        env = os.environ

        # --- strategy: validate against the known set; bad value -> field default.
        strategy_raw = env.get("GSHEETS_BACKOFF_STRATEGY")
        strategy_set = False
        strategy = cls.strategy
        if strategy_raw is not None and strategy_raw.strip():
            cand = strategy_raw.strip().lower()
            if cand in _STRATEGIES:
                strategy = cand
                strategy_set = True
        # A non-"none" strategy env var is one of the enable signals.
        strategy_enables = strategy_set and strategy != "none"

        # --- max_retries: the canonical name wins, else the legacy alias. Whichever is set is the
        #     "retries env signal": a value > 0 is itself an enable signal, and == 0 a disable
        #     signal — identically for the canonical and legacy vars (no surprising asymmetry where
        #     setting a retry COUNT silently leaves retry off).
        canonical_retries = _env_int(env, "GSHEETS_BACKOFF_MAX_RETRIES")
        legacy_retries = _env_int(env, "GSHEETS_MAX_RETRIES")
        retries_env = canonical_retries if canonical_retries is not None else legacy_retries
        if retries_env is not None:
            max_retries = max(0, retries_env)
        else:
            max_retries = cls.max_retries

        retries_enables = retries_env is not None and retries_env > 0
        retries_disables = retries_env is not None and retries_env == 0

        # --- numeric / bool fields (each falls back to the field default on a parse failure).
        base_delay = _env_float(env, "GSHEETS_BACKOFF_BASE_DELAY", cls.base_delay)
        max_delay = _env_float(env, "GSHEETS_BACKOFF_MAX_DELAY", cls.max_delay)
        retry_after_cap = _env_float(env, "GSHEETS_BACKOFF_RETRY_AFTER_CAP", cls.retry_after_cap)
        honor_retry_after = _env_bool(
            env, "GSHEETS_BACKOFF_HONOR_RETRY_AFTER", cls.honor_retry_after
        )
        total_deadline = _env_deadline(env, "GSHEETS_BACKOFF_DEADLINE", cls.total_deadline)

        # --- decide enablement from env signals (an override may still flip it on below).
        override_enabled = overrides.get("enabled")
        enabled = bool(strategy_enables or retries_enables or override_enabled is True)
        if retries_disables and not strategy_enables and override_enabled is not True:
            enabled = False

        # If enabled purely via a retries-count env var (no strategy env var), default the strategy
        # to the sensible jittered exponential rather than the bare field default.
        if retries_enables and not strategy_set:
            strategy = "exponential_jitter"

        policy = cls(
            enabled=enabled,
            strategy=strategy,
            max_retries=max_retries,
            base_delay=base_delay,
            max_delay=max_delay,
            total_deadline=total_deadline,
            honor_retry_after=honor_retry_after,
            retry_after_cap=retry_after_cap,
        )

        # --- apply explicit overrides last (None values ignored). ``enabled`` is handled here
        #     too so an override flips the master switch without re-deriving from env.
        clean = {k: v for k, v in overrides.items() if v is not None}
        if clean:
            policy = replace(policy, **clean)
        return policy


#: The true "off": a disabled policy with no looping and no overall deadline. :func:`current_policy`
#: returns this when no policy is active, and :func:`execute_with_retry` short-circuits on it.
RetryPolicy.DISABLED = RetryPolicy(enabled=False, max_retries=0, total_deadline=None)


# --------------------------------------------------------------------------- env parse helpers


def _env_int(env, name: str) -> Optional[int]:
    """Parse ``env[name]`` as an int, or ``None`` (absent / blank / unparseable)."""
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _env_float(env, name: str, default: float) -> float:
    """Parse ``env[name]`` as a float, falling back to ``default`` on absence/parse failure."""
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_bool(env, name: str, default: bool) -> bool:
    """Parse ``env[name]`` as a 1/0/true/false bool, falling back to ``default``."""
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_deadline(env, name: str, default: Optional[float]) -> Optional[float]:
    """Parse a ``total_deadline`` env var: ``<= 0`` or ``"none"`` => ``None`` (no overall cap)."""
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    text = raw.strip().lower()
    if text == "none":
        return None
    try:
        value = float(text)
    except ValueError:
        return default
    return None if value <= 0 else value


# --------------------------------------------------------------------------- contextvar plumbing
#
# There is no central ``.execute()`` wrapper in core (32 call sites); the one chokepoint is the
# auth-layer ``requestBuilder``. So per-call config flows via this contextvar: an adapter wraps
# its core call in :func:`activate`, and the builder reads :func:`current_policy` at execute time.

_ACTIVE_POLICY: ContextVar[Optional[RetryPolicy]] = ContextVar(
    "gsheets_retry_policy", default=None
)


def current_policy() -> RetryPolicy:
    """The policy active for the current context, or :data:`RetryPolicy.DISABLED` if none.

    This is what the auth-layer request builder calls at ``.execute()`` time, and what
    :func:`execute_with_retry` defaults to when ``policy`` is ``None``. Off by default: with no
    :func:`activate` in scope this returns the disabled policy, so a bare core call (no adapter
    wrapping) fails fast on a 429.
    """
    return _ACTIVE_POLICY.get() or RetryPolicy.DISABLED


@contextlib.contextmanager
def activate(policy: RetryPolicy):
    """Bind ``policy`` as the active retry policy for the duration of the ``with`` block.

    Both adapters wrap their ``build_services + core-call`` block in this so that the
    ``.execute()`` deep inside core reads the right policy via :func:`current_policy`. The reset
    is contextvar-token based, so nested/concurrent activations restore cleanly.
    """
    token = _ACTIVE_POLICY.set(policy)
    try:
        yield
    finally:
        _ACTIVE_POLICY.reset(token)


# --------------------------------------------------------------------------- the retry loop


def _extract_http_signal(exc: BaseException) -> tuple[Optional[int], Optional[str], Optional[float]]:
    """Duck-type ``(status, reason, retry_after)`` off an exception — no ``HttpError`` import.

    A top-level ``from googleapiclient.http import ...`` would leak ``argparse`` into ``sys.modules``
    via httplib2 and break the boundary guard (DESIGN §1), so we inspect attributes the same way
    ``core/errors.py`` does instead of ``isinstance``-checking. A non-HTTP exception yields
    ``(None, None, None)`` — which :meth:`RetryPolicy.is_retryable` treats as not retryable.

    - ``status`` comes off ``exc.resp.status`` (the live httplib2 response googleapiclient attaches).
    - ``retry_after`` is parsed from the response headers' ``retry-after`` (seconds form only;
      an HTTP-date form is ignored and the computed backoff is used instead).
    - ``reason`` is decoded from the JSON error body (``error.status`` then ``error.errors[0].reason``),
      matching ``core/errors.py``'s ``_extract_reason``.
    """
    resp = getattr(exc, "resp", None)

    status: Optional[int] = None
    raw_status = getattr(resp, "status", None)
    if raw_status is not None:
        try:
            status = int(raw_status)
        except (TypeError, ValueError):
            status = None

    # Retry-After header (case-insensitive; httplib2.Response is a dict of lower-cased keys).
    retry_after: Optional[float] = None
    if resp is not None:
        header = None
        try:
            getter = getattr(resp, "get", None)
            if callable(getter):
                header = getter("retry-after") or getter("Retry-After")
        except Exception:  # pragma: no cover - defensive; a weird resp must not crash the loop
            header = None
        if header is not None:
            try:
                value = float(str(header).strip())
                retry_after = value if value >= 0 else None
            except (TypeError, ValueError):
                retry_after = None  # HTTP-date form (or junk) -> fall back to computed backoff.

    reason = _decode_reason(exc)
    return status, reason, retry_after


def _decode_reason(exc: BaseException) -> Optional[str]:
    """Best-effort Google ``reason`` decode from an ``HttpError``-shaped ``exc.content`` JSON body.

    Mirrors ``core/errors.py``: prefer the canonical ``error.status`` (e.g. ``RESOURCE_EXHAUSTED``,
    ``PERMISSION_DENIED``), else the older ``error.errors[0].reason`` (e.g. ``rateLimitExceeded``).
    Never raises.
    """
    content = getattr(exc, "content", None)
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
    if not isinstance(data, dict):
        return None
    error_obj = data.get("error")
    if not isinstance(error_obj, dict):
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


def execute_with_retry(
    call: Callable[[], object],
    policy: Optional[RetryPolicy] = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    log: Optional[Callable[..., None]] = None,
):
    """Invoke ``call()`` with retry/backoff per ``policy`` (ISSUES.md #25).

    This is the loop the auth-layer request builder defers to. With retry OFF (the default) it is a
    pure pass-through — exactly one attempt, exceptions propagate untouched. With retry ON it
    retries a 429/5xx (and rate-limit-403) per the policy: computing each delay via
    :meth:`RetryPolicy.next_delay` (honoring a server ``Retry-After`` when present), capping each
    sleep at ``max_delay``, and giving up if the next sleep would breach ``total_deadline``.

    Args:
        call: A zero-arg callable performing the single API ``.execute()``.
        policy: The policy to apply; ``None`` reads the active one via :func:`current_policy`.
        sleep: Injectable sleeper (tests pass a no-op recorder so they never really wait).
        monotonic: Injectable monotonic clock (tests advance it to drive the deadline cutoff).
        log: Optional ``log(attempt=, delay=, status=)`` callback, invoked once per retry sleep
            (the auth layer wires this to a stderr line gated by ``GSHEETS_BACKOFF_LOG``).

    Returns:
        Whatever ``call()`` returns on the first success.

    Raises:
        The final exception (after exhausting retries / hitting the deadline / a non-retryable
        error), annotated with ``_gsheets_retry_attempts`` (retries performed) and
        ``_gsheets_retry_waited_ms`` (cumulative sleep, ms) so the error path can surface them
        (``classify_google_error`` reads them into the structured ``SheetsError``).
    """
    if policy is None:
        policy = current_policy()

    # Off: exactly one attempt, no looping, exceptions propagate verbatim (the true fail-fast).
    if not policy.enabled:
        return call()

    start = monotonic()
    waited = 0.0
    retries_done = 0

    while True:
        try:
            return call()
        except Exception as exc:
            status, reason, retry_after = _extract_http_signal(exc)

            # Exhausted, or a non-retryable error (incl. any non-HTTP exception) -> give up.
            if retries_done >= policy.max_retries or not policy.is_retryable(status, reason):
                _annotate(exc, retries_done, waited)
                raise

            delay = policy.next_delay(retries_done + 1, retry_after)

            # Overall wall-clock guard: never sleep past the deadline — re-raise instead so a
            # call can't silently run for minutes (ISSUES.md #25's key missing guard).
            if (
                policy.total_deadline is not None
                and (monotonic() - start) + delay > policy.total_deadline
            ):
                _annotate(exc, retries_done, waited)
                raise

            if log is not None:
                try:
                    log(attempt=retries_done + 1, delay=delay, status=status)
                except Exception:  # pragma: no cover - logging must never break the loop
                    pass

            sleep(delay)
            waited += delay
            retries_done += 1


def _annotate(exc: BaseException, retries_done: int, waited: float) -> None:
    """Stamp retry telemetry onto ``exc`` for the error path to surface (ISSUES.md #25).

    ``classify_google_error`` reads ``_gsheets_retry_attempts`` / ``_gsheets_retry_waited_ms`` off
    the raised ``HttpError`` and folds them into the structured ``SheetsError`` (``retries`` /
    ``waited_ms``). Best-effort: a frozen/exotic exception that rejects attribute assignment must
    not mask the original failure.
    """
    try:
        exc._gsheets_retry_attempts = int(retries_done)
        exc._gsheets_retry_waited_ms = int(round(waited * 1000))
    except Exception:  # pragma: no cover - defensive
        pass
