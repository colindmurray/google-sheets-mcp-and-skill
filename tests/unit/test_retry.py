"""Unit tests for the pure retry/backoff mechanism (ISSUES.md #25).

All tests are deterministic and NEVER sleep: :func:`execute_with_retry` takes injectable
``sleep`` / ``monotonic`` / ``rng`` so the loop, the jitter, and the wall-clock deadline are
driven by recorders and fake clocks. We cover:

- off-by-default (``current_policy()`` is ``DISABLED`` with no contextvar; ``execute_with_retry``
  on ``DISABLED`` calls once and propagates without looping);
- ``default_preset`` field values + the ``DISABLED`` constant;
- ``from_env`` resolution: legacy ``GSHEETS_MAX_RETRIES`` opt-in (``>0``) / disable (``==0``),
  ``GSHEETS_BACKOFF_STRATEGY`` enabling, canonical vs legacy retries, overrides, parse failures;
- ``next_delay`` per strategy (deterministic rng) + ``max_delay`` cap + ``honor_retry_after`` + cap;
- ``is_retryable`` incl. rate-limit-403;
- the loop: succeed-first-try, fail-twice-then-succeed, exhaust-then-raise (annotations set),
  ``total_deadline`` cutoff, non-retryable fail-fast;
- ``activate`` / ``current_policy`` contextvar isolation.

Fake 429-shaped exceptions are duck-typed (``.resp.status`` + ``.content`` JSON) the way a real
``googleapiclient`` ``HttpError`` looks — the loop never imports ``HttpError`` (DESIGN §1).
"""

from __future__ import annotations

import json

import pytest

from gsheets.core import retry as retry_mod
from gsheets.core.retry import (
    RetryPolicy,
    activate,
    current_policy,
    execute_with_retry,
)


# --------------------------------------------------------------------------- fakes / helpers


class _FakeResp(dict):
    """Stand-in for an httplib2 ``Response`` (a dict subclass) exposing ``.status`` + headers."""

    def __init__(self, status, headers=None):
        super().__init__(headers or {})
        self.status = status


class FakeHttpError(Exception):
    """A duck-typed ``HttpError`` (``.resp.status`` + JSON ``.content``) for the loop to inspect."""

    def __init__(self, status, *, reason=None, retry_after=None, message="boom"):
        headers = {}
        if retry_after is not None:
            headers["retry-after"] = str(retry_after)
        self.resp = _FakeResp(status, headers)
        error_obj = {"code": status, "message": message}
        if reason is not None:
            error_obj["status"] = reason
        self.content = json.dumps({"error": error_obj}).encode("utf-8")
        super().__init__(f"{status} {message}")


class _Recorder:
    """Records ``sleep`` calls and advances a fake monotonic clock by the slept amount."""

    def __init__(self):
        self.slept: list[float] = []
        self.now = 0.0

    def sleep(self, secs):
        self.slept.append(secs)
        self.now += secs

    def monotonic(self):
        return self.now


class _FixedRng:
    """Deterministic ``rng`` — ``uniform(a, b)`` returns ``b`` (the top of the jitter window)."""

    @staticmethod
    def uniform(a, b):
        return b


def _make_calls(*outcomes):
    """A zero-arg callable yielding ``outcomes`` in order; an exception value is raised."""
    seq = list(outcomes)
    state = {"i": 0}

    def call():
        item = seq[state["i"]]
        state["i"] += 1
        if isinstance(item, BaseException):
            raise item
        return item

    return call, state


@pytest.fixture(autouse=True)
def _clean_backoff_env(monkeypatch):
    """Strip every ``GSHEETS_BACKOFF_*`` / legacy var so ``from_env`` tests start from defaults."""
    for name in (
        "GSHEETS_BACKOFF_STRATEGY",
        "GSHEETS_BACKOFF_MAX_RETRIES",
        "GSHEETS_MAX_RETRIES",
        "GSHEETS_BACKOFF_BASE_DELAY",
        "GSHEETS_BACKOFF_MAX_DELAY",
        "GSHEETS_BACKOFF_DEADLINE",
        "GSHEETS_BACKOFF_HONOR_RETRY_AFTER",
        "GSHEETS_BACKOFF_RETRY_AFTER_CAP",
        "GSHEETS_BACKOFF_LOG",
    ):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- off-by-default


def test_default_policy_is_disabled():
    p = RetryPolicy()
    assert p.enabled is False
    assert p.strategy == "exponential_jitter"  # the strategy is set, but the master switch is off
    assert p.max_retries == 4


def test_current_policy_is_disabled_with_no_contextvar():
    # No activate() in scope -> the off policy.
    assert current_policy() is RetryPolicy.DISABLED


def test_disabled_constant_is_the_true_off():
    d = RetryPolicy.DISABLED
    assert d.enabled is False
    assert d.max_retries == 0
    assert d.total_deadline is None


def test_execute_with_disabled_calls_once_and_propagates():
    rec = _Recorder()
    call, state = _make_calls(FakeHttpError(429))
    with pytest.raises(FakeHttpError):
        execute_with_retry(call, RetryPolicy.DISABLED, sleep=rec.sleep, monotonic=rec.monotonic)
    assert state["i"] == 1  # exactly one attempt
    assert rec.slept == []  # never slept


def test_execute_with_no_policy_defaults_to_current_disabled():
    rec = _Recorder()
    call, state = _make_calls(FakeHttpError(503))
    # No policy arg + no activate() -> resolves to DISABLED -> one attempt, propagate.
    with pytest.raises(FakeHttpError):
        execute_with_retry(call, sleep=rec.sleep, monotonic=rec.monotonic)
    assert state["i"] == 1


def test_disabled_returns_success_value():
    call, _ = _make_calls("ok")
    assert execute_with_retry(call, RetryPolicy.DISABLED) == "ok"


# --------------------------------------------------------------------------- default_preset


def test_default_preset_values():
    p = RetryPolicy.default_preset()
    assert p.enabled is True
    assert p.strategy == "exponential_jitter"
    assert p.max_retries == 4
    assert p.base_delay == 0.5
    assert p.max_delay == 30.0
    assert p.total_deadline == 60.0
    assert p.honor_retry_after is True
    assert p.retry_after_cap == 60.0
    assert p.retry_statuses == frozenset({429, 500, 502, 503, 504})
    assert p.retry_rate_limit_403 is True


def test_policy_is_frozen():
    p = RetryPolicy()
    with pytest.raises(Exception):
        p.enabled = True  # type: ignore[misc]


# --------------------------------------------------------------------------- from_env


def test_from_env_bare_is_disabled():
    p = RetryPolicy.from_env()
    assert p.enabled is False
    # No env, no overrides -> field defaults with the master switch off.
    assert p.max_retries == 4
    assert p.strategy == "exponential_jitter"
    assert p.total_deadline == 60.0


def test_from_env_legacy_max_retries_positive_enables(monkeypatch):
    monkeypatch.setenv("GSHEETS_MAX_RETRIES", "3")
    p = RetryPolicy.from_env()
    assert p.enabled is True
    assert p.max_retries == 3
    assert p.strategy == "exponential_jitter"  # legacy enable defaults to jittered exponential


def test_from_env_legacy_max_retries_zero_disables(monkeypatch):
    monkeypatch.setenv("GSHEETS_MAX_RETRIES", "0")
    p = RetryPolicy.from_env()
    assert p.enabled is False
    assert p.max_retries == 0


def test_from_env_strategy_none_does_not_enable(monkeypatch):
    monkeypatch.setenv("GSHEETS_BACKOFF_STRATEGY", "none")
    assert RetryPolicy.from_env().enabled is False


def test_from_env_strategy_nonnone_enables(monkeypatch):
    monkeypatch.setenv("GSHEETS_BACKOFF_STRATEGY", "fixed")
    p = RetryPolicy.from_env()
    assert p.enabled is True
    assert p.strategy == "fixed"


def test_from_env_canonical_max_retries_preferred_over_legacy(monkeypatch):
    monkeypatch.setenv("GSHEETS_BACKOFF_STRATEGY", "exponential")
    monkeypatch.setenv("GSHEETS_BACKOFF_MAX_RETRIES", "7")
    monkeypatch.setenv("GSHEETS_MAX_RETRIES", "2")
    p = RetryPolicy.from_env()
    assert p.enabled is True
    assert p.max_retries == 7  # canonical wins


def test_from_env_canonical_max_retries_positive_enables(monkeypatch):
    # The canonical retries var enables retry on its own, identically to the legacy alias — setting
    # a retry COUNT must never silently leave retry off (symmetry, ISSUES.md #25 review).
    monkeypatch.setenv("GSHEETS_BACKOFF_MAX_RETRIES", "5")
    p = RetryPolicy.from_env()
    assert p.enabled is True
    assert p.max_retries == 5
    assert p.strategy == "exponential_jitter"  # count-only enable defaults to jittered exponential


def test_from_env_canonical_max_retries_zero_disables(monkeypatch):
    monkeypatch.setenv("GSHEETS_BACKOFF_MAX_RETRIES", "0")
    p = RetryPolicy.from_env()
    assert p.enabled is False
    assert p.max_retries == 0


def test_from_env_numeric_and_bool_fields(monkeypatch):
    monkeypatch.setenv("GSHEETS_BACKOFF_STRATEGY", "exponential")
    monkeypatch.setenv("GSHEETS_BACKOFF_BASE_DELAY", "1.5")
    monkeypatch.setenv("GSHEETS_BACKOFF_MAX_DELAY", "10")
    monkeypatch.setenv("GSHEETS_BACKOFF_RETRY_AFTER_CAP", "20")
    monkeypatch.setenv("GSHEETS_BACKOFF_HONOR_RETRY_AFTER", "false")
    p = RetryPolicy.from_env()
    assert p.base_delay == 1.5
    assert p.max_delay == 10.0
    assert p.retry_after_cap == 20.0
    assert p.honor_retry_after is False


def test_from_env_deadline_none_and_nonpositive(monkeypatch):
    monkeypatch.setenv("GSHEETS_BACKOFF_STRATEGY", "fixed")
    monkeypatch.setenv("GSHEETS_BACKOFF_DEADLINE", "none")
    assert RetryPolicy.from_env().total_deadline is None
    monkeypatch.setenv("GSHEETS_BACKOFF_DEADLINE", "0")
    assert RetryPolicy.from_env().total_deadline is None
    monkeypatch.setenv("GSHEETS_BACKOFF_DEADLINE", "-5")
    assert RetryPolicy.from_env().total_deadline is None
    monkeypatch.setenv("GSHEETS_BACKOFF_DEADLINE", "45")
    assert RetryPolicy.from_env().total_deadline == 45.0


def test_from_env_parse_failures_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("GSHEETS_BACKOFF_STRATEGY", "garbage")  # invalid -> not set, no enable
    monkeypatch.setenv("GSHEETS_BACKOFF_BASE_DELAY", "notafloat")
    monkeypatch.setenv("GSHEETS_BACKOFF_MAX_RETRIES", "NaN")
    p = RetryPolicy.from_env()
    assert p.enabled is False  # garbage strategy doesn't enable
    assert p.strategy == "exponential_jitter"  # field default
    assert p.base_delay == 0.5  # field default
    assert p.max_retries == 4  # unparseable -> field default


def test_from_env_override_enabled_true_wins(monkeypatch):
    p = RetryPolicy.from_env(enabled=True, max_retries=2, strategy="fixed")
    assert p.enabled is True
    assert p.max_retries == 2
    assert p.strategy == "fixed"


def test_from_env_none_override_is_ignored(monkeypatch):
    monkeypatch.setenv("GSHEETS_BACKOFF_STRATEGY", "exponential")
    monkeypatch.setenv("GSHEETS_BACKOFF_BASE_DELAY", "2.0")
    p = RetryPolicy.from_env(base_delay=None, max_delay=None)
    assert p.base_delay == 2.0  # None override ignored -> keeps env value


def test_from_env_override_does_not_disable_when_env_enabled(monkeypatch):
    monkeypatch.setenv("GSHEETS_MAX_RETRIES", "3")
    # An override that doesn't carry enabled=False keeps the env-driven enablement.
    p = RetryPolicy.from_env(base_delay=1.0)
    assert p.enabled is True


def test_from_env_legacy_zero_with_strategy_still_enables(monkeypatch):
    # An explicit strategy enables even if legacy retries == 0 (strategy enable wins).
    monkeypatch.setenv("GSHEETS_MAX_RETRIES", "0")
    monkeypatch.setenv("GSHEETS_BACKOFF_STRATEGY", "fixed")
    p = RetryPolicy.from_env()
    assert p.enabled is True


# --------------------------------------------------------------------------- next_delay


def test_next_delay_none_strategy_is_zero():
    p = RetryPolicy(strategy="none", base_delay=0.5)
    assert p.next_delay(1, None) == 0.0
    assert p.next_delay(5, None) == 0.0


def test_next_delay_fixed():
    p = RetryPolicy(strategy="fixed", base_delay=0.7, max_delay=100)
    assert p.next_delay(1, None) == 0.7
    assert p.next_delay(4, None) == 0.7  # fixed: attempt-independent


def test_next_delay_exponential():
    p = RetryPolicy(strategy="exponential", base_delay=0.5, max_delay=100)
    assert p.next_delay(1, None) == 0.5  # 0.5 * 2**0
    assert p.next_delay(2, None) == 1.0  # 0.5 * 2**1
    assert p.next_delay(3, None) == 2.0  # 0.5 * 2**2
    assert p.next_delay(4, None) == 4.0  # 0.5 * 2**3


def test_next_delay_exponential_jitter_uses_rng_top():
    p = RetryPolicy(strategy="exponential_jitter", base_delay=0.5, max_delay=100)
    # _FixedRng.uniform(0, X) -> X, i.e. the top of the full-jitter window.
    assert p.next_delay(1, None, rng=_FixedRng) == 0.5
    assert p.next_delay(3, None, rng=_FixedRng) == 2.0


def test_next_delay_capped_at_max_delay():
    p = RetryPolicy(strategy="exponential", base_delay=10.0, max_delay=15.0)
    assert p.next_delay(1, None) == 10.0
    assert p.next_delay(2, None) == 15.0  # 20.0 capped to 15.0
    assert p.next_delay(5, None) == 15.0  # way over -> capped


def test_next_delay_honors_retry_after():
    p = RetryPolicy(strategy="exponential", base_delay=0.5, honor_retry_after=True,
                    retry_after_cap=60, max_delay=120)
    # Retry-After overrides the computed backoff for that attempt.
    assert p.next_delay(1, 7.0) == 7.0
    assert p.next_delay(3, 12.0) == 12.0


def test_next_delay_retry_after_capped():
    p = RetryPolicy(strategy="fixed", base_delay=0.5, honor_retry_after=True,
                    retry_after_cap=30, max_delay=120)
    assert p.next_delay(1, 600.0) == 30.0  # huge Retry-After bounded by cap


def test_next_delay_retry_after_then_max_delay_cap():
    # retry_after_cap bounds the header; max_delay is the final cap (lower one wins).
    p = RetryPolicy(strategy="fixed", honor_retry_after=True, retry_after_cap=50, max_delay=20)
    assert p.next_delay(1, 40.0) == 20.0


def test_next_delay_ignores_retry_after_when_disabled():
    p = RetryPolicy(strategy="fixed", base_delay=0.9, honor_retry_after=False, max_delay=100)
    assert p.next_delay(1, 99.0) == 0.9  # header ignored -> strategy delay


# --------------------------------------------------------------------------- is_retryable


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_is_retryable_default_statuses(status):
    assert RetryPolicy().is_retryable(status, None) is True


@pytest.mark.parametrize("status", [200, 400, 401, 404, 418])
def test_is_retryable_rejects_non_transient(status):
    assert RetryPolicy().is_retryable(status, None) is False


def test_is_retryable_none_status_is_false():
    assert RetryPolicy().is_retryable(None, None) is False


@pytest.mark.parametrize("reason", ["rateLimitExceeded", "userRateLimitExceeded"])
def test_is_retryable_403_rate_limit(reason):
    assert RetryPolicy().is_retryable(403, reason) is True


def test_is_retryable_403_permission_denied_is_false():
    assert RetryPolicy().is_retryable(403, "PERMISSION_DENIED") is False
    assert RetryPolicy().is_retryable(403, None) is False


def test_is_retryable_403_rate_limit_off_when_disabled():
    p = RetryPolicy(retry_rate_limit_403=False)
    assert p.is_retryable(403, "rateLimitExceeded") is False


# --------------------------------------------------------------------------- the loop


def test_loop_succeeds_first_try():
    rec = _Recorder()
    call, state = _make_calls("result")
    out = execute_with_retry(
        call, RetryPolicy.default_preset(), sleep=rec.sleep, monotonic=rec.monotonic
    )
    assert out == "result"
    assert state["i"] == 1
    assert rec.slept == []


def test_loop_fail_twice_then_succeed():
    rec = _Recorder()
    call, state = _make_calls(
        FakeHttpError(429), FakeHttpError(503), "finally"
    )
    policy = RetryPolicy(enabled=True, strategy="fixed", base_delay=1.0, max_retries=5,
                         max_delay=100, total_deadline=None)
    out = execute_with_retry(call, policy, sleep=rec.sleep, monotonic=rec.monotonic)
    assert out == "finally"
    assert state["i"] == 3  # two failures + one success
    assert rec.slept == [1.0, 1.0]  # slept before each retry


def test_loop_exhausts_then_raises_with_annotations():
    rec = _Recorder()
    err = FakeHttpError(429)
    call, state = _make_calls(err, err, err, err, err, err)
    policy = RetryPolicy(enabled=True, strategy="fixed", base_delay=2.0, max_retries=3,
                         max_delay=100, total_deadline=None)
    with pytest.raises(FakeHttpError) as excinfo:
        execute_with_retry(call, policy, sleep=rec.sleep, monotonic=rec.monotonic)
    # 1 initial + 3 retries = 4 attempts; 3 sleeps.
    assert state["i"] == 4
    assert rec.slept == [2.0, 2.0, 2.0]
    raised = excinfo.value
    assert raised._gsheets_retry_attempts == 3
    assert raised._gsheets_retry_waited_ms == 6000  # 3 * 2.0s == 6000ms


def test_loop_non_retryable_fails_fast_with_zero_retries():
    rec = _Recorder()
    err = FakeHttpError(400, reason="INVALID_ARGUMENT")
    call, state = _make_calls(err)
    policy = RetryPolicy.default_preset()
    with pytest.raises(FakeHttpError) as excinfo:
        execute_with_retry(call, policy, sleep=rec.sleep, monotonic=rec.monotonic)
    assert state["i"] == 1  # no retry on a 400
    assert rec.slept == []
    assert excinfo.value._gsheets_retry_attempts == 0
    assert excinfo.value._gsheets_retry_waited_ms == 0


def test_loop_non_http_exception_not_retried():
    rec = _Recorder()
    call, state = _make_calls(ValueError("boom"))
    policy = RetryPolicy.default_preset()
    with pytest.raises(ValueError):
        execute_with_retry(call, policy, sleep=rec.sleep, monotonic=rec.monotonic)
    assert state["i"] == 1  # status None -> not retryable -> single attempt
    assert rec.slept == []


def test_loop_total_deadline_cutoff():
    rec = _Recorder()
    err = FakeHttpError(429)
    call, state = _make_calls(err, err, err, err, err)
    # base 10s exponential; deadline 12s. First retry delay = 10s (cumulative 0+10 <= 12, sleeps).
    # Second retry delay = 20s -> capped to max_delay 100 stays 20 -> (10 + 20) > 12 -> re-raise.
    policy = RetryPolicy(enabled=True, strategy="exponential", base_delay=10.0, max_retries=5,
                         max_delay=100, total_deadline=12.0)
    with pytest.raises(FakeHttpError) as excinfo:
        execute_with_retry(call, policy, sleep=rec.sleep, monotonic=rec.monotonic)
    assert rec.slept == [10.0]  # only the first retry slept; the second would breach the deadline
    assert excinfo.value._gsheets_retry_attempts == 1
    assert excinfo.value._gsheets_retry_waited_ms == 10000


def test_loop_honors_retry_after_header():
    rec = _Recorder()
    call, state = _make_calls(
        FakeHttpError(429, retry_after=5), "ok"
    )
    policy = RetryPolicy(enabled=True, strategy="exponential", base_delay=0.5, max_retries=3,
                         honor_retry_after=True, retry_after_cap=60, max_delay=100,
                         total_deadline=None)
    out = execute_with_retry(call, policy, sleep=rec.sleep, monotonic=rec.monotonic)
    assert out == "ok"
    assert rec.slept == [5.0]  # used the Retry-After header value, not the 0.5 exponential base


def test_loop_retries_rate_limit_403():
    rec = _Recorder()
    call, state = _make_calls(
        FakeHttpError(403, reason="rateLimitExceeded"), "ok"
    )
    policy = RetryPolicy(enabled=True, strategy="fixed", base_delay=1.0, max_retries=3,
                         max_delay=100, total_deadline=None)
    out = execute_with_retry(call, policy, sleep=rec.sleep, monotonic=rec.monotonic)
    assert out == "ok"
    assert state["i"] == 2


def test_loop_does_not_retry_permission_403():
    rec = _Recorder()
    call, state = _make_calls(FakeHttpError(403, reason="PERMISSION_DENIED"))
    policy = RetryPolicy.default_preset()
    with pytest.raises(FakeHttpError):
        execute_with_retry(call, policy, sleep=rec.sleep, monotonic=rec.monotonic)
    assert state["i"] == 1


def test_loop_invokes_log_callback_per_retry():
    rec = _Recorder()
    logged: list[dict] = []
    call, state = _make_calls(FakeHttpError(429), FakeHttpError(429), "ok")
    policy = RetryPolicy(enabled=True, strategy="fixed", base_delay=1.0, max_retries=3,
                         max_delay=100, total_deadline=None)
    execute_with_retry(
        call, policy, sleep=rec.sleep, monotonic=rec.monotonic,
        log=lambda **kw: logged.append(kw),
    )
    assert len(logged) == 2
    assert logged[0] == {"attempt": 1, "delay": 1.0, "status": 429}
    assert logged[1] == {"attempt": 2, "delay": 1.0, "status": 429}


def test_loop_log_exception_does_not_break_loop():
    rec = _Recorder()
    call, state = _make_calls(FakeHttpError(429), "ok")
    policy = RetryPolicy(enabled=True, strategy="fixed", base_delay=1.0, max_retries=3,
                         max_delay=100, total_deadline=None)

    def boom_log(**kw):
        raise RuntimeError("logging exploded")

    out = execute_with_retry(
        call, policy, sleep=rec.sleep, monotonic=rec.monotonic, log=boom_log
    )
    assert out == "ok"  # a broken logger must not abort the retry


def test_loop_explicit_policy_beats_active_contextvar():
    rec = _Recorder()
    call, _ = _make_calls(FakeHttpError(429))
    # An enabled policy is active, but the explicit DISABLED arg must win -> single attempt.
    with activate(RetryPolicy.default_preset()):
        with pytest.raises(FakeHttpError):
            execute_with_retry(call, RetryPolicy.DISABLED, sleep=rec.sleep, monotonic=rec.monotonic)
    assert rec.slept == []


def test_loop_reads_active_policy_when_none_passed():
    rec = _Recorder()
    call, state = _make_calls(FakeHttpError(429), "ok")
    policy = RetryPolicy(enabled=True, strategy="fixed", base_delay=1.0, max_retries=3,
                         max_delay=100, total_deadline=None)
    with activate(policy):
        out = execute_with_retry(call, sleep=rec.sleep, monotonic=rec.monotonic)
    assert out == "ok"
    assert state["i"] == 2


# --------------------------------------------------------------------------- contextvar isolation


def test_activate_sets_and_restores_policy():
    assert current_policy() is RetryPolicy.DISABLED
    preset = RetryPolicy.default_preset()
    with activate(preset):
        assert current_policy() is preset
    assert current_policy() is RetryPolicy.DISABLED  # cleanly reset on exit


def test_activate_nested_restores_outer():
    outer = RetryPolicy(enabled=True, strategy="fixed", max_retries=1)
    inner = RetryPolicy(enabled=True, strategy="exponential", max_retries=9)
    with activate(outer):
        assert current_policy() is outer
        with activate(inner):
            assert current_policy() is inner
        assert current_policy() is outer  # inner reset restores the outer policy
    assert current_policy() is RetryPolicy.DISABLED


def test_activate_resets_even_on_exception():
    with pytest.raises(RuntimeError):
        with activate(RetryPolicy.default_preset()):
            raise RuntimeError("boom")
    assert current_policy() is RetryPolicy.DISABLED


# --------------------------------------------------------------------------- error-path integration


def test_classify_reads_retry_annotations_off_http_error():
    # The loop annotates the raised HttpError; classify_google_error folds them into SheetsError.
    from gsheets.core.errors import classify_google_error

    err = FakeHttpError(429, reason="RESOURCE_EXHAUSTED")
    retry_mod._annotate(err, retries_done=3, waited=6.0)
    sheets_err = classify_google_error(err)
    assert sheets_err.retries == 3
    assert sheets_err.waited_ms == 6000
    d = sheets_err.to_dict()
    assert d["retries"] == 3
    assert d["waitedMs"] == 6000


def test_classify_without_annotations_omits_retry_fields():
    from gsheets.core.errors import classify_google_error

    err = FakeHttpError(404, reason="NOT_FOUND")
    sheets_err = classify_google_error(err)
    assert sheets_err.retries is None
    assert sheets_err.waited_ms is None
    d = sheets_err.to_dict()
    assert "retries" not in d
    assert "waitedMs" not in d
