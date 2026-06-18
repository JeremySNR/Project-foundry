"""Map a verified webhook delivery to its tenant org (issue #34 follow-up).

Webhooks carry no OIDC token, so before this every webhook-created run and every
webhook-driven PR observation ran in the *default* org regardless of tenant. The
verified principal of a webhook is the shared secret that signed it, so the org
is derived from **which committed per-org secret matched**, never the payload
(invariant #5). These tests prove the resolver primitives, the fail-closed config
parsing/validation, and end-to-end org binding on all four provider webhooks
(Linear, GitHub, GitLab, Jira) — including that single-tenant is unchanged.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from foundry.agents.manual import InMemoryFakeProvider
from foundry.api import create_app
from foundry.api.security import compute_signature
from foundry.api.webhook_org import (
    WebhookOrgSecrets,
    org_for_hmac,
    org_for_token,
)
from foundry.config import Settings, _parse_org_secret_pairs
from foundry.db import FoundryRun, create_all, make_engine, make_session_factory
from foundry.db.models import FoundryWebhookDelivery
from foundry.db.tenant import DEFAULT_ORG_ID, tenant_context
from foundry.orchestrator import FoundryOrchestrator

GLOBAL_SECRET = "global-secret"
ACME_SECRET = "acme-secret"
GLOBEX_SECRET = "globex-secret"


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)


def _tenants() -> WebhookOrgSecrets:
    return WebhookOrgSecrets.from_pairs(
        [("acme", ACME_SECRET), ("globex", GLOBEX_SECRET)]
    )


# --------------------------------------------------------------------------- #
# WebhookOrgSecrets — construction + fail-closed validation
# --------------------------------------------------------------------------- #


def test_from_pairs_trims_and_keeps_order() -> None:
    secrets = WebhookOrgSecrets.from_pairs([(" acme ", " s1 "), ("globex", "s2")])
    assert secrets.by_org == (("acme", "s1"), ("globex", "s2"))
    assert not secrets.is_empty()
    assert secrets.secrets() == ("s1", "s2")


def test_empty_is_single_tenant() -> None:
    assert WebhookOrgSecrets().is_empty()
    assert WebhookOrgSecrets.from_pairs([]).is_empty()


@pytest.mark.parametrize(
    "pairs, message",
    [
        ([("", "s")], "non-empty"),
        ([("default", "s")], "reserved default"),
        ([("acme", "")], "non-empty"),
        ([("acme", "s"), ("acme", "s2")], "duplicate org"),
        ([("acme", "s"), ("globex", "s")], "reused across orgs"),
    ],
)
def test_from_pairs_rejects_bad_config(pairs, message) -> None:
    with pytest.raises(ValueError, match=message):
        WebhookOrgSecrets.from_pairs(pairs)


# --------------------------------------------------------------------------- #
# Resolution primitives
# --------------------------------------------------------------------------- #


def test_resolve_hmac_matches_the_signing_org() -> None:
    tenants = _tenants()
    body = b'{"hello":"world"}'
    sig = compute_signature(ACME_SECRET, body)
    assert tenants.resolve_hmac(body, sig) == "acme"
    assert tenants.resolve_hmac(body, compute_signature(GLOBEX_SECRET, body)) == "globex"
    # A signature from no configured secret resolves to nothing.
    assert tenants.resolve_hmac(body, compute_signature("other", body)) is None
    assert tenants.resolve_hmac(body, None) is None


def test_resolve_token_matches_the_org_secret() -> None:
    tenants = _tenants()
    assert tenants.resolve_token(ACME_SECRET) == "acme"
    assert tenants.resolve_token(GLOBEX_SECRET) == "globex"
    assert tenants.resolve_token("nope") is None
    assert tenants.resolve_token("") is None
    assert tenants.resolve_token(None) is None


def test_org_for_hmac_global_secret_wins_for_default_org() -> None:
    tenants = _tenants()
    body = b"payload"
    # The global secret resolves to the default org (the historical path).
    assert (
        org_for_hmac(
            default_secret=GLOBAL_SECRET,
            tenants=tenants,
            body=body,
            signature=compute_signature(GLOBAL_SECRET, body),
        )
        == DEFAULT_ORG_ID
    )
    # A per-org secret resolves to its tenant.
    assert (
        org_for_hmac(
            default_secret=GLOBAL_SECRET,
            tenants=tenants,
            body=body,
            signature=compute_signature(ACME_SECRET, body),
        )
        == "acme"
    )
    # Signed by nothing configured => None (the caller 401s).
    assert (
        org_for_hmac(
            default_secret=GLOBAL_SECRET,
            tenants=tenants,
            body=body,
            signature=compute_signature("bogus", body),
        )
        is None
    )


def test_org_for_token_resolution() -> None:
    tenants = _tenants()
    assert (
        org_for_token(default_secret=GLOBAL_SECRET, tenants=tenants, token=GLOBAL_SECRET)
        == DEFAULT_ORG_ID
    )
    assert (
        org_for_token(default_secret=GLOBAL_SECRET, tenants=tenants, token=ACME_SECRET)
        == "acme"
    )
    assert (
        org_for_token(default_secret=None, tenants=tenants, token=GLOBEX_SECRET)
        == "globex"
    )
    assert org_for_token(default_secret=GLOBAL_SECRET, tenants=tenants, token="x") is None
    assert org_for_token(default_secret=None, tenants=_tenants(), token="") is None


# --------------------------------------------------------------------------- #
# Config: env parsing + fail-closed validation at load
# --------------------------------------------------------------------------- #


def test_env_parses_org_secret_pairs() -> None:
    # Splits on the first '=' so a base64-padded secret survives.
    pairs = _parse_org_secret_pairs("acme=whsec_a==, globex=whsec_b , ")
    assert pairs == (("acme", "whsec_a=="), ("globex", "whsec_b"))


def test_env_rejects_pairs_without_a_separator() -> None:
    with pytest.raises(ValueError, match="org=secret"):
        _parse_org_secret_pairs("acme-no-equals")


def test_settings_load_validates_org_secrets_fail_closed() -> None:
    env = {
        "FOUNDRY_LINEAR_WEBHOOK_SECRET": GLOBAL_SECRET,
        "FOUNDRY_WEBHOOK_ORG_SECRETS": "acme=s1,globex=s1",  # reused secret
    }
    with pytest.raises(ValueError, match="reused across orgs"):
        Settings.load(path=None, env=env)

    ok = Settings.load(
        path=None,
        env={
            "FOUNDRY_LINEAR_WEBHOOK_SECRET": GLOBAL_SECRET,
            "FOUNDRY_WEBHOOK_ORG_SECRETS": f"acme={ACME_SECRET},globex={GLOBEX_SECRET}",
        },
    )
    assert ok.webhook_org_secrets == (("acme", ACME_SECRET), ("globex", GLOBEX_SECRET))


# --------------------------------------------------------------------------- #
# create_app guards against a tenant secret colliding with a global one
# --------------------------------------------------------------------------- #


def test_create_app_rejects_tenant_secret_colliding_with_global(session_factory) -> None:
    with pytest.raises(ValueError, match="distinct from the default-org secrets"):
        create_app(
            webhook_secret=GLOBAL_SECRET,
            session_factory=session_factory,
            orchestrator=FoundryOrchestrator(
                session_factory, provider=InMemoryFakeProvider()
            ),
            webhook_org_secrets=WebhookOrgSecrets.from_pairs(
                [("acme", GLOBAL_SECRET)]  # same as the global Linear secret
            ),
        )


# --------------------------------------------------------------------------- #
# End-to-end: a webhook signed with a tenant secret lands in that org
# --------------------------------------------------------------------------- #


def _make_client(session_factory) -> TestClient:
    app = create_app(
        webhook_secret=GLOBAL_SECRET,
        session_factory=session_factory,
        orchestrator=FoundryOrchestrator(
            session_factory, provider=InMemoryFakeProvider()
        ),
        gitlab_webhook_secret=GLOBAL_SECRET,
        jira_webhook_secret=GLOBAL_SECRET,
        webhook_org_secrets=_tenants(),
    )
    return TestClient(app)


def _linear_payload(issue_id="i-acme", key="ACME-1") -> dict:
    return {
        "data": {
            "id": issue_id,
            "issueId": issue_id,
            "identifier": key,
            "title": "Add favourites",
            "description": (
                "Customers want favourites.\n\nAcceptance Criteria:\n"
                "- A favourites button exists\n- Favourites persist"
            ),
            "labels": [{"name": "foundry:candidate"}],
            "actor": {"name": "po@acme.example"},
        }
    }


def _post_linear(client, payload, *, secret, delivery):
    body = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhooks/linear",
        content=body,
        headers={
            "Linear-Delivery": delivery,
            "Linear-Signature": "sha256=" + compute_signature(secret, body),
            "Content-Type": "application/json",
        },
    )


def test_linear_intake_lands_in_the_signing_orgs_tenant(session_factory) -> None:
    client = _make_client(session_factory)

    started = _post_linear(
        client, _linear_payload(), secret=ACME_SECRET, delivery="d-acme"
    )
    assert started.status_code == 202
    assert started.json()["status"] == "started"
    run_id = started.json()["run"]["id"]

    # The run is stamped with acme — visible only inside acme's tenant context.
    with tenant_context("acme"), session_factory() as s:
        assert s.get(FoundryRun, run_id).org_id == "acme"
    # ...and invisible to the default org (the cross-tenant isolation property).
    with session_factory() as s:
        assert s.get(FoundryRun, run_id) is None


def test_linear_intake_with_the_global_secret_stays_default(session_factory) -> None:
    client = _make_client(session_factory)
    started = _post_linear(
        client,
        _linear_payload(issue_id="i-def", key="DEF-1"),
        secret=GLOBAL_SECRET,
        delivery="d-def",
    )
    assert started.json()["status"] == "started"
    run_id = started.json()["run"]["id"]
    with session_factory() as s:  # default org
        assert s.get(FoundryRun, run_id).org_id == DEFAULT_ORG_ID


def test_linear_intake_with_an_unknown_secret_is_rejected(session_factory) -> None:
    client = _make_client(session_factory)
    resp = _post_linear(
        client, _linear_payload(), secret="not-configured", delivery="d-bad"
    )
    assert resp.status_code == 401
    with session_factory() as s, tenant_context("acme"):
        assert s.execute(select(func.count()).select_from(FoundryRun)).scalar_one() == 0


def test_two_tenants_same_issue_id_are_isolated(session_factory) -> None:
    """The same upstream issue id delivered under two tenant secrets opens one
    run per org — neither pins the other, the per-org dedup/uniqueness holds."""
    client = _make_client(session_factory)
    acme = _post_linear(
        client, _linear_payload(issue_id="shared", key="SHARED-1"),
        secret=ACME_SECRET, delivery="d-1",
    )
    globex = _post_linear(
        client, _linear_payload(issue_id="shared", key="SHARED-1"),
        secret=GLOBEX_SECRET, delivery="d-2",
    )
    assert acme.json()["status"] == "started"
    assert globex.json()["status"] == "started"
    assert acme.json()["run"]["id"] != globex.json()["run"]["id"]
    with tenant_context("acme"), session_factory() as s:
        assert s.get(FoundryRun, acme.json()["run"]["id"]).org_id == "acme"
    with tenant_context("globex"), session_factory() as s:
        assert s.get(FoundryRun, globex.json()["run"]["id"]).org_id == "globex"


# --- GitHub: the delivery (dedup) row is stamped with the resolved org -------


def _pr_payload(branch="feature") -> dict:
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


def _post_github(client, payload, *, secret, event, delivery):
    body = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": "sha256=" + compute_signature(secret, body),
            "Content-Type": "application/json",
        },
    )


def _delivery_orgs(session_factory, provider: str) -> set[str]:
    # Bypass the per-org ORM filter with a raw count per org so we can see where
    # the delivery row actually landed.
    orgs: set[str] = set()
    for org in (DEFAULT_ORG_ID, "acme", "globex"):
        with tenant_context(org), session_factory() as s:
            n = s.execute(
                select(func.count())
                .select_from(FoundryWebhookDelivery)
                .where(FoundryWebhookDelivery.provider == provider)
            ).scalar_one()
            if n:
                orgs.add(org)
    return orgs


def test_github_delivery_is_recorded_under_the_signing_org(session_factory) -> None:
    """A GitHub delivery signed with a tenant secret binds that org for the whole
    handler: the dedup row (and any intake/correlation) is isolated to it, so a
    tenant's PR observation can only ever find the tenant's own runs."""
    client = _make_client(session_factory)
    resp = _post_github(
        client, _pr_payload(), secret=ACME_SECRET, event="pull_request", delivery="g-1"
    )
    # No run correlates, but the delivery was recorded under acme, not default.
    assert resp.json()["status"] == "ignored"
    assert _delivery_orgs(session_factory, "github") == {"acme"}


def test_github_delivery_with_global_secret_stays_default(session_factory) -> None:
    client = _make_client(session_factory)
    _post_github(
        client, _pr_payload(), secret=GLOBAL_SECRET, event="pull_request", delivery="g-2"
    )
    assert _delivery_orgs(session_factory, "github") == {DEFAULT_ORG_ID}


# --- GitLab + Jira: token routing -------------------------------------------


def test_gitlab_tenant_token_is_accepted_and_global_isolated(session_factory) -> None:
    client = _make_client(session_factory)
    body = json.dumps({"object_kind": "merge_request"}).encode("utf-8")
    # A tenant token is accepted (no run correlates, so "ignored" not 401).
    ok = client.post(
        "/webhooks/gitlab",
        content=body,
        headers={"X-Gitlab-Token": ACME_SECRET, "X-Gitlab-Event": "Merge Request Hook"},
    )
    assert ok.status_code == 202
    assert ok.json()["status"] == "ignored"
    # An unknown token is rejected.
    bad = client.post(
        "/webhooks/gitlab",
        content=body,
        headers={"X-Gitlab-Token": "nope", "X-Gitlab-Event": "Merge Request Hook"},
    )
    assert bad.status_code == 401


def test_gitlab_enabled_by_tenant_secret_without_a_global_secret(session_factory) -> None:
    """A pure multi-tenant GitLab deployment (no default-org secret) is still
    enabled — the endpoint 403s only when neither a global nor any tenant secret
    is configured."""
    app = create_app(
        webhook_secret=GLOBAL_SECRET,
        session_factory=session_factory,
        orchestrator=FoundryOrchestrator(
            session_factory, provider=InMemoryFakeProvider()
        ),
        gitlab_webhook_secret=None,  # no default-org GitLab secret
        webhook_org_secrets=_tenants(),
    )
    client = TestClient(app)
    body = json.dumps({"object_kind": "merge_request"}).encode("utf-8")
    ok = client.post(
        "/webhooks/gitlab",
        content=body,
        headers={"X-Gitlab-Token": ACME_SECRET, "X-Gitlab-Event": "Merge Request Hook"},
    )
    assert ok.status_code == 202  # enabled by the tenant secret
    # The default-org token no longer authenticates anything.
    none = client.post(
        "/webhooks/gitlab",
        content=body,
        headers={"X-Gitlab-Token": GLOBAL_SECRET, "X-Gitlab-Event": "Merge Request Hook"},
    )
    assert none.status_code == 401


def _jira_payload(issue_id="JIRA-1") -> dict:
    return {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "id": issue_id,
            "key": issue_id,
            "fields": {
                "summary": "Add favourites",
                "description": (
                    "Customers want favourites.\n\nAcceptance Criteria:\n"
                    "- A button exists\n- It persists"
                ),
                "labels": ["foundry:candidate"],
            },
        },
        "user": {"emailAddress": "po@acme.example"},
    }


def test_jira_intake_lands_in_the_token_orgs_tenant(session_factory) -> None:
    client = _make_client(session_factory)
    body = json.dumps(_jira_payload()).encode("utf-8")
    resp = client.post(
        "/webhooks/jira", content=body, headers={"X-Foundry-Webhook-Token": ACME_SECRET}
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "started"
    run_id = resp.json()["run"]["id"]
    with tenant_context("acme"), session_factory() as s:
        assert s.get(FoundryRun, run_id).org_id == "acme"
    with session_factory() as s:
        assert s.get(FoundryRun, run_id) is None


def test_jira_unknown_token_rejected(session_factory) -> None:
    client = _make_client(session_factory)
    body = json.dumps(_jira_payload()).encode("utf-8")
    resp = client.post(
        "/webhooks/jira", content=body, headers={"X-Foundry-Webhook-Token": "nope"}
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Single-tenant unchanged: with no per-org secrets, every delivery is default
# --------------------------------------------------------------------------- #


def test_single_tenant_unchanged_without_org_secrets(session_factory) -> None:
    app = create_app(
        webhook_secret=GLOBAL_SECRET,
        session_factory=session_factory,
        orchestrator=FoundryOrchestrator(
            session_factory, provider=InMemoryFakeProvider()
        ),
    )
    client = TestClient(app)
    started = _post_linear(
        client, _linear_payload(), secret=GLOBAL_SECRET, delivery="d-st"
    )
    assert started.json()["status"] == "started"
    run_id = started.json()["run"]["id"]
    with session_factory() as s:
        assert s.get(FoundryRun, run_id).org_id == DEFAULT_ORG_ID
    # A tenant secret means nothing here — it is just an invalid signature.
    rejected = _post_linear(
        client,
        _linear_payload(issue_id="x", key="X-1"),
        secret=ACME_SECRET,
        delivery="d-st2",
    )
    assert rejected.status_code == 401
