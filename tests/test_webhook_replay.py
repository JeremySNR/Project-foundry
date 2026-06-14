"""Webhook replay protection: durable DB-backed dedup + replay-age validation.

Covers issue #27 - replacing the in-process dedup ``set`` (per-process, lost on
restart, unbounded) with a durable, bounded, cross-worker table, plus opt-in
timestamp-age rejection for providers that carry a timestamp (Linear).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from foundry.agents.manual import InMemoryFakeProvider
from foundry.api import create_app
from foundry.api.dedup import WebhookDeduplicator, webhook_timestamp_fresh
from foundry.api.security import compute_signature
from foundry.db import create_all, make_engine, make_session_factory
from foundry.db.models import FoundryWebhookDelivery
from foundry.orchestrator import FoundryOrchestrator

SECRET = "test-secret"
NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """A movable clock so TTL/replay-age windows are deterministic offline."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


# --- WebhookDeduplicator (unit) ----------------------------------------------


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)


def _delivery_count(session_factory) -> int:
    with session_factory() as session:
        return session.execute(
            select(func.count()).select_from(FoundryWebhookDelivery)
        ).scalar_one()


def test_first_delivery_is_new_then_duplicate(session_factory) -> None:
    dedup = WebhookDeduplicator(session_factory, ttl_seconds=None)
    assert dedup.seen("linear", "evt-1") is False
    assert dedup.seen("linear", "evt-1") is True
    assert dedup.seen("linear", "evt-1") is True


def test_same_id_different_provider_is_not_a_collision(session_factory) -> None:
    dedup = WebhookDeduplicator(session_factory, ttl_seconds=None)
    assert dedup.seen("linear", "shared") is False
    # Same id from a different provider is a distinct delivery.
    assert dedup.seen("github", "shared") is False
    assert dedup.seen("github", "shared") is True


def test_missing_delivery_id_is_always_new(session_factory) -> None:
    dedup = WebhookDeduplicator(session_factory, ttl_seconds=None)
    assert dedup.seen("linear", None) is False
    assert dedup.seen("linear", "") is False
    # Nothing without an id can be deduped, so nothing is stored.
    assert _delivery_count(session_factory) == 0


def test_ttl_prunes_and_lets_an_aged_id_be_seen_again(session_factory) -> None:
    clock = FakeClock(NOW)
    dedup = WebhookDeduplicator(session_factory, ttl_seconds=100, clock=clock)

    assert dedup.seen("linear", "old") is False
    assert dedup.seen("linear", "old") is True  # still inside the TTL

    # Advance past the TTL: the row is pruned, so the id reads as new again and
    # the table does not retain the stale row.
    clock.now = NOW + timedelta(seconds=200)
    assert dedup.seen("linear", "old") is False
    assert _delivery_count(session_factory) == 1


def test_ttl_none_keeps_rows(session_factory) -> None:
    dedup = WebhookDeduplicator(session_factory, ttl_seconds=None)
    dedup.seen("linear", "a")
    dedup.seen("linear", "b")
    assert _delivery_count(session_factory) == 2


# --- webhook_timestamp_fresh (unit) ------------------------------------------


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def test_timestamp_fresh_within_window() -> None:
    ts = _ms(NOW - timedelta(seconds=30))
    assert webhook_timestamp_fresh(ts, now=NOW, max_age_seconds=300) is True


def test_timestamp_stale_rejected() -> None:
    ts = _ms(NOW - timedelta(seconds=600))
    assert webhook_timestamp_fresh(ts, now=NOW, max_age_seconds=300) is False


def test_timestamp_small_future_skew_allowed_but_far_future_rejected() -> None:
    near = _ms(NOW + timedelta(seconds=30))
    far = _ms(NOW + timedelta(seconds=600))
    assert webhook_timestamp_fresh(near, now=NOW, max_age_seconds=300) is True
    assert webhook_timestamp_fresh(far, now=NOW, max_age_seconds=300) is False


def test_timestamp_missing_or_garbage_is_fail_closed() -> None:
    assert webhook_timestamp_fresh(None, now=NOW, max_age_seconds=300) is False
    assert webhook_timestamp_fresh("not-a-number", now=NOW, max_age_seconds=300) is False


# --- integration via the app -------------------------------------------------


def _basic_payload(issue_id="issue-1", key="LIN-1", *, ts: int | None = None) -> dict:
    payload: dict = {
        "data": {
            "id": issue_id,
            "issueId": issue_id,
            "identifier": key,
            "title": "Do something",
            "labels": [{"name": "foundry:candidate"}],
            "actor": {"name": "po@example.com"},
        }
    }
    if ts is not None:
        payload["webhookTimestamp"] = ts
    return payload


def _post_linear(client, payload, *, delivery):
    body = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhooks/linear",
        content=body,
        headers={
            "Linear-Delivery": delivery,
            "Linear-Signature": "sha256=" + compute_signature(SECRET, body),
            "Content-Type": "application/json",
        },
    )


def _make_app(session_factory, **overrides) -> TestClient:
    orch = FoundryOrchestrator(session_factory, provider=InMemoryFakeProvider())
    kwargs = dict(
        webhook_secret=SECRET,
        session_factory=session_factory,
        orchestrator=orch,
    )
    kwargs.update(overrides)
    return TestClient(create_app(**kwargs))


def test_dedup_is_durable_across_app_instances(session_factory) -> None:
    """Two app instances on the same DB share dedup - the in-process set could
    not do this. This is the multi-worker / post-restart guarantee."""
    app_a = _make_app(session_factory)
    app_b = _make_app(session_factory)

    first = _post_linear(app_a, _basic_payload(), delivery="shared-1")
    assert first.json()["status"] == "started"

    # A different worker process (fresh app.state) still sees the duplicate.
    second = _post_linear(app_b, _basic_payload(), delivery="shared-1")
    assert second.json()["status"] == "duplicate"

    # Exactly one delivery row, shared between the two instances.
    assert _delivery_count(session_factory) == 1


def test_replay_age_rejects_stale_and_missing_timestamp(session_factory) -> None:
    clock = FakeClock(NOW)
    client = _make_app(
        session_factory,
        webhook_replay_max_age_seconds=300,
        clock=clock,
    )

    fresh = _post_linear(
        client, _basic_payload(ts=_ms(NOW - timedelta(seconds=10))), delivery="t-1"
    )
    assert fresh.json()["status"] == "started"

    stale = _post_linear(
        client,
        _basic_payload(issue_id="issue-2", ts=_ms(NOW - timedelta(seconds=900))),
        delivery="t-2",
    )
    assert stale.status_code == 401
    assert "replay" in stale.json()["detail"]

    missing = _post_linear(
        client, _basic_payload(issue_id="issue-3"), delivery="t-3"
    )
    assert missing.status_code == 401


def test_replay_age_disabled_by_default_ignores_timestamp(session_factory) -> None:
    """With no max-age configured, an ancient (or absent) timestamp is fine -
    the static fixtures and existing tests rely on this."""
    client = _make_app(session_factory)
    # A timestamp from a year ago is accepted because the check is off.
    old = _post_linear(
        client,
        _basic_payload(ts=_ms(NOW - timedelta(days=365))),
        delivery="ignored-1",
    )
    assert old.json()["status"] == "started"


# --- GitHub PR/CI event dedup (the replayed-state threat in #27) -------------


def _pr_payload(branch: str) -> dict:
    return {
        "action": "opened",
        "pull_request": {
            "number": 7,
            "title": "Add favourites",
            "head": {"ref": branch},
            "html_url": "https://github.com/acme/web/pull/7",
            "state": "open",
            "merged": False,
        },
    }


def _post_github(client, payload, *, event, delivery):
    body = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": "sha256=" + compute_signature(SECRET, body),
            "Content-Type": "application/json",
        },
    )


def test_replayed_github_pr_event_is_deduped(session_factory) -> None:
    """A replayed pull_request delivery re-drives PR state on the inline path;
    durable dedup now covers the observe path, not just intake."""
    client = _make_app(session_factory)
    first = _post_github(client, _pr_payload("nope"), event="pull_request", delivery="pr-1")
    # No run correlates, but the delivery is recorded as processed...
    assert first.json()["status"] == "ignored"
    # ...so the replay short-circuits as a duplicate.
    second = _post_github(client, _pr_payload("nope"), event="pull_request", delivery="pr-1")
    assert second.json()["status"] == "duplicate"
