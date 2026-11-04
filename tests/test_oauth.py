"""The login the phone uses.

These tests walk the flow a connector actually walks: discover, register, ask, be
approved in Telegram, exchange the code, call MCP. Plus the four ways it must fail —
a code spent twice, a verifier that does not match, a redirect that was never
registered, a token asked for someone else's server.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import oauth
from app.core.security import AuthError, authenticate
from app.db import SessionLocal
from app.main import app
from app.models import Principal
from app.models.base import utcnow
from app.models.oauth import AuthRequestStatus, OAuthAuthRequest

ISSUER = "https://regimen.example"
RESOURCE = f"{ISSUER}/mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "a" * 64


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


@pytest.fixture
def published(monkeypatch):
    """The service as it is on the server: a public address, therefore an issuer."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "public_base_url", ISSUER, raising=False)
    return settings


@pytest.fixture
def http(session: Session, owner: Principal, published):
    session.commit()

    def override():
        s = SessionLocal()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    from app.db import get_session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def register(session: Session, **kwargs):
    client, secret = oauth.register_client(
        session,
        name=kwargs.pop("name", "Claude"),
        redirect_uris=kwargs.pop("redirect_uris", [REDIRECT]),
        **kwargs,
    )
    session.flush()
    return client, secret


def start(session: Session, client, **kwargs) -> OAuthAuthRequest:
    return oauth.start_request(
        session,
        client_key=kwargs.pop("client_key", client.client_key),
        redirect_uri=kwargs.pop("redirect_uri", REDIRECT),
        code_challenge=kwargs.pop("code_challenge", challenge_for(VERIFIER)),
        code_challenge_method="S256",
        scope=kwargs.pop("scope", None),
        state="xyz",
        resource=kwargs.pop("resource", RESOURCE),
        canonical_resource=RESOURCE,
    )


# --- the happy path ---------------------------------------------------------------------

def test_the_whole_flow_ends_in_a_working_token(session: Session, owner: Principal):
    client, secret = register(session)
    assert secret is None, "a connector without a secret is a public client: PKCE only"

    request = start(session, client)
    assert request.status is AuthRequestStatus.PENDING, "nothing is granted before he taps"

    oauth.mark_approved(session, request.id)
    code = oauth.approve_to_code(session, request)

    pair = oauth.exchange_code(
        session, auth=oauth.ClientAuth(client.client_key), code=code,
        code_verifier=VERIFIER, redirect_uri=REDIRECT, resource=RESOURCE,
        canonical_resource=RESOURCE,
    )
    assert pair.expires_in == 3600

    identity = authenticate(session, pair.access_token)
    assert identity.principal_id == owner.id, "the token opens the owner's data, not a copy"


def test_the_token_carries_only_the_scopes_that_were_approved(session: Session,
                                                              owner: Principal):
    client, _ = register(session)
    request = start(session, client, scope="read")
    oauth.mark_approved(session, request.id)
    code = oauth.approve_to_code(session, request)
    pair = oauth.exchange_code(
        session, auth=oauth.ClientAuth(client.client_key), code=code,
        code_verifier=VERIFIER, redirect_uri=REDIRECT, resource=RESOURCE,
        canonical_resource=RESOURCE,
    )

    from app.models import Scope

    identity = authenticate(session, pair.access_token)
    assert identity.has(Scope.READ)
    assert not identity.has(Scope.WRITE_FACTS), "read means read"


def test_refresh_rotates_and_the_old_access_token_dies(session: Session, owner: Principal):
    client, _ = register(session)
    request = start(session, client)
    oauth.mark_approved(session, request.id)
    first = oauth.exchange_code(
        session, auth=oauth.ClientAuth(client.client_key),
        code=oauth.approve_to_code(session, request), code_verifier=VERIFIER,
        redirect_uri=REDIRECT, resource=RESOURCE, canonical_resource=RESOURCE,
    )

    second = oauth.refresh_tokens(
        session, auth=oauth.ClientAuth(client.client_key),
        refresh_token=first.refresh_token, scope=None, resource=None,
        canonical_resource=RESOURCE,
    )
    assert second.access_token != first.access_token

    assert authenticate(session, second.access_token).principal_id == owner.id
    with pytest.raises(AuthError):
        authenticate(session, first.access_token)


# --- the ways in that must stay shut -----------------------------------------------------

def test_a_code_cannot_be_spent_twice(session: Session, owner: Principal):
    client, _ = register(session)
    request = start(session, client)
    oauth.mark_approved(session, request.id)
    code = oauth.approve_to_code(session, request)
    spend = dict(
        auth=oauth.ClientAuth(client.client_key), code=code,
        code_verifier=VERIFIER, redirect_uri=REDIRECT, resource=RESOURCE,
        canonical_resource=RESOURCE,
    )
    pair = oauth.exchange_code(session, **spend)

    with pytest.raises(oauth.OAuthError) as caught:
        oauth.exchange_code(session, **spend)
    assert caught.value.error == "invalid_grant"
    # A replayed code means a copy is in circulation: the session it produced goes too
    with pytest.raises(AuthError):
        authenticate(session, pair.access_token)


def test_a_reused_refresh_token_revokes_everything(session: Session, owner: Principal):
    client, _ = register(session)
    request = start(session, client)
    oauth.mark_approved(session, request.id)
    first = oauth.exchange_code(
        session, auth=oauth.ClientAuth(client.client_key),
        code=oauth.approve_to_code(session, request), code_verifier=VERIFIER,
        redirect_uri=REDIRECT, resource=RESOURCE, canonical_resource=RESOURCE,
    )
    second = oauth.refresh_tokens(
        session, auth=oauth.ClientAuth(client.client_key),
        refresh_token=first.refresh_token, scope=None, resource=None,
        canonical_resource=RESOURCE,
    )

    with pytest.raises(oauth.OAuthError):
        oauth.refresh_tokens(
            session, auth=oauth.ClientAuth(client.client_key),
            refresh_token=first.refresh_token, scope=None, resource=None,
            canonical_resource=RESOURCE,
        )
    with pytest.raises(AuthError):
        authenticate(session, second.access_token)


def test_a_wrong_verifier_is_refused(session: Session, owner: Principal):
    client, _ = register(session)
    request = start(session, client)
    oauth.mark_approved(session, request.id)
    code = oauth.approve_to_code(session, request)

    with pytest.raises(oauth.OAuthError) as caught:
        oauth.exchange_code(
            session, auth=oauth.ClientAuth(client.client_key), code=code,
            code_verifier="b" * 64, redirect_uri=REDIRECT, resource=RESOURCE,
            canonical_resource=RESOURCE,
        )
    assert caught.value.error == "invalid_grant"


def test_an_unregistered_redirect_is_refused(session: Session, owner: Principal):
    client, _ = register(session)
    with pytest.raises(oauth.OAuthError) as caught:
        start(session, client, redirect_uri="https://evil.example/collect")
    assert caught.value.error == "invalid_request"


def test_a_token_for_another_server_is_refused(session: Session, owner: Principal):
    client, _ = register(session)
    with pytest.raises(oauth.OAuthError) as caught:
        start(session, client, resource="https://somebody-elses.example/mcp")
    assert caught.value.error == "invalid_target"


def test_a_request_nobody_answered_expires(session: Session, owner: Principal):
    client, _ = register(session)
    request = start(session, client)
    request.expires_at = utcnow() - dt.timedelta(seconds=1)
    session.flush()

    assert oauth.pending(session, request.id).status is AuthRequestStatus.EXPIRED
    with pytest.raises(oauth.OAuthError):
        oauth.mark_approved(session, request.id)


def test_a_denied_request_grants_nothing(session: Session, owner: Principal):
    client, _ = register(session)
    request = start(session, client)
    oauth.deny(session, request.id)

    assert request.status is AuthRequestStatus.DENIED
    with pytest.raises(oauth.OAuthError):
        oauth.approve_to_code(session, request)


def test_a_revoked_token_stops_working(session: Session, owner: Principal):
    client, _ = register(session)
    request = start(session, client)
    oauth.mark_approved(session, request.id)
    pair = oauth.exchange_code(
        session, auth=oauth.ClientAuth(client.client_key),
        code=oauth.approve_to_code(session, request), code_verifier=VERIFIER,
        redirect_uri=REDIRECT, resource=RESOURCE, canonical_resource=RESOURCE,
    )

    assert oauth.revoke(session, pair.access_token) is True
    with pytest.raises(AuthError):
        authenticate(session, pair.access_token)


def test_an_expired_access_token_stops_working(session: Session, owner: Principal):
    from app.models.oauth import OAuthToken

    client, _ = register(session)
    request = start(session, client)
    oauth.mark_approved(session, request.id)
    pair = oauth.exchange_code(
        session, auth=oauth.ClientAuth(client.client_key),
        code=oauth.approve_to_code(session, request), code_verifier=VERIFIER,
        redirect_uri=REDIRECT, resource=RESOURCE, canonical_resource=RESOURCE,
    )
    from sqlalchemy import select

    row = session.scalars(select(OAuthToken)).one()
    row.expires_at = utcnow() - dt.timedelta(seconds=1)
    session.flush()

    with pytest.raises(AuthError):
        authenticate(session, pair.access_token)


# --- what the connectors actually fetch ---------------------------------------------------

@pytest.mark.parametrize("path", [
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
])
def test_the_resource_says_where_to_log_in(http: TestClient, path: str):
    body = http.get(path).json()
    assert body["resource"] == RESOURCE
    assert body["authorization_servers"] == [ISSUER]


@pytest.mark.parametrize("path", [
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-authorization-server/mcp",
    "/.well-known/openid-configuration",
])
def test_the_authorization_server_describes_itself(http: TestClient, path: str):
    body = http.get(path).json()
    assert body["issuer"] == ISSUER
    assert body["code_challenge_methods_supported"] == ["S256"], "OAuth 2.1: S256 or nothing"
    assert "authorization_code" in body["grant_types_supported"]


def test_mcp_without_a_token_says_where_to_get_one(http: TestClient):
    response = http.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401, "a 200 with an error inside teaches a client nothing"
    challenge = response.headers["www-authenticate"]
    assert "oauth-protected-resource" in challenge
    assert challenge.startswith("Bearer ")


def test_mcp_with_a_token_is_let_through(http: TestClient, session: Session,
                                         owner: Principal):
    """The guard must open, not merely close: the old static tokens still work."""
    from app.core.security import create_client
    from app.models import ClientKind, Scope

    with SessionLocal() as setup:
        principal = setup.get(Principal, owner.id)
        _client, token = create_client(
            setup, principal=principal, name="guard-test", kind=ClientKind.CLAUDE,
            scopes={Scope.READ},
        )
        setup.commit()

    response = http.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/json, text/event-stream"},
    )
    assert response.status_code != 401, "a valid token must reach the protocol"


def test_registration_is_open_but_grants_nothing(http: TestClient):
    response = http.post(
        "/oauth/register",
        json={"client_name": "ChatGPT", "redirect_uris": [REDIRECT],
              "grant_types": ["authorization_code", "refresh_token"]},
    )
    assert response.status_code == 201
    client_id = response.json()["client_id"]

    with SessionLocal() as check:
        from sqlalchemy import select

        from app.models.oauth import OAuthClient, OAuthToken

        registered = check.scalar(
            select(OAuthClient).where(OAuthClient.client_key == client_id)
        )
        assert registered is not None
        assert check.scalars(select(OAuthToken)).all() == [], "registering is not access"


def test_registration_without_a_redirect_is_refused(http: TestClient):
    response = http.post("/oauth/register", json={"client_name": "nowhere"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"
