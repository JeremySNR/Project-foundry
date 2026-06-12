"""Tests for the LLM-backed risk classifiers (fake StructuredLLM, no network).

The contract under test: the deterministic heuristics are a hard floor and the
LLM pass may only escalate - add sensitive-area flags, raise the overall level,
attach cited evidence - never downgrade. LLM failures degrade to the floor.
"""

from __future__ import annotations

from foundry.engines import (
    FakeStructuredLLM,
    HeuristicAnalyzer,
    HeuristicRiskClassifier,
    LlmDiffRiskClassifier,
    LlmRiskClassifier,
    StaticContextEnricher,
)
from foundry.orchestrator import FoundryOrchestrator
from foundry.schemas.common import AgentMode, ApprovalRole, OverallRisk
from foundry.schemas.ticket import RawTicket

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


def _analysed(ticket: RawTicket):
    analysis = HeuristicAnalyzer().analyse(ticket)
    context = StaticContextEnricher().enrich(ticket, analysis)
    return analysis, context


def _llm_response(**overrides) -> dict:
    base = {"overall_risk": "low", "findings": [], "summary": ""}
    base.update(overrides)
    return base


_GLOBS = {"auth": ("**/auth/**",), "payments": ("**/billing/**",)}


# -- ticket stage: escalation ---------------------------------------------------


def test_llm_finding_escalates_clean_ticket() -> None:
    # No keyword hits, but the LLM recognises session issuance in disguise.
    ticket = _ready_ticket(
        description=READY_DESC + "\nAlso refresh the remember-me cookie lifetime."
    )
    analysis, context = _analysed(ticket)
    llm = FakeStructuredLLM(
        [
            _llm_response(
                overall_risk="medium",
                findings=[
                    {
                        "area": "auth",
                        "evidence": "'remember-me cookie lifetime' is session handling",
                    }
                ],
            )
        ]
    )
    risk = LlmRiskClassifier(llm).classify(ticket, analysis, context)
    # The auth flag drives the heuristic area->risk mapping: HIGH even though
    # the model itself only said "medium".
    assert risk.sensitive_areas.auth is True
    assert risk.overall_risk is OverallRisk.HIGH
    assert ApprovalRole.ENGINEERING in risk.required_approvals
    assert risk.allowed_agent_mode is AgentMode.HUMAN_ONLY
    cited = [e for e in risk.evidence if e.source == "llm" and e.area == "auth"]
    assert cited and "remember-me" in cited[0].detail


def test_llm_added_payments_area_pulls_in_security_approval() -> None:
    ticket = _ready_ticket(description=READY_DESC + "\nShow the card-on-file widget.")
    analysis, context = _analysed(ticket)
    llm = FakeStructuredLLM(
        [
            _llm_response(
                findings=[
                    {"area": "payments", "evidence": "'card-on-file widget' is payments UI"}
                ]
            )
        ]
    )
    risk = LlmRiskClassifier(llm).classify(ticket, analysis, context)
    assert risk.sensitive_areas.payments is True
    assert ApprovalRole.SECURITY in risk.required_approvals


# -- ticket stage: the floor ------------------------------------------------------


def test_llm_low_verdict_cannot_downgrade_heuristic_high() -> None:
    ticket = _ready_ticket(
        title="Change login session token handling",
        description=READY_DESC + "\nThis touches auth/login and jwt issuance.",
    )
    analysis, context = _analysed(ticket)
    baseline = HeuristicRiskClassifier().classify(ticket, analysis, context)
    assert baseline.overall_risk is OverallRisk.HIGH  # the floor we must keep

    llm = FakeStructuredLLM([_llm_response(overall_risk="low", findings=[])])
    risk = LlmRiskClassifier(llm).classify(ticket, analysis, context)
    assert risk.overall_risk is OverallRisk.HIGH
    assert risk.sensitive_areas.auth is True
    assert ApprovalRole.ENGINEERING in risk.required_approvals
    # Heuristic evidence survives the merge.
    assert any(e.source == "heuristic" and e.area == "auth" for e in risk.evidence)


def test_blocked_routing_outcome_survives_llm_low() -> None:
    # No confident repository: BLOCKED regardless of what the model says
    # (the schema does not even let it output "blocked").
    ticket = _ready_ticket(known_repositories=[])
    analysis, context = _analysed(ticket)
    llm = FakeStructuredLLM([_llm_response(overall_risk="low")])
    risk = LlmRiskClassifier(llm).classify(ticket, analysis, context)
    assert risk.overall_risk is OverallRisk.BLOCKED
    assert risk.allowed_agent_mode is AgentMode.HUMAN_ONLY


# -- ticket stage: validation retry and degraded mode -----------------------------


def test_invalid_then_valid_retries_with_feedback() -> None:
    ticket = _ready_ticket()
    analysis, context = _analysed(ticket)
    # "blocked" is not a permitted LLM verdict - schema-rejected, then retried.
    llm = FakeStructuredLLM(
        [_llm_response(overall_risk="blocked"), _llm_response(overall_risk="medium")]
    )
    risk = LlmRiskClassifier(llm, max_attempts=2).classify(ticket, analysis, context)
    assert len(llm.calls) == 2
    assert "invalid" in llm.calls[1]["user"].lower()
    assert risk.overall_risk is OverallRisk.MEDIUM


def test_persistent_llm_failure_degrades_to_floor_loudly() -> None:
    ticket = _ready_ticket()
    analysis, context = _analysed(ticket)
    bad = _llm_response(overall_risk="not_a_level")
    llm = FakeStructuredLLM([bad, bad])
    risk = LlmRiskClassifier(llm, max_attempts=2).classify(ticket, analysis, context)
    baseline = HeuristicRiskClassifier().classify(ticket, analysis, context)
    # The heuristic floor stands, and the degradation is recorded, not silent.
    assert risk.overall_risk is baseline.overall_risk
    assert risk.sensitive_areas == baseline.sensitive_areas
    assert any("unavailable" in r for r in risk.risk_reasons)
    assert any(
        e.source == "llm" and "unavailable" in e.detail for e in risk.evidence
    )


class _RaisingClient:
    """Fake OpenAI client surface whose API call raises a raw SDK-style error."""

    class chat:  # noqa: N801 - mimics the SDK attribute path
        class completions:  # noqa: N801
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("429 Too Many Requests (simulated SDK error)")


def test_raw_sdk_exception_degrades_ticket_classifier_to_floor() -> None:
    # Issue #12: openai.RateLimitError etc. used to propagate raw past the
    # LLMError catch and crash intake. Through the wrapped OpenAIStructuredLLM
    # seam they now degrade to the heuristic floor like any other LLM failure.
    from foundry.engines.llm import OpenAIStructuredLLM

    ticket = _ready_ticket()
    analysis, context = _analysed(ticket)
    llm = OpenAIStructuredLLM(client=_RaisingClient())
    risk = LlmRiskClassifier(llm).classify(ticket, analysis, context)
    baseline = HeuristicRiskClassifier().classify(ticket, analysis, context)
    assert risk.overall_risk is baseline.overall_risk
    assert any("unavailable" in r for r in risk.risk_reasons)


# -- ticket stage: prompt hygiene --------------------------------------------------


def test_prompt_marks_ticket_untrusted_and_excludes_comments() -> None:
    ticket = _ready_ticket(comments=["stale comment mentioning stripe and prod deploy"])
    analysis, context = _analysed(ticket)
    llm = FakeStructuredLLM([_llm_response()])
    LlmRiskClassifier(llm).classify(ticket, analysis, context)
    call = llm.calls[0]
    assert "UNTRUSTED" in call["system"]
    assert "Add customer favourites" in call["user"]
    # risk_blob discipline: comments never feed the risk pass.
    assert "stale comment" not in call["user"]


# -- diff stage --------------------------------------------------------------------


def test_diff_glob_floor_always_kept() -> None:
    llm = FakeStructuredLLM([{"findings": []}])
    findings = LlmDiffRiskClassifier(llm, _GLOBS).classify_diff(
        ["src/auth/session.ts", "src/ui/button.tsx"]
    )
    assert findings.areas == {"auth": ["src/auth/session.ts"]}
    assert any(e.source == "diff" and e.area == "auth" for e in findings.evidence)


def test_diff_llm_adds_area_globs_missed_with_evidence() -> None:
    llm = FakeStructuredLLM(
        [
            {
                "findings": [
                    {
                        "area": "auth",
                        "paths": ["src/tokens/issue.ts"],
                        "evidence": "touches session issuance in src/tokens/issue.ts",
                    }
                ]
            }
        ]
    )
    findings = LlmDiffRiskClassifier(llm, _GLOBS).classify_diff(
        ["src/tokens/issue.ts", "src/ui/button.tsx"]
    )
    assert findings.areas == {"auth": ["src/tokens/issue.ts"]}
    assert any(
        e.source == "llm" and "session issuance" in e.detail for e in findings.evidence
    )


def test_diff_hallucinated_paths_are_dropped() -> None:
    llm = FakeStructuredLLM(
        [
            {
                "findings": [
                    {
                        "area": "payments",
                        "paths": ["src/made/up.ts"],
                        "evidence": "imaginary",
                    }
                ]
            }
        ]
    )
    findings = LlmDiffRiskClassifier(llm, _GLOBS).classify_diff(["src/ui/button.tsx"])
    assert findings.areas == {}
    assert not any(e.source == "llm" for e in findings.evidence)


def test_diff_llm_failure_falls_back_to_globs() -> None:
    llm = FakeStructuredLLM([])  # raises LLMError on first call
    findings = LlmDiffRiskClassifier(llm, _GLOBS).classify_diff(
        ["billing/charge.py"]
    )
    assert findings.areas == {"payments": ["billing/charge.py"]}


def test_diff_raw_sdk_exception_falls_back_to_globs() -> None:
    from foundry.engines.llm import OpenAIStructuredLLM

    llm = OpenAIStructuredLLM(client=_RaisingClient())
    findings = LlmDiffRiskClassifier(llm, _GLOBS).classify_diff(
        ["billing/charge.py", "src/ui/button.tsx"]
    )
    assert findings.areas == {"payments": ["billing/charge.py"]}


def test_raw_sdk_exception_never_breaks_record_pr() -> None:
    # Issue #12's worst consequence: an OpenAI rate limit inside record_pr ->
    # _unexpected_sensitive_areas aborted the whole PR webhook event and lost
    # its audit rows. The wrapped seam keeps PR-event processing alive.
    from foundry.agents.manual import InMemoryFakeProvider
    from foundry.engines.llm import OpenAIStructuredLLM
    from foundry.schemas.common import PRStatus, RunStatus
    from foundry.schemas.pr import PullRequestState

    provider = InMemoryFakeProvider()
    orch = FoundryOrchestrator(
        _session_factory(),
        provider=provider,
        diff_risk_classifier=LlmDiffRiskClassifier(
            OpenAIStructuredLLM(client=_RaisingClient()), _GLOBS
        ),
    )
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    orch.approve(run_id, user="lead@example.com")
    job = orch.dispatch_agent(run_id)
    final = provider.run(job.job_id)
    pr = PullRequestState(
        repo="customer-web",
        pr_number=1,
        url=final.pr_url,
        branch=final.branch,
        status=PRStatus.OPEN,
        files_changed=["src/features/favourites/index.ts"],
    )
    assert orch.record_pr(run_id, pr) is RunStatus.PR_OPEN


# -- the floor when it is a custom classifier ----------------------------------------


class _CustomFloor:
    """A floor whose risk is NOT encoded in sensitive-area flags - e.g. fed by
    an external scanner. _combine must not undercut it (issue #12, latent)."""

    def classify(self, ticket, analysis, context):
        from foundry.schemas.risk import RiskAssessment, SensitiveAreas

        return RiskAssessment(
            overall_risk=OverallRisk.HIGH,
            risk_reasons=["external scanner flagged this ticket"],
            sensitive_areas=SensitiveAreas(),
            allowed_agent_mode=AgentMode.HUMAN_ONLY,
            required_approvals=[ApprovalRole.SECURITY],
            evidence=[],
        )


def test_custom_floor_risk_approvals_and_mode_survive_combine() -> None:
    ticket = _ready_ticket()
    analysis, context = _analysed(ticket)
    # The LLM sees nothing; the combined area flags are all false, so before
    # the fix the recompute-from-flags dropped the floor's HIGH/SECURITY/mode.
    llm = FakeStructuredLLM([_llm_response(overall_risk="low")])
    risk = LlmRiskClassifier(llm, floor=_CustomFloor()).classify(
        ticket, analysis, context
    )
    assert risk.overall_risk is OverallRisk.HIGH
    assert ApprovalRole.SECURITY in risk.required_approvals
    assert risk.allowed_agent_mode is AgentMode.HUMAN_ONLY


# -- orchestrator drop-in -----------------------------------------------------------


def _session_factory():
    from foundry.db import create_all, make_engine, make_session_factory

    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)


def test_drops_in_as_orchestrator_risk_classifier() -> None:
    llm = FakeStructuredLLM([_llm_response()])
    orch = FoundryOrchestrator(
        _session_factory(), risk_classifier=LlmRiskClassifier(llm)
    )
    run_id = orch.intake_and_plan(_ready_ticket(), trigger_type="label")
    assert orch.get_run(run_id).status.value == "waiting_approval"
    assert len(llm.calls) == 1
