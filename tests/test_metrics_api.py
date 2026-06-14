"""GET /metrics/delivery: the audit trail turned into ROI evidence."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from foundry.agents.manual import InMemoryFakeProvider
from foundry.api import create_app
from foundry.api.security import compute_signature
from foundry.db import create_all, make_engine, make_session_factory
from foundry.orchestrator import FoundryOrchestrator

SECRET = "test-secret"
API_TOKEN = "test-api-token"
APPROVERS = {"lead@example.com": ["engineering", "security"]}
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}

READY_DESC = (
    "Customers want to favourite items.\n\n"
    "Acceptance Criteria:\n"
    "- A favourites button exists\n"
    "- Favourites persist across sessions\n"
)


@pytest.fixture
def client() -> TestClient:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(sf, provider=InMemoryFakeProvider())
    return TestClient(
        create_app(
            webhook_secret=SECRET,
            session_factory=sf,
            orchestrator=orch,
            approvers=APPROVERS,
            api_token=API_TOKEN,
        )
    )


def _post_webhook(client, payload, *, delivery):
    body = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhooks/linear",
        content=body,
        headers={
            "Linear-Delivery": delivery,
            "Linear-Signature": "sha256=" + compute_signature(SECRET, body),
        },
    )


def _post_github(client, payload):
    body = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256=" + compute_signature(SECRET, body),
        },
    )


def _ready_payload(issue_id="issue-r", key="LIN-123") -> dict:
    return {
        "data": {
            "id": issue_id,
            "issueId": issue_id,
            "identifier": key,
            "title": "Add customer favourites",
            "description": READY_DESC,
            "labels": [{"name": "foundry:candidate"}, {"name": "repo:customer-web"}],
            "actor": {"name": "po@example.com"},
        }
    }


def _pr_payload(branch, *, number=42, state="open", merged=False) -> dict:
    return {
        "pull_request": {
            "number": number,
            "html_url": f"https://github.com/o/customer-web/pull/{number}",
            "head": {"ref": branch},
            "title": "",
            "state": state,
            "draft": False,
            "merged": merged,
        },
        "repository": {"full_name": "o/customer-web"},
    }


def _run_to_merged(client, issue_id="issue-r", key="LIN-123", number=42) -> None:
    _post_webhook(client, _ready_payload(issue_id, key), delivery=f"d-{issue_id}")
    client.post(
        f"/runs/{_latest_run_id(client)}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
        headers=AUTH,
    )
    branch = f"cursor/{key.lower()}-favourites"
    resp = _post_github(
        client, _pr_payload(branch, number=number, state="closed", merged=True)
    )
    assert resp.json()["run_status"] == "complete"


def _latest_run_id(client) -> str:
    runs = client.get("/runs").json()["runs"]
    return sorted(runs, key=lambda r: r["created_at"])[-1]["id"]


def test_metrics_requires_bearer_token(client) -> None:
    assert client.get("/metrics/delivery").status_code == 401
    assert (
        client.get(
            "/metrics/delivery", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_metrics_rejects_bad_window(client) -> None:
    assert client.get("/metrics/delivery?days=0", headers=AUTH).status_code == 422


def test_metrics_empty_database(client) -> None:
    body = client.get("/metrics/delivery", headers=AUTH).json()
    assert body["runs_finished"] == 0
    assert body["prs_shipped"] == 0
    assert body["time_to_merge_seconds"]["median"] is None
    assert body["precision_by_confidence_band"] == []
    assert body["top_priors"] == []


def test_trends_requires_bearer_token(client) -> None:
    assert client.get("/metrics/delivery/trends").status_code == 401


def test_trends_rejects_bad_window_and_bucket(client) -> None:
    assert client.get("/metrics/delivery/trends?days=0", headers=AUTH).status_code == 422
    assert (
        client.get("/metrics/delivery/trends?bucket=month", headers=AUTH).status_code
        == 422
    )


def test_trends_empty_database(client) -> None:
    body = client.get("/metrics/delivery/trends", headers=AUTH).json()
    assert body["days"] == 90
    assert body["bucket"] == "week"
    assert body["periods"] == []


def test_trends_reports_a_merge_in_a_period(client) -> None:
    _run_to_merged(client)
    body = client.get("/metrics/delivery/trends?bucket=day", headers=AUTH).json()
    assert body["bucket"] == "day"
    assert len(body["periods"]) == 1
    period = body["periods"][0]
    assert period["prs_shipped"] == 1
    assert period["blocked"] == 0
    assert period["runs_finished"] == 1


def test_metrics_counts_a_merge_and_a_block(client) -> None:
    _run_to_merged(client)

    # A second run is blocked: its PR is closed without merging.
    _post_webhook(
        client, _ready_payload("issue-b", "LIN-200"), delivery="d-issue-b"
    )
    run_b = _latest_run_id(client)
    client.post(
        f"/runs/{run_b}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
        headers=AUTH,
    )
    _post_github(
        client,
        _pr_payload("cursor/lin-200-x", number=43, state="closed", merged=False),
    )

    body = client.get("/metrics/delivery?days=30", headers=AUTH).json()
    assert body["days"] == 30
    assert body["runs_finished"] == 2
    assert body["prs_shipped"] == 1
    assert body["blocked"] == 1
    assert body["blocks_by_reason"] == {"pr_closed_unmerged": 1}
    assert body["time_to_merge_seconds"]["count"] == 1
    assert body["time_to_merge_seconds"]["median"] >= 0

    # Both runs routed at confidence 90 (explicit repo label): one band,
    # 2 routed, 1 merged.
    assert body["precision_by_confidence_band"] == [
        {"band": "90-99", "routed": 2, "merged": 1, "precision": 0.5}
    ]
    assert body["top_priors"][0]["repo"] == "customer-web"
    assert body["top_priors"][0]["routed"] == 2
    assert body["top_priors"][0]["merged"] == 1


def test_blocked_run_superseded_by_later_merge(client) -> None:
    # First attempt on the issue gets blocked (PR closed unmerged)...
    _post_webhook(client, _ready_payload("issue-s", "LIN-300"), delivery="d-s1")
    run_1 = _latest_run_id(client)
    client.post(
        f"/runs/{run_1}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
        headers=AUTH,
    )
    _post_github(
        client,
        _pr_payload("cursor/lin-300-a", number=50, state="closed", merged=False),
    )
    # ...then a human reruns the same issue and it merges.
    _run_to_merged(client, issue_id="issue-s", key="LIN-300", number=51)

    body = client.get("/metrics/delivery", headers=AUTH).json()
    assert body["blocked"] == 1
    assert body["blocked_superseded_by_merged_run"] == 1


def test_agent_metrics_requires_bearer_token(client) -> None:
    assert client.get("/metrics/agents").status_code == 401
    assert client.get("/metrics/agents?days=0", headers=AUTH).status_code == 422


def test_agent_metrics_empty_database(client) -> None:
    body = client.get("/metrics/agents", headers=AUTH).json()
    assert body["days"] == 90
    assert body["min_samples"] == 3
    assert body["providers"] == []


def test_agent_metrics_scores_the_dispatched_provider(client) -> None:
    _run_to_merged(client)
    body = client.get("/metrics/agents?days=30", headers=AUTH).json()
    assert body["days"] == 30
    providers = body["providers"]
    assert len(providers) == 1
    card = providers[0]
    # The in-memory fake provider shipped the merge.
    assert card["provider"] == "fake"
    assert card["runs"] == 1
    assert card["merged"] == 1
    assert card["success_rate"] == 1.0
    assert {r["repo"] for r in card["by_repo"]} == {"customer-web"}


def test_agent_recommendation_requires_bearer_token(client) -> None:
    assert client.get("/metrics/agents/recommendation").status_code == 401
    assert (
        client.get("/metrics/agents/recommendation?days=0", headers=AUTH).status_code
        == 422
    )


def test_agent_recommendation_empty_database(client) -> None:
    body = client.get("/metrics/agents/recommendation", headers=AUTH).json()
    assert body["days"] == 90
    assert body["recommended"] is None
    assert body["ranked"] == []


def test_agent_recommendation_below_floor_is_not_recommended(client) -> None:
    _run_to_merged(client)
    body = client.get("/metrics/agents/recommendation?days=30", headers=AUTH).json()
    # One merged run is below the default 3-sample floor: the fake provider shows
    # in the ranking but isn't eligible to be recommended yet.
    assert body["days"] == 30
    assert {c["provider"] for c in body["ranked"]} == {"fake"}
    assert body["recommended"] is None


def test_agent_recommendation_picks_the_proven_provider(client) -> None:
    # Three merged runs clear the default floor.
    for i in range(3):
        _run_to_merged(
            client, issue_id=f"issue-{i}", key=f"LIN-{900 + i}", number=900 + i
        )
    body = client.get("/metrics/agents/recommendation", headers=AUTH).json()
    assert body["recommended"] == "fake"
    assert body["ranked"][0]["provider"] == "fake"
    assert body["ranked"][0]["eligible"] is True
    assert "fake" in body["reason"]


def test_final_summary_appears_in_timeline(client) -> None:
    _run_to_merged(client)
    run_id = _latest_run_id(client)
    timeline = client.get(f"/runs/{run_id}/timeline", headers=AUTH).json()
    summaries = [
        a for a in timeline["artifacts"] if a["artifact_type"] == "final_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["content"]["outcome"] == "merged"
