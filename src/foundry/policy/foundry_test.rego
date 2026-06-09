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
