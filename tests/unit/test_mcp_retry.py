"""End-to-end proof that a per-call ``retry`` param reaches the core contextvar (ISSUES.md #25).

The whole point of the contextvar mechanism (``core.retry``) is that an adapter activates a
``RetryPolicy`` and the auth-layer request builder reads it at ``.execute()`` time — DEEP inside
core, in whatever thread FastMCP offloaded the (sync) tool body to. The unit tests in
``test_retry.py`` prove the pure mechanism, and ``test_mcp_server.py`` proves ``_resolve_retry``'s
mapping; this file closes the loop by driving the REAL in-memory ``fastmcp.Client`` and asserting
that, AT THE MOMENT the monkeypatched core fn runs, ``core.retry.current_policy()`` is exactly the
policy the per-call ``retry`` param asked for.

This is the contextvar-across-threads correctness proof — it is why ``_call`` activates the policy
INSIDE its sync body (not in the async wrapper): a ``ContextVar`` set in the event-loop thread is
NOT visible to the worker thread FastMCP runs the sync tool in, so activating there would silently
no-op and ``.execute()`` would see ``DISABLED`` regardless of the param.

This is an ADAPTER test (it imports ``fastmcp``/``pydantic`` by design). The subprocess boundary
guard in ``test_boundary_guard.py`` keeps the pure-core import clean independently. It requests the
``stub_mcp_credentials`` fixture so the Client can connect creds-free in CI (the lifespan's
``build_services`` would otherwise fail there).
"""

from __future__ import annotations

import asyncio

import pytest

import gsheets.mcp_server as srv
from gsheets.core import retry as retry_mod


def _read_values_payload() -> dict:
    """A minimal valid ``read_values`` core-result dict the mirror model accepts."""
    return {
        "ok": True,
        "spreadsheetId": "FAKEID",
        "render": "plain",
        "ranges": [{"range": "S!A1:B2", "values": [["a", "b"], ["c", "d"]]}],
    }


def _drive_read_values(monkeypatch, *, tool_args: dict) -> retry_mod.RetryPolicy:
    """Call ``sheets_read_values`` through a real in-memory Client, recording the ACTIVE policy.

    The core fn (``srv._read_values``) is monkeypatched with a fake that captures
    ``core.retry.current_policy()`` at call time (i.e. in the actual worker thread/context the tool
    body runs in) and returns a minimal valid result. Returns the captured policy.
    """
    from fastmcp import Client

    recorded: dict[str, retry_mod.RetryPolicy] = {}

    def fake_read_values(services, spreadsheet_id, ranges, **kwargs):
        # current_policy() must reflect the per-call retry param HERE — proving the contextvar set
        # inside _call's sync body is visible at the core .execute() point.
        recorded["policy"] = retry_mod.current_policy()
        return _read_values_payload()

    monkeypatch.setattr(srv, "_read_values", fake_read_values)
    monkeypatch.setattr(srv, "_services", lambda ctx: object())

    async def go():
        async with Client(srv.mcp) as client:
            await client.call_tool("sheets_read_values", tool_args)

    asyncio.run(go())
    assert "policy" in recorded, "core fn was never invoked"
    return recorded["policy"]


def test_retry_preset_default_reaches_core_contextvar(monkeypatch, stub_mcp_credentials):
    # retry={"preset": "default"} -> the active policy AT THE CORE CALL is the default preset.
    policy = _drive_read_values(
        monkeypatch,
        tool_args={
            "spreadsheet_id": "FAKEID",
            "ranges": ["S!A1:B2"],
            "retry": {"preset": "default"},
        },
    )
    assert policy == retry_mod.RetryPolicy.default_preset()
    assert policy.enabled is True
    assert policy.strategy == "exponential_jitter"


def test_no_retry_param_reaches_core_as_disabled(monkeypatch, stub_mcp_credentials):
    # Omitting retry -> OFF by default: the active policy at the core call is DISABLED (creds-free
    # CI has no GSHEETS_BACKOFF_* var set, so from_env() resolves to a disabled policy).
    for key in list(srv.os.environ):
        if key.startswith("GSHEETS_BACKOFF_") or key == "GSHEETS_MAX_RETRIES":
            monkeypatch.delenv(key, raising=False)

    policy = _drive_read_values(
        monkeypatch,
        tool_args={"spreadsheet_id": "FAKEID", "ranges": ["S!A1:B2"]},
    )
    assert policy.enabled is False
    # Equivalent to the explicit DISABLED policy (off, no looping).
    assert policy.max_retries == 0 or policy == retry_mod.RetryPolicy.from_env()


def test_retry_preset_off_reaches_core_as_disabled(monkeypatch, stub_mcp_credentials):
    # retry={"preset": "off"} forces the DISABLED policy even if an env var would enable it.
    monkeypatch.setenv("GSHEETS_BACKOFF_STRATEGY", "exponential")  # would otherwise enable
    policy = _drive_read_values(
        monkeypatch,
        tool_args={
            "spreadsheet_id": "FAKEID",
            "ranges": ["S!A1:B2"],
            "retry": {"preset": "off"},
        },
    )
    assert policy is retry_mod.RetryPolicy.DISABLED
    assert policy.enabled is False


def test_granular_retry_reaches_core_contextvar(monkeypatch, stub_mcp_credentials):
    # Granular fields flow through to the active core policy (deadline -> total_deadline).
    for key in list(srv.os.environ):
        if key.startswith("GSHEETS_BACKOFF_") or key == "GSHEETS_MAX_RETRIES":
            monkeypatch.delenv(key, raising=False)

    policy = _drive_read_values(
        monkeypatch,
        tool_args={
            "spreadsheet_id": "FAKEID",
            "ranges": ["S!A1:B2"],
            "retry": {"strategy": "fixed", "max_retries": 2, "deadline": 7.5},
        },
    )
    assert policy.enabled is True
    assert policy.strategy == "fixed"
    assert policy.max_retries == 2
    assert policy.total_deadline == 7.5


def test_active_policy_resets_after_tool_call(monkeypatch, stub_mcp_credentials):
    # The activation is scoped: after the tool returns, the ambient policy is back to DISABLED (the
    # contextvar token is reset in _call's `with` block, so a per-call policy never leaks).
    _drive_read_values(
        monkeypatch,
        tool_args={
            "spreadsheet_id": "FAKEID",
            "ranges": ["S!A1:B2"],
            "retry": {"preset": "default"},
        },
    )
    assert retry_mod.current_policy() is retry_mod.RetryPolicy.DISABLED
