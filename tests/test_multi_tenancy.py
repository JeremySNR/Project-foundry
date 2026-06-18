"""Row-level multi-tenancy: org_id stamping + isolation (issue #156).

These tests prove the central tenant seam (``foundry.db.tenant`` +
``foundry.db.base``): every tenant-scoped row is stamped with the active org at
write time, every ORM read is filtered to the active org, and a unit of work
scoped to one org can neither read nor write another org's rows. The default
(single-tenant) org is exercised too, so the regression that "single-tenant is
unchanged" is explicit.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from foundry.agents.manual import InMemoryFakeProvider
from foundry.api.app import create_app
from foundry.api.oidc import OidcAuthError
from foundry.api.oidc_login import SESSION_COOKIE, OidcLogin
from foundry.api.sessions import SessionSigner
from foundry.api.tenant import resolve_request_org
from foundry.compliance.evidence import verify_integrity
from foundry.db import (
    FoundryArtifact,
    FoundryAuditEvent,
    FoundryPolicyDecision,
    FoundryRun,
    create_all,
    make_engine,
    make_session_factory,
)
from foundry.db.models import TENANT_SCOPED_MODELS
from foundry.db.tenant import DEFAULT_ORG_ID, current_org_id, tenant_context
from foundry.orchestrator import FoundryOrchestrator
from foundry.schemas.ticket import RawTicket

READY_DESC = """\
Customers want to favourite items.

Acceptance Criteria:
- A favourites button exists
- Favourites persist across sessions
"""


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)


def _ticket(**overrides) -> RawTicket:
    base = dict(
        issue_id="i-1",
        issue_key="LIN-123",
        title="Add customer favourites",
        description=READY_DESC,
        known_repositories=["customer-web"],
    )
    base.update(overrides)
    return RawTicket(**base)


def _intake(session_factory, org: str | None = None) -> str:
    orch = FoundryOrchestrator(session_factory)
    if org is None:
        return orch.intake_and_plan(_ticket(), trigger_type="label")
    with tenant_context(org):
        return orch.intake_and_plan(_ticket(), trigger_type="label")


# --------------------------------------------------------------------------- #
# Schema + context plumbing
# --------------------------------------------------------------------------- #

def test_every_tenant_table_has_org_id() -> None:
    """All tenant-scoped tables carry the column the isolation relies on.

    Eleven today: the original eight (#156) plus the three SCIM provisioning
    tables (#157), all of which inherit ``TenantScoped`` so a provisioned
    directory is isolated per org.
    """
    assert len(TENANT_SCOPED_MODELS) == 11
    for model in TENANT_SCOPED_MODELS:
        assert "org_id" in model.__table__.columns
        col = model.__table__.columns["org_id"]
        assert not col.nullable


def test_default_context_is_the_default_org() -> None:
    assert current_org_id() == DEFAULT_ORG_ID


def test_tenant_context_restores_previous_org() -> None:
    assert current_org_id() == DEFAULT_ORG_ID
    with tenant_context("acme"):
        assert current_org_id() == "acme"
        with tenant_context("globex"):
            assert current_org_id() == "globex"
        assert current_org_id() == "acme"
    assert current_org_id() == DEFAULT_ORG_ID


def test_blank_org_falls_back_to_default() -> None:
    with tenant_context("   "):
        assert current_org_id() == DEFAULT_ORG_ID


# --------------------------------------------------------------------------- #
# Write-path stamping
# --------------------------------------------------------------------------- #

def test_intake_stamps_every_row_with_the_active_org(session_factory) -> None:
    run_id = _intake(session_factory, org="acme")
    with tenant_context("acme"), session_factory() as s:
        run = s.get(FoundryRun, run_id)
        assert run.org_id == "acme"
        # The whole artifact/audit/decision graph for the run inherits the org.
        for art in s.query(FoundryArtifact).filter_by(run_id=run_id):
            assert art.org_id == "acme"
        events = s.query(FoundryAuditEvent).filter_by(run_id=run_id).all()
        assert events and all(e.org_id == "acme" for e in events)
        decisions = s.query(FoundryPolicyDecision).filter_by(run_id=run_id).all()
        assert decisions and all(d.org_id == "acme" for d in decisions)


def test_intake_without_a_tenant_uses_the_default_org(session_factory) -> None:
    run_id = _intake(session_factory)
    with session_factory() as s:
        assert s.get(FoundryRun, run_id).org_id == DEFAULT_ORG_ID


# --------------------------------------------------------------------------- #
# Read isolation — the cross-tenant leakage proof
# --------------------------------------------------------------------------- #

def test_cross_tenant_reads_are_isolated(session_factory) -> None:
    """Two orgs each open a run; neither can see the other's, no matter how the
    read is phrased (orchestrator list, direct query, or get-by-id)."""
    acme_run = _intake(session_factory, org="acme")
    globex_run = _intake(session_factory, org="globex")
    assert acme_run != globex_run

    acme_orch = FoundryOrchestrator(session_factory)
    globex_orch = FoundryOrchestrator(session_factory)

    with tenant_context("acme"):
        ids = {r.id for r in acme_orch.list_runs()}
        assert ids == {acme_run}
        assert acme_orch.get_run(acme_run) is not None
        # The other org's run is invisible — not even fetchable by id.
        assert acme_orch.get_run(globex_run) is None
        with session_factory() as s:
            assert s.query(FoundryRun).count() == 1
            assert s.get(FoundryRun, globex_run) is None
            # Child tables are filtered too.
            assert (
                s.query(FoundryArtifact).filter_by(run_id=globex_run).count() == 0
            )

    with tenant_context("globex"):
        ids = {r.id for r in globex_orch.list_runs()}
        assert ids == {globex_run}
        assert globex_orch.get_run(acme_run) is None


def test_default_org_cannot_see_a_tenant_run(session_factory) -> None:
    """A read with no tenant in scope sees only default-org rows — a tenant's
    rows are not leaked to the unscoped default surface."""
    acme_run = _intake(session_factory, org="acme")
    default_run = _intake(session_factory)
    with session_factory() as s:  # default org
        ids = {r.id for r in s.query(FoundryRun).all()}
        assert ids == {default_run}
        assert acme_run not in ids


# --------------------------------------------------------------------------- #
# Write isolation
# --------------------------------------------------------------------------- #

def test_one_org_cannot_mutate_another_orgs_row(session_factory) -> None:
    """An org can't update a row it can't read: the row is unfetchable in its
    context, and a query-scoped UPDATE matches nothing across the org boundary."""
    acme_run = _intake(session_factory, org="acme")
    with tenant_context("globex"), session_factory() as s:
        assert s.get(FoundryRun, acme_run) is None
        affected = (
            s.query(FoundryRun)
            .filter(FoundryRun.id == acme_run)
            .update({FoundryRun.current_step: "tampered"})
        )
        s.commit()
        assert affected == 0
    # acme's row is untouched.
    with tenant_context("acme"), session_factory() as s:
        assert s.get(FoundryRun, acme_run).current_step != "tampered"


def test_same_issue_id_active_in_two_orgs(session_factory) -> None:
    """The one-active-run-per-issue uniqueness is per-org, so two tenants can
    each have an active run for the same upstream issue id."""
    acme_run = _intake(session_factory, org="acme")
    globex_run = _intake(session_factory, org="globex")  # same issue_id "i-1"
    assert acme_run != globex_run


# --------------------------------------------------------------------------- #
# Audit-trail integrity is preserved under tenancy (invariant: hash chain)
# --------------------------------------------------------------------------- #

def test_audit_chain_verifies_within_a_tenant(session_factory) -> None:
    run_id = _intake(session_factory, org="acme")
    with tenant_context("acme"), session_factory() as s:
        artifacts = s.query(FoundryArtifact).filter_by(run_id=run_id).all()
        events = (
            s.query(FoundryAuditEvent)
            .filter_by(run_id=run_id)
            .order_by(FoundryAuditEvent.sequence)
            .all()
        )
        result = verify_integrity(artifacts, events)
        assert result["verified"] is True


# --------------------------------------------------------------------------- #
# Tenant identity from the authenticated principal, not request input
# --------------------------------------------------------------------------- #

class _FakeVerifier:
    """A stand-in OIDC verifier: maps a known bearer token to verified claims."""

    def __init__(self, tokens: dict[str, dict]) -> None:
        self._tokens = tokens

    def verify(self, token: str) -> dict:
        try:
            return self._tokens[token]
        except KeyError:
            raise OidcAuthError("unknown token") from None


def test_resolve_request_org_reads_only_the_verified_token() -> None:
    verifier = _FakeVerifier({"tok-acme": {"sub": "u", "org": "acme"}})

    def scope(token: str | None) -> dict:
        headers = []
        if token is not None:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        return {"type": "http", "headers": headers}

    # Verified org claim wins.
    assert (
        resolve_request_org(scope("tok-acme"), verifier=verifier, org_claim="org")
        == "acme"
    )
    # Unknown / missing token -> default org (fail-closed, never an error).
    assert (
        resolve_request_org(scope("bogus"), verifier=verifier, org_claim="org")
        == DEFAULT_ORG_ID
    )
    assert (
        resolve_request_org(scope(None), verifier=verifier, org_claim="org")
        == DEFAULT_ORG_ID
    )
    # No org_claim configured -> single-tenant default even with a valid token.
    assert (
        resolve_request_org(scope("tok-acme"), verifier=verifier, org_claim=None)
        == DEFAULT_ORG_ID
    )


def test_resolve_request_org_reads_org_from_session_cookie() -> None:
    """The dashboard authenticates with a signed SSO session cookie (no bearer
    token). Its stamped, verified ``org`` scopes the request — the read-path twin
    of the bearer-token resolution (issue #34/#156)."""
    signer = SessionSigner("session-secret")

    def scope(*, cookie: str | None = None, token: str | None = None) -> dict:
        headers = []
        if token is not None:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        if cookie is not None:
            headers.append((b"cookie", f"{SESSION_COOKIE}={cookie}".encode()))
        return {"type": "http", "headers": headers}

    acme_cookie = signer.mint({"sub": "a@acme", "org": "acme"}, ttl_seconds=3600)

    # A valid session cookie's org wins when no bearer token is present.
    assert (
        resolve_request_org(
            scope(cookie=acme_cookie), verifier=None, org_claim="org",
            session_signer=signer,
        )
        == "acme"
    )
    # A cookie minted by a single-tenant login (no org) -> default org.
    plain = signer.mint({"sub": "x"}, ttl_seconds=3600)
    assert (
        resolve_request_org(
            scope(cookie=plain), verifier=None, org_claim="org", session_signer=signer
        )
        == DEFAULT_ORG_ID
    )
    # A tampered/forged cookie (wrong secret) -> default org, never an error.
    forged = SessionSigner("other-secret").mint({"org": "acme"}, ttl_seconds=3600)
    assert (
        resolve_request_org(
            scope(cookie=forged), verifier=None, org_claim="org", session_signer=signer
        )
        == DEFAULT_ORG_ID
    )
    # No org_claim configured -> single-tenant default even with an org cookie.
    assert (
        resolve_request_org(
            scope(cookie=acme_cookie), verifier=None, org_claim=None,
            session_signer=signer,
        )
        == DEFAULT_ORG_ID
    )
    # The bearer token wins over the cookie when both are present.
    verifier = _FakeVerifier({"tok-globex": {"sub": "u", "org": "globex"}})
    assert (
        resolve_request_org(
            scope(cookie=acme_cookie, token="tok-globex"), verifier=verifier,
            org_claim="org", session_signer=signer,
        )
        == "globex"
    )


def test_api_runs_listing_is_isolated_by_dashboard_session_cookie(session_factory) -> None:
    """End-to-end: the dashboard's cookie-authenticated GET /runs is scoped to the
    org stamped into the signed SSO session cookie, never request input (#34/#156)."""
    acme_run = _intake(session_factory, org="acme")
    _intake(session_factory, org="globex")

    signer = SessionSigner("session-secret")
    login = OidcLogin(
        client_id="dash",
        client_secret="s",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token",
        redirect_uri="https://foundry.example.com/cb",
        verifier=_FakeVerifier({}),
        signer=signer,
        org_claim="org",
    )
    app = create_app(
        webhook_secret="whsecret",
        session_factory=session_factory,
        orchestrator=FoundryOrchestrator(
            session_factory, provider=InMemoryFakeProvider()
        ),
        oidc_verifier=_FakeVerifier({}),
        oidc_org_claim="org",
        oidc_login=login,
    )
    client = TestClient(app)

    acme_cookie = signer.mint({"sub": "a@acme", "org": "acme"}, ttl_seconds=3600)
    resp = client.get("/runs", headers={"Cookie": f"{SESSION_COOKIE}={acme_cookie}"})
    assert resp.status_code == 200
    assert {r["id"] for r in resp.json()["runs"]} == {acme_run}

    # A single-tenant cookie (no org) sees the default org, neither tenant's runs.
    plain = signer.mint({"sub": "x"}, ttl_seconds=3600)
    resp = client.get("/runs", headers={"Cookie": f"{SESSION_COOKIE}={plain}"})
    assert {r["id"] for r in resp.json()["runs"]} == set()


def test_api_runs_listing_is_isolated_by_verified_token(session_factory) -> None:
    """End-to-end: two tenants hit GET /runs with their own OIDC token and each
    sees only their org's runs. The org comes from the verified token (the
    principal), never from request input (invariant #5)."""
    acme_run = _intake(session_factory, org="acme")
    globex_run = _intake(session_factory, org="globex")

    verifier = _FakeVerifier(
        {
            "tok-acme": {"sub": "a@acme", "org": "acme"},
            "tok-globex": {"sub": "b@globex", "org": "globex"},
        }
    )
    app = create_app(
        webhook_secret="whsecret",
        session_factory=session_factory,
        orchestrator=FoundryOrchestrator(
            session_factory, provider=InMemoryFakeProvider()
        ),
        oidc_verifier=verifier,
        oidc_org_claim="org",
    )
    client = TestClient(app)

    acme = client.get("/runs", headers={"Authorization": "Bearer tok-acme"})
    assert acme.status_code == 200
    acme_ids = {r["id"] for r in acme.json()["runs"]}
    assert acme_ids == {acme_run}

    globex = client.get("/runs", headers={"Authorization": "Bearer tok-globex"})
    globex_ids = {r["id"] for r in globex.json()["runs"]}
    assert globex_ids == {globex_run}

    # No token => the unscoped default org, which sees neither tenant's runs.
    anon = client.get("/runs")
    assert {r["id"] for r in anon.json()["runs"]} == set()
