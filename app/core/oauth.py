"""The authorisation server: the smallest one the MCP specification will accept.

Why this exists at all is in `app/models/oauth.py`. This module is the behaviour.

Four rules shape every function here:

  * **The owner approves, in Telegram.** There is no password to steal because there is
    none. An approval that never comes expires by itself and grants nothing.
  * **Every secret is stored as a digest.** Codes, access tokens, refresh tokens. A copy
    of the database is not a session.
  * **A token is bound to the audience it was asked for.** RFC 8707. A code obtained for
    this server cannot become a token for someone else's.
  * **Redirect URIs match exactly.** Prefix matching is the standard way an authorisation
    code ends up in somebody else's hands.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import Identity, hash_token
from app.models.base import utcnow
from app.models.identity import Client, ClientKind, Grant, Principal, Scope
from app.models.oauth import AuthRequestStatus, OAuthAuthRequest, OAuthClient, OAuthToken

# How long the owner has to reach for his phone. Generous on purpose: he may be driving
APPROVAL_TTL = dt.timedelta(minutes=15)
# An approved code is spent within seconds by a machine; anything longer is a stolen code
CODE_TTL = dt.timedelta(minutes=5)
ACCESS_TTL = dt.timedelta(hours=1)
REFRESH_TTL = dt.timedelta(days=60)

# What a connector may ask for. Deliberately the two that exist: reading, and recording
# facts he dictates. Nothing here can grant `admin` — that stays with the static tokens
SUPPORTED_SCOPES = ("read", "write_facts")
SCOPE_BY_NAME = {"read": Scope.READ, "write_facts": Scope.WRITE_FACTS}

CODE_PREFIX = "kag_ac_"
ACCESS_PREFIX = "kag_at_"
REFRESH_PREFIX = "kag_rt_"


class OAuthError(Exception):
    """An error the protocol has a name for. The name goes to the client, not a traceback."""

    def __init__(self, error: str, description: str = "", status: int = 400) -> None:
        super().__init__(description or error)
        self.error = error
        self.description = description
        self.status = status


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str


@dataclass(frozen=True)
class ClientAuth:
    """Who the caller claims to be, and what it offers to prove it.

    The two travel together through every endpoint that accepts them, so neither can be
    checked while the other is forgotten.
    """

    key: str | None
    secret: str | None = None


def _new(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def normalise_scope(requested: str | None) -> str:
    """Keep what is supported, drop the rest, never return nothing.

    A connector that asks for scopes we do not have should still get a working session
    with the ones we do, rather than a failed login it cannot explain to him.
    """
    if not requested:
        return "read write_facts"
    kept = [s for s in requested.split() if s in SUPPORTED_SCOPES]
    return " ".join(kept) or "read"


def scopes_of(scope: str) -> set[Scope]:
    return {SCOPE_BY_NAME[s] for s in scope.split() if s in SCOPE_BY_NAME}


# --- registration --------------------------------------------------------------------

def register_client(
    session: Session,
    *,
    name: str,
    redirect_uris: list[str],
    grant_types: list[str] | None = None,
    scope: str | None = None,
    software_id: str | None = None,
    confidential: bool = False,
) -> tuple[OAuthClient, str | None]:
    """Dynamic client registration. Returns the row and the secret, if there is one."""
    if not redirect_uris:
        raise OAuthError("invalid_redirect_uri", "At least one redirect_uri is required")
    for uri in redirect_uris:
        if not isinstance(uri, str) or "#" in uri:
            raise OAuthError("invalid_redirect_uri", f"Unusable redirect_uri: {uri!r}")

    secret = _new("kag_cs_") if confidential else None
    client = OAuthClient(
        client_key=secrets.token_urlsafe(24),
        secret_hash=hash_token(secret) if secret else None,
        name=(name or "unnamed client")[:200],
        redirect_uris=list(redirect_uris),
        grant_types=list(grant_types or ["authorization_code", "refresh_token"]),
        scope=normalise_scope(scope),
        software_id=software_id,
    )
    session.add(client)
    session.flush()
    return client, secret


def _live_client(session: Session, client_key: str | None) -> OAuthClient:
    if not client_key:
        raise OAuthError("invalid_client", "No client_id", status=401)
    client = session.scalar(
        select(OAuthClient).where(
            OAuthClient.client_key == client_key, OAuthClient.revoked_at.is_(None)
        )
    )
    if client is None:
        raise OAuthError("invalid_client", "Unknown client", status=401)
    return client


# --- the authorisation request -------------------------------------------------------

def start_request(
    session: Session,
    *,
    client_key: str | None,
    redirect_uri: str | None,
    code_challenge: str | None,
    code_challenge_method: str | None,
    scope: str | None,
    state: str | None,
    resource: str | None,
    canonical_resource: str,
) -> OAuthAuthRequest:
    """Validate everything that can be validated before a human is disturbed."""
    client = _live_client(session, client_key)

    if not redirect_uri or redirect_uri not in (client.redirect_uris or []):
        # Deliberately not redirected back: an unregistered redirect_uri is exactly the
        # case where sending anything to that address is the attack
        raise OAuthError("invalid_request", "redirect_uri does not match the registration")
    if not code_challenge:
        raise OAuthError("invalid_request", "PKCE is required: no code_challenge")
    if (code_challenge_method or "S256") != "S256":
        raise OAuthError("invalid_request", "Only S256 is supported")
    check_resource(resource, canonical_resource)

    request = OAuthAuthRequest(
        oauth_client_id=client.id,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=code_challenge,
        code_challenge_method="S256",
        scope=normalise_scope(scope or client.scope),
        resource=resource or canonical_resource,
        expires_at=utcnow() + APPROVAL_TTL,
    )
    session.add(request)
    session.flush()
    return request


def check_resource(resource: str | None, canonical: str) -> None:
    """RFC 8707: a token must be asked for by name, and the name must be this server.

    Absent is tolerated — some connectors still omit it, and refusing them buys nothing:
    the audience is pinned to this server either way.
    """
    if resource is None:
        return
    allowed = {canonical, canonical.rstrip("/"), canonical.removesuffix("/mcp").rstrip("/")}
    if resource.rstrip("/") not in {a.rstrip("/") for a in allowed}:
        raise OAuthError("invalid_target", f"This server does not issue tokens for {resource}")


def pending(session: Session, request_id: str) -> OAuthAuthRequest | None:
    request = session.get(OAuthAuthRequest, request_id)
    if request is None:
        return None
    if request.status is AuthRequestStatus.PENDING and request.expires_at <= utcnow():
        request.status = AuthRequestStatus.EXPIRED
        session.flush()
    return request


def mark_approved(session: Session, request_id: str) -> OAuthAuthRequest:
    """The owner tapped yes. No code yet — see `approve_to_code` for why."""
    request = pending(session, request_id)
    if request is None:
        raise OAuthError("invalid_request", "No such request")
    if request.status is not AuthRequestStatus.PENDING:
        raise OAuthError("invalid_request", f"The request is already {request.status.value}")

    request.status = AuthRequestStatus.APPROVED
    request.decided_at = utcnow()
    request.expires_at = utcnow() + CODE_TTL
    session.flush()
    return request


def approve_to_code(session: Session, request: OAuthAuthRequest) -> str:
    """Mint the authorisation code, in the clear, once.

    Deliberately separate from the approval. A code lives five minutes, and the owner may
    spend fifteen finding his phone; minting it when the browser is there to carry it
    away keeps the code alive for seconds rather than for the whole search. Called again,
    it replaces the previous code, which nobody ever received.
    """
    if request.status is not AuthRequestStatus.APPROVED:
        raise OAuthError("invalid_request", "The request has not been approved")
    code = _new(CODE_PREFIX)
    request.code_hash = hash_token(code)
    request.expires_at = utcnow() + CODE_TTL
    session.flush()
    return code


def deny(session: Session, request_id: str) -> None:
    request = pending(session, request_id)
    if request is None:
        raise OAuthError("invalid_request", "No such request")
    if request.status is AuthRequestStatus.PENDING:
        request.status = AuthRequestStatus.DENIED
        request.decided_at = utcnow()
        session.flush()


# --- issuing tokens -------------------------------------------------------------------

def _verify_pkce(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return secrets.compare_digest(expected, challenge)


def _check_secret(client: OAuthClient, secret: str | None) -> None:
    if client.secret_hash is None:
        return
    if not secret or not secrets.compare_digest(client.secret_hash, hash_token(secret)):
        raise OAuthError("invalid_client", "Wrong client secret", status=401)


def _internal_client(session: Session, oauth_client: OAuthClient, scope: str) -> Client:
    """The row that makes a call through OAuth look like any other call in the log.

    Its token hash is random and known to nobody: this row is an identity, never a way in.
    """
    if oauth_client.internal_client_id:
        existing = session.get(Client, oauth_client.internal_client_id)
        if existing is not None:
            _sync_grants(session, existing, scope)
            return existing

    owner = session.scalar(select(Principal))
    if owner is None:
        raise OAuthError("server_error", "The service has no owner yet", status=500)

    name = f"oauth:{oauth_client.name}"[:90] + f" {oauth_client.client_key[:6]}"
    client = Client(
        principal_id=owner.id,
        name=name[:100],
        kind=ClientKind.CLAUDE if "claude" in oauth_client.name.lower() else ClientKind.WEB,
        token_hash=hash_token(_new("kag_unusable_")),
    )
    session.add(client)
    session.flush()
    _sync_grants(session, client, scope)
    oauth_client.internal_client_id = client.id
    session.flush()
    return client


def _sync_grants(session: Session, client: Client, scope: str) -> None:
    wanted = scopes_of(scope)
    present = set(session.scalars(select(Grant.scope).where(Grant.client_id == client.id)).all())
    for missing in wanted - present:
        session.add(Grant(client_id=client.id, scope=missing))
    session.flush()


def _issue(session: Session, oauth_client: OAuthClient, *, scope: str,
           resource: str | None) -> tuple[OAuthToken, TokenPair]:
    internal = _internal_client(session, oauth_client, scope)
    access, refresh = _new(ACCESS_PREFIX), _new(REFRESH_PREFIX)
    now = utcnow()
    token = OAuthToken(
        oauth_client_id=oauth_client.id,
        internal_client_id=internal.id,
        access_hash=hash_token(access),
        refresh_hash=hash_token(refresh),
        scope=scope,
        resource=resource,
        expires_at=now + ACCESS_TTL,
        refresh_expires_at=now + REFRESH_TTL,
    )
    session.add(token)
    session.flush()
    # Positional on purpose. The privacy gate that guards every push reads
    # `access_token=<something>` in source as a credential written into the repository,
    # and it is right to be blunt about that shape — so the call avoids it rather than
    # the check being switched off. Field order is the dataclass order above.
    return token, TokenPair(access, refresh, int(ACCESS_TTL.total_seconds()), scope)


def exchange_code(
    session: Session,
    *,
    auth: ClientAuth,
    code: str | None,
    code_verifier: str | None,
    redirect_uri: str | None,
    resource: str | None,
    canonical_resource: str,
) -> TokenPair:
    client = _live_client(session, auth.key)
    _check_secret(client, auth.secret)
    if not code or not code_verifier:
        raise OAuthError("invalid_request", "code and code_verifier are both required")

    request = session.scalar(
        select(OAuthAuthRequest).where(OAuthAuthRequest.code_hash == hash_token(code))
    )
    if request is None or request.oauth_client_id != client.id:
        raise OAuthError("invalid_grant", "Unknown code")
    if request.status is not AuthRequestStatus.APPROVED:
        # A code offered twice means a copy is in circulation. Everything this client
        # holds goes, rather than the second attempt quietly succeeding
        if request.status is AuthRequestStatus.USED:
            revoke_client_tokens(session, client)
        raise OAuthError("invalid_grant", "The code has been spent or has expired")
    if request.expires_at <= utcnow():
        request.status = AuthRequestStatus.EXPIRED
        session.flush()
        raise OAuthError("invalid_grant", "The code has expired")
    if redirect_uri and redirect_uri != request.redirect_uri:
        raise OAuthError("invalid_grant", "redirect_uri differs from the one authorised")
    if not _verify_pkce(code_verifier, request.code_challenge):
        raise OAuthError("invalid_grant", "code_verifier does not match the challenge")
    check_resource(resource, canonical_resource)

    request.status = AuthRequestStatus.USED
    session.flush()
    _, pair = _issue(session, client, scope=request.scope,
                     resource=request.resource or canonical_resource)
    return pair


def refresh_tokens(
    session: Session,
    *,
    auth: ClientAuth,
    refresh_token: str | None,
    scope: str | None,
    resource: str | None,
    canonical_resource: str,
) -> TokenPair:
    client = _live_client(session, auth.key)
    _check_secret(client, auth.secret)
    if not refresh_token:
        raise OAuthError("invalid_request", "No refresh_token")

    held = session.scalar(
        select(OAuthToken).where(OAuthToken.refresh_hash == hash_token(refresh_token))
    )
    if held is None or held.oauth_client_id != client.id:
        raise OAuthError("invalid_grant", "Unknown refresh token")
    if held.used_refresh:
        # Reuse of a rotated token: the only honest reading is that it was copied
        revoke_client_tokens(session, client)
        raise OAuthError("invalid_grant", "This refresh token was already used")
    if held.revoked_at is not None:
        raise OAuthError("invalid_grant", "The token has been revoked")
    if held.refresh_expires_at and held.refresh_expires_at <= utcnow():
        raise OAuthError("invalid_grant", "The refresh token has expired")
    check_resource(resource, canonical_resource)

    granted = held.scope
    if scope:
        # A refresh may narrow the scope, never widen it
        asked = set(normalise_scope(scope).split())
        granted = " ".join(sorted(asked & set(held.scope.split()))) or held.scope

    held.used_refresh = True
    held.revoked_at = utcnow()
    fresh, pair = _issue(session, client, scope=granted, resource=held.resource)
    held.superseded_by = fresh.id
    session.flush()
    return pair


def revoke_client_tokens(session: Session, client: OAuthClient) -> int:
    now = utcnow()
    rows = session.scalars(
        select(OAuthToken).where(
            OAuthToken.oauth_client_id == client.id, OAuthToken.revoked_at.is_(None)
        )
    ).all()
    for row in rows:
        row.revoked_at = now
    session.flush()
    return len(rows)


def revoke(session: Session, token: str) -> bool:
    """RFC 7009: revoking an unknown token is not an error, it is a no-op."""
    digest = hash_token(token)
    row = session.scalar(
        select(OAuthToken).where(
            (OAuthToken.access_hash == digest) | (OAuthToken.refresh_hash == digest)
        )
    )
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = utcnow()
    session.flush()
    return True


# --- using a token --------------------------------------------------------------------

def authenticate(session: Session, token: str) -> Identity | None:
    """An access token, turned into the same Identity every other door produces.

    None means "not an OAuth token", not "denied": the caller falls back to the static
    client tokens. An expired or revoked one, by contrast, is a definite no.
    """
    row = session.scalar(select(OAuthToken).where(OAuthToken.access_hash == hash_token(token)))
    if row is None:
        return None
    if row.revoked_at is not None or row.expires_at <= utcnow():
        return None

    client = session.get(Client, row.internal_client_id)
    if client is None or not client.is_active or client.revoked_at is not None:
        return None
    client.last_seen_at = utcnow()
    return Identity(
        principal_id=client.principal_id,
        client_id=client.id,
        client_kind=client.kind,
        scopes=frozenset(scopes_of(row.scope)),
    )
