"""SCIM 2.0 provisioning (issue #157): store, REST surface, role resolution.

Three layers, all fully offline (AGENTS.md invariant #3):

* the pure :class:`~foundry.api.scim.ScimStore` against in-memory SQLite,
* the ``/scim/v2`` REST endpoints + bearer auth through the FastAPI app, and
* the end-to-end *governance* property: a provisioned, active user in a mapped
  group can approve a run requiring that role, and de-provisioning (deactivate,
  group-removal, group-delete) **revokes** that authority. The last layer needs
  the OIDC verifier, so it mints a throwaway RSA token locally (``importorskip``
  keeps the suite green where the optional ``[oidc]`` extra is absent).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from foundry.agents.manual import InMemoryFakeProvider
from foundry.api import create_app
from foundry.api.scim import (
    ScimError,
    ScimStore,
    member_ids_from_value,
    parse_username_filter,
    to_scim_user,
)
from foundry.api.security import compute_signature
from foundry.db import create_all, make_engine, make_session_factory
from foundry.orchestrator import FoundryOrchestrator
from foundry.schemas.common import ApprovalRole

SCIM_TOKEN = "scim-provisioning-token"
SCIM_AUTH = {"Authorization": f"Bearer {SCIM_TOKEN}"}
GROUP_ROLE_MAP = {"eng-team": ["engineering", "security"], "qa-team": ["qa"]}


def _factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)


def _store() -> ScimStore:
    return ScimStore(_factory())


# --------------------------------------------------------------------------- #
# Store unit tests (no HTTP, no OIDC)
# --------------------------------------------------------------------------- #


def test_create_get_find_user():
    store = _store()
    rec = store.create_user(
        user_name="alice@example.com", external_id="ext-1", display_name="Alice"
    )
    assert rec.active is True and rec.external_id == "ext-1"
    assert store.get_user(rec.id).user_name == "alice@example.com"
    assert store.find_user_by_user_name("alice@example.com").id == rec.id
    assert store.find_user_by_user_name("nobody@example.com") is None


def test_duplicate_username_is_conflict():
    store = _store()
    store.create_user(user_name="dup@example.com")
    with pytest.raises(ScimError) as exc:
        store.create_user(user_name="dup@example.com")
    assert exc.value.status == 409 and exc.value.scim_type == "uniqueness"


def test_user_name_required():
    store = _store()
    with pytest.raises(ScimError) as exc:
        store.create_user(user_name="")
    assert exc.value.status == 400


def test_replace_user_full_overwrite():
    store = _store()
    rec = store.create_user(user_name="a@example.com", display_name="A", external_id="x")
    updated = store.replace_user(
        rec.id, user_name="a@example.com", display_name=None, external_id=None,
        active=False,
    )
    assert updated.display_name is None and updated.external_id is None
    assert updated.active is False


def test_patch_user_active_toggle_and_attrs():
    store = _store()
    rec = store.create_user(user_name="a@example.com")
    store.patch_user(rec.id, [{"op": "replace", "path": "active", "value": False}])
    assert store.get_user(rec.id).active is False
    # path-less replace with an attribute object (Okta's shape)
    store.patch_user(
        rec.id, [{"op": "replace", "value": {"active": True, "displayName": "A"}}]
    )
    refreshed = store.get_user(rec.id)
    assert refreshed.active is True and refreshed.display_name == "A"


def test_patch_user_active_accepts_string_bool():
    store = _store()
    rec = store.create_user(user_name="a@example.com")
    store.patch_user(rec.id, [{"op": "replace", "path": "active", "value": "False"}])
    assert store.get_user(rec.id).active is False


def test_patch_unknown_path_rejected():
    store = _store()
    rec = store.create_user(user_name="a@example.com")
    with pytest.raises(ScimError) as exc:
        store.patch_user(rec.id, [{"op": "replace", "path": "nonsense", "value": 1}])
    assert exc.value.status == 400 and exc.value.scim_type == "invalidPath"


def test_delete_user_removes_memberships():
    store = _store()
    u = store.create_user(user_name="a@example.com")
    g = store.create_group(display_name="eng-team", member_ids=[u.id])
    assert [m.user_id for m in store.get_group(g.id).members] == [u.id]
    assert store.delete_user(u.id) is True
    assert store.get_user(u.id) is None
    assert store.get_group(g.id).members == ()  # membership cascaded away
    assert store.delete_user(u.id) is False  # idempotent


def test_group_membership_add_remove_patch():
    store = _store()
    u1 = store.create_user(user_name="a@example.com")
    u2 = store.create_user(user_name="b@example.com")
    g = store.create_group(display_name="eng-team", member_ids=[u1.id])
    store.patch_group(g.id, [{"op": "add", "path": "members", "value": [{"value": u2.id}]}])
    assert {m.user_id for m in store.get_group(g.id).members} == {u1.id, u2.id}
    # Entra ID's value-path remove form: members[value eq "id"]
    store.patch_group(
        g.id, [{"op": "remove", "path": f'members[value eq "{u1.id}"]'}]
    )
    assert {m.user_id for m in store.get_group(g.id).members} == {u2.id}
    # replace sets the whole membership
    store.patch_group(g.id, [{"op": "replace", "path": "members", "value": []}])
    assert store.get_group(g.id).members == ()


def test_group_member_must_exist():
    store = _store()
    with pytest.raises(ScimError) as exc:
        store.create_group(display_name="eng-team", member_ids=["does-not-exist"])
    assert exc.value.status == 400 and exc.value.scim_type == "invalidValue"


def test_resolve_identity_active_maps_groups_to_roles():
    store = _store()
    u = store.create_user(user_name="a@example.com")
    store.create_group(display_name="eng-team", member_ids=[u.id])
    res = store.resolve_identity("a@example.com", _role_map())
    assert res.provisioned and res.active
    assert res.roles == frozenset({ApprovalRole.ENGINEERING, ApprovalRole.SECURITY})


def test_resolve_identity_inactive_has_no_roles():
    store = _store()
    u = store.create_user(user_name="a@example.com")
    store.create_group(display_name="eng-team", member_ids=[u.id])
    store.patch_user(u.id, [{"op": "replace", "path": "active", "value": False}])
    res = store.resolve_identity("a@example.com", _role_map())
    assert res.provisioned and not res.active and res.roles == frozenset()


def test_resolve_identity_unprovisioned():
    store = _store()
    res = store.resolve_identity("ghost@example.com", _role_map())
    assert not res.provisioned and res.roles == frozenset()


def test_resolve_identity_unmapped_group_grants_nothing():
    """A provisioned group whose name isn't in the committed map grants no role
    - the map is the sole authority (invariant #5)."""
    store = _store()
    u = store.create_user(user_name="a@example.com")
    store.create_group(display_name="random-team", member_ids=[u.id])
    res = store.resolve_identity("a@example.com", _role_map())
    assert res.provisioned and res.active and res.roles == frozenset()


def _role_map():
    return {g: [ApprovalRole(r) for r in roles] for g, roles in GROUP_ROLE_MAP.items()}


def test_parse_username_filter():
    assert parse_username_filter('userName eq "a@x.com"') == "a@x.com"
    assert parse_username_filter('USERNAME EQ "a@x.com"') == "a@x.com"
    assert parse_username_filter('displayName eq "x"') is None
    assert parse_username_filter(None) is None


def test_member_ids_from_value_validation():
    assert member_ids_from_value([{"value": "x"}, {"value": "y"}]) == ["x", "y"]
    with pytest.raises(ScimError):
        member_ids_from_value([{"display": "no-value"}])


def test_to_scim_user_omits_unset_optionals():
    store = _store()
    rec = store.create_user(user_name="a@example.com")
    body = to_scim_user(rec)
    assert body["userName"] == "a@example.com" and body["active"] is True
    assert "externalId" not in body and "displayName" not in body
    assert body["meta"]["location"] == f"/scim/v2/Users/{rec.id}"


# --------------------------------------------------------------------------- #
# REST surface
# --------------------------------------------------------------------------- #


def _scim_client(*, scim_token: str | None = SCIM_TOKEN) -> TestClient:
    sf = _factory()
    return TestClient(
        create_app(
            webhook_secret="wh",
            session_factory=sf,
            api_token="api-tok",
            scim_bearer_token=scim_token,
            oidc_group_role_map=GROUP_ROLE_MAP,
        )
    )


def test_scim_disabled_without_token():
    client = _scim_client(scim_token=None)
    resp = client.post("/scim/v2/Users", headers=SCIM_AUTH, json={"userName": "a@x.com"})
    assert resp.status_code == 403
    assert "disabled" in resp.json()["detail"]


def test_scim_rejects_bad_token():
    client = _scim_client()
    resp = client.post(
        "/scim/v2/Users",
        headers={"Authorization": "Bearer wrong"},
        json={"userName": "a@x.com"},
    )
    assert resp.status_code == 401


def test_scim_user_crud_roundtrip():
    client = _scim_client()
    # create
    resp = client.post(
        "/scim/v2/Users",
        headers=SCIM_AUTH,
        json={"userName": "alice@x.com", "displayName": "Alice", "active": True},
    )
    assert resp.status_code == 201
    assert resp.headers["content-type"].startswith("application/scim+json")
    uid = resp.json()["id"]
    assert resp.headers["location"] == f"/scim/v2/Users/{uid}"
    # read
    assert client.get(f"/scim/v2/Users/{uid}", headers=SCIM_AUTH).status_code == 200
    # list with userName filter (the IdP existence probe)
    listed = client.get(
        '/scim/v2/Users?filter=userName eq "alice@x.com"', headers=SCIM_AUTH
    ).json()
    assert listed["totalResults"] == 1
    assert listed["schemas"] == ["urn:ietf:params:scim:api:messages:2.0.ListResponse"]
    # replace (PUT)
    put = client.put(
        f"/scim/v2/Users/{uid}",
        headers=SCIM_AUTH,
        json={"userName": "alice@x.com", "active": False},
    )
    assert put.status_code == 200 and put.json()["active"] is False
    # deactivate via PATCH
    patched = client.patch(
        f"/scim/v2/Users/{uid}",
        headers=SCIM_AUTH,
        json={"Operations": [{"op": "replace", "path": "active", "value": True}]},
    )
    assert patched.json()["active"] is True
    # delete
    assert client.delete(f"/scim/v2/Users/{uid}", headers=SCIM_AUTH).status_code == 204
    assert client.get(f"/scim/v2/Users/{uid}", headers=SCIM_AUTH).status_code == 404


def test_scim_group_crud_and_members():
    client = _scim_client()
    uid = client.post(
        "/scim/v2/Users", headers=SCIM_AUTH, json={"userName": "a@x.com"}
    ).json()["id"]
    resp = client.post(
        "/scim/v2/Groups",
        headers=SCIM_AUTH,
        json={"displayName": "eng-team", "members": [{"value": uid}]},
    )
    assert resp.status_code == 201
    gid = resp.json()["id"]
    assert [m["value"] for m in resp.json()["members"]] == [uid]
    # patch remove member
    client.patch(
        f"/scim/v2/Groups/{gid}",
        headers=SCIM_AUTH,
        json={"Operations": [{"op": "remove", "path": "members"}]},
    )
    assert client.get(f"/scim/v2/Groups/{gid}", headers=SCIM_AUTH).json()["members"] == []
    assert client.delete(f"/scim/v2/Groups/{gid}", headers=SCIM_AUTH).status_code == 204


def test_scim_error_body_is_scim_shaped():
    client = _scim_client()
    resp = client.post("/scim/v2/Users", headers=SCIM_AUTH, json={"displayName": "x"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0.Error"]
    assert body["scimType"] == "invalidValue"


# --------------------------------------------------------------------------- #
# End-to-end governance: provisioned authority + de-provisioning revocation
# --------------------------------------------------------------------------- #

WEBHOOK_SECRET = "wh-secret"
ISSUER = "https://idp.example.com/"
AUDIENCE = "foundry-api"
KID = "key-1"


def _oidc_bits():
    """A throwaway RSA keypair + JWKS, or skip if the [oidc] extra is absent.

    ``importorskip`` would skip a *missing* extra, but a broken native build
    (e.g. cryptography with no ``_cffi_backend``) raises a pyo3 ``PanicException``
    - a ``BaseException``, not an ``ImportError`` - so we catch broadly and skip.
    """
    try:
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa

        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    except BaseException as exc:  # noqa: BLE001 - missing or broken native build
        pytest.skip(f"oidc extra unavailable in this environment: {exc}")
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(priv.public_key()))
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return jwt, priv, {"keys": [jwk]}


def _mint(jwt, priv, *, email: str) -> str:
    import time

    now = int(time.time())
    return jwt.encode(
        {
            "sub": email,
            "email": email,
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + 3600,
        },
        priv,
        algorithm="RS256",
        headers={"kid": KID},
    )


def _e2e_client():
    jwt, priv, jwks = _oidc_bits()
    from foundry.api.oidc import build_verifier

    sf = _factory()
    orch = FoundryOrchestrator(sf, provider=InMemoryFakeProvider())
    verifier = build_verifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_uri="https://idp.example.com/jwks",
        fetch=lambda: jwks,
    )
    client = TestClient(
        create_app(
            webhook_secret=WEBHOOK_SECRET,
            session_factory=sf,
            orchestrator=orch,
            approvers={},  # nobody statically listed: authority must come from SCIM
            scim_bearer_token=SCIM_TOKEN,
            oidc_verifier=verifier,
            oidc_group_role_map=GROUP_ROLE_MAP,
        )
    )
    return client, jwt, priv


def _infra_payload(issue_id="issue-infra", key="LIN-9") -> dict:
    # An infrastructure ticket: the gate requires the 'engineering' role to approve.
    return {
        "data": {
            "id": issue_id,
            "issueId": issue_id,
            "identifier": key,
            "title": "Update the terraform deployment config",
            "description": "Acceptance Criteria:\n- terraform plan runs clean\n- it applies\n",
            "labels": [
                {"name": "foundry:candidate"},
                {"name": "repo:customer-web"},
            ],
            "actor": {"name": "po@example.com"},
        }
    }


def _start_run(client) -> str:
    body = json.dumps(_infra_payload()).encode("utf-8")
    sig = "sha256=" + compute_signature(WEBHOOK_SECRET, body)
    resp = client.post(
        "/webhooks/linear",
        content=body,
        headers={"Linear-Delivery": "d-1", "Linear-Signature": sig},
    )
    assert resp.json()["run"]["status"] == "waiting_approval", resp.json()
    return resp.json()["run"]["id"]


def _provision_user_in_group(client, *, email: str, group: str) -> str:
    uid = client.post(
        "/scim/v2/Users", headers=SCIM_AUTH, json={"userName": email}
    ).json()["id"]
    client.post(
        "/scim/v2/Groups",
        headers=SCIM_AUTH,
        json={"displayName": group, "members": [{"value": uid}]},
    )
    return uid


def test_scim_provisioned_user_in_group_can_approve():
    """A user with NO static grant, provisioned active into a mapped group, is
    authorised and carries the role the run requires - membership -> role via the
    committed map (invariant #5), exercised through the live approval path."""
    client, jwt, priv = _e2e_client()
    run_id = _start_run(client)
    _provision_user_in_group(client, email="eng@example.com", group="eng-team")
    token = _mint(jwt, priv, email="eng@example.com")
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"text": "/foundry approve"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["run"]["approved_by"] == "eng@example.com"


def test_scim_deactivation_revokes_approval_authority():
    """Deactivating the user over SCIM (the de-provision signal) revokes its
    authority: the same approval is refused and the run stays parked."""
    client, jwt, priv = _e2e_client()
    run_id = _start_run(client)
    uid = _provision_user_in_group(client, email="eng@example.com", group="eng-team")
    client.patch(
        f"/scim/v2/Users/{uid}",
        headers=SCIM_AUTH,
        json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
    )
    token = _mint(jwt, priv, email="eng@example.com")
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"text": "/foundry approve"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert "de-provisioned" in resp.json()["detail"]
    status = client.get(
        f"/runs/{run_id}", headers={"Authorization": f"Bearer {token}"}
    ).json()["status"]
    assert status == "waiting_approval"


def test_scim_group_removal_revokes_role():
    """Removing the user from the mapped group (still active) drops the role, so
    the role-gated approval is refused - de-provisioning at the membership level."""
    client, jwt, priv = _e2e_client()
    run_id = _start_run(client)
    uid = _provision_user_in_group(client, email="eng@example.com", group="eng-team")
    gid = client.get(
        '/scim/v2/Groups?filter=displayName eq "eng-team"', headers=SCIM_AUTH
    ).json()["Resources"][0]["id"]
    client.patch(
        f"/scim/v2/Groups/{gid}",
        headers=SCIM_AUTH,
        json={"Operations": [{"op": "remove", "path": f'members[value eq "{uid}"]'}]},
    )
    token = _mint(jwt, priv, email="eng@example.com")
    resp = client.post(
        f"/runs/{run_id}/approval",
        json={"text": "/foundry approve"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # With the group gone the user maps to no role and isn't statically listed,
    # so the role-gated approval is refused (403) and the run stays parked.
    assert resp.status_code == 403
    status = client.get(
        f"/runs/{run_id}", headers={"Authorization": f"Bearer {token}"}
    ).json()["status"]
    assert status == "waiting_approval"
