"""Delivery planning stage.

Turns a *ready* ticket plus verified context into a coding-agent-ready
:class:`DeliveryPlan`. Hard rule enforced here: ``agent_instructions`` is only
populated when the ticket is genuinely ready to build (acceptance criteria
present) AND a confident repository exists. Otherwise the plan is produced for
humans but carries no instructions an agent could act on.
"""

from __future__ import annotations

from typing import Protocol

from foundry.schemas.analysis import TicketAnalysis
from foundry.schemas.context import ContextBundle
from foundry.schemas.plan import DeliveryPlan, ImplementationStep, TestPlan
from foundry.schemas.risk import RiskAssessment
from foundry.schemas.ticket import RawTicket

# Default forbidden globs for the coding agent (also enforced by policy).
DEFAULT_FORBIDDEN_GLOBS = ["infra/**", "migrations/**", "**/.env*", "**/secrets/**"]

# The guardrail block and the PR-handoff closing are rendered by Foundry, never
# by a model: every planner (template or LLM) shares this exact text so an LLM
# planner can enrich the *plan* but can never relax a constraint.
CONSTRAINTS_BLOCK = """\
Constraints:
- Do not modify files matching: {forbidden}
- Do not add dependencies unless explicitly required.
- Do not perform database migrations.
- Do not change auth, payment, PII or infrastructure code.
- Stop and ask for human input if the change grows beyond the stated scope."""

CLOSING_BLOCK = """\
When you are done, open a draft PR whose description summarises what changed,
why, how it was tested, and any follow-ups."""

_INSTRUCTION_TEMPLATE = (
    """\
You are working on Linear issue {issue_key}: {title}.

Goal:
{goal}

Scope:
{scope}

Out of scope:
{out_of_scope}

Repository:
{repo}

Branch:
{branch}

Implementation plan:
{steps}

"""
    + CONSTRAINTS_BLOCK
    + "\n\n"
    + CLOSING_BLOCK
    + "\n"
)


class DeliveryPlanner(Protocol):
    def plan(
        self,
        ticket: RawTicket,
        analysis: TicketAnalysis,
        context: ContextBundle,
        risk: RiskAssessment,
    ) -> DeliveryPlan: ...


def branch_name_for(ticket: RawTicket) -> str:
    """Deterministic, sanitised branch name for a ticket."""
    slug = "".join(
        c if c.isalnum() else "-" for c in ticket.title.lower()
    ).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug[:40].strip("-")
    key = (ticket.issue_key or ticket.issue_id).lower()
    return f"foundry/{key}-{slug}" if slug else f"foundry/{key}"


class TemplatePlanner:
    """Reference planner that assembles a structured plan from upstream artifacts."""

    def plan(
        self,
        ticket: RawTicket,
        analysis: TicketAnalysis,
        context: ContextBundle,
        risk: RiskAssessment,
    ) -> DeliveryPlan:
        best_repo = context.best_repository
        affected = [best_repo.repo] if best_repo else []

        steps = [
            ImplementationStep(
                step=i,
                description=f"Satisfy acceptance criterion: {criterion}",
                expected_output="Code + tests covering this criterion.",
            )
            for i, criterion in enumerate(analysis.acceptance_criteria, start=1)
        ]

        test_plan = TestPlan(
            unit_tests=[f"Cover: {c}" for c in analysis.acceptance_criteria],
            manual_checks=context.test_commands,
        )

        open_questions = list(analysis.missing_information)

        plan = DeliveryPlan(
            goal=analysis.summary,
            scope=list(analysis.acceptance_criteria),
            out_of_scope=["Anything not listed in the acceptance criteria."],
            affected_repositories=affected,
            expected_files_or_areas=[],  # populated by richer context enrichers
            implementation_steps=steps,
            test_plan=test_plan,
            rollback_considerations=["Revert the PR; no data migration involved."],
            open_questions=open_questions,
            agent_instructions=None,
        )

        if self._can_instruct_agent(analysis, context):
            plan.agent_instructions = self._render_instructions(
                ticket, plan, best_repo.repo
            )
        return plan

    @staticmethod
    def _can_instruct_agent(analysis: TicketAnalysis, context: ContextBundle) -> bool:
        return analysis.is_ready_to_build and context.has_confident_repository()

    @staticmethod
    def _render_instructions(
        ticket: RawTicket, plan: DeliveryPlan, repo: str
    ) -> str:
        steps = "\n".join(
            f"{s.step}. {s.description}" for s in plan.implementation_steps
        )
        return _INSTRUCTION_TEMPLATE.format(
            issue_key=ticket.issue_key or ticket.issue_id,
            title=ticket.title,
            goal=plan.goal,
            scope="\n".join(f"- {s}" for s in plan.scope) or "- (none)",
            out_of_scope="\n".join(f"- {s}" for s in plan.out_of_scope),
            repo=repo,
            branch=branch_name_for(ticket),
            steps=steps or "(derive from acceptance criteria)",
            forbidden=", ".join(DEFAULT_FORBIDDEN_GLOBS),
        )
