"""Client authentication and permission checks.

Centralised through scopes rather than `if token == admin` scattered around — otherwise
a second person, a year from now, turns into an audit of every handler.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.identity import Client, ClientKind, Grant, Principal, Scope

TOKEN_PREFIX = "kag_"


class AuthError(Exception):
    """No access. The message does not reveal whether the token exists."""


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Identity:
    """Who is calling. Passed into the domain; the domain checks no permissions itself."""

    principal_id: str
    client_id: str
    client_kind: ClientKind
    scopes: frozenset[Scope]

    def has(self, scope: Scope) -> bool:
        return scope in self.scopes or Scope.ADMIN in self.scopes

    def require(self, scope: Scope) -> None:
        if not self.has(scope):
            raise AuthError(f"Requires the {scope.value} scope")


def authenticate(session: Session, token: str | None) -> Identity:
    if not token:
        raise AuthError("No token")

    client = session.scalar(
        select(Client).where(
            Client.token_hash == hash_token(token),
            Client.is_active.is_(True),
            Client.revoked_at.is_(None),
        )
    )
    if client is None:
        # A token issued through OAuth belongs to the same owner and produces the same
        # Identity — the doors differ, what they open does not. Imported here because
        # the OAuth module is built on this one
        from app.core import oauth

        identity = oauth.authenticate(session, token)
        if identity is not None:
            return identity
        raise AuthError("Unknown or revoked token")

    scopes = session.scalars(select(Grant.scope).where(Grant.client_id == client.id)).all()
    client.last_seen_at = utcnow()
    return Identity(
        principal_id=client.principal_id,
        client_id=client.id,
        client_kind=client.kind,
        scopes=frozenset(scopes),
    )


def authenticate_telegram(session: Session, chat_id: int) -> Identity:
    """Telegram is identified by the owner's chat_id: other chats are ignored."""
    client = session.scalar(
        select(Client).where(
            Client.kind == ClientKind.TELEGRAM,
            Client.telegram_chat_id == chat_id,
            Client.is_active.is_(True),
            Client.revoked_at.is_(None),
        )
    )
    if client is None:
        raise AuthError("This chat is not linked to the owner")

    scopes = session.scalars(select(Grant.scope).where(Grant.client_id == client.id)).all()
    client.last_seen_at = utcnow()
    return Identity(
        principal_id=client.principal_id,
        client_id=client.id,
        client_kind=client.kind,
        scopes=frozenset(scopes),
    )


def create_client(
    session: Session,
    *,
    principal: Principal,
    name: str,
    kind: ClientKind,
    scopes: set[Scope],
    telegram_chat_id: int | None = None,
) -> tuple[Client, str]:
    """Returns the client and the token in the clear. The token is shown ONCE."""
    token = generate_token()
    client = Client(
        principal_id=principal.id,
        name=name,
        kind=kind,
        token_hash=hash_token(token),
        telegram_chat_id=telegram_chat_id,
    )
    session.add(client)
    session.flush()
    for scope in scopes:
        session.add(Grant(client_id=client.id, scope=scope))
    return client, token
