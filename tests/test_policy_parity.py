"""Machine-verified lock-step between LocalPolicyEngine and the Rego bundle.

Both engines are anchored to one shared vector file
(``tests/data/policy_vectors.json``): this module runs every vector through
``LocalPolicyEngine`` and asserts the decision matches ``expected``;
``scripts/policy_parity.py`` runs the *same* vectors through ``opa eval`` in the
OPA CI job and asserts the same ``expected``. Transitively, the two backends
produce identical decisions - AGENTS.md invariant #2 is no longer convention.

Reasons and required-approvals are compared as sets: Rego materialises sets in
sorted order while the Python engine preserves insertion order, so only the set
of strings is load-bearing for audit parity.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from foundry.policy import LocalPolicyEngine, PolicyInput
from foundry.policy.engine import (
    _ADVISORY_ACTIONS,
    _AUTONOMOUS_ACTIONS,
    _FORBIDDEN_ACTIONS,
)
from foundry.schemas.common import PolicyAction

_VECTORS_PATH = Path(__file__).parent / "data" / "policy_vectors.json"
_VECTORS = json.loads(_VECTORS_PATH.read_text())["vectors"]


def _threshold(vector: dict) -> int:
    return int(vector.get("config", {}).get("repo_confidence_threshold", 70))


@pytest.mark.parametrize("vector", _VECTORS, ids=[v["name"] for v in _VECTORS])
def test_local_engine_matches_shared_vector(vector: dict) -> None:
    expected = vector["expected"]
    engine = LocalPolicyEngine(repo_confidence_threshold=_threshold(vector))

    if not vector.get("valid_action", True):
        # An action outside the PolicyAction enum is default-denied at the schema
        # boundary in Python (it never reaches evaluate); Rego enforces the same
        # outcome in-rule. We can only assert the shared `allow` here.
        with pytest.raises(ValidationError):
            PolicyInput.model_validate(vector["input"])
        assert expected["allow"] is False
        return

    decision = engine.evaluate(PolicyInput.model_validate(vector["input"]))

    assert decision.allowed is expected["allow"]
    assert decision.allowed_agent_mode.value == expected["allowed_agent_mode"]
    assert set(decision.reasons) == set(expected["reasons"])
    assert {r.value for r in decision.required_approvals} == set(
        expected["required_approvals"]
    )


def test_unrecognised_action_hits_python_default_deny_branch() -> None:
    """The engine's default-deny branch must be a live safety net, not dead code.

    Typed callers can only pass a classified ``PolicyAction``, so the branch was
    previously unreachable. ``model_construct`` bypasses validation the way a
    future non-enum caller (or OPA's raw JSON) would, exercising the branch and
    proving the reason formatting tolerates a non-enum value.
    """
    payload = PolicyInput.model_construct(action="totally_made_up_action")
    decision = LocalPolicyEngine().evaluate(payload)
    assert decision.allowed is False
    assert decision.allowed_agent_mode.value == "human_only"
    assert any("not covered by this policy" in r for r in decision.reasons)


def test_every_policy_action_is_classified_exactly_once() -> None:
    """Default-deny only protects actions that aren't deliberately classified.

    Guard the partition so a newly added ``PolicyAction`` cannot silently inherit
    advisory/autonomous behaviour: an unclassified member fails here, forcing a
    conscious decision (and the default-deny branch above is its safety net).
    """
    sets = (_FORBIDDEN_ACTIONS, _ADVISORY_ACTIONS, _AUTONOMOUS_ACTIONS)
    for action in PolicyAction:
        memberships = [s for s in sets if action in s]
        assert len(memberships) == 1, f"{action} must be in exactly one action set"
    # The three sets are mutually exclusive.
    assert not (_FORBIDDEN_ACTIONS & _ADVISORY_ACTIONS)
    assert not (_FORBIDDEN_ACTIONS & _AUTONOMOUS_ACTIONS)
    assert not (_ADVISORY_ACTIONS & _AUTONOMOUS_ACTIONS)
