"""Risk classification stage.

Produces a :class:`RiskAssessment` from the ticket text and context. This is
*advisory* input to the policy gate - it flags sensitive areas and proposes a
risk level, but the hard allow/deny decision is made by ``foundry.policy``.
"""

from __future__ import annotations

import fnmatch
from typing import Mapping, Protocol, Sequence

from foundry.schemas.analysis import TicketAnalysis
from foundry.schemas.common import AgentMode, ApprovalRole, OverallRisk
from foundry.schemas.context import ContextBundle
from foundry.schemas.risk import (
    DiffRiskFindings,
    RiskAssessment,
    RiskEvidence,
    SensitiveAreas,
)
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


def merge_sensitive_keywords(
    extra: Mapping[str, Sequence[str]] | None,
) -> dict[str, tuple[str, ...]]:
    """Layer operator-supplied keywords *on top of* the built-in floor.

    The built-in ``_SENSITIVE_KEYWORDS`` table is the offline/no-key reference
    detection set. A deployment can extend it - teach the heuristic its own
    domain vocabulary (e.g. ``"pan"``/``"cardholder"`` for ``payments``,
    ``"member record"`` for ``customer_data``) - via ``risk.extra_sensitive_keywords``
    (issue #31). This is the ticket-text twin of the already-configurable
    diff-stage ``policy.sensitive_path_globs``.

    Strictly additive: every built-in keyword is preserved and the extras are
    appended (lower-cased, de-duplicated, built-ins first), so detection can only
    ever be *added to*, never removed - more keywords flag more areas, never
    fewer, so risk can only escalate (invariant #1). Extras keyed on an area that
    is not a built-in sensitive area are ignored here; ``Settings`` validates the
    area names at load (fail-closed), so a typo never reaches this point silently.
    """
    merged = {area: tuple(keywords) for area, keywords in _SENSITIVE_KEYWORDS.items()}
    for area, keywords in (extra or {}).items():
        if area not in merged:
            continue
        seen = set(merged[area])
        added = []
        for kw in keywords:
            normalised = kw.lower()
            if normalised and normalised not in seen:
                seen.add(normalised)
                added.append(normalised)
        if added:
            merged[area] = (*merged[area], *added)
    return merged


def glob_match(path: str, pattern: str) -> bool:
    """fnmatch with a usable ``**/`` prefix: ``**/auth/**`` also matches a path
    that *starts* with ``auth/`` (fnmatch alone would require a leading slash).
    """
    if fnmatch.fnmatch(path, pattern):
        return True
    return pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:])


def sensitive_areas_for_paths(
    files: list[str], globs_map: Mapping[str, tuple[str, ...]]
) -> dict[str, list[str]]:
    """Classify changed file paths against sensitive-area globs.

    This is the diff-aware half of risk classification: the upfront pass reads
    the *ticket text*, but the risk that matters materialises in the *diff*.
    Returns ``{area: [matching files...]}`` for every area actually touched.
    """
    touched: dict[str, list[str]] = {}
    for path in files:
        for area, patterns in globs_map.items():
            if any(glob_match(path, p) for p in patterns):
                touched.setdefault(area, []).append(path)
    return {area: sorted(paths) for area, paths in sorted(touched.items())}


class RiskClassifier(Protocol):
    def classify(
        self, ticket: RawTicket, analysis: TicketAnalysis, context: ContextBundle
    ) -> RiskAssessment: ...


class DiffRiskClassifier(Protocol):
    """Classifies the sensitive areas a PR diff touches, from its file paths."""

    def classify_diff(
        self, files: list[str], ticket: RawTicket | None = None
    ) -> DiffRiskFindings: ...


class GlobDiffRiskClassifier:
    """Deterministic diff-stage classifier: sensitive-area path globs only.

    This is the floor every other diff classifier builds on - its matches are
    never dropped, only added to.
    """

    def __init__(self, globs_map: Mapping[str, tuple[str, ...]]) -> None:
        self._globs = globs_map

    def classify_diff(
        self, files: list[str], ticket: RawTicket | None = None
    ) -> DiffRiskFindings:
        areas = sensitive_areas_for_paths(files, self._globs)
        evidence = [
            RiskEvidence(
                area=area,
                detail=f"changed path(s) match sensitive globs: {', '.join(paths)}",
                source="diff",
            )
            for area, paths in areas.items()
        ]
        return DiffRiskFindings(areas=areas, evidence=evidence)


class HeuristicRiskClassifier:
    """Keyword-driven reference risk classifier.

    ``keywords`` defaults to the built-in ``_SENSITIVE_KEYWORDS`` floor. A
    deployment can pass a merged map (see :func:`merge_sensitive_keywords`) to
    extend detection with its own domain vocabulary without forking
    (``risk.extra_sensitive_keywords``, issue #31) - strictly additive, so the
    classifier can only ever flag *more* areas, never fewer.
    """

    def __init__(self, keywords: Mapping[str, Sequence[str]] | None = None) -> None:
        self._keywords: Mapping[str, Sequence[str]] = (
            keywords if keywords is not None else _SENSITIVE_KEYWORDS
        )

    def classify(
        self, ticket: RawTicket, analysis: TicketAnalysis, context: ContextBundle
    ) -> RiskAssessment:
        # Use risk_blob (title + description only) to avoid stale comments
        # inflating risk scores.
        blob = ticket.risk_blob()
        hits = {
            area: [k for k in keywords if k in blob]
            for area, keywords in self._keywords.items()
        }
        sensitive = SensitiveAreas(**{area: bool(found) for area, found in hits.items()})

        reasons: list[str] = []
        evidence: list[RiskEvidence] = []
        for area in sensitive.names():
            reasons.append(f"Ticket text suggests it touches '{area}'.")
            evidence.append(
                RiskEvidence(
                    area=area,
                    detail="keyword(s) in ticket title/description: "
                    + ", ".join(f"'{k}'" for k in hits[area]),
                    source="heuristic",
                )
            )

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
            evidence=evidence,
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
