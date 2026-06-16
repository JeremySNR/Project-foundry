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


def test_by_repo_requires_bearer_token(client) -> None:
    assert client.get("/metrics/delivery/by-repo").status_code == 401


def test_by_repo_rejects_bad_window(client) -> None:
    assert (
        client.get("/metrics/delivery/by-repo?days=0", headers=AUTH).status_code == 422
    )


def test_by_repo_empty_database(client) -> None:
    body = client.get("/metrics/delivery/by-repo", headers=AUTH).json()
    assert body["runs_finished"] == 0
    assert body["repos"] == []


def test_by_repo_groups_a_merge_and_a_block(client) -> None:
    _run_to_merged(client)

    # A second run on the same repo is blocked (PR closed without merging).
    _post_webhook(
        client, _ready_payload("issue-b", "LIN-200"), delivery="d-issue-b"
    )
    client.post(
        f"/runs/{_latest_run_id(client)}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
        headers=AUTH,
    )
    _post_github(
        client,
        _pr_payload("cursor/lin-200-x", number=43, state="closed", merged=False),
    )

    body = client.get("/metrics/delivery/by-repo?days=30", headers=AUTH).json()
    assert body["days"] == 30
    assert body["runs_finished"] == 2
    repos = {r["repo"]: r for r in body["repos"]}
    assert "customer-web" in repos
    web = repos["customer-web"]
    assert web["runs_finished"] == 2
    assert web["prs_shipped"] == 1
    assert web["blocked"] == 1
    assert web["merge_rate"] == 0.5
    assert web["time_to_merge_seconds"]["count"] == 1


def test_by_work_type_requires_bearer_token(client) -> None:
    assert client.get("/metrics/delivery/by-work-type").status_code == 401


def test_by_work_type_rejects_bad_window(client) -> None:
    assert (
        client.get(
            "/metrics/delivery/by-work-type?days=0", headers=AUTH
        ).status_code
        == 422
    )


def test_by_work_type_empty_database(client) -> None:
    body = client.get("/metrics/delivery/by-work-type", headers=AUTH).json()
    assert body["runs_finished"] == 0
    assert body["work_types"] == []


def test_by_work_type_groups_a_merged_run(client) -> None:
    _run_to_merged(client)

    body = client.get("/metrics/delivery/by-work-type?days=30", headers=AUTH).json()
    assert body["days"] == 30
    assert body["runs_finished"] == 1
    # "Add customer favourites" classifies as a feature.
    types = {t["work_type"]: t for t in body["work_types"]}
    assert "feature" in types
    feat = types["feature"]
    assert feat["runs_finished"] == 1
    assert feat["prs_shipped"] == 1
    assert feat["merge_rate"] == 1.0
    assert feat["time_to_merge_seconds"]["count"] == 1


def test_by_repo_trends_requires_bearer_token(client) -> None:
    assert client.get("/metrics/delivery/by-repo/trends").status_code == 401


def test_by_repo_trends_rejects_bad_window_and_bucket(client) -> None:
    assert (
        client.get(
            "/metrics/delivery/by-repo/trends?days=0", headers=AUTH
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/metrics/delivery/by-repo/trends?bucket=month", headers=AUTH
        ).status_code
        == 422
    )


def test_by_repo_trends_empty_database(client) -> None:
    body = client.get("/metrics/delivery/by-repo/trends", headers=AUTH).json()
    assert body["days"] == 90
    assert body["bucket"] == "week"
    assert body["periods"] == []
    assert body["repos"] == []


def test_by_repo_trends_reports_a_merge_in_a_period(client) -> None:
    _run_to_merged(client)
    body = client.get(
        "/metrics/delivery/by-repo/trends?days=30&bucket=day", headers=AUTH
    ).json()
    assert body["days"] == 30
    assert body["bucket"] == "day"
    repos = {r["repo"]: r for r in body["repos"]}
    assert "customer-web" in repos
    web = repos["customer-web"]
    assert web["runs_finished"] == 1
    assert web["prs_shipped"] == 1
    # One run -> one populated period, aligned to the shared axis.
    assert [c["period_start"] for c in web["series"]] == body["periods"]
    assert sum(c["prs_shipped"] for c in web["series"]) == 1


def test_by_work_type_trends_requires_bearer_token(client) -> None:
    assert client.get("/metrics/delivery/by-work-type/trends").status_code == 401


def test_by_work_type_trends_rejects_bad_window_and_bucket(client) -> None:
    assert (
        client.get(
            "/metrics/delivery/by-work-type/trends?days=0", headers=AUTH
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/metrics/delivery/by-work-type/trends?bucket=month", headers=AUTH
        ).status_code
        == 422
    )


def test_by_work_type_trends_empty_database(client) -> None:
    body = client.get("/metrics/delivery/by-work-type/trends", headers=AUTH).json()
    assert body["days"] == 90
    assert body["bucket"] == "week"
    assert body["periods"] == []
    assert body["work_types"] == []


def test_by_work_type_trends_reports_a_merge_in_a_period(client) -> None:
    _run_to_merged(client)
    body = client.get(
        "/metrics/delivery/by-work-type/trends?days=30&bucket=day", headers=AUTH
    ).json()
    assert body["days"] == 30
    assert body["bucket"] == "day"
    types = {t["work_type"]: t for t in body["work_types"]}
    # "Add customer favourites" classifies as a feature.
    assert "feature" in types
    feat = types["feature"]
    assert feat["runs_finished"] == 1
    assert feat["prs_shipped"] == 1
    # One run -> one populated period, aligned to the shared axis.
    assert [c["period_start"] for c in feat["series"]] == body["periods"]
    assert sum(c["prs_shipped"] for c in feat["series"]) == 1


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


def test_agent_trends_requires_bearer_token(client) -> None:
    assert client.get("/metrics/agents/trends").status_code == 401


def test_agent_trends_rejects_bad_window_and_bucket(client) -> None:
    assert client.get("/metrics/agents/trends?days=0", headers=AUTH).status_code == 422
    assert (
        client.get("/metrics/agents/trends?bucket=month", headers=AUTH).status_code
        == 422
    )


def test_agent_trends_empty_database(client) -> None:
    body = client.get("/metrics/agents/trends", headers=AUTH).json()
    assert body["days"] == 90
    assert body["bucket"] == "week"
    assert body["min_samples"] == 3
    assert body["providers"] == []
    assert body["periods"] == []


def test_agent_trends_reports_the_dispatched_provider(client) -> None:
    _run_to_merged(client)
    body = client.get("/metrics/agents/trends?days=30&bucket=day", headers=AUTH).json()
    assert body["days"] == 30
    assert body["bucket"] == "day"
    providers = body["providers"]
    assert len(providers) == 1
    card = providers[0]
    # The in-memory fake provider shipped the merge.
    assert card["provider"] == "fake"
    assert card["runs"] == 1 and card["merged"] == 1
    # One run -> one populated period, aligned to the shared axis.
    assert [c["period_start"] for c in card["series"]] == body["periods"]
    assert sum(c["merged"] for c in card["series"]) == 1


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


def test_fleet_requires_bearer_token(client) -> None:
    assert client.get("/metrics/fleet").status_code == 401
    assert (
        client.get(
            "/metrics/fleet", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_fleet_empty_database(client) -> None:
    body = client.get("/metrics/fleet", headers=AUTH).json()
    assert body["total_runs"] == 0
    assert body["runs_active"] == 0
    assert body["runs_terminal"] == 0
    assert body["awaiting_human"] == 0
    assert body["agents_running"] == 0
    assert body["prs_open"] == 0
    # No in-flight job reported cost: None, never a conjured $0.
    assert body["active_cost_usd"] is None
    assert body["by_status"] == {}


def test_fleet_counts_live_and_terminal_states(client) -> None:
    # One run runs all the way to a merged PR (terminal: complete)...
    _run_to_merged(client)

    # ...one is parked waiting for a human approval (active + in the queue)...
    _post_webhook(client, _ready_payload("issue-w", "LIN-400"), delivery="d-w")

    # ...and one is approved and dispatched, so an agent is running on it
    # (active, but no longer awaiting a human).
    _post_webhook(client, _ready_payload("issue-a", "LIN-500"), delivery="d-a")
    run_a = _latest_run_id(client)
    client.post(
        f"/runs/{run_a}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
        headers=AUTH,
    )

    body = client.get("/metrics/fleet", headers=AUTH).json()
    assert body["total_runs"] == 3
    assert body["runs_active"] == 2  # waiting_approval + agent_running
    assert body["runs_terminal"] == 1  # complete
    assert body["awaiting_human"] == 1  # only the waiting_approval run
    assert body["agents_running"] == 1
    assert body["by_status"]["complete"] == 1
    assert body["by_status"]["waiting_approval"] == 1
    assert body["by_status"]["agent_running"] == 1
    # The fake provider reports no cost, so spend-in-flight stays None.
    assert body["active_cost_usd"] is None


def _client_with(**kwargs) -> TestClient:
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
            **kwargs,
        )
    )


def test_fleet_includes_approval_queue_summary(client) -> None:
    # No SLA configured by default: the breach signal is inert, oldest is None.
    body = client.get("/metrics/fleet", headers=AUTH).json()
    assert body["approval_sla_seconds"] is None
    assert body["approvals_breaching_sla"] == 0
    assert body["oldest_wait_seconds"] is None

    # Park a run on a human; the strip now reports a non-negative oldest wait.
    _post_webhook(client, _ready_payload("issue-w", "LIN-400"), delivery="d-w")
    body = client.get("/metrics/fleet", headers=AUTH).json()
    assert body["awaiting_human"] == 1
    assert body["oldest_wait_seconds"] is not None
    assert body["oldest_wait_seconds"] >= 0


def test_approvals_requires_bearer_token(client) -> None:
    assert client.get("/metrics/approvals").status_code == 401
    assert (
        client.get(
            "/metrics/approvals", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_approvals_empty_database(client) -> None:
    body = client.get("/metrics/approvals", headers=AUTH).json()
    assert body["count"] == 0
    assert body["runs"] == []
    assert body["oldest_wait_seconds"] is None
    assert body["sla_breaches"] == 0
    assert body["sla_seconds"] is None


def test_approvals_lists_parked_run(client) -> None:
    _post_webhook(client, _ready_payload("issue-w", "LIN-400"), delivery="d-w")
    body = client.get("/metrics/approvals", headers=AUTH).json()
    assert body["count"] == 1
    entry = body["runs"][0]
    assert entry["linear_issue_key"] == "LIN-400"
    assert entry["status"] == "waiting_approval"
    assert entry["waiting_seconds"] >= 0
    assert entry["sla_breached"] is False  # no SLA configured


def test_approvals_reflects_configured_sla() -> None:
    client = _client_with(approval_sla_seconds=14_400)  # 4h
    _post_webhook(client, _ready_payload("issue-w", "LIN-400"), delivery="d-w")
    body = client.get("/metrics/approvals", headers=AUTH).json()
    assert body["sla_seconds"] == 14_400
    # A just-parked run hasn't breached a 4h SLA.
    assert body["sla_breaches"] == 0
    assert body["runs"][0]["sla_breached"] is False
    fleet = client.get("/metrics/fleet", headers=AUTH).json()
    assert fleet["approval_sla_seconds"] == 14_400


def test_executions_requires_bearer_token(client) -> None:
    assert client.get("/metrics/executions").status_code == 401
    assert (
        client.get(
            "/metrics/executions", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_executions_empty_database(client) -> None:
    body = client.get("/metrics/executions", headers=AUTH).json()
    assert body["count"] == 0
    assert body["runs"] == []
    assert body["oldest_running_seconds"] is None
    assert body["sla_breaches"] == 0
    assert body["sla_seconds"] is None


def test_executions_lists_in_flight_agent_run(client) -> None:
    # Drive a run to agent_running: ready ticket -> approve -> dispatched.
    _post_webhook(client, _ready_payload("issue-a", "LIN-500"), delivery="d-a")
    run_a = _latest_run_id(client)
    client.post(
        f"/runs/{run_a}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
        headers=AUTH,
    )
    body = client.get("/metrics/executions", headers=AUTH).json()
    assert body["count"] == 1
    entry = body["runs"][0]
    assert entry["run_id"] == run_a
    assert entry["status"] == "agent_running"
    assert entry["running_seconds"] >= 0
    assert entry["sla_breached"] is False  # no SLA configured


def test_fleet_includes_execution_queue_summary() -> None:
    client = _client_with(execution_sla_seconds=3600)  # 1h
    # No agent running yet: the strip's execution summary is empty/inert.
    body = client.get("/metrics/fleet", headers=AUTH).json()
    assert body["execution_sla_seconds"] == 3600
    assert body["executions_breaching_sla"] == 0
    assert body["oldest_execution_seconds"] is None

    # Dispatch an agent; the strip now reports a non-negative oldest run-time.
    _post_webhook(client, _ready_payload("issue-a", "LIN-500"), delivery="d-a")
    run_a = _latest_run_id(client)
    client.post(
        f"/runs/{run_a}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
        headers=AUTH,
    )
    body = client.get("/metrics/fleet", headers=AUTH).json()
    assert body["agents_running"] == 1
    assert body["oldest_execution_seconds"] is not None
    assert body["oldest_execution_seconds"] >= 0
    # A just-dispatched run hasn't breached a 1h SLA.
    assert body["executions_breaching_sla"] == 0


def _run_to_pr_open(client, issue_id="issue-p", key="LIN-700", number=77) -> str:
    """Drive a run to PR_OPEN: ready ticket -> approve -> agent ships an open PR
    (not merged), so it parks awaiting review/CI."""
    _post_webhook(client, _ready_payload(issue_id, key), delivery=f"d-{issue_id}")
    run_id = _latest_run_id(client)
    client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
        headers=AUTH,
    )
    branch = f"cursor/{key.lower()}-favourites"
    resp = _post_github(
        client, _pr_payload(branch, number=number, state="open", merged=False)
    )
    assert resp.json()["run_status"] == "pr_open"
    return run_id


def test_reviews_requires_bearer_token(client) -> None:
    assert client.get("/metrics/reviews").status_code == 401
    assert (
        client.get(
            "/metrics/reviews", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_reviews_empty_database(client) -> None:
    body = client.get("/metrics/reviews", headers=AUTH).json()
    assert body["count"] == 0
    assert body["runs"] == []
    assert body["oldest_unreviewed_seconds"] is None
    assert body["sla_breaches"] == 0
    assert body["sla_seconds"] is None
    # Staleness ("stale since last push") summary defaults are inert.
    assert body["oldest_inactive_seconds"] is None
    assert body["stale_breaches"] == 0
    assert body["stale_sla_seconds"] is None


def test_reviews_lists_open_pr(client) -> None:
    run_id = _run_to_pr_open(client)
    body = client.get("/metrics/reviews", headers=AUTH).json()
    assert body["count"] == 1
    entry = body["runs"][0]
    assert entry["run_id"] == run_id
    assert entry["status"] == "pr_open"
    assert entry["unreviewed_seconds"] >= 0
    assert entry["sla_breached"] is False  # no SLA configured
    # The "stale since last push" age is carried alongside the open age; with no
    # later push it equals the open age, and with no staleness SLA it never breaches.
    assert entry["inactive_seconds"] >= 0
    assert entry["inactive_seconds"] <= entry["unreviewed_seconds"]
    assert entry["stale_breached"] is False


def test_reviews_staleness_sla_is_plumbed_through() -> None:
    # The staleness SLA is wired from app config into the endpoint. A just-opened
    # PR is fresh (idle ~0s), so it does not breach a 48h SLA - matching how the
    # review-latency SLA treats a fresh PR. Breach logic is covered by the unit
    # tests in test_review_queue.py with a controlled clock.
    client = _client_with(review_stale_sla_seconds=172_800)  # 48h
    _run_to_pr_open(client)
    body = client.get("/metrics/reviews", headers=AUTH).json()
    assert body["stale_sla_seconds"] == 172_800
    assert body["count"] == 1
    assert body["runs"][0]["stale_breached"] is False
    assert body["stale_breaches"] == 0


def test_fleet_includes_review_staleness_summary() -> None:
    client = _client_with(review_stale_sla_seconds=172_800)  # 48h
    # No PR open yet: the staleness summary is empty/inert.
    body = client.get("/metrics/fleet", headers=AUTH).json()
    assert body["review_stale_sla_seconds"] == 172_800
    assert body["reviews_stale"] == 0
    assert body["oldest_inactive_seconds"] is None

    # Ship an open PR; the strip now reports a non-negative idle age, not breaching.
    _run_to_pr_open(client)
    body = client.get("/metrics/fleet", headers=AUTH).json()
    assert body["prs_open"] == 1
    assert body["oldest_inactive_seconds"] is not None
    assert body["oldest_inactive_seconds"] >= 0
    assert body["reviews_stale"] == 0


def test_fleet_includes_review_queue_summary() -> None:
    client = _client_with(review_sla_seconds=86_400)  # 24h
    # No PR open yet: the strip's review summary is empty/inert.
    body = client.get("/metrics/fleet", headers=AUTH).json()
    assert body["review_sla_seconds"] == 86_400
    assert body["reviews_breaching_sla"] == 0
    assert body["oldest_review_seconds"] is None

    # Ship an open PR; the strip now reports a non-negative oldest review age.
    _run_to_pr_open(client)
    body = client.get("/metrics/fleet", headers=AUTH).json()
    assert body["prs_open"] == 1
    assert body["oldest_review_seconds"] is not None
    assert body["oldest_review_seconds"] >= 0
    # A just-opened PR hasn't breached a 24h SLA.
    assert body["reviews_breaching_sla"] == 0


def test_final_summary_appears_in_timeline(client) -> None:
    _run_to_merged(client)
    run_id = _latest_run_id(client)
    timeline = client.get(f"/runs/{run_id}/timeline", headers=AUTH).json()
    summaries = [
        a for a in timeline["artifacts"] if a["artifact_type"] == "final_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["content"]["outcome"] == "merged"


# --- GET /metrics/policy: the effective gate, surfaced read-only (issue #31) ---


def test_policy_requires_bearer_token(client) -> None:
    assert client.get("/metrics/policy").status_code == 401
    assert (
        client.get(
            "/metrics/policy", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_policy_unconfigured_reports_not_configured(client) -> None:
    # The default fixture builds create_app without an effective_policy (no
    # Settings), so the endpoint answers "nothing to show" rather than 404 - the
    # dashboard panel hides on this.
    body = client.get("/metrics/policy", headers=AUTH).json()
    assert body == {
        "configured": False,
        "provider": None,
        "policy": None,
        # No windows configured (and none could be active) => always null.
        "active_freeze": None,
    }


def test_policy_reports_the_effective_gate() -> None:
    summary = {
        "repo_confidence_threshold": 80,
        "max_files_changed": 50,
        "forbidden_globs": ["**/secrets/**"],
        "repo_forbidden_globs": {"payments": ["**/keys/**"]},
        "repo_required_roles": {"payments": ["security"]},
        "min_approvals": 2,
        "repo_min_approvals": {"payments": 3},
        "path_required_roles": {"**/billing/**": ["security"]},
        "max_agent_retries": 1,
        "retry_on": ["ci_failure"],
        "max_cost_per_run": 5.0,
        "estimated_cost_per_dispatch": 1.0,
        "approver_count": 2,
    }
    api = _client_with(effective_policy=summary, policy_provider="opa")
    body = api.get("/metrics/policy", headers=AUTH).json()
    assert body["configured"] is True
    assert body["provider"] == "opa"
    assert body["policy"] == summary


def test_policy_summary_is_copied_not_aliased() -> None:
    # A caller mutating the dict it passed in must not change what the endpoint
    # serves - create_app copies it at build time.
    summary = {"min_approvals": 1, "forbidden_globs": []}
    api = _client_with(effective_policy=summary)
    summary["min_approvals"] = 99
    body = api.get("/metrics/policy", headers=AUTH).json()
    assert body["policy"]["min_approvals"] == 1


def test_app_from_settings_wires_the_effective_policy() -> None:
    # The real entrypoint computes the summary from the live config with the same
    # `effective_policy_summary` `foundry-policy explain` uses, so the in-app view
    # can't drift from the CLI's.
    from foundry.api import app_from_settings
    from foundry.config import Settings
    from foundry.policy.library import effective_policy_summary

    settings = Settings.from_env(
        {
            "FOUNDRY_LINEAR_WEBHOOK_SECRET": SECRET,
            "FOUNDRY_API_TOKEN": API_TOKEN,
        }
    )
    api = TestClient(app_from_settings(settings))
    body = api.get("/metrics/policy", headers=AUTH).json()
    assert body["configured"] is True
    assert body["provider"] == settings.policy_provider
    assert body["policy"] == effective_policy_summary(settings)


# --- GET /metrics/policy: the live "is a change freeze active now" signal (#31) ---


def _saturday_freeze():
    from foundry.policy.freeze import ChangeFreezeWindow

    return ChangeFreezeWindow(
        reason="weekend release blackout",
        weekdays=("sat", "sun"),
        start="00:00",
        end="23:59",
        tz="UTC",
    )


def test_policy_reports_no_active_freeze_when_none_configured() -> None:
    # With no windows configured the field is present and null - so the dashboard
    # has a stable shape to read and never shows a freeze banner by accident.
    summary = {"min_approvals": 1, "forbidden_globs": []}
    api = _client_with(effective_policy=summary)
    body = api.get("/metrics/policy", headers=AUTH).json()
    assert body["active_freeze"] is None


def test_policy_reports_active_freeze_at_request_time() -> None:
    # A Saturday clock falls inside a sat/sun window => the endpoint names the
    # active window (description + reason), evaluated at request time against the
    # injected clock, exactly as the orchestrator decides to hold a re-dispatch.
    from datetime import datetime, timezone

    saturday = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)  # a Saturday
    api = _client_with(
        effective_policy={"min_approvals": 1, "forbidden_globs": []},
        change_freeze_windows=[_saturday_freeze()],
        clock=lambda: saturday,
    )
    body = api.get("/metrics/policy", headers=AUTH).json()
    assert body["active_freeze"] is not None
    assert body["active_freeze"]["reason"] == "weekend release blackout"
    assert "sat/sun" in body["active_freeze"]["description"]


def test_policy_reports_no_active_freeze_outside_the_window() -> None:
    # A Wednesday clock is outside the sat/sun window => null even though a window
    # is configured. The window is still surfaced statically in `policy`.
    from datetime import datetime, timezone

    wednesday = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)  # a Wednesday
    api = _client_with(
        effective_policy={"min_approvals": 1, "forbidden_globs": []},
        change_freeze_windows=[_saturday_freeze()],
        clock=lambda: wednesday,
    )
    body = api.get("/metrics/policy", headers=AUTH).json()
    assert body["active_freeze"] is None


def test_app_from_settings_wires_active_freeze_windows(tmp_path) -> None:
    # The real entrypoint hands the parsed windows from the live config to the
    # endpoint, so the live signal works on a deployment config, not just a
    # hand-built create_app.
    from datetime import datetime, timezone

    from foundry.api import app_from_settings
    from foundry.config import Settings

    config = tmp_path / "foundry.yaml"
    config.write_text(
        "policy:\n"
        "  change_freeze_windows:\n"
        "    - reason: weekend release blackout\n"
        "      weekdays: [sat, sun]\n"
        "      start: '00:00'\n"
        "      end: '23:59'\n"
        "      tz: UTC\n",
        encoding="utf-8",
    )
    settings = Settings.load(
        config,
        env={
            "FOUNDRY_LINEAR_WEBHOOK_SECRET": SECRET,
            "FOUNDRY_API_TOKEN": API_TOKEN,
        },
    )
    saturday = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
    api = TestClient(app_from_settings(settings))
    api.app.state.clock = lambda: saturday
    body = api.get("/metrics/policy", headers=AUTH).json()
    assert body["active_freeze"] is not None
    assert body["active_freeze"]["reason"] == "weekend release blackout"
    # And the window is also surfaced statically in the effective gate.
    assert any("sat/sun" in w for w in body["policy"]["change_freeze_windows"])


# --- GET /metrics/policy/check: the compliance verdict vs a baseline (issue #31) ---


def test_policy_check_requires_bearer_token(client) -> None:
    assert client.get("/metrics/policy/check").status_code == 401
    assert (
        client.get(
            "/metrics/policy/check", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_policy_check_unconfigured_reports_not_configured(client) -> None:
    # No baseline configured (the default fixture): the endpoint answers "nothing
    # to check" rather than 404, and the dashboard panel hides on this.
    body = client.get("/metrics/policy/check", headers=AUTH).json()
    assert body == {
        "configured": False,
        "baseline": None,
        "ok": None,
        "findings": [],
        "weaknesses": [],
    }


def test_policy_check_reports_the_verdict() -> None:
    # The endpoint serves whatever pre-computed verdict create_app was handed -
    # app_from_settings computes it from compare_policy_strictness (covered below).
    check = {
        "baseline": "soc2",
        "ok": False,
        "findings": [
            {"knob": "min_approvals", "ok": True, "detail": "2 (baseline requires >= 2)"},
            {"knob": "forbidden_globs", "ok": False, "detail": "missing ['**/secrets/**']"},
        ],
        "weaknesses": ["forbidden_globs"],
    }
    api = _client_with(policy_check=check)
    body = api.get("/metrics/policy/check", headers=AUTH).json()
    assert body["configured"] is True
    assert body["baseline"] == "soc2"
    assert body["ok"] is False
    assert body["weaknesses"] == ["forbidden_globs"]
    assert body["findings"] == check["findings"]


def test_policy_check_is_copied_not_aliased() -> None:
    # A caller mutating the dict it passed in must not change what the endpoint
    # serves - create_app copies it at build time.
    check = {"baseline": "soc2", "ok": True, "findings": [], "weaknesses": []}
    api = _client_with(policy_check=check)
    check["ok"] = False
    body = api.get("/metrics/policy/check", headers=AUTH).json()
    assert body["ok"] is True


def test_app_from_settings_wires_a_passing_compliance_check(tmp_path) -> None:
    # A config that adopts the soc2 preset wholesale must check as at least as
    # strict as the soc2 baseline (it IS the baseline), computed from the same
    # compare_policy_strictness the `foundry-policy check` CLI exits on.
    from foundry.api import app_from_settings
    from foundry.config import Settings
    from foundry.policy.library import load_preset_yaml

    config = tmp_path / "foundry.yaml"
    config.write_text(load_preset_yaml("soc2"), encoding="utf-8")
    settings = Settings.load(
        config,
        env={
            "FOUNDRY_LINEAR_WEBHOOK_SECRET": SECRET,
            "FOUNDRY_API_TOKEN": API_TOKEN,
            "FOUNDRY_POLICY_BASELINE": "soc2",
        },
    )
    api = TestClient(app_from_settings(settings))
    body = api.get("/metrics/policy/check", headers=AUTH).json()
    assert body["configured"] is True
    assert body["baseline"] == "soc2"
    assert body["ok"] is True
    assert body["weaknesses"] == []


def test_app_from_settings_flags_a_weaker_config() -> None:
    # A bare config (built-in defaults) is weaker than the soc2 baseline on the
    # knobs soc2 tightens, so the in-app verdict reports it - the same FAIL the
    # CLI `check` would exit non-zero on.
    from foundry.api import app_from_settings
    from foundry.config import Settings

    settings = Settings.from_env(
        {
            "FOUNDRY_LINEAR_WEBHOOK_SECRET": SECRET,
            "FOUNDRY_API_TOKEN": API_TOKEN,
            "FOUNDRY_POLICY_BASELINE": "soc2",
        }
    )
    api = TestClient(app_from_settings(settings))
    body = api.get("/metrics/policy/check", headers=AUTH).json()
    assert body["configured"] is True
    assert body["baseline"] == "soc2"
    assert body["ok"] is False
    assert body["weaknesses"]  # at least one control falls short


def test_app_from_settings_fails_loud_on_unresolvable_baseline() -> None:
    # A typo'd baseline must fail at startup, not silently serve no signal - a
    # compliance check that quietly "passes" on a bad baseline is the misleading
    # outcome this must avoid.
    from foundry.api import app_from_settings
    from foundry.config import Settings

    settings = Settings.from_env(
        {
            "FOUNDRY_LINEAR_WEBHOOK_SECRET": SECRET,
            "FOUNDRY_API_TOKEN": API_TOKEN,
            "FOUNDRY_POLICY_BASELINE": "no-such-preset",
        }
    )
    with pytest.raises(ValueError, match="policy_baseline is misconfigured"):
        app_from_settings(settings)


# -- GET /metrics/failures (failure-side triage feed, issue #37) -------------


def _run_to_blocked(client, issue_id="issue-b", key="LIN-900") -> str:
    """Drive a run to BLOCKED via a human stop: ready ticket -> approve isn't
    needed; a stop on the parked run blocks it (RUN_BLOCKED, human_stopped)."""
    _post_webhook(client, _ready_payload(issue_id, key), delivery=f"d-{issue_id}")
    run_id = _latest_run_id(client)
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "/foundry stop"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    return run_id


def test_failures_requires_bearer_token(client) -> None:
    assert client.get("/metrics/failures").status_code == 401
    assert (
        client.get(
            "/metrics/failures", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_failures_rejects_bad_window(client) -> None:
    assert client.get("/metrics/failures?days=0", headers=AUTH).status_code == 422


def test_failures_empty_database(client) -> None:
    body = client.get("/metrics/failures", headers=AUTH).json()
    assert body["days"] == 7
    assert body["count"] == 0
    assert body["runs"] == []
    assert body["newest_failure_seconds"] is None
    assert body["oldest_failure_seconds"] is None
    assert body["blocked"] == 0
    assert body["failed"] == 0


def test_failures_lists_blocked_run(client) -> None:
    run_id = _run_to_blocked(client)
    body = client.get("/metrics/failures", headers=AUTH).json()
    assert body["count"] == 1
    entry = body["runs"][0]
    assert entry["run_id"] == run_id
    assert entry["status"] == "blocked"
    assert entry["failed_seconds"] >= 0
    # The reason is read from the RUN_BLOCKED event's metadata.
    assert entry["reason"] == "human_stopped"
    assert body["blocked"] == 1
    assert body["failed"] == 0


# -- GET /metrics/failures/by-category (aggregate triage cut, issue #37) ------


def test_failures_by_category_requires_bearer_token(client) -> None:
    assert client.get("/metrics/failures/by-category").status_code == 401
    assert (
        client.get(
            "/metrics/failures/by-category", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_failures_by_category_rejects_bad_window(client) -> None:
    assert (
        client.get("/metrics/failures/by-category?days=0", headers=AUTH).status_code
        == 422
    )


def test_failures_by_category_empty_database(client) -> None:
    body = client.get("/metrics/failures/by-category", headers=AUTH).json()
    assert body["days"] == 7
    assert body["count"] == 0
    assert body["blocked"] == 0
    assert body["failed"] == 0
    assert body["distinct_categories"] == 0
    assert body["categories"] == []


def test_failures_by_category_rolls_up_blocked_run(client) -> None:
    _run_to_blocked(client)
    body = client.get("/metrics/failures/by-category", headers=AUTH).json()
    assert body["count"] == 1
    assert body["blocked"] == 1
    assert body["failed"] == 0
    assert body["distinct_categories"] == 1
    cat = body["categories"][0]
    # Grouped by the same reason the feed reports for the run.
    assert cat["category"] == "human_stopped"
    assert cat["count"] == 1
    assert cat["blocked"] == 1
    assert cat["newest_failure_seconds"] >= 0
    assert cat["oldest_failure_seconds"] >= 0


def test_failures_by_category_matches_feed_totals(client) -> None:
    # Two blocked runs share the human_stopped reason: the feed lists 2 runs, the
    # roll-up reports 1 category with count 2 - and the totals agree.
    _run_to_blocked(client, issue_id="issue-b1", key="LIN-901")
    _run_to_blocked(client, issue_id="issue-b2", key="LIN-902")
    feed = client.get("/metrics/failures", headers=AUTH).json()
    agg = client.get("/metrics/failures/by-category", headers=AUTH).json()
    assert feed["count"] == agg["count"] == 2
    assert feed["blocked"] == agg["blocked"]
    assert agg["distinct_categories"] == 1
    assert agg["categories"][0]["count"] == 2
    assert sum(c["count"] for c in agg["categories"]) == feed["count"]


# -- GET /metrics/failures/by-repo (repo-axis triage cut, issue #37) ----------


def test_failures_by_repo_requires_bearer_token(client) -> None:
    assert client.get("/metrics/failures/by-repo").status_code == 401
    assert (
        client.get(
            "/metrics/failures/by-repo", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_failures_by_repo_rejects_bad_window(client) -> None:
    assert (
        client.get("/metrics/failures/by-repo?days=0", headers=AUTH).status_code == 422
    )


def test_failures_by_repo_empty_database(client) -> None:
    body = client.get("/metrics/failures/by-repo", headers=AUTH).json()
    assert body["days"] == 7
    assert body["count"] == 0
    assert body["blocked"] == 0
    assert body["failed"] == 0
    assert body["distinct_repos"] == 0
    assert body["repos"] == []


def test_failures_by_repo_rolls_up_blocked_run(client) -> None:
    _run_to_blocked(client)
    body = client.get("/metrics/failures/by-repo", headers=AUTH).json()
    assert body["count"] == 1
    assert body["blocked"] == 1
    assert body["failed"] == 0
    assert body["distinct_repos"] == 1
    repo = body["repos"][0]
    # The run never routed, so it buckets under the (unrouted) sentinel.
    assert repo["repo"] == "(unrouted)"
    assert repo["count"] == 1
    assert repo["blocked"] == 1
    assert repo["newest_failure_seconds"] >= 0
    assert repo["oldest_failure_seconds"] >= 0


def test_failures_by_repo_matches_feed_totals(client) -> None:
    # Two blocked runs: the feed lists 2 runs, the roll-up reports 1 repo bucket
    # with count 2 - and the totals agree.
    _run_to_blocked(client, issue_id="issue-r1", key="LIN-921")
    _run_to_blocked(client, issue_id="issue-r2", key="LIN-922")
    feed = client.get("/metrics/failures", headers=AUTH).json()
    agg = client.get("/metrics/failures/by-repo", headers=AUTH).json()
    assert feed["count"] == agg["count"] == 2
    assert feed["blocked"] == agg["blocked"]
    assert agg["distinct_repos"] == 1
    assert agg["repos"][0]["count"] == 2
    assert sum(r["count"] for r in agg["repos"]) == feed["count"]


# -- GET /metrics/failures/by-work-type (failure work-type cut, issue #37) ----


def test_failures_by_work_type_requires_bearer_token(client) -> None:
    assert client.get("/metrics/failures/by-work-type").status_code == 401
    assert (
        client.get(
            "/metrics/failures/by-work-type",
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )


def test_failures_by_work_type_rejects_bad_window(client) -> None:
    assert (
        client.get(
            "/metrics/failures/by-work-type?days=0", headers=AUTH
        ).status_code
        == 422
    )


def test_failures_by_work_type_empty_database(client) -> None:
    body = client.get("/metrics/failures/by-work-type", headers=AUTH).json()
    assert body["days"] == 7
    assert body["count"] == 0
    assert body["blocked"] == 0
    assert body["failed"] == 0
    assert body["distinct_work_types"] == 0
    assert body["work_types"] == []


def test_failures_by_work_type_rolls_up_blocked_run(client) -> None:
    _run_to_blocked(client)
    body = client.get("/metrics/failures/by-work-type", headers=AUTH).json()
    assert body["count"] == 1
    assert body["blocked"] == 1
    assert body["failed"] == 0
    assert body["distinct_work_types"] == 1
    wt = body["work_types"][0]
    # A real run carries a classified work type from its TICKET_ANALYSIS artifact.
    assert isinstance(wt["work_type"], str) and wt["work_type"]
    assert wt["count"] == 1
    assert wt["blocked"] == 1
    assert wt["newest_failure_seconds"] >= 0
    assert wt["oldest_failure_seconds"] >= 0


def test_failures_by_work_type_matches_feed_totals(client) -> None:
    _run_to_blocked(client, issue_id="issue-w1", key="LIN-931")
    _run_to_blocked(client, issue_id="issue-w2", key="LIN-932")
    feed = client.get("/metrics/failures", headers=AUTH).json()
    agg = client.get("/metrics/failures/by-work-type", headers=AUTH).json()
    assert feed["count"] == agg["count"] == 2
    assert feed["blocked"] == agg["blocked"]
    assert sum(w["count"] for w in agg["work_types"]) == feed["count"]


# -- GET /metrics/failures/trends (failure over-time cut, issue #37) ----------


def test_failures_trends_requires_bearer_token(client) -> None:
    assert client.get("/metrics/failures/trends").status_code == 401
    assert (
        client.get(
            "/metrics/failures/trends", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_failures_trends_rejects_bad_window(client) -> None:
    assert client.get("/metrics/failures/trends?days=0", headers=AUTH).status_code == 422


def test_failures_trends_rejects_bad_bucket(client) -> None:
    assert (
        client.get("/metrics/failures/trends?bucket=month", headers=AUTH).status_code
        == 422
    )


def test_failures_trends_empty_database(client) -> None:
    body = client.get("/metrics/failures/trends", headers=AUTH).json()
    assert body["days"] == 30
    assert body["bucket"] == "day"  # the failure-trend default
    assert body["count"] == 0
    assert body["blocked"] == 0
    assert body["failed"] == 0
    assert body["periods"] == []


def test_failures_trends_buckets_blocked_run(client) -> None:
    _run_to_blocked(client)
    body = client.get("/metrics/failures/trends", headers=AUTH).json()
    assert body["count"] == 1
    assert body["blocked"] == 1
    assert body["failed"] == 0
    # The single block lands in exactly one (today's) bucket.
    assert len(body["periods"]) == 1
    period = body["periods"][0]
    assert period["count"] == 1
    assert period["blocked"] == 1
    assert period["failed"] == 0


def test_failures_trends_matches_feed_totals(client) -> None:
    # The over-time cut must agree with the per-run feed it sits beside: same
    # runs, same window, same derivation - the totals can't drift.
    _run_to_blocked(client, issue_id="issue-t1", key="LIN-911")
    _run_to_blocked(client, issue_id="issue-t2", key="LIN-912")
    feed = client.get("/metrics/failures", headers=AUTH).json()
    trend = client.get("/metrics/failures/trends", headers=AUTH).json()
    assert feed["count"] == trend["count"] == 2
    assert feed["blocked"] == trend["blocked"]
    assert sum(p["count"] for p in trend["periods"]) == feed["count"]


def test_failures_trends_accepts_week_bucket(client) -> None:
    _run_to_blocked(client)
    body = client.get(
        "/metrics/failures/trends?bucket=week", headers=AUTH
    ).json()
    assert body["bucket"] == "week"
    assert body["count"] == 1
