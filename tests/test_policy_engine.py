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
