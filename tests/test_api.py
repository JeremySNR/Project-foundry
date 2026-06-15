"""API tests: signed intake -> orchestrator, idempotency, approvals, run status."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from foundry.agents.manual import InMemoryFakeProvider
from foundry.api import create_app
from foundry.api.security import compute_signature
from foundry.db import create_all, make_engine, make_session_factory
from foundry.orchestrator import FoundryOrchestrator
from foundry.schemas.ticket import RawTicket

SECRET = "test-secret"
API_TOKEN = "test-api-token"
# user -> approval roles their sign-off grants (config, never request payload).
APPROVERS = {"lead@example.com": ["engineering", "security"]}
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}

READY_DESC = (
    "Customers want to favourite items.\n\n"
    "Acceptance Criteria:\n"
    "- A favourites button exists\n"
    "- Favourites persist across sessions\n"
)


def _make_client(**overrides) -> TestClient:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(sf, provider=InMemoryFakeProvider())
    kwargs = dict(
        webhook_secret=SECRET,
        session_factory=sf,
        orchestrator=orch,
        approvers=APPROVERS,
        api_token=API_TOKEN,
    )
    kwargs.update(overrides)
    return TestClient(create_app(**kwargs))


@pytest.fixture
def client() -> TestClient:
    return _make_client()


def _post_webhook(client, payload, *, delivery, sign=True):
    body = json.dumps(payload).encode("utf-8")
    headers = {"Linear-Delivery": delivery, "Content-Type": "application/json"}
    headers["Linear-Signature"] = (
        "sha256=" + compute_signature(SECRET, body) if sign else "sha256=deadbeef"
    )
    return client.post("/webhooks/linear", content=body, headers=headers)


def _basic_payload(issue_id="issue-1", key="LIN-1") -> dict:
    """Triggers a run but is too thin to be buildable (-> needs clarification)."""
    return {
        "data": {
            "id": issue_id,
            "issueId": issue_id,
            "identifier": key,
            "title": "Do something",
            "labels": [{"name": "foundry:candidate"}],
            "actor": {"name": "po@example.com"},
        }
    }


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


def test_healthz(client) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_unauthorised_webhook_rejected_no_run(client) -> None:
    resp = _post_webhook(client, _basic_payload(), delivery="d1", sign=False)
    assert resp.status_code == 401
    assert client.get("/runs").json()["runs"] == []


def test_webhook_with_no_signature_header_rejected_no_run(client) -> None:
    """A completely *missing* signature header (not just a wrong one) must fail
    closed - the absence of a header is not an authentication bypass."""
    body = json.dumps(_basic_payload()).encode("utf-8")
    resp = client.post(
        "/webhooks/linear",
        content=body,
        headers={"Linear-Delivery": "d-nohdr", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert client.get("/runs").json()["runs"] == []


def test_github_webhook_with_no_signature_header_rejected(client) -> None:
    body = json.dumps(_basic_payload()).encode("utf-8")
    resp = client.post(
        "/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


def test_linear_webhook_fails_closed_without_configured_secret() -> None:
    """No webhook secret configured ⇒ the endpoint authenticates nothing: even a
    correctly-signed (under any key) delivery is refused and starts no run."""
    c = _make_client(webhook_secret="")
    body = json.dumps(_basic_payload()).encode("utf-8")
    # Sign under the test secret; the server has no secret, so it must reject.
    sig = "sha256=" + compute_signature(SECRET, body)
    resp = c.post(
        "/webhooks/linear",
        content=body,
        headers={"Linear-Delivery": "d-nosecret", "Linear-Signature": sig},
    )
    assert resp.status_code == 401
    assert c.get("/runs").json()["runs"] == []


def test_duplicate_delivery_creates_one_run(client) -> None:
    payload = _basic_payload()
    first = _post_webhook(client, payload, delivery="d-dup")
    second = _post_webhook(client, payload, delivery="d-dup")
    assert first.json()["status"] == "started"
    assert second.json()["status"] == "duplicate"
    assert len(client.get("/runs").json()["runs"]) == 1


def test_same_issue_active_run_does_not_duplicate(client) -> None:
    payload = _ready_payload()
    _post_webhook(client, payload, delivery="d-a")
    second = _post_webhook(client, payload, delivery="d-b")
    assert second.json()["status"] == "exists"
    assert len(client.get("/runs").json()["runs"]) == 1


def test_clarified_ticket_can_be_reanalysed(client) -> None:
    """A needs-clarification run does not pin the issue forever."""
    thin = _basic_payload(issue_id="i-re", key="LIN-RE")
    first = _post_webhook(client, thin, delivery="d-re-1")
    assert first.json()["run"]["status"] == "needs_clarification"

    # The author adds acceptance criteria and a repo label; re-trigger works.
    improved = _ready_payload(issue_id="i-re", key="LIN-RE")
    second = _post_webhook(client, improved, delivery="d-re-2")
    assert second.json()["status"] == "started"
    assert second.json()["run"]["status"] == "waiting_approval"
    assert len(client.get("/runs").json()["runs"]) == 2


def test_non_trigger_event_is_ignored(client) -> None:
    payload = {"data": {"id": "i9", "issueId": "i9", "labels": []}}
    resp = _post_webhook(client, payload, delivery="d-ignore")
    assert resp.json()["status"] == "ignored"
    assert client.get("/runs").json()["runs"] == []


def test_intake_runs_orchestrator_and_persists_status(client) -> None:
    resp = _post_webhook(client, _basic_payload(), delivery="d-int")
    run = resp.json()["run"]
    # A thin ticket is analysed and parked for clarification, not left "analysing".
    assert run["status"] == "needs_clarification"
    assert run["created_by"] == "po@example.com"


def test_comment_command_triggers_run(client) -> None:
    payload = {
        "data": {
            "id": "i-cmd",
            "issueId": "i-cmd",
            "identifier": "LIN-9",
            "title": "Investigate",
            "body": "/foundry start",
        }
    }
    resp = _post_webhook(client, payload, delivery="d-cmd")
    assert resp.json()["status"] == "started"
    assert resp.json()["run"]["trigger_type"] == "comment_command"


def test_run_status_404_for_unknown(client) -> None:
    assert client.get("/runs/nope").status_code == 404


def _start_ready_run(client) -> str:
    resp = _post_webhook(client, _ready_payload(), delivery="d-ready")
    run = resp.json()["run"]
    assert run["status"] == "waiting_approval"
    return run["id"]


# -- epic auto-decomposition on the webhook intake path (issue #35) ------------

_EPIC_DESC = (
    "Roll favourites out across our surfaces.\n\n"
    "Repositories:\n"
    "- customer-web: add the favourites button\n"
    "- mobile-app: add the favourites button\n\n"
    "Acceptance Criteria:\n"
    "- A favourites button exists\n"
    "- Favourites persist across sessions\n"
)


def _epic_payload(issue_id="issue-epic", key="LIN-900") -> dict:
    return {
        "data": {
            "id": issue_id,
            "issueId": issue_id,
            "identifier": key,
            "title": "Add favourites everywhere",
            "description": _EPIC_DESC,
            "labels": [{"name": "foundry:candidate"}],
            "actor": {"name": "po@example.com"},
        }
    }


def test_webhook_does_not_decompose_epics_by_default() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(sf, provider=InMemoryFakeProvider())
    client = TestClient(
        create_app(
            webhook_secret=SECRET,
            session_factory=sf,
            orchestrator=orch,
            approvers=APPROVERS,
            api_token=API_TOKEN,
        )
    )
    resp = _post_webhook(client, _epic_payload(), delivery="d-epic-off")
    run_id = resp.json()["run"]["id"]
    assert orch.child_runs(run_id) == []
    assert orch.list_epics() == []


def test_webhook_fans_epic_into_child_runs_when_enabled() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(sf, provider=InMemoryFakeProvider())
    client = TestClient(
        create_app(
            webhook_secret=SECRET,
            session_factory=sf,
            orchestrator=orch,
            approvers=APPROVERS,
            api_token=API_TOKEN,
            auto_decompose_epics=True,
        )
    )
    resp = _post_webhook(client, _epic_payload(), delivery="d-epic-on")
    assert resp.status_code == 202
    # The webhook reports the parent (epic-root) run...
    parent_run_id = resp.json()["run"]["id"]
    # ...and the epic fanned out into one independently-gated child run per repo.
    children = orch.child_runs(parent_run_id)
    assert len(children) == 2
    assert all(c.parent_run_id == parent_run_id for c in children)
    assert [r.id for r in orch.list_epics()] == [parent_run_id]


# -- approval API auth ---------------------------------------------------------


def test_approval_requires_bearer_token(client) -> None:
    run_id = _start_ready_run(client)
    no_token = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
    )
    assert no_token.status_code == 401

    bad_token = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert bad_token.status_code == 401
    # Nothing was approved.
    assert client.get(f"/runs/{run_id}").json()["status"] == "waiting_approval"


def test_approval_api_disabled_without_configured_token() -> None:
    c = _make_client(api_token=None)
    body = json.dumps(_ready_payload()).encode("utf-8")
    sig = "sha256=" + compute_signature(SECRET, body)
    run_id = c.post(
        "/webhooks/linear",
        content=body,
        headers={"Linear-Delivery": "d", "Linear-Signature": sig},
    ).json()["run"]["id"]

    resp = c.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
        headers=AUTH,
    )
    # Fail closed: no configured token means the endpoint is disabled.
    assert resp.status_code == 403


def test_only_authorised_user_can_approve(client) -> None:
    run_id = _start_ready_run(client)
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "stranger@example.com", "text": "/foundry approve"},
        headers=AUTH,
    )
    assert resp.status_code == 403


def test_authorised_approve_dispatches_agent(client) -> None:
    run_id = _start_ready_run(client)
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dispatched"] is True
    assert body["run"]["status"] == "agent_running"
    assert body["run"]["approved_by"] == "lead@example.com"


def test_roles_in_body_are_ignored(client) -> None:
    """A caller cannot self-assert approval roles; they come from config."""
    c = _make_client(approvers={"pm@example.com": []})
    body = json.dumps(_ready_payload()).encode("utf-8")
    sig = "sha256=" + compute_signature(SECRET, body)
    run_id = c.post(
        "/webhooks/linear",
        content=body,
        headers={"Linear-Delivery": "d", "Linear-Signature": sig},
    ).json()["run"]["id"]

    resp = c.post(
        f"/runs/{run_id}/approval",
        json={
            "user": "pm@example.com",
            "text": "/foundry approve",
            # Claimed roles must have no effect.
            "roles": ["security", "engineering"],
        },
        headers=AUTH,
    )
    # The run is approved (a role-less approver can approve ordinary work) -
    # this ticket is low-risk, so dispatch proceeds; the point is that the
    # roles granted are pm@'s configured roles (none), not the claimed ones.
    assert resp.status_code == 200
    assert resp.json()["run"]["approved_by"] == "pm@example.com"


def _ready_infra_payload(issue_id="issue-infra", key="LIN-INF") -> dict:
    """Ready work that requires an ENGINEERING approval (infrastructure)."""
    return {
        "data": {
            "id": issue_id,
            "issueId": issue_id,
            "identifier": key,
            "title": "Update the terraform deployment config",
            "description": (
                "Acceptance Criteria:\n"
                "- terraform plan runs clean\n"
                "- the deployment config applies\n"
            ),
            "labels": [{"name": "foundry:candidate"}, {"name": "repo:customer-web"}],
            "actor": {"name": "po@example.com"},
        }
    }


def test_approve_without_required_role_is_forbidden_and_records_nothing() -> None:
    """Issue #18: a configured approver who lacks a role the run requires is
    refused with 403, and no approval is recorded (the run stays waiting)."""
    c = _make_client(approvers={"pm@example.com": []})
    body = json.dumps(_ready_infra_payload()).encode("utf-8")
    sig = "sha256=" + compute_signature(SECRET, body)
    run_id = c.post(
        "/webhooks/linear",
        content=body,
        headers={"Linear-Delivery": "d-infra", "Linear-Signature": sig},
    ).json()["run"]["id"]

    resp = c.post(
        f"/runs/{run_id}/approval",
        json={"user": "pm@example.com", "text": "/foundry approve"},
        headers=AUTH,
    )
    assert resp.status_code == 403
    assert "approval refused" in resp.json()["detail"]
    # Nothing was recorded: the run is still awaiting approval.
    run = c.get(f"/runs/{run_id}", headers=AUTH).json()
    assert run["status"] == "waiting_approval"
    assert run["approved_by"] is None


def test_approve_with_required_role_dispatches_infra_work(client) -> None:
    """The same infra work, approved by an engineering-capable approver, records
    the approval and dispatches (medium risk -> draft PR)."""
    body = json.dumps(_ready_infra_payload()).encode("utf-8")
    sig = "sha256=" + compute_signature(SECRET, body)
    run_id = client.post(
        "/webhooks/linear",
        content=body,
        headers={"Linear-Delivery": "d-infra2", "Linear-Signature": sig},
    ).json()["run"]["id"]

    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["run"]["status"] == "agent_running"


def test_reject_terminates_run(client) -> None:
    run_id = _start_ready_run(client)
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "reject"},
        headers=AUTH,
    )
    assert resp.json()["run"]["status"] == "rejected"


def test_approve_on_unready_run_conflicts(client) -> None:
    # A needs-clarification run cannot be approved.
    resp = _post_webhook(client, _basic_payload(issue_id="i-x", key="LIN-X"), delivery="d-x")
    run_id = resp.json()["run"]["id"]
    out = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
        headers=AUTH,
    )
    assert out.status_code == 409


def test_unrecognised_command_is_rejected(client) -> None:
    run_id = _start_ready_run(client)
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "/foundry frobnicate"},
        headers=AUTH,
    )
    assert resp.status_code == 400


# -- approval via signed Linear comments ----------------------------------------


def _comment_payload(issue_id, key, body_text, *, email="lead@example.com") -> dict:
    return {
        "data": {
            "issueId": issue_id,
            "identifier": key,
            "body": body_text,
            "actor": {"name": "Lead", "email": email},
        }
    }


def test_linear_comment_approves_run(client) -> None:
    run_id = _start_ready_run(client)
    resp = _post_webhook(
        client,
        _comment_payload("issue-r", "LIN-123", "/foundry approve"),
        delivery="d-approve",
    )
    body = resp.json()
    assert body["status"] == "applied"
    assert body["dispatched"] is True
    assert client.get(f"/runs/{run_id}").json()["status"] == "agent_running"


def test_linear_comment_from_unauthorised_user_is_ignored(client) -> None:
    run_id = _start_ready_run(client)
    resp = _post_webhook(
        client,
        _comment_payload(
            "issue-r", "LIN-123", "/foundry approve", email="stranger@example.com"
        ),
        delivery="d-stranger",
    )
    assert resp.json()["status"] == "ignored"
    assert client.get(f"/runs/{run_id}").json()["status"] == "waiting_approval"


def test_linear_comment_reject(client) -> None:
    run_id = _start_ready_run(client)
    resp = _post_webhook(
        client,
        _comment_payload("issue-r", "LIN-123", "/foundry reject"),
        delivery="d-reject",
    )
    assert resp.json()["status"] == "applied"
    assert client.get(f"/runs/{run_id}").json()["status"] == "rejected"


# -- GitHub webhook closes the loop -------------------------------------------


def _post_github(client, payload, *, event, sign=True):
    body = json.dumps(payload).encode("utf-8")
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    headers["X-Hub-Signature-256"] = (
        "sha256=" + compute_signature(SECRET, body) if sign else "sha256=bad"
    )
    return client.post("/webhooks/github", content=body, headers=headers)


def _approve_and_dispatch(client) -> str:
    run_id = _start_ready_run(client)
    client.post(
        f"/runs/{run_id}/approval",
        json={"user": "lead@example.com", "text": "/foundry approve"},
        headers=AUTH,
    )
    return run_id


def _pr_payload(branch, *, number=42, title="", state="open", merged=False) -> dict:
    return {
        "pull_request": {
            "number": number,
            "html_url": f"https://github.com/o/customer-web/pull/{number}",
            "head": {"ref": branch},
            "title": title,
            "state": state,
            "draft": False,
            "merged": merged,
        },
        "repository": {"full_name": "o/customer-web"},
    }


def test_github_unauthorised_rejected(client) -> None:
    resp = _post_github(client, {}, event="pull_request", sign=False)
    assert resp.status_code == 401


def test_github_pr_for_unknown_branch_ignored(client) -> None:
    resp = _post_github(
        client, _pr_payload("someone-elses-branch"), event="pull_request"
    )
    assert resp.json()["status"] == "ignored"


def test_github_pr_closes_loop_to_pr_open(client) -> None:
    run_id = _approve_and_dispatch(client)
    assert client.get(f"/runs/{run_id}").json()["status"] == "agent_running"

    branch = "foundry/lin-123-add-customer-favourites"
    resp = _post_github(client, _pr_payload(branch), event="pull_request")
    assert resp.json()["status"] == "recorded"
    assert resp.json()["run_status"] == "pr_open"
    assert client.get(f"/runs/{run_id}").json()["status"] == "pr_open"


def test_github_pr_correlated_by_issue_key_in_branch(client) -> None:
    """Delegated agents (Cursor via Linear) pick their own branch names; the
    embedded issue key still associates the PR with the run."""
    run_id = _approve_and_dispatch(client)
    branch = "cursor/lin-123-favourites-button"  # not the Foundry-chosen branch
    resp = _post_github(client, _pr_payload(branch), event="pull_request")
    assert resp.json()["status"] == "recorded"
    assert resp.json()["run_id"] == run_id
    assert client.get(f"/runs/{run_id}").json()["status"] == "pr_open"


def test_github_pr_correlated_by_issue_key_in_title(client) -> None:
    run_id = _approve_and_dispatch(client)
    resp = _post_github(
        client,
        _pr_payload("agent/some-opaque-name", title="LIN-123 add favourites"),
        event="pull_request",
    )
    assert resp.json()["status"] == "recorded"
    assert resp.json()["run_id"] == run_id


def test_github_pr_update_events_do_not_crash(client) -> None:
    """synchronize/review/CI events after the PR opened are recorded, not 500s."""
    run_id = _approve_and_dispatch(client)
    branch = "foundry/lin-123-add-customer-favourites"
    _post_github(client, _pr_payload(branch), event="pull_request")

    # A later synchronize event for the same PR.
    second = _post_github(client, _pr_payload(branch), event="pull_request")
    assert second.status_code == 202
    assert second.json()["status"] == "recorded"

    # A review event (no file list) keeps the run's status.
    review = dict(_pr_payload(branch))
    review["review"] = {"state": "approved", "user": {"type": "User"}}
    third = _post_github(client, review, event="pull_request_review")
    assert third.status_code == 202
    assert client.get(f"/runs/{run_id}").json()["status"] == "pr_open"


def test_github_merged_pr_completes_run(client) -> None:
    run_id = _approve_and_dispatch(client)
    branch = "foundry/lin-123-add-customer-favourites"
    _post_github(client, _pr_payload(branch), event="pull_request")
    resp = _post_github(
        client,
        _pr_payload(branch, state="closed", merged=True),
        event="pull_request",
    )
    assert resp.json()["run_status"] == "complete"
    assert client.get(f"/runs/{run_id}").json()["status"] == "complete"


def test_github_event_for_finished_run_is_ignored_not_500(client) -> None:
    run_id = _approve_and_dispatch(client)
    branch = "foundry/lin-123-add-customer-favourites"
    _post_github(client, _pr_payload(branch), event="pull_request")
    _post_github(
        client, _pr_payload(branch, state="closed", merged=True), event="pull_request"
    )
    # The run is complete; a stray late event is acknowledged and ignored.
    late = _post_github(client, _pr_payload(branch), event="pull_request")
    assert late.status_code == 202
    assert late.json()["status"] == "ignored"
    assert client.get(f"/runs/{run_id}").json()["status"] == "complete"


# -- settings-driven app boot ---------------------------------------------------


def test_app_from_settings_boots_with_defaults() -> None:
    from foundry.api import app_from_settings
    from foundry.config import Settings

    app = app_from_settings(Settings.from_env({"FOUNDRY_LINEAR_WEBHOOK_SECRET": "s"}))
    c = TestClient(app)
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/runs").json()["runs"] == []


def test_app_from_settings_wires_connectors_when_tokens_present() -> None:
    from foundry.api import app_from_settings
    from foundry.config import Settings

    # Tokens present => Linear tracker + GitHub connector are constructed (lazily,
    # no network at construction time). The app should still boot cleanly.
    app = app_from_settings(
        Settings.from_env(
            {
                "FOUNDRY_LINEAR_WEBHOOK_SECRET": "s",
                "FOUNDRY_LINEAR_API_TOKEN": "lt",
                "FOUNDRY_GITHUB_API_TOKEN": "gt",
            }
        )
    )
    assert TestClient(app).get("/healthz").status_code == 200


def test_settings_custom_trigger_label_is_honored() -> None:
    from foundry.api import app_from_settings
    from foundry.config import Settings

    settings = Settings.from_env({"FOUNDRY_LINEAR_WEBHOOK_SECRET": SECRET})
    settings = settings._with({"trigger_label": "ai:go"})
    c = TestClient(app_from_settings(settings))

    # The default label no longer triggers...
    default_labelled = _basic_payload()
    body = json.dumps(default_labelled).encode("utf-8")
    sig = "sha256=" + compute_signature(SECRET, body)
    resp = c.post(
        "/webhooks/linear",
        content=body,
        headers={"Linear-Delivery": "d1", "Linear-Signature": sig},
    )
    assert resp.json()["status"] == "ignored"

    # ...but the configured one does.
    custom = {
        "data": {
            "id": "i2",
            "issueId": "i2",
            "identifier": "LIN-2",
            "title": "x",
            "labels": [{"name": "ai:go"}],
        }
    }
    body2 = json.dumps(custom).encode("utf-8")
    sig2 = "sha256=" + compute_signature(SECRET, body2)
    resp2 = c.post(
        "/webhooks/linear",
        content=body2,
        headers={"Linear-Delivery": "d2", "Linear-Signature": sig2},
    )
    assert resp2.json()["status"] == "started"


# -- run timeline --------------------------------------------------------------


def test_timeline_requires_token(client) -> None:
    _post_webhook(client, _ready_payload(), delivery="d-tl-0")
    run_id = client.get("/runs").json()["runs"][0]["id"]
    assert client.get(f"/runs/{run_id}/timeline").status_code == 401
    assert (
        client.get(
            f"/runs/{run_id}/timeline", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_timeline_disabled_without_configured_token() -> None:
    client = _make_client(api_token=None)
    _post_webhook(client, _ready_payload(), delivery="d-tl-1")
    run_id = client.get("/runs").json()["runs"][0]["id"]
    assert client.get(f"/runs/{run_id}/timeline", headers=AUTH).status_code == 403


def test_timeline_unknown_run_404(client) -> None:
    assert client.get("/runs/nope/timeline", headers=AUTH).status_code == 404


# -- epic view (parent/child runs, issue #35) ----------------------------------


def _make_epic(client) -> tuple[str, str]:
    """Create a parent run + one child linked to it; return (parent, child)."""
    orch = client.app.state.orchestrator
    parent = orch.intake_and_plan(
        RawTicket(
            issue_id="epic-1",
            issue_key="LIN-900",
            title="Migrate logging",
            description=READY_DESC,
            known_repositories=["customer-web"],
        ),
        trigger_type="label",
    )
    child = orch.intake_and_plan(
        RawTicket(
            issue_id="epic-1-child",
            issue_key="LIN-901",
            title="Migrate logging in web",
            description=READY_DESC,
            known_repositories=["customer-web"],
        ),
        trigger_type="label",
        parent_run_id=parent,
    )
    return parent, child


def test_epic_requires_token(client) -> None:
    parent, _ = _make_epic(client)
    assert client.get(f"/runs/{parent}/epic").status_code == 401


def test_epic_lists_children_and_rollup(client) -> None:
    parent, child = _make_epic(client)
    body = client.get(f"/runs/{parent}/epic", headers=AUTH).json()
    assert body["run"]["id"] == parent
    assert [c["id"] for c in body["children"]] == [child]
    assert body["rollup"]["total"] == 1
    assert body["rollup"]["status"] == "in_progress"


def test_epic_resolves_root_from_child(client) -> None:
    parent, child = _make_epic(client)
    # Asking for a child's epic returns the whole epic, rooted at the parent.
    body = client.get(f"/runs/{child}/epic", headers=AUTH).json()
    assert body["run"]["id"] == parent
    assert [c["id"] for c in body["children"]] == [child]


def test_epic_unknown_run_404(client) -> None:
    assert client.get("/runs/nope/epic", headers=AUTH).status_code == 404


def test_run_dict_exposes_parent_run_id(client) -> None:
    parent, child = _make_epic(client)
    child_dict = client.get(f"/runs/{child}").json()
    assert child_dict["parent_run_id"] == parent
    parent_dict = client.get(f"/runs/{parent}").json()
    assert parent_dict["parent_run_id"] is None


# -- epic board (GET /epics, dashboard data, issue #35) ------------------------


def test_epics_requires_token(client) -> None:
    _make_epic(client)
    assert client.get("/epics").status_code == 401


def test_epics_disabled_without_configured_token() -> None:
    client = _make_client(api_token=None)
    assert client.get("/epics").status_code == 403


def test_epics_lists_roots_with_rollup_and_children(client) -> None:
    parent, child = _make_epic(client)
    body = client.get("/epics", headers=AUTH).json()
    assert body["total"] == 1
    (epic,) = body["epics"]
    assert epic["run"]["id"] == parent
    assert [c["id"] for c in epic["children"]] == [child]
    # The rollup is the same server-computed shape as GET /runs/{id}/epic.
    assert epic["rollup"]["total"] == 1
    assert epic["rollup"]["status"] == "in_progress"
    assert epic["rollup"]["counts"]["active"] == 1


def test_epics_omits_runs_without_children(client) -> None:
    # A plain single-repo run is not an epic and must not appear.
    orch = client.app.state.orchestrator
    orch.intake_and_plan(
        RawTicket(
            issue_id="solo",
            issue_key="LIN-1",
            title="Standalone task",
            description=READY_DESC,
            known_repositories=["customer-web"],
        ),
        trigger_type="label",
    )
    body = client.get("/epics", headers=AUTH).json()
    assert body == {"epics": [], "total": 0}


def test_dashboard_maps_every_epic_status_to_a_badge() -> None:
    """Drift guard: every EpicStatus the /epics rollup can emit must have an
    explicit badge class in the dashboard, or a real status (e.g. partial) is
    silently rendered with the muted fallback."""
    from foundry.api.dashboard import DASHBOARD_HTML
    from foundry.epics import EpicStatus

    start = DASHBOARD_HTML.index("const EPIC_BADGE = {")
    badge_block = DASHBOARD_HTML[start : DASHBOARD_HTML.index("};", start)]
    for status in EpicStatus:
        assert f"{status.value}:" in badge_block, f"no epic badge for {status.value}"


def test_dashboard_talks_to_epics_endpoint() -> None:
    from foundry.api.dashboard import DASHBOARD_HTML

    assert 'fetch("epics"' in DASHBOARD_HTML


def test_dashboard_talks_to_agent_trends_endpoint() -> None:
    from foundry.api.dashboard import DASHBOARD_HTML

    assert 'fetch("metrics/agents/trends' in DASHBOARD_HTML


# -- Enricher wiring via build_orchestrator ------------------------------------


def test_build_orchestrator_static_uses_static_enricher() -> None:
    from foundry.api.app import build_orchestrator
    from foundry.config import Settings
    from foundry.engines.enrichment import StaticContextEnricher
    from foundry.db import create_all, make_engine, make_session_factory

    engine = make_engine()
    create_all(engine)
    sf = make_session_factory(engine)
    settings = Settings.from_env({"FOUNDRY_LINEAR_WEBHOOK_SECRET": "s"})
    assert settings.context_provider == "static"

    orch = build_orchestrator(settings, sf)
    assert isinstance(orch._enricher, StaticContextEnricher)


def test_build_orchestrator_catalog_uses_catalog_enricher() -> None:
    from foundry.api.app import build_orchestrator
    from foundry.config import Settings
    from foundry.engines.enrichment import CatalogContextEnricher
    from foundry.db import create_all, make_engine, make_session_factory
    from dataclasses import replace

    engine = make_engine()
    create_all(engine)
    sf = make_session_factory(engine)
    base = Settings.from_env({"FOUNDRY_LINEAR_WEBHOOK_SECRET": "s"})
    settings = replace(base, context_provider="catalog")

    orch = build_orchestrator(settings, sf)
    assert isinstance(orch._enricher, CatalogContextEnricher)


def test_build_orchestrator_code_uses_code_enricher() -> None:
    from foundry.api.app import build_orchestrator
    from foundry.config import Settings
    from foundry.engines.code_context import CodeContextEnricher
    from foundry.db import create_all, make_engine, make_session_factory
    from dataclasses import replace

    engine = make_engine()
    create_all(engine)
    sf = make_session_factory(engine)
    base = Settings.from_env({"FOUNDRY_LINEAR_WEBHOOK_SECRET": "s"})
    settings = replace(base, context_provider="code")

    orch = build_orchestrator(settings, sf)
    assert isinstance(orch._enricher, CodeContextEnricher)


def test_build_orchestrator_slack_notifier_fail_closed() -> None:
    """Outbound Slack is wired only when BOTH the bot token and channel are set."""
    from dataclasses import replace

    from foundry.api.app import build_orchestrator
    from foundry.config import Settings
    from foundry.connectors.slack import SlackNotifier
    from foundry.db import create_all, make_engine, make_session_factory

    engine = make_engine()
    create_all(engine)
    sf = make_session_factory(engine)
    base = Settings.from_env({"FOUNDRY_LINEAR_WEBHOOK_SECRET": "s"})

    # Neither / only one => no notifier.
    assert build_orchestrator(base, sf)._notifier is None
    assert build_orchestrator(replace(base, slack_bot_token="xoxb-1"), sf)._notifier is None
    assert build_orchestrator(replace(base, slack_channel="C1"), sf)._notifier is None

    # Both => a SlackNotifier is wired.
    both = replace(base, slack_bot_token="xoxb-1", slack_channel="C1")
    assert isinstance(build_orchestrator(both, sf)._notifier, SlackNotifier)


def test_build_orchestrator_static_carries_yaml_keywords() -> None:
    """Keywords from context.repo_keywords are wired into StaticContextEnricher."""
    from foundry.api.app import build_orchestrator
    from foundry.config import Settings
    from foundry.engines.enrichment import StaticContextEnricher
    from foundry.db import create_all, make_engine, make_session_factory
    from dataclasses import replace

    engine = make_engine()
    create_all(engine)
    sf = make_session_factory(engine)
    base = Settings.from_env({"FOUNDRY_LINEAR_WEBHOOK_SECRET": "s"})
    settings = replace(base, context_repo_keywords=(("org/billing", ("invoice",)),))

    orch = build_orchestrator(settings, sf)
    assert isinstance(orch._enricher, StaticContextEnricher)
    assert "org/billing" in orch._enricher._catalog


# -- GitHub webhook freshness nudge -------------------------------------------


def test_github_webhook_nudges_catalog_pushed_at() -> None:
    """A GitHub push payload updates pushed_at on the catalog row."""
    import json
    from foundry.api.app import build_orchestrator
    from foundry.config import Settings
    from foundry.db import create_all, make_engine, make_session_factory
    from foundry.db.models import FoundryRepoCatalogEntry
    from foundry.api.security import compute_signature
    from datetime import timezone

    engine = make_engine()
    create_all(engine)
    sf = make_session_factory(engine)

    with sf() as session:
        session.add(FoundryRepoCatalogEntry(
            repo="org/watched-repo",
            topics="[]",
            top_dirs="[]",
            recent_pr_titles="[]",
            top_contributors="[]",
            created_at=__import__("datetime").datetime.now(timezone.utc),
            updated_at=__import__("datetime").datetime.now(timezone.utc),
        ))
        session.commit()

    base = Settings.from_env({"FOUNDRY_LINEAR_WEBHOOK_SECRET": SECRET})
    orch = build_orchestrator(base, sf)
    from fastapi.testclient import TestClient
    from foundry.api.app import create_app
    tc = TestClient(create_app(
        webhook_secret=SECRET,
        session_factory=sf,
        orchestrator=orch,
        api_token=API_TOKEN,
        github_webhook_secret=SECRET,
    ))

    payload = {
        "action": "opened",
        "repository": {"full_name": "org/watched-repo"},
        "pull_request": {
            "number": 1,
            "head": {"ref": "branch-x", "sha": "abc"},
            "base": {"ref": "main"},
            "state": "open",
            "draft": False,
            "merged": False,
            "merged_at": None,
            "title": "some PR",
            "html_url": "https://github.com/org/watched-repo/pull/1",
            "user": {"type": "User"},
        },
    }
    body = json.dumps(payload).encode()
    sig = "sha256=" + compute_signature(SECRET, body)
    resp = tc.post(
        "/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": sig},
    )
    assert resp.status_code == 202

    with sf() as session:
        entry = session.get(FoundryRepoCatalogEntry, "org/watched-repo")
        assert entry is not None
        assert entry.pushed_at is not None


def test_github_webhook_nudge_absent_row_via_client(client) -> None:
    """Webhook returns 202 even when no catalog row exists for the repo."""
    payload = {
        "action": "opened",
        "repository": {"full_name": "org/no-catalog-row"},
        "pull_request": {
            "number": 1,
            "head": {"ref": "branch-x", "sha": "abc"},
            "base": {"ref": "main"},
            "state": "open",
            "draft": False,
            "merged": False,
            "merged_at": None,
            "title": "some PR",
            "html_url": "https://github.com/org/no-catalog-row/pull/1",
            "user": {"type": "User"},
        },
    }
    body = json.dumps(payload).encode()
    sig = "sha256=" + compute_signature(SECRET, body)
    resp = client.post(
        "/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": sig},
    )
    assert resp.status_code == 202


def test_timeline_exposes_full_decision_record(client) -> None:
    _post_webhook(client, _ready_payload(), delivery="d-tl-2")
    run_id = client.get("/runs").json()["runs"][0]["id"]
    # Approve via the signed Linear comment surface so the agent dispatches.
    approval = {
        "data": {
            "id": "c-tl",
            "issueId": "issue-r",
            "identifier": "LIN-123",
            "body": "/foundry approve",
            "actor": {"name": "lead", "email": "lead@example.com"},
        }
    }
    _post_webhook(client, approval, delivery="d-tl-3")

    timeline = client.get(f"/runs/{run_id}/timeline", headers=AUTH).json()
    assert timeline["run"]["id"] == run_id

    artifact_types = {a["artifact_type"] for a in timeline["artifacts"]}
    assert "ticket_analysis" in artifact_types
    assert "risk_assessment" in artifact_types
    # Artifact content is parsed JSON, not a string blob.
    assert all(isinstance(a["content"], dict) for a in timeline["artifacts"])

    event_types = [e["event_type"] for e in timeline["audit_events"]]
    assert "run.started" in event_types
    assert "approval.granted" in event_types
    # Audit events arrive in their guaranteed per-run order.
    sequences = [e["sequence"] for e in timeline["audit_events"]]
    assert sequences == sorted(sequences)

    decisions = timeline["policy_decisions"]
    assert decisions, "policy decisions must be visible"
    assert {"policy_name", "allowed", "reason", "input", "decision"} <= set(
        decisions[0]
    )

    assert timeline["agent_jobs"], "the dispatched agent job must be visible"
    assert timeline["agent_jobs"][0]["provider"] == "fake"

    # Spend vs cap is surfaced so an approver sees budget before approving.
    assert {"consumed_usd", "cap_usd", "estimated_cost_per_dispatch"} == set(
        timeline["budget"]
    )


# -- compliance evidence pack --------------------------------------------------


def test_evidence_requires_token(client) -> None:
    run_id = _start_ready_run(client)
    assert client.get(f"/runs/{run_id}/evidence").status_code == 401
    assert (
        client.get(
            f"/runs/{run_id}/evidence", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_evidence_disabled_without_configured_token() -> None:
    c = _make_client(api_token=None)
    # The endpoint is fail-closed like the timeline: no token => 403.
    assert c.get("/runs/whatever/evidence", headers=AUTH).status_code == 403


def test_evidence_unknown_run_404(client) -> None:
    assert client.get("/runs/nope/evidence", headers=AUTH).status_code == 404


def test_evidence_bad_format_422(client) -> None:
    run_id = _start_ready_run(client)
    resp = client.get(f"/runs/{run_id}/evidence?format=xml", headers=AUTH)
    assert resp.status_code == 422


def test_evidence_pdf_render(client) -> None:
    pytest.importorskip("fpdf")
    run_id = _approve_and_dispatch(client)
    resp = client.get(f"/runs/{run_id}/evidence?format=pdf", headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert f"evidence-{run_id}.pdf" in resp.headers["content-disposition"]
    assert resp.content.startswith(b"%PDF-")


def test_evidence_pack_for_driven_run(client) -> None:
    run_id = _approve_and_dispatch(client)
    pack = client.get(f"/runs/{run_id}/evidence", headers=AUTH).json()

    assert pack["run"]["id"] == run_id
    assert pack["ticket"] is not None
    assert pack["plan"] is not None
    assert pack["risk_assessment"] is not None
    assert pack["approvals"], "the recorded approval must appear, with identity"
    assert pack["approvals"][0]["approver"] == "lead@example.com"
    assert pack["policy_decisions"], "policy-gate decisions must appear"
    assert pack["agent_jobs"], "the dispatched job must appear"

    # Integrity holds for an untampered, freshly-written run.
    assert pack["integrity"]["verified"] is True
    assert pack["integrity"]["artifacts"]["failed"] == []

    # Controls are present and evaluated against the run's evidence.
    control_ids = {c["control_id"] for c in pack["control_mappings"]}
    assert {"CC8.1", "Article 14"} <= control_ids


def test_evidence_html_render(client) -> None:
    run_id = _approve_and_dispatch(client)
    resp = client.get(f"/runs/{run_id}/evidence?format=html", headers=AUTH)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert resp.text.startswith("<!doctype html>")
    assert "Compliance evidence pack" in resp.text


# -- org-wide evidence archive -------------------------------------------------


def test_evidence_archive_requires_token(client) -> None:
    assert client.get("/evidence").status_code == 401
    assert (
        client.get("/evidence", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )


def test_evidence_archive_disabled_without_configured_token() -> None:
    c = _make_client(api_token=None)
    assert c.get("/evidence", headers=AUTH).status_code == 403


def test_evidence_archive_bad_format_422(client) -> None:
    assert client.get("/evidence?format=xml", headers=AUTH).status_code == 422


def test_evidence_archive_bad_date_422(client) -> None:
    assert client.get("/evidence?from=not-a-date", headers=AUTH).status_code == 422


def test_evidence_archive_inverted_range_422(client) -> None:
    resp = client.get(
        "/evidence?from=2026-06-10&to=2026-06-01", headers=AUTH
    )
    assert resp.status_code == 422


def test_evidence_archive_packages_runs_in_range(client) -> None:
    run_id = _approve_and_dispatch(client)
    # A wide window certainly contains the just-created run.
    archive = client.get("/evidence?days=3650", headers=AUTH).json()

    assert archive["run_count"] >= 1
    ids = [p["run"]["id"] for p in archive["runs"]]
    assert run_id in ids

    # Each entry is a full pack with integrity + controls.
    pack = next(p for p in archive["runs"] if p["run"]["id"] == run_id)
    assert pack["integrity"]["verified"] is True
    assert pack["plan"] is not None

    summary = archive["summary"]
    assert summary["verified"] is True
    assert summary["runs_verified"] == archive["run_count"]
    control_ids = {c["control_id"] for c in summary["control_coverage"]}
    assert {"CC8.1", "Article 14"} <= control_ids


def test_evidence_archive_empty_window_is_well_formed(client) -> None:
    _approve_and_dispatch(client)
    # A window that ends before the run was created excludes everything.
    archive = client.get(
        "/evidence?from=2000-01-01&to=2000-12-31", headers=AUTH
    ).json()
    assert archive["run_count"] == 0
    assert archive["runs"] == []
    assert archive["summary"]["verified"] is True


def test_evidence_archive_html_render(client) -> None:
    _approve_and_dispatch(client)
    resp = client.get("/evidence?days=3650&format=html", headers=AUTH)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert resp.text.startswith("<!doctype html>")
    assert "Compliance evidence archive" in resp.text


def test_evidence_archive_pdf_render(client) -> None:
    pytest.importorskip("fpdf")
    _approve_and_dispatch(client)
    resp = client.get("/evidence?days=3650&format=pdf", headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "evidence-archive.pdf" in resp.headers["content-disposition"]
    assert resp.content.startswith(b"%PDF-")


# -- epic (cross-run) evidence export ------------------------------------------


def test_epic_evidence_requires_token(client) -> None:
    parent, _ = _make_epic(client)
    assert client.get(f"/runs/{parent}/epic/evidence").status_code == 401


def test_epic_evidence_disabled_without_configured_token() -> None:
    c = _make_client(api_token=None)
    assert c.get("/runs/whatever/epic/evidence", headers=AUTH).status_code == 403


def test_epic_evidence_unknown_run_404(client) -> None:
    assert client.get("/runs/nope/epic/evidence", headers=AUTH).status_code == 404


def test_epic_evidence_bad_format_422(client) -> None:
    parent, _ = _make_epic(client)
    resp = client.get(f"/runs/{parent}/epic/evidence?format=xml", headers=AUTH)
    assert resp.status_code == 422


def test_epic_evidence_bundles_parent_and_children(client) -> None:
    parent, child = _make_epic(client)
    pack = client.get(f"/runs/{parent}/epic/evidence", headers=AUTH).json()

    assert pack["epic"]["root_run_id"] == parent
    assert pack["epic"]["child_run_ids"] == [child]
    assert pack["run_count"] == 2  # parent + one child
    assert pack["root"]["run"]["id"] == parent
    assert [p["run"]["id"] for p in pack["children"]] == [child]
    assert pack["children"][0]["run"]["parent_run_id"] == parent
    # The rollup agrees with GET /runs/{id}/epic (one in-flight child).
    assert pack["epic"]["rollup"]["total"] == 1
    assert pack["epic"]["rollup"]["status"] == "in_progress"


def test_epic_evidence_resolves_root_from_child(client) -> None:
    parent, child = _make_epic(client)
    # Asking for a child's epic evidence returns the whole epic, rooted at parent.
    pack = client.get(f"/runs/{child}/epic/evidence", headers=AUTH).json()
    assert pack["epic"]["root_run_id"] == parent
    assert pack["epic"]["child_run_ids"] == [child]


def test_epic_evidence_html_render(client) -> None:
    parent, _ = _make_epic(client)
    resp = client.get(f"/runs/{parent}/epic/evidence?format=html", headers=AUTH)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert resp.text.startswith("<!doctype html>")
    assert "Epic evidence pack" in resp.text


def test_epic_evidence_pdf_render(client) -> None:
    pytest.importorskip("fpdf")
    parent, _ = _make_epic(client)
    resp = client.get(f"/runs/{parent}/epic/evidence?format=pdf", headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert f"epic-evidence-{parent}.pdf" in resp.headers["content-disposition"]
    assert resp.content.startswith(b"%PDF-")


# -- dashboard -----------------------------------------------------------------


def test_dashboard_served_when_token_configured(client) -> None:
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Foundry" in resp.text
    assert "runs/" in resp.text  # talks to the timeline API


def test_dashboard_disabled_without_token() -> None:
    client = _make_client(api_token=None)
    assert client.get("/dashboard").status_code == 403


def test_dashboard_maps_every_run_status_to_a_badge() -> None:
    """Drift guard: every RunStatus the API can emit must have an explicit badge
    class in the dashboard, otherwise a real status (e.g. execution_failed) is
    silently rendered with the muted fallback. The static HTML is the contract,
    since the page renders client-side from /runs."""
    from foundry.api.dashboard import DASHBOARD_HTML
    from foundry.schemas.common import RunStatus

    # Extract the STATUS_BADGE object literal so a status value appearing
    # elsewhere in the page cannot mask a missing mapping.
    start = DASHBOARD_HTML.index("const STATUS_BADGE = {")
    badge_block = DASHBOARD_HTML[start : DASHBOARD_HTML.index("};", start)]
    for status in RunStatus:
        assert f"{status.value}:" in badge_block, f"no dashboard badge for {status.value}"


def test_build_orchestrator_llm_risk_provider_wires_both_classifiers() -> None:
    from dataclasses import replace

    from foundry.api.app import build_orchestrator
    from foundry.config import Settings
    from foundry.db import create_all, make_engine, make_session_factory
    from foundry.engines.llm_risk import LlmDiffRiskClassifier, LlmRiskClassifier
    from foundry.engines.risk import GlobDiffRiskClassifier, HeuristicRiskClassifier

    engine = make_engine()
    create_all(engine)
    sf = make_session_factory(engine)
    base = Settings.from_env({"FOUNDRY_LINEAR_WEBHOOK_SECRET": "s"})

    orch = build_orchestrator(base, sf)
    assert isinstance(orch._risk, HeuristicRiskClassifier)
    assert isinstance(orch._diff_risk, GlobDiffRiskClassifier)

    orch = build_orchestrator(replace(base, risk_provider="llm"), sf)
    assert isinstance(orch._risk, LlmRiskClassifier)
    assert isinstance(orch._diff_risk, LlmDiffRiskClassifier)
