"""Ticket Intelligence Engine - analysis stage.

``TicketAnalyzer`` is the protocol the orchestrator depends on. A real
implementation will be an LLM/LangGraph agent constrained to emit
``TicketAnalysis`` JSON. ``HeuristicAnalyzer`` is a deterministic reference
implementation: it makes the contract exercisable end-to-end with no model, and
encodes the hard rules ("no acceptance criteria → do not start coding";
"bug without reproduction → needs clarification") so they are tested regardless
of which backend produces the analysis.
"""

from __future__ import annotations

import re
from typing import Protocol

from foundry.schemas.analysis import TicketAnalysis
from foundry.schemas.common import ImplementationReadiness, WorkType
from foundry.schemas.ticket import RawTicket


class TicketAnalyzer(Protocol):
    def analyse(self, ticket: RawTicket) -> TicketAnalysis: ...


# Keyword → work type. All keywords are matched; the type with the most hits wins.
# Priority is used as a tiebreaker: earlier entries beat later ones.
_WORK_TYPE_KEYWORDS: tuple[tuple[WorkType, tuple[str, ...]], ...] = (
    (WorkType.INCIDENT, ("incident", "outage", "sev1", "sev2", "production down")),
    (WorkType.BUG, ("bug", "broken", "crash", "regression", "fails", "traceback", "not working")),
    (WorkType.TECH_DEBT, ("refactor", "tech debt", "cleanup", "deprecate", "migrate code")),
    (WorkType.QUESTION, ("question", "how do we", "should we", "what is the")),
)

_REPRODUCTION_HINTS = ("steps to reproduce", "reproduce", "repro:", "stack trace", "logs")

# Headings under which acceptance criteria are commonly listed.
# Handles plain text, markdown bold (**...**), and ATX headings (## ...).
_AC_HEADING = re.compile(
    r"(?:\*{1,2}|#{1,6}\s*)?"  # optional markdown bold or heading prefix
    r"\b"                       # word boundary: don't match "ac:" inside "Mac:"/"Tarmac:"
    r"(acceptance criteria|acceptance:|ac:|definition of done|dod:)"
    r"(?:\*{1,2})?",            # optional closing bold markers
    re.IGNORECASE,
)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.):])\s+(.*\S)\s*$")


def _detect_work_type(blob: str) -> WorkType:
    """Pick the work type with the most keyword hits; priority breaks ties."""
    scores: dict[WorkType, int] = {}
    for work_type, keywords in _WORK_TYPE_KEYWORDS:
        count = sum(1 for k in keywords if k in blob)
        if count:
            scores[work_type] = count

    if not scores:
        return WorkType.FEATURE

    # Highest score wins; earlier position in tuple breaks ties.
    priority = {wt: i for i, (wt, _) in enumerate(_WORK_TYPE_KEYWORDS)}
    return min(scores, key=lambda wt: (-scores[wt], priority[wt]))


def _extract_acceptance_criteria(description: str) -> list[str]:
    """Pull bullet lines that follow an acceptance-criteria heading.

    Deterministic and forgiving: once a heading is seen, consecutive bullet lines
    are collected until a blank line or a non-bullet line ends the section.
    """
    lines = description.splitlines()
    criteria: list[str] = []
    in_section = False
    for line in lines:
        if _AC_HEADING.search(line):
            in_section = True
            # Allow "Acceptance criteria: do the thing" on one line.
            after = _AC_HEADING.sub("", line).strip(" :-*#")
            if after:
                criteria.append(after)
            continue
        if in_section:
            match = _BULLET.match(line)
            if match:
                criteria.append(match.group(1).strip())
            elif line.strip() == "":
                continue
            else:
                # A non-blank, non-bullet line ends the section.
                break
    return criteria


class HeuristicAnalyzer:
    """Deterministic, rule-based reference analyzer (no model required)."""

    def analyse(self, ticket: RawTicket) -> TicketAnalysis:
        blob = ticket.text_blob()
        work_type = _detect_work_type(blob)
        acceptance_criteria = _extract_acceptance_criteria(ticket.description)

        missing: list[str] = []
        if not acceptance_criteria:
            missing.append("acceptance criteria")
        if work_type is WorkType.BUG and not any(h in blob for h in _REPRODUCTION_HINTS):
            missing.append("reproduction steps")
        if not ticket.description.strip():
            missing.append("a description of the desired outcome")

        readiness = self._readiness(work_type, acceptance_criteria, missing)
        ambiguity = self._ambiguity_score(ticket, acceptance_criteria, missing)
        confidence = max(0, 100 - ambiguity)

        return TicketAnalysis(
            ticket_id=ticket.issue_key or ticket.issue_id,
            title=ticket.title,
            work_type=work_type,
            summary=self._summary(ticket, work_type),
            user_problem=ticket.description.strip() or None,
            business_value=None,
            acceptance_criteria=acceptance_criteria,
            missing_information=missing,
            assumptions=[],
            ambiguity_score=ambiguity,
            implementation_readiness=readiness,
            confidence=confidence,
        )

    @staticmethod
    def _readiness(
        work_type: WorkType, acceptance_criteria: list[str], missing: list[str]
    ) -> ImplementationReadiness:
        if work_type is WorkType.QUESTION:
            # A question is not a unit of implementable work.
            return ImplementationReadiness.NOT_SUITABLE
        if not acceptance_criteria or missing:
            return ImplementationReadiness.NEEDS_CLARIFICATION
        return ImplementationReadiness.READY

    @staticmethod
    def _ambiguity_score(
        ticket: RawTicket, acceptance_criteria: list[str], missing: list[str]
    ) -> int:
        score = 0
        if not acceptance_criteria:
            score += 40
        if len(ticket.description.strip()) < 40:
            score += 25
        # Each missing field adds proportionally less weight to avoid saturation.
        score += sum(min(20, 15 - i * 3) for i, _ in enumerate(missing))
        return min(score, 100)

    @staticmethod
    def _summary(ticket: RawTicket, work_type: WorkType) -> str:
        return f"{work_type.value.replace('_', ' ').title()}: {ticket.title}".strip()
