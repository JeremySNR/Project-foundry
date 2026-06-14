"""LLM-assisted epic decomposition (issue #35).

Offline, no DB, no network: a :class:`FakeStructuredLLM` stands in for the model
so the safety discipline is exercised deterministically - the heuristic floor
wins when it finds structure, the LLM recovers prose-described epics, proposed
repos are grounded in the ticket text, and any failure degrades to the floor.
"""

from __future__ import annotations

from foundry.engines.decomposition import EpicDecomposition, decompose_epic
from foundry.engines.llm import FakeStructuredLLM
from foundry.engines.llm_decomposition import LlmDecomposer, build_llm_decomposer
from foundry.schemas.ticket import RawTicket

AC = (
    "Acceptance Criteria:\n"
    "- The ledger uses the new write path\n"
    "- Existing reads are unchanged\n"
)


def _ticket(description: str, *, known_repositories=None, title="Migrate ledger") -> RawTicket:
    return RawTicket(
        issue_id="epic-1",
        issue_key="LIN-100",
        title=title,
        description=description,
        labels=["epic", "migration"],
        known_repositories=list(known_repositories or []),
    )


def _output(*repos, is_epic=True, reason="inferred"):
    return {
        "is_epic": is_epic,
        "repositories": [{"repo": r, "scope": s} for r, s in repos],
        "reason": reason,
    }


# -- the heuristic floor always wins ------------------------------------------


def test_explicit_section_uses_floor_without_calling_llm() -> None:
    ticket = _ticket(
        "Repositories:\n"
        "- billing-api: migrate the ledger writes\n"
        "- customer-web: update the checkout call\n\n" + AC
    )
    llm = FakeStructuredLLM([])  # would raise if consulted
    result = LlmDecomposer(llm).decompose(ticket)

    assert result == decompose_epic(ticket)
    assert llm.calls == []  # the model is never asked when the floor decides


def test_known_repositories_fallback_uses_floor_without_calling_llm() -> None:
    ticket = _ticket(
        "Codebase-wide rename.\n\n" + AC,
        known_repositories=["billing-api", "customer-web"],
    )
    llm = FakeStructuredLLM([])
    result = LlmDecomposer(llm).decompose(ticket)

    assert result.is_epic is True
    assert [c.known_repositories[0] for c in result.children] == [
        "billing-api",
        "customer-web",
    ]
    assert llm.calls == []


# -- the LLM recovers prose-described epics the floor declines ----------------


def test_llm_recovers_prose_epic_the_floor_misses() -> None:
    # No section, no known_repositories -> floor declines. The repos are named
    # in prose, so the LLM can recover the split.
    ticket = _ticket(
        "Migrate the ledger writes in billing-api and update the checkout "
        "call in customer-web as one coordinated change.\n\n" + AC
    )
    assert decompose_epic(ticket).is_epic is False  # floor really does decline

    llm = FakeStructuredLLM(
        [_output(("billing-api", "migrate the ledger writes"),
                 ("customer-web", "update the checkout call"))]
    )
    result = LlmDecomposer(llm).decompose(ticket)

    assert result.is_epic is True
    assert [c.known_repositories[0] for c in result.children] == [
        "billing-api",
        "customer-web",
    ]
    assert "migrate the ledger writes" in result.children[0].description
    assert "billing-api, customer-web" in result.reason
    assert llm.calls  # the model was consulted exactly because the floor folded


def test_children_carry_epic_acceptance_criteria_and_assumptions() -> None:
    ticket = _ticket(
        "Work spans billing-api and customer-web.\n\n" + AC
    )
    llm = FakeStructuredLLM([_output(("billing-api", "do A"), ("customer-web", "do B"))])
    result = LlmDecomposer(llm).decompose(ticket)

    for child in result.children:
        assert "The ledger uses the new write path" in child.description
        assert "Existing reads are unchanged" in child.description
    assert any("inferred by the LLM" in a for a in result.assumptions)
    assert "epic acceptance criteria applied to every child run" in result.assumptions
    # Child ids/keys/labels match the deterministic producer's shape.
    assert [c.issue_id for c in result.children] == [
        "epic-1::billing-api",
        "epic-1::customer-web",
    ]
    assert all(c.labels == ["epic", "migration"] for c in result.children)


# -- grounding: no invented repositories --------------------------------------


def test_ungrounded_repos_are_dropped_and_can_collapse_the_epic() -> None:
    # The model names two repos but neither appears in the ticket text -> both
    # dropped -> fewer than two grounded -> degrade to the floor (not an epic).
    ticket = _ticket("A generic refactor with no repo names.\n\n" + AC)
    llm = FakeStructuredLLM([_output(("ghost-svc", "x"), ("phantom-api", "y"))])
    result = LlmDecomposer(llm).decompose(ticket)

    assert result.is_epic is False
    assert result.children == []
    assert any("LLM decomposition unavailable" in a for a in result.assumptions)


def test_only_grounded_repos_are_kept() -> None:
    # One real repo (in the text), one hallucinated -> only one grounded ->
    # below the two-repo floor -> degrade.
    ticket = _ticket("Change billing-api and something else.\n\n" + AC)
    llm = FakeStructuredLLM([_output(("billing-api", "real"), ("made-up-svc", "fake"))])
    result = LlmDecomposer(llm).decompose(ticket)

    assert result.is_epic is False


def test_known_repositories_count_as_grounding() -> None:
    # The repos aren't in the prose, but they're associated with the ticket, so
    # they are grounded even though the floor's >=2-known fallback would also
    # have fired (this test pins that the LLM path grounds on known repos too).
    ticket = _ticket(
        "Coordinated change across our services.\n\n" + AC,
        known_repositories=["billing-api"],  # one known -> floor declines
    )
    assert decompose_epic(ticket).is_epic is False
    llm = FakeStructuredLLM(
        [_output(("billing-api", "a"), ("customer-web", "b"))]
    )
    # customer-web must be grounded in the text for this to be an epic; it isn't,
    # so only billing-api grounds -> degrade. Confirms text-grounding is required.
    result = LlmDecomposer(llm).decompose(ticket)
    assert result.is_epic is False


def test_prose_token_grounds_a_slashed_repo_name() -> None:
    ticket = _ticket(
        "Touch org/billing-api and org/customer-web together.\n\n" + AC
    )
    llm = FakeStructuredLLM(
        [_output(("org/billing-api", "a"), ("org/customer-web", "b"))]
    )
    result = LlmDecomposer(llm).decompose(ticket)
    assert [c.known_repositories[0] for c in result.children] == [
        "org/billing-api",
        "org/customer-web",
    ]


def test_non_slug_repo_proposals_are_skipped() -> None:
    # "the billing service" has spaces -> not a repo slug -> skipped, leaving
    # only one grounded repo -> degrade.
    ticket = _ticket("Change billing-api - the billing service.\n\n" + AC)
    llm = FakeStructuredLLM(
        [_output(("billing-api", "real"), ("the billing service", "prose"))]
    )
    assert LlmDecomposer(llm).decompose(ticket).is_epic is False


def test_duplicate_repo_proposal_is_deduped_first_wins() -> None:
    ticket = _ticket("Across billing-api and customer-web.\n\n" + AC)
    llm = FakeStructuredLLM(
        [_output(("billing-api", "first"),
                 ("billing-api", "second"),
                 ("customer-web", "third"))]
    )
    result = LlmDecomposer(llm).decompose(ticket)
    assert [c.known_repositories[0] for c in result.children] == [
        "billing-api",
        "customer-web",
    ]
    assert "first" in result.children[0].description


# -- the LLM may add an epic, never remove one --------------------------------


def test_llm_is_epic_false_keeps_floor_result() -> None:
    ticket = _ticket("Change billing-api and customer-web.\n\n" + AC)
    llm = FakeStructuredLLM([_output(is_epic=False)])
    result = LlmDecomposer(llm).decompose(ticket)
    # Model says "not an epic" and the floor agreed -> not an epic, no children.
    assert result.is_epic is False
    assert result.children == []


# -- degradation on failure ---------------------------------------------------


def test_llm_error_degrades_to_floor() -> None:
    ticket = _ticket("Change billing-api and customer-web.\n\n" + AC)
    llm = FakeStructuredLLM([])  # empty -> generate raises LLMError
    result = LlmDecomposer(llm).decompose(ticket)
    assert result.is_epic is False
    assert any("LLM decomposition unavailable" in a for a in result.assumptions)


def test_invalid_then_valid_output_uses_corrective_retry() -> None:
    ticket = _ticket("Across billing-api and customer-web.\n\n" + AC)
    llm = FakeStructuredLLM(
        [
            {"is_epic": True, "repositories": "not-a-list"},  # invalid -> retry
            _output(("billing-api", "a"), ("customer-web", "b")),
        ]
    )
    result = LlmDecomposer(llm).decompose(ticket)
    assert result.is_epic is True
    assert len(llm.calls) == 2
    # The retry prompt carries the validator feedback.
    assert "rejected by the schema validator" in llm.calls[1]["user"]


def test_two_invalid_outputs_exhaust_attempts_and_degrade() -> None:
    ticket = _ticket("Across billing-api and customer-web.\n\n" + AC)
    llm = FakeStructuredLLM([{"bad": 1}, {"bad": 2}])
    result = LlmDecomposer(llm, max_attempts=2).decompose(ticket)
    assert result.is_epic is False
    assert any("LLM decomposition unavailable" in a for a in result.assumptions)


def test_floor_is_injectable() -> None:
    # A floor that always declines lets the LLM drive every decision.
    class _NeverEpic:
        def decompose(self, ticket: RawTicket) -> EpicDecomposition:
            return EpicDecomposition(is_epic=False, reason="forced floor")

    ticket = _ticket("Touch billing-api and customer-web.\n\n" + AC)
    llm = FakeStructuredLLM([_output(("billing-api", "a"), ("customer-web", "b"))])
    result = LlmDecomposer(llm, floor=_NeverEpic()).decompose(ticket)
    assert result.is_epic is True


def test_build_factory_returns_decomposer() -> None:
    # No client/key needed to construct; the OpenAI client is created lazily.
    decomposer = build_llm_decomposer(model="gpt-5.5")
    assert isinstance(decomposer, LlmDecomposer)
