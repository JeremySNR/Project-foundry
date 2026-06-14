"""OpaPolicyEngine: the HTTP/parse seam to a production OPA server.

These tests inject a fake ``http_post`` (no network - AGENTS.md invariant #3) and
exercise request shaping, response parsing, and error handling. The policy
*logic* is covered by the Rego suite + the shared parity vectors; here we only
prove the client wires OPA's decision document into a ``PolicyDecision``.
"""

from __future__ import annotations

import pytest

from foundry.policy import OpaPolicyEngine, PolicyInput
from foundry.schemas.common import AgentMode, ApprovalRole


def _ready_low_risk() -> PolicyInput:
    return PolicyInput.model_validate(
        {
            "action": "start_agent",
            "ticket": {"readiness": "ready"},
            "risk": {"overall_risk": "low"},
            "repo": {"confidence": 90},
            "approval": {},
        }
    )


def test_parses_opa_decision_document() -> None:
    def fake_post(url: str, body: dict) -> dict:
        return {
            "result": {
                "allow": True,
                "reasons": ["all minimum policy checks passed"],
                "allowed_agent_mode": "draft_pr",
                "required_approvals": ["engineering"],
            }
        }

    engine = OpaPolicyEngine(base_url="http://opa:8181", http_post=fake_post)
    decision = engine.evaluate(_ready_low_risk())

    assert decision.allowed is True
    assert decision.allowed_agent_mode is AgentMode.DRAFT_PR
    assert decision.reasons == ["all minimum policy checks passed"]
    assert decision.required_approvals == [ApprovalRole.ENGINEERING]
    assert decision.policy_name == "foundry.ticket_to_pr.v1"


def test_posts_to_decision_endpoint_with_threshold_injected() -> None:
    captured: dict = {}

    def fake_post(url: str, body: dict) -> dict:
        captured["url"] = url
        captured["body"] = body
        return {"result": {"allow": True, "reasons": [], "allowed_agent_mode": "human_only"}}

    engine = OpaPolicyEngine(
        base_url="http://opa:8181/",  # trailing slash should be normalised
        repo_confidence_threshold=85,
        http_post=fake_post,
    )
    engine.evaluate(_ready_low_risk())

    assert captured["url"] == "http://opa:8181/v1/data/foundry/ticket_to_pr/decision"
    # The configured threshold rides along in the input so Rego evaluates against
    # the same value the Python engine would - they cannot drift.
    assert captured["body"]["input"]["repo_confidence_threshold"] == 85
    assert captured["body"]["input"]["action"] == "start_agent"


def test_empty_result_raises_runtime_error() -> None:
    engine = OpaPolicyEngine(
        base_url="http://opa:8181", http_post=lambda url, body: {"result": {}}
    )
    with pytest.raises(RuntimeError, match="OPA policy evaluation failed"):
        engine.evaluate(_ready_low_risk())


def test_missing_allow_key_raises_runtime_error() -> None:
    engine = OpaPolicyEngine(
        base_url="http://opa:8181",
        http_post=lambda url, body: {"result": {"reasons": ["x"]}},
    )
    with pytest.raises(RuntimeError, match="OPA policy evaluation failed"):
        engine.evaluate(_ready_low_risk())


def test_build_policy_engine_selects_backend_from_settings() -> None:
    from foundry.api.app import build_policy_engine
    from foundry.config import Settings
    from foundry.policy import LocalPolicyEngine

    local = build_policy_engine(Settings.from_env({}))
    assert isinstance(local, LocalPolicyEngine)

    opa = build_policy_engine(
        Settings.from_env(
            {"FOUNDRY_POLICY_PROVIDER": "opa", "FOUNDRY_POLICY_OPA_URL": "http://opa:8181"}
        )
    )
    assert isinstance(opa, OpaPolicyEngine)
    # The configured threshold is threaded into the OPA backend.
    assert opa._repo_confidence_threshold == 70
