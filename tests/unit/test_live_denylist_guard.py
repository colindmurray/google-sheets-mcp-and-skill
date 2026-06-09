"""Unit tests for the live-suite Production denylist guard (DESIGN §0 — no plaintext ids).

The live integration module (``tests/integration/test_live_smoke.py``) must refuse to run
against a Production spreadsheet, but this repo is PUBLIC so the guard carries NO plaintext id —
only a salted one-way hash plus an optional runtime env denylist. These tests exercise that
guard's pure logic WITHOUT any network/credentials (the helpers under test are plain functions),
so the no-plaintext protection stays covered in CI rather than only in a gated live run.

Importing the helpers from the ``live``-marked module is safe: the ``pytest.mark.live`` marker
applies to tests defined there, not to plain functions imported elsewhere.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

# Load the live integration module by FILE PATH (not via a `tests.integration` import) so this
# unit test works regardless of whether `tests/` is importable as a package under the active
# pytest rootdir/import mode. Importing plain helper functions never runs the `live`-marked
# tests, so no network/credentials are touched.
_LIVE_PATH = (
    Path(__file__).resolve().parents[1] / "integration" / "test_live_smoke.py"
)
_spec = importlib.util.spec_from_file_location("_live_smoke_for_guard_tests", _LIVE_PATH)
live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(live)


def test_no_plaintext_production_id_in_committed_source():
    """The committed live module must contain NO real 40+ char spreadsheet id (DESIGN §0)."""
    import re
    from pathlib import Path

    src = Path(live.__file__).read_text(encoding="utf-8")
    # Google spreadsheet ids are ~44 chars of [A-Za-z0-9_-] and always mix letters AND digits.
    # Flag any such literal run, excluding the 64-hex salted digests (the only long tokens that
    # legitimately remain). Requiring both a letter and a digit ignores ASCII rule separators.
    candidates = re.findall(r"[A-Za-z0-9_-]{40,}", src)
    leaks = [
        c
        for c in candidates
        if not re.fullmatch(r"[0-9a-f]{64}", c)
        and re.search(r"[A-Za-z]", c)
        and re.search(r"[0-9]", c)
    ]
    assert not leaks, f"possible real spreadsheet id(s) committed: {leaks}"


def test_salted_hash_is_recomputable_and_one_way():
    """The salted-hash helper matches a manual recomputation and is a 64-hex digest."""
    sid = "some-fake-spreadsheet-id-1234567890"
    expected = hashlib.sha256(f"{live._DENYLIST_SALT}:{sid}".encode()).hexdigest()
    got = live._salted_hash(sid)
    assert got == expected
    assert len(got) == 64 and all(c in "0123456789abcdef" for c in got)


def test_hash_guard_catches_a_declared_production_id():
    """A synthetic 'Production' id whose hash is in the set is denied — without storing the id.

    We add a throwaway id's hash to the frozen hash set (mirroring how the real Production id is
    pinned by digest only) and confirm the guard denies the plaintext id via hashing alone.
    """
    fake_prod = "fake-production-id-do-not-use-0000000000"
    digest = live._salted_hash(fake_prod)
    extended = frozenset(live._PRODUCTION_DENYLIST_HASHES | {digest})
    # Patch the module's hash set for the duration of this assertion.
    original = live._PRODUCTION_DENYLIST_HASHES
    live._PRODUCTION_DENYLIST_HASHES = extended
    try:
        assert live._is_denied(fake_prod) is True
    finally:
        live._PRODUCTION_DENYLIST_HASHES = original


def test_non_production_id_is_allowed():
    """An ordinary scratch id (not in any denylist) is allowed."""
    assert live._is_denied("1lSzvHS3-totally-unrelated-scratch-id") is False


def test_env_denylist_denies_declared_ids(monkeypatch):
    """``GSHEETS_PRODUCTION_DENYLIST`` (comma-separated) denies ids at runtime, never committed."""
    monkeypatch.setenv("GSHEETS_PRODUCTION_DENYLIST", "scratch-A , scratch-B")
    assert live._is_denied("scratch-A") is True
    assert live._is_denied("scratch-B") is True
    assert live._is_denied("scratch-C") is False


def test_env_denylist_empty_or_unset_denies_nothing(monkeypatch):
    """No env denylist => the env source contributes no denials."""
    monkeypatch.delenv("GSHEETS_PRODUCTION_DENYLIST", raising=False)
    assert live._env_denylist() == frozenset()
    monkeypatch.setenv("GSHEETS_PRODUCTION_DENYLIST", "  ,  , ")
    assert live._env_denylist() == frozenset()
