"""Tests for the deterministic reference intelligence engines."""

from __future__ import annotations

from foundry.engines import (
    HeuristicAnalyzer,
    HeuristicRiskClassifier,
    StaticContextEnricher,
    TemplatePlanner,
    branch_name_for,
    merge_sensitive_keywords,
)
from foundry.engines.risk import _SENSITIVE_KEYWORDS
from foundry.schemas.common import (
    AgentMode,
    ApprovalRole,
    ImplementationReadiness,
    OverallRisk,
    WorkType,
)
from foundry.schemas.ticket import LinkedResource, RawTicket

READY_DESC = """\
We want customers to be able to favourite items.

Acceptance Criteria:
- A favourites button exists on each item
- Favourites persist across sessions
"""


def _ready_ticket(**overrides) -> RawTicket:
    base = dict(
        issue_id="i-1",
        issue_key="LIN-123",
        title="Add customer favourites",
        description=READY_DESC,
        known_repositories=["customer-web"],
    )
    base.update(overrides)
    return RawTicket(**base)


# -- analyzer -----------------------------------------------------------------


def test_clear_feature_with_ac_is_ready() -> None:
    analysis = HeuristicAnalyzer().analyse(_ready_ticket())
    assert analysis.work_type is WorkType.FEATURE
    assert analysis.implementation_readiness is ImplementationReadiness.READY
    assert len(analysis.acceptance_criteria) == 2
    assert analysis.is_ready_to_build is True


def test_vague_feature_needs_clarification() -> None:
    ticket = RawTicket(issue_id="i", issue_key="LIN-9", title="Make it nicer")
    analysis = HeuristicAnalyzer().analyse(ticket)
    assert analysis.implementation_readiness is ImplementationReadiness.NEEDS_CLARIFICATION
    assert "acceptance criteria" in analysis.missing_information


def test_bug_without_repro_needs_clarification() -> None:
    ticket = RawTicket(
        issue_id="i",
        issue_key="LIN-7",
        title="Checkout button is broken",
        description="It errors sometimes.",
    )
    analysis = HeuristicAnalyzer().analyse(ticket)
    assert analysis.work_type is WorkType.BUG
    assert "reproduction steps" in analysis.missing_information
    assert analysis.is_ready_to_build is False


def test_empty_ticket_is_not_buildable() -> None:
    analysis = HeuristicAnalyzer().analyse(
        RawTicket(issue_id="i", issue_key="LIN-0", title="x")
    )
    assert analysis.is_ready_to_build is False
    assert analysis.ambiguity_score >= 50


def test_question_is_not_suitable() -> None:
    ticket = RawTicket(
        issue_id="i",
        issue_key="LIN-Q",
        title="Question: how do we handle refunds?",
    )
    analysis = HeuristicAnalyzer().analyse(ticket)
    assert analysis.work_type is WorkType.QUESTION
    assert analysis.implementation_readiness is ImplementationReadiness.NOT_SUITABLE


def test_analysis_is_deterministic() -> None:
    a = HeuristicAnalyzer().analyse(_ready_ticket())
    b = HeuristicAnalyzer().analyse(_ready_ticket())
    assert a == b


def test_ac_heading_requires_word_boundary() -> None:
    # "ac:" must not match inside "Mac:"/"Tarmac:" and open a phantom
    # acceptance-criteria section that flips readiness to READY.
    ticket = RawTicket(
        issue_id="i",
        issue_key="LIN-AC",
        title="Repave the apron",
        description="Tarmac: needs repaving\n- lay new asphalt\n",
    )
    analysis = HeuristicAnalyzer().analyse(ticket)
    assert analysis.acceptance_criteria == []
    assert "acceptance criteria" in analysis.missing_information
    assert analysis.is_ready_to_build is False


def test_ac_abbreviation_heading_still_detected() -> None:
    # The legitimate "AC:" abbreviation must still open the section.
    ticket = RawTicket(
        issue_id="i",
        issue_key="LIN-AC2",
        title="Add favourites",
        description="AC:\n- it works\n- it persists\n",
        known_repositories=["customer-web"],
    )
    analysis = HeuristicAnalyzer().analyse(ticket)
    assert analysis.acceptance_criteria == ["it works", "it persists"]


# -- enrichment ---------------------------------------------------------------


def test_known_repo_is_confident() -> None:
    ticket = _ready_ticket()
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)
    assert context.has_confident_repository() is True
    assert context.best_repository.repo == "customer-web"


def test_no_repo_signal_yields_unknowns() -> None:
    ticket = RawTicket(issue_id="i", issue_key="LIN-3", title="Do a thing", description="x")
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)
    assert context.has_confident_repository() is False
    assert context.unknowns


def test_catalog_keyword_match() -> None:
    ticket = RawTicket(
        issue_id="i",
        issue_key="LIN-4",
        title="Improve favourites",
        description="Acceptance Criteria:\n- favourites work",
    )
    analysis = HeuristicAnalyzer().analyse(ticket)
    enricher = StaticContextEnricher(repo_catalog={"customer-web": ["favourites"]})
    context = enricher.enrich(ticket, analysis)
    assert any(c.repo == "customer-web" for c in context.candidate_repositories)


def test_explicit_association_outranks_heavy_keyword_match() -> None:
    # A pile of coincidental keyword hits is capped below the explicit-association
    # tier (90), so an explicitly-associated repo always wins the routing.
    ticket = RawTicket(
        issue_id="i",
        issue_key="LIN-OUT",
        title="favourites wishlist basket",
        description=(
            "Acceptance Criteria:\n"
            "- favourites wishlist basket cart checkout all work together\n"
        ),
        known_repositories=["explicit-repo"],
    )
    analysis = HeuristicAnalyzer().analyse(ticket)
    enricher = StaticContextEnricher(
        repo_catalog={
            "keyword-repo": ["favourites", "wishlist", "basket", "cart", "checkout"],
        }
    )
    context = enricher.enrich(ticket, analysis)
    by_repo = {c.repo: c.confidence for c in context.candidate_repositories}
    assert by_repo["explicit-repo"] == 90
    assert by_repo["keyword-repo"] <= 89
    assert context.best_repository.repo == "explicit-repo"


def test_linked_pr_surfaces_related_pr() -> None:
    ticket = _ready_ticket(
        linked_resources=[
            LinkedResource(kind="github_pr", url="https://github.com/x/y/pull/5", repo="y")
        ]
    )
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)
    assert "https://github.com/x/y/pull/5" in context.related_prs


# -- risk ---------------------------------------------------------------------


def test_clean_feature_is_low_risk() -> None:
    ticket = _ready_ticket()
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)
    risk = HeuristicRiskClassifier().classify(ticket, analysis, context)
    assert risk.overall_risk is OverallRisk.LOW
    assert risk.allowed_agent_mode is AgentMode.DRAFT_PR


def test_auth_ticket_flags_sensitive_and_requires_engineering() -> None:
    ticket = _ready_ticket(
        title="Change login session token handling",
        description="Acceptance Criteria:\n- auth tokens rotate\nThis touches auth/login.",
    )
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)
    risk = HeuristicRiskClassifier().classify(ticket, analysis, context)
    assert risk.sensitive_areas.auth is True
    assert risk.overall_risk is OverallRisk.HIGH
    assert ApprovalRole.ENGINEERING in risk.required_approvals
    assert risk.allowed_agent_mode is AgentMode.HUMAN_ONLY


def test_no_repo_match_is_blocked_risk() -> None:
    ticket = RawTicket(
        issue_id="i",
        issue_key="LIN-5",
        title="Add favourites",
        description="Acceptance Criteria:\n- it works",
    )
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)  # no repo signal
    risk = HeuristicRiskClassifier().classify(ticket, analysis, context)
    assert risk.overall_risk is OverallRisk.BLOCKED


def test_extra_keywords_flag_area_builtins_miss() -> None:
    # "PAN" (primary account number) is real cardholder-data vocabulary the
    # built-in payments keywords don't carry. Without extras it stays low risk.
    ticket = _ready_ticket(
        title="Tokenise the PAN before storage",
        description="Acceptance Criteria:\n- the PAN never lands in logs",
    )
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)
    assert HeuristicRiskClassifier().classify(
        ticket, analysis, context
    ).sensitive_areas.payments is False

    merged = merge_sensitive_keywords({"payments": ["pan", "cardholder"]})
    risk = HeuristicRiskClassifier(keywords=merged).classify(ticket, analysis, context)
    assert risk.sensitive_areas.payments is True
    assert risk.overall_risk is OverallRisk.HIGH
    assert ApprovalRole.SECURITY in risk.required_approvals
    assert risk.allowed_agent_mode is AgentMode.HUMAN_ONLY


def test_extra_keywords_are_strictly_additive() -> None:
    # The built-in payments keywords still fire when extras are supplied.
    ticket = _ready_ticket(
        title="Update Stripe invoice handling",
        description="Acceptance Criteria:\n- invoices reconcile",
    )
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)
    merged = merge_sensitive_keywords({"payments": ["pan"]})
    risk = HeuristicRiskClassifier(keywords=merged).classify(ticket, analysis, context)
    assert risk.sensitive_areas.payments is True


def test_merge_sensitive_keywords_preserves_floor_and_dedupes() -> None:
    base = merge_sensitive_keywords(None)
    # Floor preserved verbatim when there are no extras.
    assert base["payments"] == _SENSITIVE_KEYWORDS["payments"]
    # Extras appended, lower-cased and de-duplicated; built-ins kept first.
    merged = merge_sensitive_keywords({"payments": ["PAN", "pan", "stripe"]})
    assert merged["payments"][: len(_SENSITIVE_KEYWORDS["payments"])] == (
        _SENSITIVE_KEYWORDS["payments"]
    )
    assert merged["payments"].count("stripe") == 1  # already a built-in, not re-added
    assert merged["payments"].count("pan") == 1  # case-folded duplicate collapsed


def test_merge_sensitive_keywords_ignores_unknown_area() -> None:
    # Settings validates area names; the merge helper itself just ignores them
    # so a stray key can never synthesise a non-existent area.
    merged = merge_sensitive_keywords({"not_an_area": ["whatever"]})
    assert "not_an_area" not in merged
    assert set(merged) == set(_SENSITIVE_KEYWORDS)


# -- planner ------------------------------------------------------------------


def test_ready_plan_has_agent_instructions() -> None:
    ticket = _ready_ticket()
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)
    risk = HeuristicRiskClassifier().classify(ticket, analysis, context)
    plan = TemplatePlanner().plan(ticket, analysis, context, risk)
    assert plan.agent_instructions is not None
    assert "LIN-123" in plan.agent_instructions
    assert len(plan.implementation_steps) == 2
    assert plan.affected_repositories == ["customer-web"]


def test_not_ready_plan_has_no_agent_instructions() -> None:
    ticket = RawTicket(issue_id="i", issue_key="LIN-6", title="Vague")
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)
    risk = HeuristicRiskClassifier().classify(ticket, analysis, context)
    plan = TemplatePlanner().plan(ticket, analysis, context, risk)
    assert plan.agent_instructions is None


def test_branch_name_is_sanitised() -> None:
    ticket = _ready_ticket(title="Add Customer Favourites!!!")
    assert branch_name_for(ticket) == "foundry/lin-123-add-customer-favourites"


# -- diff-aware risk classification --------------------------------------------


def test_sensitive_areas_for_paths_matches_globs() -> None:
    from foundry.engines.risk import sensitive_areas_for_paths

    globs = {
        "auth": ("**/auth/**", "**/login/**"),
        "payments": ("**/billing/**",),
    }
    touched = sensitive_areas_for_paths(
        ["src/auth/session.ts", "billing/charge.py", "src/ui/button.tsx"],
        globs,
    )
    assert touched == {
        "auth": ["src/auth/session.ts"],
        "payments": ["billing/charge.py"],
    }


def test_sensitive_areas_for_paths_empty_when_clean() -> None:
    from foundry.engines.risk import sensitive_areas_for_paths

    assert sensitive_areas_for_paths(["src/ui/button.tsx"], {"auth": ("**/auth/**",)}) == {}


def test_glob_match_handles_leading_doublestar() -> None:
    from foundry.engines.risk import glob_match

    # fnmatch alone would miss a top-level directory against "**/x/**".
    assert glob_match("auth/handler.py", "**/auth/**")
    assert glob_match("src/auth/handler.py", "**/auth/**")
    assert not glob_match("src/author/file.py", "**/auth/**")


def test_forbidden_path_match_is_depth_agnostic_for_bare_globs() -> None:
    """Issue #177: a bare relative forbidden glob blocks the dir at any depth,
    not just the repo root, so an operator's ``secrets/**`` protects a nested
    ``app/secrets/...`` too (the sticky BLOCK, not the softer escalation)."""
    from foundry.engines.risk import forbidden_path_match, glob_match

    # The gap glob_match leaves: a bare relative glob only matches the top level.
    assert not glob_match("app/secrets/key.pem", "secrets/**")
    assert not glob_match("services/api/migrations/0001.py", "migrations/**")

    # forbidden_path_match closes it - bare relative pattern matches at any depth.
    assert forbidden_path_match("app/secrets/key.pem", "secrets/**")
    assert forbidden_path_match("secrets/key.pem", "secrets/**")
    assert forbidden_path_match("services/api/migrations/0001.py", "migrations/**")

    # A bare filename glob is likewise depth-agnostic (stricter = safe).
    assert forbidden_path_match("build/Dockerfile", "Dockerfile")

    # Still a strict superset of glob_match: everything glob_match blocks, this
    # blocks too (the depth-agnostic default globs are unchanged).
    assert forbidden_path_match("services/api/migrations/0001.py", "**/migrations/**")
    assert forbidden_path_match("app/.env.local", "**/.env*")

    # A rooted pattern is honoured as written - not expanded to any depth.
    assert not forbidden_path_match("app/secrets/key.pem", "/secrets/**")
    assert forbidden_path_match("secrets/key.pem", "/secrets/**") is False

    # Genuine non-matches stay non-matches (no over-broad blocking).
    assert not forbidden_path_match("src/secretsmanager/util.py", "secrets/**")
    assert not forbidden_path_match("src/app/config.py", "secrets/**")
