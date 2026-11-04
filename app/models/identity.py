"""Who is calling the service.

There is deliberately no role model: there is one owner and there will be no coach. But
three things are in place, without which a second person would be a painful migration:

  * `principals` — people. One row for now
  * `clients` — an assistant, Telegram, the web. Each holds its own revocable token
  * `grants` — the principal × client × scope link

Permission checks go through scopes centrally, not `if token == admin` scattered around.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import Boolean, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, IdMixin, TimestampMixin


class Scope(enum.StrEnum):
    """Three levels. No more are needed while there is one user."""

    READ = "read"
    # Writing a fact: "ate it", "did the set". Registering what happened, not changing the world.
    WRITE_FACTS = "write_facts"
    ADMIN = "admin"


class ClientKind(enum.StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    TELEGRAM = "telegram"
    WEB = "web"
    SCHEDULER = "scheduler"


class Principal(Base, IdMixin, TimestampMixin):
    """A person. Exactly one for now."""

    __tablename__ = "principals"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    clients: Mapped[list[Client]] = relationship(back_populates="principal")


class Client(Base, IdMixin, TimestampMixin):
    """A program that talks to the service. The token is stored only as a hash."""

    __tablename__ = "clients"
    __table_args__ = (UniqueConstraint("principal_id", "name", name="uq_client_name"),)

    principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[ClientKind] = mapped_column(Enum(ClientKind), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Telegram binding: the owner's chat_id. Messages from any other chat are ignored.
    telegram_chat_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    principal: Mapped[Principal] = relationship(back_populates="clients")
    grants: Mapped[list[Grant]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )


class Grant(Base, IdMixin, TimestampMixin):
    __tablename__ = "grants"
    __table_args__ = (UniqueConstraint("client_id", "scope", name="uq_grant"),)

    client_id: Mapped[str] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope: Mapped[Scope] = mapped_column(Enum(Scope), nullable=False)

    client: Mapped[Client] = relationship(back_populates="grants")
