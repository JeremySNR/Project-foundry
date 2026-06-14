"""Rate limiting: the pure fixed-window core, and the API/webhook middleware.

Offline, no network: the limiter clock is injected and the app uses the
TestClient, which presents a single client host so the per-client cap is
exercised deterministically.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from foundry.agents.manual import InMemoryFakeProvider
from foundry.api import create_app
from foundry.api.ratelimit import RateLimiter
from foundry.api.security import compute_signature
from foundry.db import create_all, make_engine, make_session_factory
from foundry.orchestrator import FoundryOrchestrator

SECRET = "test-secret"
API_TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}


# --- the pure limiter --------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_limiter_allows_up_to_limit_then_refuses() -> None:
    clock = FakeClock()
    limiter = RateLimiter(limit=3, window_seconds=60, clock=clock)
    results = [limiter.check("k") for _ in range(3)]
    assert all(r.allowed for r in results)
    assert [r.remaining for r in results] == [2, 1, 0]
    refused = limiter.check("k")
    assert not refused.allowed
    assert refused.remaining == 0
    assert refused.retry_after >= 1


def test_limiter_window_resets_after_elapse() -> None:
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window_seconds=60, clock=clock)
    assert limiter.check("k").allowed
    assert limiter.check("k").allowed
    assert not limiter.check("k").allowed
    clock.advance(60)  # window fully elapsed
    assert limiter.check("k").allowed


def test_limiter_keys_are_independent() -> None:
    limiter = RateLimiter(limit=1, window_seconds=60, clock=FakeClock())
    assert limiter.check("a").allowed
    assert not limiter.check("a").allowed
    assert limiter.check("b").allowed  # a different client is unaffected


def test_limiter_refused_request_is_not_counted() -> None:
    """A client that keeps hammering past the cap must still recover at the
    window boundary - a refused call must not push the window forward."""
    clock = FakeClock()
    limiter = RateLimiter(limit=1, window_seconds=60, clock=clock)
    assert limiter.check("k").allowed
    clock.advance(30)
    assert not limiter.check("k").allowed  # refused mid-window
    clock.advance(30)  # original window (started at t=0) elapses at t=60
    assert limiter.check("k").allowed


def test_limiter_rejects_invalid_config() -> None:
    with pytest.raises(ValueError):
        RateLimiter(limit=0)
    with pytest.raises(ValueError):
        RateLimiter(limit=1, window_seconds=0)


# --- the middleware ----------------------------------------------------------


def _make_client(**overrides) -> TestClient:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(sf, provider=InMemoryFakeProvider())
    kwargs = dict(
        webhook_secret=SECRET,
        session_factory=sf,
        orchestrator=orch,
        approvers={"lead@example.com": ["engineering"]},
        api_token=API_TOKEN,
    )
    kwargs.update(overrides)
    return TestClient(create_app(**kwargs))


def test_api_requests_throttled_after_limit() -> None:
    client = _make_client(rate_limit_api_per_minute=3)
    # GET /runs falls in the "api" bucket.
    for _ in range(3):
        assert client.get("/runs").status_code == 200
    throttled = client.get("/runs")
    assert throttled.status_code == 429
    assert throttled.json()["detail"] == "rate limit exceeded; retry later"
    assert int(throttled.headers["Retry-After"]) >= 1
    assert throttled.headers["X-RateLimit-Limit"] == "3"
    assert throttled.headers["X-RateLimit-Remaining"] == "0"


def test_rate_limit_headers_on_allowed_response() -> None:
    client = _make_client(rate_limit_api_per_minute=5)
    resp = client.get("/runs")
    assert resp.status_code == 200
    assert resp.headers["X-RateLimit-Limit"] == "5"
    assert resp.headers["X-RateLimit-Remaining"] == "4"


def test_webhook_and_api_buckets_are_independent() -> None:
    """Exhausting the API bucket must not throttle webhook deliveries."""
    client = _make_client(rate_limit_api_per_minute=1, rate_limit_webhook_per_minute=5)
    assert client.get("/runs").status_code == 200
    assert client.get("/runs").status_code == 429  # api bucket spent

    # The webhook surface still has its own budget. A correctly-signed delivery
    # is processed (202), not throttled.
    payload = {"data": {"id": "i-rl", "issueId": "i-rl", "labels": []}}
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Linear-Delivery": "d-rl",
        "Content-Type": "application/json",
        "Linear-Signature": "sha256=" + compute_signature(SECRET, body),
    }
    resp = client.post("/webhooks/linear", content=body, headers=headers)
    assert resp.status_code == 202


def test_healthz_not_rate_limited() -> None:
    client = _make_client(rate_limit_api_per_minute=1)
    # /healthz is outside both buckets: poll it well past the cap.
    for _ in range(5):
        assert client.get("/healthz").status_code == 200


def test_throttled_webhook_does_not_start_run() -> None:
    """A 429 short-circuits before signature/intake: no run side effect."""
    client = _make_client(rate_limit_webhook_per_minute=1)
    payload = {
        "data": {
            "id": "i-rl2",
            "issueId": "i-rl2",
            "identifier": "LIN-RL",
            "title": "thing",
            "labels": [{"name": "foundry:candidate"}],
        }
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Linear-Signature": "sha256=" + compute_signature(SECRET, body),
    }
    first = client.post(
        "/webhooks/linear", content=body, headers={**headers, "Linear-Delivery": "d1"}
    )
    assert first.status_code == 202
    second = client.post(
        "/webhooks/linear", content=body, headers={**headers, "Linear-Delivery": "d2"}
    )
    assert second.status_code == 429
    # Only the first delivery created a run; the throttled one never reached intake.
    assert len(client.get("/runs").json()["runs"]) == 1


def test_disabled_rate_limiting_lets_everything_through() -> None:
    client = _make_client(rate_limit_enabled=False, rate_limit_api_per_minute=1)
    for _ in range(5):
        resp = client.get("/runs")
        assert resp.status_code == 200
        assert "X-RateLimit-Limit" not in resp.headers
