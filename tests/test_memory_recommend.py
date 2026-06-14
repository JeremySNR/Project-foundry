"""recommend_provider: the scorecards turned into one explainable agent pick."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import FoundryRunOutcome
from foundry.memory.scorecards import recommend_provider
from foundry.schemas.common import RunStatus

NOW = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)

_counter = 0


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)


def _add_outcome(
    session,
    *,
    provider: str | None,
    outcome: str,
    work_type: str | None = "feature",
    repo: str | None = "billing-service",
    jobs_count: int = 1,
    cost_usd: float | None = None,
    completed_at: datetime | None = None,
):
    """Insert a run + its derived outcome row directly (FK-safe)."""
    global _counter
    _counter += 1
    rid = f"r-{_counter}"
    session.add(
        FoundryRun(
            id=rid,
            linear_issue_id=f"i-{_counter}",
            linear_issue_key=f"ENG-{_counter}",
            status=RunStatus.COMPLETE,
            trigger_type="label",
        )
    )
    session.add(
        FoundryRunOutcome(
            run_id=rid,
            linear_issue_id=f"i-{_counter}",
            issue_key_prefix="ENG",
            outcome=outcome,
            repo=repo if provider else None,
            provider=provider,
            work_type=work_type,
            trigger_type="label",
            created_at_run=NOW - timedelta(days=1),
            completed_at=completed_at or NOW,
            jobs_count=jobs_count,
            cost_usd=cost_usd,
            recorded_at=NOW,
        )
    )


def test_empty_history_recommends_nothing(session_factory) -> None:
    with session_factory() as s:
        report = recommend_provider(s, work_type="feature")
    assert report["recommended"] is None
    assert report["ranked"] == []
    assert "not enough evidence" in report["reason"]


def test_recommends_the_higher_merge_rate_provider(session_factory) -> None:
    with session_factory() as s:
        # claude_code: 3 of 3 merged on features.
        for _ in range(3):
            _add_outcome(s, provider="claude_code", outcome="merged")
        # cursor: 1 of 3 merged on features.
        _add_outcome(s, provider="cursor", outcome="merged")
        _add_outcome(s, provider="cursor", outcome="failed")
        _add_outcome(s, provider="cursor", outcome="blocked")
        s.commit()
        report = recommend_provider(s, work_type="feature")

    assert report["recommended"] == "claude_code"
    assert report["ranked"][0]["provider"] == "claude_code"
    # cursor's smoothed rate is below 0.5, so it is listed but not eligible.
    cursor = next(c for c in report["ranked"] if c["provider"] == "cursor")
    assert cursor["eligible"] is False
    assert "claude_code" in report["reason"]


def test_below_min_samples_is_ineligible(session_factory) -> None:
    with session_factory() as s:
        _add_outcome(s, provider="cursor", outcome="merged")
        _add_outcome(s, provider="cursor", outcome="merged")
        s.commit()
        # 2 runs < default floor of 3 -> not enough to recommend.
        default = recommend_provider(s, work_type="feature")
        # Lower the floor and it qualifies.
        lowered = recommend_provider(s, work_type="feature", min_samples=2)

    assert default["recommended"] is None
    assert default["ranked"][0]["eligible"] is False
    assert lowered["recommended"] == "cursor"


def test_candidate_allow_list_excludes_others(session_factory) -> None:
    with session_factory() as s:
        for _ in range(3):
            _add_outcome(s, provider="claude_code", outcome="merged")
        for _ in range(3):
            _add_outcome(s, provider="cursor", outcome="merged")
        s.commit()
        # claude_code wins on merit, but only cursor is dispatchable.
        report = recommend_provider(
            s, work_type="feature", candidates=["cursor", "webhook"]
        )

    assert report["recommended"] == "cursor"
    assert {c["provider"] for c in report["ranked"]} == {"cursor"}
    assert report["candidates"] == ["cursor", "webhook"]


def test_scope_falls_back_when_pair_is_thin(session_factory) -> None:
    with session_factory() as s:
        # Only one feature run in api-service (thin pair), but plenty of
        # feature runs overall for claude_code.
        _add_outcome(s, provider="claude_code", outcome="merged", repo="api-service")
        for _ in range(3):
            _add_outcome(
                s, provider="claude_code", outcome="merged", repo="billing-service"
            )
        s.commit()
        report = recommend_provider(s, work_type="feature", repo="api-service")

    # The (feature, api-service) pair has < 3 runs, so it falls back to the
    # work-type scope and still recommends with confidence.
    assert report["scope"] == "feature"
    assert report["recommended"] == "claude_code"


def test_pair_scope_used_when_it_has_evidence(session_factory) -> None:
    with session_factory() as s:
        for _ in range(3):
            _add_outcome(
                s, provider="claude_code", outcome="merged", repo="api-service"
            )
        s.commit()
        report = recommend_provider(s, work_type="feature", repo="api-service")

    assert report["scope"] == "feature in api-service"
    assert report["recommended"] == "claude_code"


def test_cheaper_agent_breaks_a_confidence_tie(session_factory) -> None:
    with session_factory() as s:
        # Both 3 of 3 merged (identical confidence); cursor is cheaper.
        for _ in range(3):
            _add_outcome(s, provider="claude_code", outcome="merged", cost_usd=5.0)
        for _ in range(3):
            _add_outcome(s, provider="cursor", outcome="merged", cost_usd=1.0)
        s.commit()
        report = recommend_provider(s, work_type="feature")

    assert report["recommended"] == "cursor"
    assert "~$1.0/run" in report["reason"]


def test_since_window_filters_old_runs(session_factory) -> None:
    with session_factory() as s:
        for _ in range(3):
            _add_outcome(
                s,
                provider="cursor",
                outcome="merged",
                completed_at=NOW - timedelta(days=300),
            )
        s.commit()
        recent = recommend_provider(
            s, work_type="feature", since=NOW - timedelta(days=90)
        )

    # All three runs are outside the 90-day window: no evidence in scope.
    assert recent["recommended"] is None
    assert recent["ranked"] == []


def test_undispatched_runs_never_recommended(session_factory) -> None:
    with session_factory() as s:
        for _ in range(3):
            _add_outcome(s, provider=None, outcome="rejected")
        s.commit()
        report = recommend_provider(s, work_type="feature")
    assert report["recommended"] is None
    assert report["ranked"] == []
