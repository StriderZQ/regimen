"""The service as its own OAuth authorisation server.

The phone is the reason this exists. Claude and ChatGPT reach an MCP server from their
own machines, not from the owner's, and their connector screens offer exactly two ways in:
OAuth, or nothing at all. There is no field for a static token. A service that wants to be
reachable from a phone therefore has to issue tokens itself.

Three tables, and each answers one question:

  oauth_clients       — which program asked for access, and where it may be sent back
  oauth_auth_requests — one attempt to log in, alive for minutes
  oauth_tokens        — what was issued, and until when

Who approves: the owner, in Telegram. No password is stored anywhere, because there is
none — the right to approve belongs to whoever holds the phone the bot talks to. An
approval that never arrives simply expires.

Nothing here is ever stored in the clear. Codes, access tokens and refresh tokens live as
SHA-256 digests, the same way client tokens always have: a stolen database copy must not
be a stolen session.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import JSON, Boolean, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, IdMixin, TimestampMixin


class AuthRequestStatus(enum.StrEnum):
    PENDING = "pending"    # waiting for the owner to tap a button
    APPROVED = "approved"  # a code has been issued and not yet spent
    DENIED = "denied"      # he said no
    USED = "used"          # the code was exchanged for a token; never again
    EXPIRED = "expired"    # nobody answered in time


class OAuthClient(Base, IdMixin, TimestampMixin):
    """A program that registered itself: Claude, ChatGPT, anything speaking the protocol.

    Registration is open — that is what dynamic client registration means, and both
    connectors rely on it. Registering grants nothing: a client with no approved request
    holds no token and can read nothing. The gate is the owner's thumb, not the door.
    """

    __tablename__ = "oauth_clients"

    # The public identifier handed to the client. Not the row id: that one is internal
    client_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # Public clients (PKCE only) have none. Confidential ones store a digest
    secret_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Exact strings the client may be redirected back to. Never prefix-matched:
    # a prefix match is how an open redirect becomes a stolen authorisation code
    redirect_uris: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    grant_types: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    scope: Mapped[str] = mapped_column(String(200), nullable=False, default="read")
    # The internal client row, so a call through OAuth is logged like any other call
    internal_client_id: Mapped[str | None] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    software_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    requests: Mapped[list[OAuthAuthRequest]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )


class OAuthAuthRequest(Base, IdMixin, TimestampMixin):
    """One login attempt, from the moment the browser arrives to the moment it leaves.

    It carries the PKCE challenge and the audience the token is meant for, because both
    have to be checked again at the token endpoint — a code issued for one server must
    not become a token for another.
    """

    __tablename__ = "oauth_auth_requests"

    oauth_client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_challenge: Mapped[str] = mapped_column(String(200), nullable=False)
    code_challenge_method: Mapped[str] = mapped_column(String(10), nullable=False, default="S256")
    scope: Mapped[str] = mapped_column(String(200), nullable=False, default="read")
    # RFC 8707: which resource the token is for. Checked again when the code is spent
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[AuthRequestStatus] = mapped_column(
        Enum(AuthRequestStatus), default=AuthRequestStatus.PENDING, nullable=False
    )
    code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    decided_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    # What the owner saw in Telegram, so the message can be edited after he answers
    telegram_message_id: Mapped[int | None] = mapped_column(nullable=True)

    client: Mapped[OAuthClient] = relationship(back_populates="requests")


class OAuthToken(Base, IdMixin, TimestampMixin):
    """An issued pair. The access token expires in an hour; the refresh token rotates.

    Rotation is the point: a refresh token used twice means a copy is in circulation, and
    the whole chain is revoked rather than the second use quietly succeeding.
    """

    __tablename__ = "oauth_tokens"

    oauth_client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    internal_client_id: Mapped[str] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    access_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    refresh_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    scope: Mapped[str] = mapped_column(String(200), nullable=False, default="read")
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    refresh_expires_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    # Set when this pair is rotated away or revoked. A revoked token is kept, not deleted:
    # the trail of who held access and when is worth more than the row it costs
    revoked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    used_refresh: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
