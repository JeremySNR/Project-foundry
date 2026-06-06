"""Risk classification stage.

Produces a :class:`RiskAssessment` from the ticket text and context. This is
*advisory* input to the policy gate - it flags sensitive areas and proposes a
risk level, but the hard allow/deny decision is made by ``foundry.policy``.
"""

from __future__ import annotations

from typing import Protocol

from foundry.schemas.analysis import TicketAnalysis
from foundry.schemas.common import AgentMode, ApprovalRole, OverallRisk
from foundry.schemas.context import ContextBundle
from foundry.schemas.risk import RiskAssessment, SensitiveAreas
from foundry.schemas.ticket import RawTicket

# Keyword signals for each sensitive area. Prefer multi-word phrases over
# single words to reduce false positives (e.g. "error" is not a payment signal,
# "checkout" alone doesn't mean payments, "infra" alone is too broad).
_SENSITIVE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "auth": ("oauth", "sso", "session token", "login flow", "authentication", "authorisation",
             "authorization", "access token", "jwt", "password reset"),
    "payments": ("payment", "billing", "stripe", "invoice", "payment gateway",
                 "credit card", "card number", "transaction"),
    "customer_data": ("customer data", "customer record", "personal data"),
    "pii": ("pii", "gdpr", "email address", "phone number", "passport",
            "date of birth", "national insurance", "social security"),
    "database_migration": ("migration", "schema change", "alter table", "drop column",
                            "drop table", "add column"),
    "infrastructure": ("terraform", "kubernetes", "helm chart", "deployment config",
                       "infrastructure as code", "k8s manifest"),
    "production_deploy": ("deploy to production", "prod deploy", "release to prod",
                          "production release"),
}


class RiskClassifier(Protocol):
    def classify(
        self, ticket: RawTicket, analysis: TicketAnalysis, context: ContextBundle
    ) -> RiskAssessment: ...


class HeuristicRiskClassifier:
    """Keyword-driven reference risk classifier."""

    def classify(
        self, ticket: RawTicket, analysis: TicketAnalysis, context: ContextBundle
    ) -> RiskAssessment:
        # Use risk_blob (title + description only) to avoid stale comments
        # inflating risk scores.
        blob = ticket.risk_blob()
        flags = {
            area: any(k in blob for k in keywords)
            for area, keywords in _SENSITIVE_KEYWORDS.items()
        }
        sensitive = SensitiveAreas(**flags)

        reasons: list[str] = []
        for area in sensitive.names():
            reasons.append(f"Ticket text suggests it touches '{area}'.")

        overall = self._overall_risk(sensitive, context)
        if overall is OverallRisk.BLOCKED:
            reasons.append("No confident repository match; work cannot be scoped.")

        required = self._required_approvals(sensitive)
        mode = self._agent_mode(overall, sensitive)

        return RiskAssessment(
            overall_risk=overall,
            risk_reasons=reasons,
            sensitive_areas=sensitive,
            allowed_agent_mode=mode,
            required_approvals=required,
        )

    @staticmethod
    def _overall_risk(sensitive: SensitiveAreas, context: ContextBundle) -> OverallRisk:
        if not context.has_confident_repository():
            return OverallRisk.BLOCKED
        if sensitive.production_deploy or sensitive.database_migration:
            return OverallRisk.HIGH
        if sensitive.auth or sensitive.payments or sensitive.customer_data or sensitive.pii:
            return OverallRisk.HIGH
        if sensitive.infrastructure:
            return OverallRisk.MEDIUM
        return OverallRisk.LOW

    @staticmethod
    def _required_approvals(sensitive: SensitiveAreas) -> list[ApprovalRole]:
        required: list[ApprovalRole] = []
        if sensitive.auth or sensitive.infrastructure:
            required.append(ApprovalRole.ENGINEERING)
        if sensitive.customer_data or sensitive.pii or sensitive.payments:
            required.append(ApprovalRole.SECURITY)
        # Preserve order, de-duplicate.
        seen: set[ApprovalRole] = set()
        return [r for r in required if not (r in seen or seen.add(r))]

    @staticmethod
    def _agent_mode(overall: OverallRisk, sensitive: SensitiveAreas) -> AgentMode:
        if overall in (OverallRisk.BLOCKED, OverallRisk.HIGH):
            return AgentMode.HUMAN_ONLY
        return AgentMode.DRAFT_PR
