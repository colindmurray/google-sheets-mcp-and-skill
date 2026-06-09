"""Live integration smoke tests (DESIGN §10; research §5).

Marked ``@pytest.mark.live`` and gated on ``GSHEETS_LIVE=1``; the target sheet id comes from
``GSHEETS_TEST_SPREADSHEET_ID`` (NEVER committed, NEVER Production). Exercises a real authed
read/round-trip end-to-end. Real cases land with the implementation.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def test_placeholder():
    pass
