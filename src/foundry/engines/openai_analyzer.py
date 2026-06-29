"""OpenAI (GPT-5.5) backed ticket analyzer.

Implements the :class:`TicketAnalyzer` protocol using a :class:`StructuredLLM`.
This is the *pre-approval gate* intelligence: judge readiness, surface what's
missing, and normalise acceptance criteria from natural-language tickets - the
thing the heuristic analyzer can't actually do. It deliberately does **not** plan
the implementation; that stays with the coding agent (Cursor).

Robustness: the model output is validated against the ``TicketAnalysis`` schema
and retried with corrective feedback; identity fields are overwritten from the
ticket so a hallucinated id/title can't slip through. If the LLM is unavailable
(or persistently returns invalid output), the analyzer degrades to the
deterministic :class:`HeuristicAnalyzer` and records the degradation in the
analysis assumptions - mirroring the risk classifiers' degrade-to-floor design -
so an LLM outage never fails intake.
"""

from __future__ import annotations

from pydantic import ValidationError

from foundry.schemas.analysis import TicketAnalysis
from foundry.schemas.ticket import RawTicket

from .analyzer import HeuristicAnalyzer, TicketAnalyzer
from .llm import LLMError, StructuredLLM

_SYSTEM_PROMPT = """\
You analyse engineering tickets. You do NOT write code and you do NOT produce an \
implementation plan. Classify the work, identify missing information, extract or \
normalise acceptance criteria, and decide whether the ticket is ready to implement.

Hard rules:
- Return ONLY a JSON object matching the TicketAnalysis schema.
- Do not invent facts. List anything you infer under "assumptions", separate from facts.
- If the ticket is unclear or lacks acceptance criteria, set implementation_readiness \
to "needs_clarification" and list what is missing.
- If the ticket is a question rather than a unit of work, set "not_suitable".
- For a bug, missing reproduction steps means it is not ready.
- Only set "ready" when the work is clear enough that a coding agent could start.
"""


def _render_user(ticket: RawTicket) -> str:
    parts = [
        f"Issue: {ticket.issue_key or ticket.issue_id}",
        f"Title: {ticket.title}",
        f"Labels: {', '.join(ticket.labels) or '(none)'}",
        "",
        "Description:",
        ticket.description or "(empty)",
    ]
    if ticket.comments:
        parts += ["", "Comments:", *ticket.comments]
    return "\n".join(parts)


class OpenAITicketAnalyzer:
    def __init__(
        self,
        llm: StructuredLLM,
        *,
        max_attempts: int = 2,
        fallback: TicketAnalyzer | None = None,
    ) -> None:
        self._llm = llm
        self._max_attempts = max(1, max_attempts)
        self._fallback = fallback or HeuristicAnalyzer()

    def analyse(self, ticket: RawTicket) -> TicketAnalysis:
        try:
            return self._llm_analyse(ticket)
        except LLMError as exc:
            return self._degraded(ticket, exc)

    def _llm_analyse(self, ticket: RawTicket) -> TicketAnalysis:
        schema = TicketAnalysis.model_json_schema()
        user = _render_user(ticket)
        last_error: Exception | None = None

        for attempt in range(self._max_attempts):
            prompt = user if attempt == 0 else f"{user}\n\n{self._feedback(last_error)}"
            raw = self._llm.generate(
                system=_SYSTEM_PROMPT,
                user=prompt,
                schema=schema,
                schema_name="TicketAnalysis",
            )
            # A conforming StructuredLLM returns a JSON object, but guard the
            # shape before indexing so a non-dict response degrades to the
            # heuristic floor (issue contract: an LLM never fails intake)
            # rather than raising an uncaught TypeError from the assignments.
            if not isinstance(raw, dict):
                last_error = LLMError(
                    f"LLM returned a JSON {type(raw).__name__}, expected an object"
                )
                continue
            # Identity comes from the ticket, never the model.
            raw["ticket_id"] = ticket.issue_key or ticket.issue_id
            raw["title"] = ticket.title
            try:
                return TicketAnalysis.model_validate(raw)
            except ValidationError as exc:
                last_error = exc

        raise LLMError(
            f"OpenAI analyzer could not produce a valid TicketAnalysis after "
            f"{self._max_attempts} attempts: {last_error}"
        )

    def _degraded(self, ticket: RawTicket, exc: LLMError) -> TicketAnalysis:
        """Fall back to the deterministic analyzer, loudly.

        The heuristic baseline is conservative (no acceptance criteria means
        not buildable), so degrading can only park a run for clarification,
        never wave unready work through - and the note lands in the persisted
        TICKET_ANALYSIS artifact so the degradation is auditable.
        """
        note = f"LLM analysis unavailable ({exc}); deterministic heuristic analysis used."
        analysis = self._fallback.analyse(ticket)
        return analysis.model_copy(
            update={"assumptions": [*analysis.assumptions, note]}
        )

    @staticmethod
    def _feedback(error: Exception | None) -> str:
        return (
            "Your previous response was invalid and rejected by the schema "
            f"validator:\n{error}\nReturn a corrected JSON object only."
        )


def build_openai_analyzer(
    *, model: str = "gpt-5.5", client: object | None = None
) -> OpenAITicketAnalyzer:
    """Convenience factory for the live OpenAI-backed analyzer."""
    from .llm import OpenAIStructuredLLM

    return OpenAITicketAnalyzer(OpenAIStructuredLLM(client=client, model=model))
