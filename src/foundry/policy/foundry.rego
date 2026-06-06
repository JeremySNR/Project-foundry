# Foundry Ticket-to-PR policy (production backend).
#
# This Rego bundle mirrors foundry/policy/engine.py:LocalPolicyEngine. Keep the
# two in lock-step; the Python evaluator is the default for local/test use and
# this bundle is used when an OPA server is configured.
#
# Decision document: data.foundry.ticket_to_pr.decision
#   { "allow": bool, "reasons": [string], "allowed_agent_mode": string,
#     "required_approvals": [string] }

package foundry.ticket_to_pr

import rego.v1

repo_confidence_threshold := 70

# Actions that launch or progress autonomous work and must pass the gate.
autonomous_actions := {
	"start_agent",
	"create_branch",
	"open_pr",
	"retry_agent",
	"mark_complete",
}

default is_autonomous := false

is_autonomous if input.action in autonomous_actions

# --- required approvals derived from sensitive areas ---
required_approvals contains "engineering" if input.risk.auth
required_approvals contains "engineering" if input.risk.infrastructure
required_approvals contains "security" if input.risk.customer_data
required_approvals contains "security" if input.risk.pii
required_approvals contains "security" if input.risk.payments

# --- deny reasons (only evaluated for autonomous actions) ---
deny_reasons contains "production deployment is blocked in the MVP" if {
	is_autonomous
	input.risk.production_deploy
}

deny_reasons contains "database migrations are blocked in the MVP" if {
	is_autonomous
	input.risk.database_migration
}

deny_reasons contains msg if {
	is_autonomous
	input.repo.confidence < repo_confidence_threshold
	msg := sprintf("repository confidence %d is below the threshold of %d", [input.repo.confidence, repo_confidence_threshold])
}

deny_reasons contains msg if {
	is_autonomous
	input.ticket.readiness != "ready"
	msg := sprintf("ticket readiness is '%s', not 'ready'", [input.ticket.readiness])
}

deny_reasons contains "risk assessment marked the work as blocked" if {
	is_autonomous
	input.risk.overall_risk == "blocked"
}

deny_reasons contains msg if {
	is_autonomous
	some role in required_approvals
	not input.approval[role]
	msg := sprintf("sensitive work requires '%s' approval, which is missing", [role])
}

default allow := false

# Read-only / advisory actions are always allowed.
allow if not is_autonomous

# Autonomous actions are allowed only with zero deny reasons.
allow if {
	is_autonomous
	count(deny_reasons) == 0
}

allowed_agent_mode := "draft_pr" if {
	allow
	input.risk.overall_risk in {"low", "medium"}
} else := "human_only"

decision := {
	"allow": allow,
	"reasons": [r | some r in deny_reasons],
	"allowed_agent_mode": allowed_agent_mode,
	"required_approvals": [a | some a in required_approvals],
}
