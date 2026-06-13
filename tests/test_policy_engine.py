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


# -- boundary values: the gate's comparisons are off-by-one sensitive ----------


def _start(**risk_repo) -> PolicyInput:
    base = {
        "action": "start_agent",
        "ticket": {"readiness": "ready"},
        "risk": {"overall_risk": "low"},
        "repo": {"confidence": 90},
        "approval": {},
    }
    base.update(risk_repo)
    return PolicyInput.model_validate(base)


def test_repo_confidence_exactly_at_threshold_is_allowed() -> None:
    """The rule is ``confidence < threshold`` (default 70), so exactly 70 passes
    and 69 fails - the boundary the deny message references."""
    at = _engine().evaluate(_start(repo={"confidence": 70}))
    assert at.allowed is True
    below = _engine().evaluate(_start(repo={"confidence": 69}))
    assert below.allowed is False
    assert any("confidence" in r for r in below.reasons)


def test_budget_cost_exactly_at_cap_denies() -> None:
    """``cost_usd >= max_cost_usd`` means reaching the cap (not just exceeding it)
    denies the retry - the equality case the inline comment relies on."""
    base = {
        "action": "retry_agent",
        "ticket": {"readiness": "ready"},
        "risk": {"overall_risk": "low"},
        "repo": {"confidence": 90},
        "approval": {},
    }
    at_cap = _engine().evaluate(
        PolicyInput.model_validate(
            {**base, "budget": {"cost_usd": 5.0, "max_cost_usd": 5.0}}
        )
    )
    assert at_cap.allowed is False
    assert any("budget cap" in r for r in at_cap.reasons)
    # A cent under the cap is still allowed.
    under = _engine().evaluate(
        PolicyInput.model_validate(
            {**base, "budget": {"cost_usd": 4.99, "max_cost_usd": 5.0}}
        )
    )
    assert under.allowed is True


def test_retry_attempt_exactly_at_zero_cap_denies() -> None:
    """With the retry cap set to 0, the very first re-dispatch (attempt 1) is
    over the cap; attempt 0 (never produced by the orchestrator) is the boundary
    that is still allowed."""
    base = {
        "action": "retry_agent",
        "ticket": {"readiness": "ready"},
        "risk": {"overall_risk": "low"},
        "repo": {"confidence": 90},
        "approval": {},
    }
    first_retry = _engine().evaluate(
        PolicyInput.model_validate(
            {**base, "retry": {"attempt": 1, "max_attempts": 0}}
        )
    )
    assert first_retry.allowed is False
    assert any("exceeds the maximum" in r for r in first_retry.reasons)
    at_boundary = _engine().evaluate(
        PolicyInput.model_validate(
            {**base, "retry": {"attempt": 0, "max_attempts": 0}}
        )
    )
    assert at_boundary.allowed is True


def test_multi_area_work_requires_every_derived_role() -> None:
    """auth -> engineering and payments -> security; a run touching both needs
    *both* roles. A single approval is not enough; both together unlock it."""
    both_areas = {
        "action": "start_agent",
        "ticket": {"readiness": "ready"},
        "risk": {"overall_risk": "high", "auth": True, "payments": True},
        "repo": {"confidence": 90},
    }
    none = _engine().evaluate(
        PolicyInput.model_validate({**both_areas, "approval": {}})
    )
    assert none.allowed is False
    assert ApprovalRole.ENGINEERING in none.required_approvals
    assert ApprovalRole.SECURITY in none.required_approvals

    partial = _engine().evaluate(
        PolicyInput.model_validate({**both_areas, "approval": {"engineering": True}})
    )
    assert partial.allowed is False
    assert any("security" in r for r in partial.reasons)

    both = _engine().evaluate(
        PolicyInput.model_validate(
            {**both_areas, "approval": {"engineering": True, "security": True}}
        )
    )
    assert both.allowed is True


# -- autonomous-action coverage: branch/PR/complete are governed, not free -----


def test_branch_pr_and_complete_actions_are_governed_like_start_agent() -> None:
    """``CREATE_BRANCH``/``OPEN_PR``/``MARK_COMPLETE`` are autonomous actions, so
    the same hard blocks that gate ``START_AGENT`` apply - they are not advisory
    free passes. (The orchestrator only ever evaluates START_AGENT/RETRY_AGENT
    today; this pins that the gate would govern them if a path ever did.)"""
    for action in ("create_branch", "open_pr", "mark_complete"):
        # An unready, low-confidence ticket is denied for each.
        denied = _engine().evaluate(
            PolicyInput.model_validate(
                {
                    "action": action,
                    "ticket": {"readiness": "needs_clarification"},
                    "risk": {"overall_risk": "low"},
                    "repo": {"confidence": 10},
                    "approval": {},
                }
            )
        )
        assert denied.allowed is False, action
        # A ready, confident, low-risk ticket passes the same gate.
        allowed = _engine().evaluate(
            PolicyInput.model_validate(
                {
                    "action": action,
                    "ticket": {"readiness": "ready"},
                    "risk": {"overall_risk": "low"},
                    "repo": {"confidence": 90},
                    "approval": {},
                }
            )
        )
        assert allowed.allowed is True, action
