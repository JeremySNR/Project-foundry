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
