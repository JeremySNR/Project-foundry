"""Policy gate tests - the Python mirror of foundry_test.rego.

These hold the LocalPolicyEngine to the same behaviour the Rego bundle asserts.
"""

from __future__ import annotations

from foundry.policy import LocalPolicyEngine, PolicyInput
from foundry.schemas.common import AgentMode, ApprovalRole, PolicyAction


def _engine() -> LocalPolicyEngine:
    return LocalPolicyEngine()


def test_low_risk_frontend_change_allows_draft_pr() -> None:
    decision = _engine().evaluate(
        PolicyInput.model_validate(
            {
                "action": "start_agent",
                "ticket": {"work_type": "feature", "readiness": "ready"},
                "risk": {"overall_risk": "low"},
                "repo": {"name": "customer-web", "confidence": 90},
                "approval": {},
            }
        )
    )
    assert decision.allowed is True
    assert decision.allowed_agent_mode is AgentMode.DRAFT_PR


def test_auth_change_requires_engineering_approval() -> None:
    payload = PolicyInput.model_validate(
        {
            "action": "start_agent",
            "ticket": {"readiness": "ready"},
            "risk": {"overall_risk": "medium", "auth": True},
            "repo": {"confidence": 90},
            "approval": {},
        }
    )
    decision = _engine().evaluate(payload)
    assert decision.allowed is False
    assert ApprovalRole.ENGINEERING in decision.required_approvals

    payload.approval = {"engineering": True}
    approved = _engine().evaluate(payload)
    assert approved.allowed is True


def test_migration_blocks_autonomous_execution() -> None:
    decision = _engine().evaluate(
        PolicyInput.model_validate(
            {
                "action": "start_agent",
                "ticket": {"readiness": "ready"},
                "risk": {"overall_risk": "medium", "database_migration": True},
                "repo": {"confidence": 95},
                "approval": {},
            }
        )
    )
    assert decision.allowed is False
    assert any("migration" in r for r in decision.reasons)


def test_unknown_repo_blocks_execution() -> None:
    decision = _engine().evaluate(
        PolicyInput.model_validate(
            {
                "action": "start_agent",
                "ticket": {"readiness": "ready"},
                "risk": {"overall_risk": "low"},
                "repo": {"confidence": 40},
                "approval": {},
            }
        )
    )
    assert decision.allowed is False
    assert any("confidence" in r for r in decision.reasons)


def test_not_ready_blocks_execution() -> None:
    decision = _engine().evaluate(
        PolicyInput.model_validate(
            {
                "action": "start_agent",
                "ticket": {"readiness": "needs_clarification"},
                "risk": {"overall_risk": "low"},
                "repo": {"confidence": 90},
                "approval": {},
            }
        )
    )
    assert decision.allowed is False


def test_production_deploy_blocked_in_mvp() -> None:
    decision = _engine().evaluate(
        PolicyInput.model_validate(
            {
                "action": "open_pr",
                "ticket": {"readiness": "ready"},
                "risk": {"overall_risk": "low", "production_deploy": True},
                "repo": {"confidence": 90},
                "approval": {},
            }
        )
    )
    assert decision.allowed is False


def test_high_risk_only_allows_human_only_mode() -> None:
    decision = _engine().evaluate(
        PolicyInput.model_validate(
            {
                "action": "start_agent",
                "ticket": {"readiness": "ready"},
                "risk": {"overall_risk": "high"},
                "repo": {"confidence": 90},
                "approval": {},
            }
        )
    )
    # High risk is allowed through the gate but must not run autonomously.
    assert decision.allowed_agent_mode is AgentMode.HUMAN_ONLY


def test_read_only_analysis_always_allowed() -> None:
    decision = _engine().evaluate(
        PolicyInput.model_validate(
            {
                "action": "analyse_ticket",
                "ticket": {"readiness": "needs_clarification"},
                "risk": {"overall_risk": "high"},
                "repo": {"confidence": 0},
                "approval": {},
            }
        )
    )
    assert decision.allowed is True


def test_customer_data_requires_security_approval() -> None:
    payload = PolicyInput.model_validate(
        {
            "action": "start_agent",
            "ticket": {"readiness": "ready"},
            "risk": {"overall_risk": "medium", "customer_data": True},
            "repo": {"confidence": 90},
            "approval": {},
        }
    )
    decision = _engine().evaluate(payload)
    assert decision.allowed is False
    assert ApprovalRole.SECURITY in decision.required_approvals


def test_decision_records_policy_name_and_id() -> None:
    decision = _engine().evaluate(
        PolicyInput(action=PolicyAction.ANALYSE_TICKET)
    )
    assert decision.policy_name == "foundry.ticket_to_pr.v1"
    assert decision.decision_id


def test_auto_merge_denied_even_for_perfect_run() -> None:
    """'No auto-merge' is an enforced decision, not an absence of code."""
    decision = _engine().evaluate(
        PolicyInput.model_validate(
            {
                "action": "auto_merge",
                "ticket": {"readiness": "ready"},
                "risk": {"overall_risk": "low"},
                "repo": {"name": "customer-web", "confidence": 100},
                "approval": {"engineering": True, "security": True},
            }
        )
    )
    assert decision.allowed is False
    assert decision.allowed_agent_mode is AgentMode.HUMAN_ONLY
    assert any("never run autonomously" in r for r in decision.reasons)


def test_retry_within_cap_allowed_past_cap_denied() -> None:
    base = {
        "action": "retry_agent",
        "ticket": {"readiness": "ready"},
        "risk": {"overall_risk": "low"},
        "repo": {"confidence": 90},
        "approval": {},
    }
    within = _engine().evaluate(
        PolicyInput.model_validate({**base, "retry": {"attempt": 2, "max_attempts": 2}})
    )
    assert within.allowed is True
    over = _engine().evaluate(
        PolicyInput.model_validate({**base, "retry": {"attempt": 3, "max_attempts": 2}})
    )
    assert over.allowed is False
    assert any("exceeds the maximum" in r for r in over.reasons)


def test_retry_over_budget_denied_under_budget_allowed() -> None:
    base = {
        "action": "retry_agent",
        "ticket": {"readiness": "ready"},
        "risk": {"overall_risk": "low"},
        "repo": {"confidence": 90},
        "approval": {},
    }
    over = _engine().evaluate(
        PolicyInput.model_validate(
            {**base, "budget": {"cost_usd": 5.5, "max_cost_usd": 5.0}}
        )
    )
    assert over.allowed is False
    assert any("budget cap" in r for r in over.reasons)
    under = _engine().evaluate(
        PolicyInput.model_validate(
            {**base, "budget": {"cost_usd": 1.0, "max_cost_usd": 5.0}}
        )
    )
    assert under.allowed is True
    # No cap configured -> spend is informational only.
    uncapped = _engine().evaluate(
        PolicyInput.model_validate({**base, "budget": {"cost_usd": 999.0}})
    )
    assert uncapped.allowed is True


def test_production_deploy_action_denied_unconditionally() -> None:
    decision = _engine().evaluate(
        PolicyInput.model_validate(
            {
                "action": "production_deploy",
                "ticket": {"readiness": "ready"},
                "risk": {"overall_risk": "low"},
                "repo": {"confidence": 100},
                "approval": {},
            }
        )
    )
    assert decision.allowed is False
