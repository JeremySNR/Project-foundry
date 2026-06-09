"""Wrapper that runs the live E2E smoke script under pytest.

Marked ``e2e`` and gated on ``FOUNDRY_E2E=1`` - it touches real Linear/GitHub/
agent services and is never part of the default suite or CI. Run explicitly:

    FOUNDRY_E2E=1 pytest -m e2e tests/test_e2e_smoke.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("FOUNDRY_E2E") != "1",
        reason="live E2E smoke is opt-in (FOUNDRY_E2E=1)",
    ),
]


def test_live_ticket_to_pr_smoke() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from smoke_e2e import main

    assert main() == 0
