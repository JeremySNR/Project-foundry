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

# Read-only / advisory actions: always allowed, still recorded. Anything not in
# this set or autonomous_actions is denied (default-deny).
advisory_actions := {
	"analyse_ticket",
	"create_plan",
	"request_approval",
	"request_changes",
}

# Actions that may never run autonomously in this version, regardless of risk
# level or approvals.
forbidden_actions := {
	"auto_merge",
	"production_deploy",
}

default is_autonomous := false

is_autonomous if input.action in autonomous_actions

default is_advisory := false

is_advisory if input.action in advisory_actions

default is_forbidden := false

is_forbidden if input.action in forbidden_actions

default is_unknown := false

is_unknown if {
	not is_autonomous
	not is_advisory
	not is_forbidden
}

# --- required approvals derived from sensitive areas ---
required_approvals contains "engineering" if input.risk.auth
required_approvals contains "engineering" if input.risk.infrastructure
required_approvals contains "security" if input.risk.customer_data
required_approvals contains "security" if input.risk.pii
required_approvals contains "security" if input.risk.payments

# --- deny reasons ---
deny_reasons contains msg if {
	is_forbidden
	msg := sprintf("action '%s' may never run autonomously in this version", [input.action])
}

deny_reasons contains msg if {
	is_unknown
	msg := sprintf("action '%s' is not covered by this policy; denying by default", [input.action])
}

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
	input.action == "retry_agent"
	attempt := object.get(input, ["retry", "attempt"], 0)
	max_attempts := object.get(input, ["retry", "max_attempts"], 2)
	attempt > max_attempts
	msg := sprintf("remediation attempt %d exceeds the maximum of %d", [attempt, max_attempts])
}

deny_reasons contains msg if {
	input.action == "retry_agent"
	max_cost := object.get(input, ["budget", "max_cost_usd"], null)
	max_cost != null
	cost := object.get(input, ["budget", "cost_usd"], 0)
	cost >= max_cost
	msg := sprintf("run spend $%.2f has reached the budget cap of $%.2f", [cost, max_cost])
}

deny_reasons contains msg if {
	is_autonomous
	some role in required_approvals
	not input.approval[role]
	msg := sprintf("sensitive work requires '%s' approval, which is missing", [role])
}

default allow := false

# Read-only / advisory actions are always allowed.
allow if is_advisory

# Autonomous actions are allowed only with zero deny reasons.
allow if {
	is_autonomous
	count(deny_reasons) == 0
}

allowed_agent_mode := "draft_pr" if {
	allow
	input.risk.overall_risk in {"low", "medium"}
} else := "human_only"

# Positive reasons mirror LocalPolicyEngine.evaluate() so audit trails are
# consistent regardless of which backend evaluated the policy.
allow_reasons contains msg if {
	is_advisory
	msg := sprintf("action '%s' is read-only / advisory", [input.action])
}

allow_reasons contains "all minimum policy checks passed" if {
	is_autonomous
	count(deny_reasons) == 0
}

# The reasons field surfaces deny reasons for blocked actions and allow reasons
# for permitted ones, matching the Python engine's behaviour.
reasons := [r | some r in deny_reasons] if count(deny_reasons) > 0
else := [r | some r in allow_reasons]

decision := {
	"allow": allow,
	"reasons": reasons,
	"allowed_agent_mode": allowed_agent_mode,
	"required_approvals": [a | some a in required_approvals],
}
