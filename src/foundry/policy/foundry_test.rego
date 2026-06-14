# OPA tests for the Foundry Ticket-to-PR policy.
# Run with:  opa test src/foundry/policy
#
# These mirror the Python tests in tests/test_policy_engine.py so both backends
# are held to the same behaviour.

package foundry.ticket_to_pr_test

import data.foundry.ticket_to_pr
import rego.v1

# A low-risk frontend copy change with a confident repo should allow draft PR.
test_low_risk_allows_draft_pr if {
	decision := ticket_to_pr.decision with input as {
		"action": "start_agent",
		"ticket": {"work_type": "feature", "readiness": "ready"},
		"risk": {"overall_risk": "low"},
		"repo": {"name": "customer-web", "confidence": 90},
		"approval": {},
		"approval_present": true,
	}
	decision.allow == true
	decision.allowed_agent_mode == "draft_pr"
}

# An auth change requires engineering approval.
test_auth_requires_engineering_approval if {
	decision := ticket_to_pr.decision with input as {
		"action": "start_agent",
		"ticket": {"readiness": "ready"},
		"risk": {"overall_risk": "medium", "auth": true},
		"repo": {"confidence": 90},
		"approval": {},
	}
	decision.allow == false
}

test_auth_allowed_with_engineering_approval if {
	decision := ticket_to_pr.decision with input as {
		"action": "start_agent",
		"ticket": {"readiness": "ready"},
		"risk": {"overall_risk": "medium", "auth": true},
		"repo": {"confidence": 90},
		"approval": {"engineering": true},
		"approval_present": true,
	}
	decision.allow == true
}

# A migration blocks autonomous execution.
test_migration_blocks_execution if {
	decision := ticket_to_pr.decision with input as {
		"action": "start_agent",
		"ticket": {"readiness": "ready"},
		"risk": {"overall_risk": "medium", "database_migration": true},
		"repo": {"confidence": 90},
		"approval": {},
	}
	decision.allow == false
}

# An unknown / low-confidence repo blocks execution.
test_low_confidence_repo_blocks_execution if {
	decision := ticket_to_pr.decision with input as {
		"action": "start_agent",
		"ticket": {"readiness": "ready"},
		"risk": {"overall_risk": "low"},
		"repo": {"confidence": 40},
		"approval": {},
	}
	decision.allow == false
}

# Readiness below "ready" blocks execution.
test_not_ready_blocks_execution if {
	decision := ticket_to_pr.decision with input as {
		"action": "start_agent",
		"ticket": {"readiness": "needs_clarification"},
		"risk": {"overall_risk": "low"},
		"repo": {"confidence": 90},
		"approval": {},
	}
	decision.allow == false
}

# Read-only analysis is always allowed.
test_analysis_always_allowed if {
	decision := ticket_to_pr.decision with input as {
		"action": "analyse_ticket",
		"ticket": {"readiness": "needs_clarification"},
		"risk": {"overall_risk": "high"},
		"repo": {"confidence": 0},
		"approval": {},
	}
	decision.allow == true
}

# Auto-merge is denied unconditionally, even for a perfect low-risk run.
test_auto_merge_always_denied if {
	decision := ticket_to_pr.decision with input as {
		"action": "auto_merge",
		"ticket": {"readiness": "ready"},
		"risk": {"overall_risk": "low"},
		"repo": {"confidence": 100},
		"approval": {"engineering": true, "security": true},
	}
	decision.allow == false
	decision.allowed_agent_mode == "human_only"
}

# Production deploys are denied unconditionally.
test_production_deploy_always_denied if {
	decision := ticket_to_pr.decision with input as {
		"action": "production_deploy",
		"ticket": {"readiness": "ready"},
		"risk": {"overall_risk": "low"},
		"repo": {"confidence": 100},
		"approval": {},
	}
	decision.allow == false
}

# A remediation retry within the cap is allowed; past the cap it is denied.
test_retry_within_cap_allowed if {
	decision := ticket_to_pr.decision with input as {
		"action": "retry_agent",
		"ticket": {"readiness": "ready"},
		"risk": {"overall_risk": "low"},
		"repo": {"confidence": 90},
		"retry": {"attempt": 2, "max_attempts": 2},
		"approval": {},
		"approval_present": true,
	}
	decision.allow == true
}

test_retry_past_cap_denied if {
	decision := ticket_to_pr.decision with input as {
		"action": "retry_agent",
		"ticket": {"readiness": "ready"},
		"risk": {"overall_risk": "low"},
		"repo": {"confidence": 90},
		"retry": {"attempt": 3, "max_attempts": 2},
		"approval": {},
	}
	decision.allow == false
}

# A retry past the budget cap is denied; under the cap (or no cap) it is allowed.
test_retry_over_budget_denied if {
	decision := ticket_to_pr.decision with input as {
		"action": "retry_agent",
		"ticket": {"readiness": "ready"},
		"risk": {"overall_risk": "low"},
		"repo": {"confidence": 90},
		"budget": {"cost_usd": 5.5, "max_cost_usd": 5.0},
		"approval": {},
	}
	decision.allow == false
}

test_retry_under_budget_allowed if {
	decision := ticket_to_pr.decision with input as {
		"action": "retry_agent",
		"ticket": {"readiness": "ready"},
		"risk": {"overall_risk": "low"},
		"repo": {"confidence": 90},
		"budget": {"cost_usd": 1.0, "max_cost_usd": 5.0},
		"approval": {},
		"approval_present": true,
	}
	decision.allow == true
}

# The budget cap binds on the first dispatch too (issue #29): with nothing
# spent yet, the pending estimate alone can push a single attempt over the cap.
test_start_agent_over_budget_denied if {
	decision := ticket_to_pr.decision with input as {
		"action": "start_agent",
		"ticket": {"readiness": "ready"},
		"risk": {"overall_risk": "low"},
		"repo": {"confidence": 90},
		"budget": {"cost_usd": 0.0, "pending_cost_usd": 5.0, "max_cost_usd": 5.0},
		"approval": {},
	}
	decision.allow == false
}

test_start_agent_under_budget_allowed if {
	decision := ticket_to_pr.decision with input as {
		"action": "start_agent",
		"ticket": {"readiness": "ready"},
		"risk": {"overall_risk": "low"},
		"repo": {"confidence": 90},
		"budget": {"cost_usd": 0.0, "pending_cost_usd": 4.0, "max_cost_usd": 5.0},
		"approval": {},
		"approval_present": true,
	}
	decision.allow == true
}

# Projected spend = recorded cost + the next dispatch's estimate; a retry whose
# recorded spend is under the cap is still denied once the estimate is added.
test_projected_spend_combines_recorded_and_pending if {
	decision := ticket_to_pr.decision with input as {
		"action": "retry_agent",
		"ticket": {"readiness": "ready"},
		"risk": {"overall_risk": "low"},
		"repo": {"confidence": 90},
		"budget": {"cost_usd": 4.0, "pending_cost_usd": 2.0, "max_cost_usd": 5.0},
		"approval": {},
	}
	decision.allow == false
}

# The budget rule gates only the spending actions; open_pr is not blocked on spend.
test_open_pr_not_blocked_on_spend if {
	decision := ticket_to_pr.decision with input as {
		"action": "open_pr",
		"ticket": {"readiness": "ready"},
		"risk": {"overall_risk": "low"},
		"repo": {"confidence": 90},
		"budget": {"cost_usd": 99.0, "max_cost_usd": 5.0},
		"approval": {},
		"approval_present": true,
	}
	decision.allow == true
}

# Every autonomous action requires at least one recorded human approval (issue
# #18). A ready, low-risk, confident-repo run with no approval present is denied.
test_start_agent_without_approval_denied if {
	decision := ticket_to_pr.decision with input as {
		"action": "start_agent",
		"ticket": {"readiness": "ready"},
		"risk": {"overall_risk": "low"},
		"repo": {"confidence": 90},
		"approval": {},
	}
	decision.allow == false
	"autonomous action requires at least one recorded human approval" in decision.reasons
}

# The backstop covers retries too.
test_retry_without_approval_denied if {
	decision := ticket_to_pr.decision with input as {
		"action": "retry_agent",
		"ticket": {"readiness": "ready"},
		"risk": {"overall_risk": "low"},
		"repo": {"confidence": 90},
		"retry": {"attempt": 1, "max_attempts": 2},
		"approval": {},
	}
	decision.allow == false
	"autonomous action requires at least one recorded human approval" in decision.reasons
}

# Advisory reads never launch work, so the approval rule does not gate them.
test_advisory_allowed_without_approval if {
	decision := ticket_to_pr.decision with input as {
		"action": "analyse_ticket",
		"ticket": {"readiness": "ready"},
		"risk": {"overall_risk": "low"},
		"repo": {"confidence": 90},
		"approval": {},
	}
	decision.allow == true
}

# An action the policy does not recognise is denied by default.
test_unknown_action_denied_by_default if {
	decision := ticket_to_pr.decision with input as {
		"action": "launch_the_missiles",
		"ticket": {"readiness": "ready"},
		"risk": {"overall_risk": "low"},
		"repo": {"confidence": 100},
		"approval": {},
	}
	decision.allow == false
}
