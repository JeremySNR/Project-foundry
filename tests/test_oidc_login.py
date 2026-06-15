"""Browser-side OIDC login / SSO for the dashboard (issue #34).

Fully offline (AGENTS.md invariant #3): a throwaway RSA key is minted locally and
its public half served through a fake JWKS ``fetch``; the IdP token endpoint is a
fake ``exchange`` callable that returns a locally-signed id_token - no network,
no real IdP. ``importorskip`` keeps the suite green where the optional ``[oidc]``
extra (pyjwt[crypto]) is absent.
"""

from __future__ import annotations

import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

jwt = pytest.importorskip("jwt")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from foundry.agents.manual import InMemoryFakeProvider  # noqa: E402
from foundry.api import create_app  # noqa: E402
from foundry.api.oidc import OidcAuthError, build_verifier  # noqa: E402
from foundry.api.oidc_login import (  # noqa: E402
    LOGIN_STATE_COOKIE,
    SESSION_COOKIE,
    OidcLogin,
    OidcLoginError,
    pkce_challenge,
)
from foundry.api.sessions import SessionSigner  # noqa: E402
from foundry.config import Settings  # noqa: E402
from foundry.db import create_all, make_engine, make_session_factory  # noqa: E402
from foundry.orchestrator import FoundryOrchestrator  # noqa: E402

ISSUER = "https://idp.example.com/"
AUDIENCE = "foundry-api"
CLIENT_ID = "foundry-dashboard"
AUTH_EP = "https://idp.example.com/authorize"
TOKEN_EP = "https://idp.example.com/token"
REDIRECT = "https://foundry.example.com/dashboard/auth/callback"
END_SESSION_EP = "https://idp.example.com/logout"
POST_LOGOUT = "https://foundry.example.com/dashboard"
KID = "key-1"


def _keypair(kid: str = KID):
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(priv.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return priv, {"keys": [jwk]}


def _id_token(priv, *, nonce, sub="alice@example.com", email="alice@example.com",
              issuer=ISSUER, audience=CLIENT_ID, key=None):
    now = int(time.time())
    claims = {"sub": sub, "iss": issuer, "aud": audience, "iat": now, "exp": now + 3600}
    if nonce is not None:
        claims["nonce"] = nonce
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, key or priv, algorithm="RS256", headers={"kid": KID})


def _make_login(priv, jwks, *, exchange=None, signer=None, **over) -> tuple[OidcLogin, dict]:
    """An :class:`OidcLogin` whose token exchange is a local fake.

    Returns ``(login, holder)``; the default exchanger mints an id_token using
    ``holder['nonce']`` / ``holder['sub']`` so a test can set them after reading
    the nonce from the authorize redirect (just as a real IdP would echo it).
    """
    holder: dict = {"sub": "alice@example.com"}
    if exchange is None:
        def exchange(form):  # noqa: ANN001
            return {"id_token": _id_token(priv, nonce=holder.get("nonce"), sub=holder["sub"], email=holder["sub"])}
    verifier = build_verifier(
        issuer=ISSUER, audience=CLIENT_ID, jwks_uri="x", fetch=lambda: jwks
    )
    login = OidcLogin(
        client_id=CLIENT_ID,
        client_secret="client-secret",
        authorization_endpoint=AUTH_EP,
        token_endpoint=TOKEN_EP,
        redirect_uri=REDIRECT,
        verifier=verifier,
        signer=signer or SessionSigner("session-secret"),
        cookie_secure=False,
        exchange=exchange,
        **over,
    )
    return login, holder


# --------------------------------------------------------------------------- #
# SessionSigner unit tests
# --------------------------------------------------------------------------- #


def test_session_signer_round_trip():
    s = SessionSigner("a-secret")
    token = s.mint({"sub": "alice@example.com"}, ttl_seconds=60)
    assert s.read(token)["sub"] == "alice@example.com"


def test_session_signer_rejects_wrong_secret():
    token = SessionSigner("secret-one").mint({"sub": "a"}, ttl_seconds=60)
    assert SessionSigner("secret-two").read(token) is None


def test_session_signer_rejects_tampered_payload():
    s = SessionSigner("k")
    token = s.mint({"sub": "alice"}, ttl_seconds=60)
    _, _, sig = token.partition(".")
    # Re-sign? No - a different payload with the original signature must fail.
    forged_payload = SessionSigner("k").mint({"sub": "mallory"}, ttl_seconds=60)
    forged = forged_payload.partition(".")[0] + "." + sig
    assert s.read(forged) is None


def test_session_signer_rejects_garbage_and_none():
    s = SessionSigner("k")
    assert s.read(None) is None
    assert s.read("not-a-token") is None
    assert s.read("a.b.c") is None


def test_session_signer_enforces_expiry():
    clock = {"t": 1000.0}
    s = SessionSigner("k", clock=lambda: clock["t"])
    token = s.mint({"sub": "a"}, ttl_seconds=10)
    assert s.read(token) is not None
    clock["t"] += 11
    assert s.read(token) is None


def test_session_signer_requires_secret():
    with pytest.raises(ValueError):
        SessionSigner("")


# --------------------------------------------------------------------------- #
# PKCE helper
# --------------------------------------------------------------------------- #


def test_pkce_challenge_is_s256_base64url_no_padding():
    import base64

    verifier = "the-code-verifier-value"
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert pkce_challenge(verifier) == expected
    assert "=" not in pkce_challenge(verifier)


# --------------------------------------------------------------------------- #
# OidcLogin.begin / complete unit tests
# --------------------------------------------------------------------------- #


def test_begin_builds_authorize_url_and_matching_state_cookie():
    priv, jwks = _keypair()
    login, _ = _make_login(priv, jwks)
    url, cookie = login.begin()
    assert url.startswith(AUTH_EP + "?")
    q = parse_qs(urlparse(url).query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == [CLIENT_ID]
    assert q["redirect_uri"] == [REDIRECT]
    assert q["code_challenge_method"] == ["S256"]
    assert q["scope"] == ["openid email"]
    # The signed cookie carries state/nonce/verifier; the challenge matches.
    stash = login.signer.read(cookie)
    assert stash["s"] == q["state"][0]
    assert stash["n"] == q["nonce"][0]
    assert pkce_challenge(stash["v"]) == q["code_challenge"][0]


def _begin_and_arm(login, holder):
    url, cookie = login.begin()
    q = parse_qs(urlparse(url).query)
    holder["nonce"] = q["nonce"][0]
    return q["state"][0], cookie


def test_complete_happy_path_returns_session_cookie():
    priv, jwks = _keypair()
    login, holder = _make_login(priv, jwks)
    state, cookie = _begin_and_arm(login, holder)
    session = login.complete(code="auth-code", state=state, login_cookie=cookie)
    assert login.signer.read(session)["sub"] == "alice@example.com"


def test_complete_rejects_state_mismatch():
    priv, jwks = _keypair()
    login, holder = _make_login(priv, jwks)
    _, cookie = _begin_and_arm(login, holder)
    with pytest.raises(OidcLoginError, match="state mismatch"):
        login.complete(code="c", state="not-the-state", login_cookie=cookie)


def test_complete_rejects_missing_login_cookie():
    priv, jwks = _keypair()
    login, holder = _make_login(priv, jwks)
    state, _ = _begin_and_arm(login, holder)
    with pytest.raises(OidcLoginError, match="expired or missing"):
        login.complete(code="c", state=state, login_cookie=None)


def test_complete_rejects_expired_login_cookie():
    priv, jwks = _keypair()
    clock = {"t": 1000.0}
    login, holder = _make_login(
        priv, jwks, signer=SessionSigner("session-secret", clock=lambda: clock["t"])
    )
    state, cookie = _begin_and_arm(login, holder)
    clock["t"] += login.login_ttl_seconds + 1
    with pytest.raises(OidcLoginError, match="expired or missing"):
        login.complete(code="c", state=state, login_cookie=cookie)


def test_complete_rejects_missing_code():
    priv, jwks = _keypair()
    login, holder = _make_login(priv, jwks)
    state, cookie = _begin_and_arm(login, holder)
    with pytest.raises(OidcLoginError, match="missing 'code'"):
        login.complete(code=None, state=state, login_cookie=cookie)


def test_complete_rejects_missing_id_token():
    priv, jwks = _keypair()
    login, holder = _make_login(priv, jwks, exchange=lambda form: {"access_token": "x"})
    state, cookie = _begin_and_arm(login, holder)
    with pytest.raises(OidcLoginError, match="id_token"):
        login.complete(code="c", state=state, login_cookie=cookie)


def test_complete_rejects_nonce_mismatch():
    priv, jwks = _keypair()
    login, holder = _make_login(priv, jwks)
    state, cookie = _begin_and_arm(login, holder)
    holder["nonce"] = "a-different-nonce"  # token won't carry the begin() nonce
    with pytest.raises(OidcLoginError, match="nonce"):
        login.complete(code="c", state=state, login_cookie=cookie)


def test_complete_propagates_token_verification_failure():
    priv, jwks = _keypair()
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    holder: dict = {"sub": "alice@example.com"}

    def exchange(form):
        return {"id_token": _id_token(priv, nonce=holder.get("nonce"), key=attacker)}

    login, _ = _make_login(priv, jwks, exchange=exchange)
    url, cookie = login.begin()
    q = parse_qs(urlparse(url).query)
    holder["nonce"] = q["nonce"][0]
    with pytest.raises(OidcAuthError):
        login.complete(code="c", state=q["state"][0], login_cookie=cookie)


def test_complete_rejects_token_without_subject():
    priv, jwks = _keypair()
    holder: dict = {}

    def exchange(form):
        return {"id_token": _id_token(priv, nonce=holder.get("nonce"), sub="", email=None)}

    login, _ = _make_login(priv, jwks, exchange=exchange)
    url, cookie = login.begin()
    q = parse_qs(urlparse(url).query)
    holder["nonce"] = q["nonce"][0]
    with pytest.raises(OidcLoginError, match="subject"):
        login.complete(code="c", state=q["state"][0], login_cookie=cookie)


# --------------------------------------------------------------------------- #
# RP-Initiated Logout (federated logout) - end_session_url unit tests
# --------------------------------------------------------------------------- #


def test_end_session_url_none_when_not_configured():
    priv, jwks = _keypair()
    login, _ = _make_login(priv, jwks)
    assert login.end_session_url() is None


def test_end_session_url_carries_client_id_in_lieu_of_id_token_hint():
    priv, jwks = _keypair()
    login, _ = _make_login(priv, jwks, end_session_endpoint=END_SESSION_EP)
    url = login.end_session_url()
    assert url.startswith(END_SESSION_EP + "?")
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == [CLIENT_ID]
    # No id_token is stored client-side, so none is sent (client_id identifies us).
    assert "id_token_hint" not in q
    assert "post_logout_redirect_uri" not in q


def test_end_session_url_includes_post_logout_redirect_uri():
    priv, jwks = _keypair()
    login, _ = _make_login(
        priv,
        jwks,
        end_session_endpoint=END_SESSION_EP,
        post_logout_redirect_uri=POST_LOGOUT,
    )
    q = parse_qs(urlparse(login.end_session_url()).query)
    assert q["client_id"] == [CLIENT_ID]
    assert q["post_logout_redirect_uri"] == [POST_LOGOUT]


def test_end_session_url_appends_to_existing_query():
    priv, jwks = _keypair()
    login, _ = _make_login(
        priv, jwks, end_session_endpoint=END_SESSION_EP + "?ui_locales=en"
    )
    url = login.end_session_url()
    assert "?ui_locales=en&" in url
    q = parse_qs(urlparse(url).query)
    assert q["ui_locales"] == ["en"]
    assert q["client_id"] == [CLIENT_ID]


# --------------------------------------------------------------------------- #
# API integration (TestClient drives the real routes + cookie jar)
# --------------------------------------------------------------------------- #


def _client(*, with_login=True, api_token=None, login_kwargs=None):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    sf = make_session_factory(engine)
    orch = FoundryOrchestrator(sf, provider=InMemoryFakeProvider())
    priv, jwks = _keypair()
    bearer_verifier = build_verifier(
        issuer=ISSUER, audience=AUDIENCE, jwks_uri="x", fetch=lambda: jwks
    )
    login = None
    holder: dict = {"sub": "alice@example.com"}
    if with_login:
        login, holder = _make_login(priv, jwks, **(login_kwargs or {}))
    client = TestClient(
        create_app(
            webhook_secret="s",
            session_factory=sf,
            orchestrator=orch,
            api_token=api_token,
            oidc_verifier=bearer_verifier,
            oidc_login=login,
        )
    )
    return client, holder


def _drive_login(client, holder) -> None:
    """Run the full browser flow so the client ends up with a session cookie."""
    resp = client.get("/dashboard/login", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(AUTH_EP)
    q = parse_qs(urlparse(location).query)
    holder["nonce"] = q["nonce"][0]
    cb = client.get(
        "/dashboard/auth/callback",
        params={"code": "auth-code", "state": q["state"][0]},
        follow_redirects=False,
    )
    assert cb.status_code == 302, cb.text
    assert cb.headers["location"] == "/dashboard"
    assert SESSION_COOKIE in client.cookies


def test_login_route_redirects_and_sets_state_cookie():
    client, _ = _client()
    resp = client.get("/dashboard/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith(AUTH_EP)
    assert LOGIN_STATE_COOKIE in resp.cookies


def test_full_login_then_session_cookie_authenticates_reads():
    client, holder = _client()
    # Before login a read is unauthorised (no token, no cookie).
    assert client.get("/metrics/delivery").status_code == 401
    _drive_login(client, holder)
    # The session cookie alone now authenticates the dashboard's read calls.
    assert client.get("/metrics/delivery").status_code == 200
    assert client.get("/runs").status_code == 200


def test_session_cookie_does_not_authorise_approval():
    """A browser session cookie is auto-sent, so it must NOT drive an approval
    (CSRF safety). The approval endpoint still requires a bearer/webhook."""
    client, holder = _client()
    _drive_login(client, holder)
    resp = client.post(
        "/runs/any-run/approval",
        json={"text": "/foundry approve", "user": "alice@example.com"},
    )
    assert resp.status_code == 401


def test_logout_clears_session_cookie():
    client, holder = _client()
    _drive_login(client, holder)
    assert client.get("/metrics/delivery").status_code == 200
    resp = client.get("/dashboard/logout", follow_redirects=False)
    assert resp.status_code == 302
    # Default (no end_session_endpoint): returns to the local dashboard.
    assert resp.headers["location"] == "/dashboard"
    assert client.get("/metrics/delivery").status_code == 401


def test_logout_redirects_to_idp_when_federated_logout_configured():
    client, holder = _client(
        login_kwargs={
            "end_session_endpoint": END_SESSION_EP,
            "post_logout_redirect_uri": POST_LOGOUT,
        }
    )
    _drive_login(client, holder)
    assert client.get("/metrics/delivery").status_code == 200
    resp = client.get("/dashboard/logout", follow_redirects=False)
    assert resp.status_code == 302
    # Redirected on to the IdP to terminate the SSO session, not just /dashboard.
    location = resp.headers["location"]
    assert location.startswith(END_SESSION_EP)
    q = parse_qs(urlparse(location).query)
    assert q["client_id"] == [CLIENT_ID]
    assert q["post_logout_redirect_uri"] == [POST_LOGOUT]
    # The local session cookie is cleared regardless of the federated redirect.
    assert client.get("/metrics/delivery").status_code == 401


def test_callback_rejects_forged_state():
    client, _ = _client()
    client.get("/dashboard/login", follow_redirects=False)  # arm the state cookie
    resp = client.get(
        "/dashboard/auth/callback",
        params={"code": "c", "state": "forged"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_callback_idp_error_returns_to_dashboard():
    client, _ = _client()
    client.get("/dashboard/login", follow_redirects=False)
    resp = client.get(
        "/dashboard/auth/callback",
        params={"error": "access_denied"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard"


def test_login_routes_disabled_when_not_configured():
    client, _ = _client(with_login=False)
    assert client.get("/dashboard/login", follow_redirects=False).status_code == 403
    assert (
        client.get(
            "/dashboard/auth/callback", params={"code": "c", "state": "s"},
            follow_redirects=False,
        ).status_code
        == 403
    )


def test_dashboard_injects_login_and_session_flags():
    client, holder = _client()
    page = client.get("/dashboard").text
    assert "window.__FOUNDRY_OIDC_LOGIN__ = true;" in page
    assert "window.__FOUNDRY_SESSION__ = false;" in page
    _drive_login(client, holder)
    page2 = client.get("/dashboard").text
    assert "window.__FOUNDRY_SESSION__ = true;" in page2


def test_dashboard_flags_false_without_login_config():
    client, _ = _client(with_login=False, api_token="static-tok")
    page = client.get("/dashboard").text
    assert "window.__FOUNDRY_OIDC_LOGIN__ = false;" in page
    assert "window.__FOUNDRY_SESSION__ = false;" in page


def test_static_token_still_authenticates_reads_alongside_login():
    client, _ = _client(api_token="static-tok")
    resp = client.get(
        "/metrics/delivery", headers={"Authorization": "Bearer static-tok"}
    )
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Config (fail-closed, all-or-nothing browser login)
# --------------------------------------------------------------------------- #

_BEARER_ENV = {
    "FOUNDRY_OIDC_ISSUER": ISSUER,
    "FOUNDRY_OIDC_AUDIENCE": AUDIENCE,
    "FOUNDRY_OIDC_JWKS_URI": "https://idp.example.com/jwks",
}
_LOGIN_ENV = {
    "FOUNDRY_OIDC_CLIENT_ID": CLIENT_ID,
    "FOUNDRY_OIDC_AUTHORIZATION_ENDPOINT": AUTH_EP,
    "FOUNDRY_OIDC_TOKEN_ENDPOINT": TOKEN_EP,
    "FOUNDRY_OIDC_REDIRECT_URI": REDIRECT,
}


def test_login_configured_property_true_when_all_set():
    s = Settings.load(env={**_BEARER_ENV, **_LOGIN_ENV})
    assert s.oidc_login_configured is True


def test_partial_login_config_rejected():
    with pytest.raises(ValueError, match="browser login requires"):
        Settings.load(env={**_BEARER_ENV, "FOUNDRY_OIDC_CLIENT_ID": CLIENT_ID})


def test_login_without_bearer_config_rejected():
    with pytest.raises(ValueError, match="bearer config"):
        Settings.load(env=dict(_LOGIN_ENV))


def test_default_has_no_login():
    assert Settings.load().oidc_login_configured is False


def test_login_config_from_yaml(tmp_path):
    cfg = tmp_path / "foundry.yaml"
    cfg.write_text(
        "auth:\n"
        "  oidc:\n"
        f"    issuer: {ISSUER}\n"
        f"    audience: {AUDIENCE}\n"
        "    jwks_uri: https://idp.example.com/jwks\n"
        f"    client_id: {CLIENT_ID}\n"
        f"    authorization_endpoint: {AUTH_EP}\n"
        f"    token_endpoint: {TOKEN_EP}\n"
        f"    redirect_uri: {REDIRECT}\n"
        "    scopes: [openid, email, profile]\n"
        "    session_ttl_seconds: 3600\n"
        "    cookie_secure: false\n"
    )
    s = Settings.load(cfg)
    assert s.oidc_login_configured is True
    assert s.oidc_scopes == ("openid", "email", "profile")
    assert s.oidc_session_ttl_seconds == 3600
    assert s.oidc_cookie_secure is False


def test_federated_logout_config_from_env():
    s = Settings.load(
        env={
            **_BEARER_ENV,
            **_LOGIN_ENV,
            "FOUNDRY_OIDC_END_SESSION_ENDPOINT": END_SESSION_EP,
            "FOUNDRY_OIDC_POST_LOGOUT_REDIRECT_URI": POST_LOGOUT,
        }
    )
    assert s.oidc_end_session_endpoint == END_SESSION_EP
    assert s.oidc_post_logout_redirect_uri == POST_LOGOUT


def test_federated_logout_config_from_yaml(tmp_path):
    cfg = tmp_path / "foundry.yaml"
    cfg.write_text(
        "auth:\n"
        "  oidc:\n"
        f"    issuer: {ISSUER}\n"
        f"    audience: {AUDIENCE}\n"
        "    jwks_uri: https://idp.example.com/jwks\n"
        f"    client_id: {CLIENT_ID}\n"
        f"    authorization_endpoint: {AUTH_EP}\n"
        f"    token_endpoint: {TOKEN_EP}\n"
        f"    redirect_uri: {REDIRECT}\n"
        f"    end_session_endpoint: {END_SESSION_EP}\n"
        f"    post_logout_redirect_uri: {POST_LOGOUT}\n"
    )
    s = Settings.load(cfg)
    assert s.oidc_end_session_endpoint == END_SESSION_EP
    assert s.oidc_post_logout_redirect_uri == POST_LOGOUT


def test_end_session_without_login_config_rejected():
    with pytest.raises(ValueError, match="RP-initiated logout"):
        Settings.load(
            env={**_BEARER_ENV, "FOUNDRY_OIDC_END_SESSION_ENDPOINT": END_SESSION_EP}
        )


def test_post_logout_without_end_session_rejected():
    with pytest.raises(ValueError, match="post_logout_redirect_uri"):
        Settings.load(
            env={
                **_BEARER_ENV,
                **_LOGIN_ENV,
                "FOUNDRY_OIDC_POST_LOGOUT_REDIRECT_URI": POST_LOGOUT,
            }
        )


def test_default_has_no_federated_logout():
    s = Settings.load()
    assert s.oidc_end_session_endpoint is None
    assert s.oidc_post_logout_redirect_uri is None


def test_app_from_settings_fails_loud_without_secrets(tmp_path):
    from foundry.api.app import app_from_settings
    from foundry.api.oidc import OidcConfigError

    env = {
        **_BEARER_ENV,
        **_LOGIN_ENV,
        "FOUNDRY_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "FOUNDRY_LINEAR_WEBHOOK_SECRET": "wh",
        "FOUNDRY_OIDC_CLIENT_SECRET": "cs",
        # FOUNDRY_SESSION_SECRET deliberately absent.
    }
    s = Settings.load(env=env)
    with pytest.raises(OidcConfigError, match="session signing secret"):
        app_from_settings(s)
