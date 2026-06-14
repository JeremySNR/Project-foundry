"""Epic decomposition producer - the pure ``decompose_epic`` (issue #35).

Offline, no DB: the deterministic split of an epic ticket into one scoped child
per repo. The orchestrator wiring (parent + child runs) is exercised in
``test_epics.py``.
"""

from __future__ import annotations

from foundry.engines.decomposition import decompose_epic
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


# -- explicit repositories section --------------------------------------------


def test_repos_section_with_scopes_decomposes_per_repo() -> None:
    ticket = _ticket(
        "Roll the ledger migration across services.\n\n"
        "Repositories:\n"
        "- billing-api: migrate the ledger writes\n"
        "- customer-web: update the checkout call\n\n"
        + AC
    )
    result = decompose_epic(ticket)

    assert result.is_epic is True
    assert [c.known_repositories for c in result.children] == [
        ["billing-api"],
        ["customer-web"],
    ]
    # Each child is scoped and carries the per-repo scope as its headline.
    assert "migrate the ledger writes" in result.children[0].description
    assert "update the checkout call" in result.children[1].description
    assert "billing-api" in result.reason


def test_children_carry_epic_acceptance_criteria() -> None:
    ticket = _ticket(
        "Repos:\n- a-service: do A\n- b-service: do B\n\n" + AC
    )
    result = decompose_epic(ticket)
    for child in result.children:
        assert "The ledger uses the new write path" in child.description
        assert "Existing reads are unchanged" in child.description
    assert "epic acceptance criteria applied to every child run" in result.assumptions


def test_child_ids_are_distinct_and_derived() -> None:
    ticket = _ticket("Repos:\n- org/a: x\n- org/b: y\n\n" + AC)
    result = decompose_epic(ticket)
    ids = [c.issue_id for c in result.children]
    keys = [c.issue_key for c in result.children]
    assert ids == ["epic-1::org-a", "epic-1::org-b"]
    assert keys == ["LIN-100-1", "LIN-100-2"]
    assert len(set(ids)) == len(ids)
    # Labels carry down; each child is scoped to exactly one repo.
    assert all(c.labels == ["epic", "migration"] for c in result.children)
    assert all(len(c.known_repositories) == 1 for c in result.children)


def test_checkbox_markers_are_tolerated() -> None:
    ticket = _ticket(
        "Affected repositories:\n"
        "- [ ] billing-api: migrate writes\n"
        "- [x] customer-web: update reads\n\n" + AC
    )
    result = decompose_epic(ticket)
    assert [c.known_repositories[0] for c in result.children] == [
        "billing-api",
        "customer-web",
    ]


def test_bare_repo_names_without_scope_decompose() -> None:
    ticket = _ticket("Repositories:\n- billing-api\n- customer-web\n\n" + AC)
    result = decompose_epic(ticket)
    assert result.is_epic is True
    assert [c.known_repositories[0] for c in result.children] == [
        "billing-api",
        "customer-web",
    ]


def test_non_repo_like_bullets_are_skipped() -> None:
    # "the billing service" has whitespace -> not a repo slug; only the two
    # real repo bullets count, so this still decomposes into exactly two.
    ticket = _ticket(
        "Repositories:\n"
        "- the billing service should change: prose, not a repo\n"
        "- billing-api: migrate writes\n"
        "- customer-web: update reads\n\n" + AC
    )
    result = decompose_epic(ticket)
    assert [c.known_repositories[0] for c in result.children] == [
        "billing-api",
        "customer-web",
    ]


def test_section_ends_at_non_bullet_line() -> None:
    ticket = _ticket(
        "Repositories:\n"
        "- billing-api: migrate writes\n"
        "- customer-web: update reads\n"
        "Some trailing prose that should not be parsed.\n"
        "- not-a-repo: ignored, the section already ended\n\n" + AC
    )
    result = decompose_epic(ticket)
    assert [c.known_repositories[0] for c in result.children] == [
        "billing-api",
        "customer-web",
    ]


def test_repeated_repo_is_deduped_first_wins() -> None:
    ticket = _ticket(
        "Repos:\n- a-service: first\n- a-service: second\n- b-service: third\n\n" + AC
    )
    result = decompose_epic(ticket)
    assert [c.known_repositories[0] for c in result.children] == ["a-service", "b-service"]
    assert "first" in result.children[0].description


# -- known_repositories fallback ----------------------------------------------


def test_fallback_to_known_repositories() -> None:
    ticket = _ticket(
        "Codebase-wide rename, no structured breakdown.\n\n" + AC,
        known_repositories=["billing-api", "customer-web", "search-svc"],
    )
    result = decompose_epic(ticket)
    assert result.is_epic is True
    assert len(result.children) == 3
    assert [c.known_repositories[0] for c in result.children] == [
        "billing-api",
        "customer-web",
        "search-svc",
    ]
    assert "associated with 3 repositories" in result.reason


def test_section_takes_priority_over_known_repositories() -> None:
    ticket = _ticket(
        "Repositories:\n- a-service: do A\n- b-service: do B\n\n" + AC,
        known_repositories=["c-service", "d-service", "e-service"],
    )
    result = decompose_epic(ticket)
    # The explicit section wins over the known_repositories fallback.
    assert [c.known_repositories[0] for c in result.children] == ["a-service", "b-service"]


# -- not an epic --------------------------------------------------------------


def test_single_repo_is_not_an_epic() -> None:
    ticket = _ticket(AC, known_repositories=["billing-api"])
    result = decompose_epic(ticket)
    assert result.is_epic is False
    assert result.children == []
    assert "not an epic" in result.reason


def test_no_repos_is_not_an_epic() -> None:
    ticket = _ticket(AC)
    result = decompose_epic(ticket)
    assert result.is_epic is False
    assert result.children == []


def test_single_bullet_section_is_not_an_epic() -> None:
    # One repo in the section and none elsewhere -> not enough to decompose.
    ticket = _ticket("Repositories:\n- billing-api: do it\n\n" + AC)
    result = decompose_epic(ticket)
    assert result.is_epic is False


def test_decompose_is_pure() -> None:
    ticket = _ticket("Repos:\n- a-svc: x\n- b-svc: y\n\n" + AC)
    assert decompose_epic(ticket) == decompose_epic(ticket)
