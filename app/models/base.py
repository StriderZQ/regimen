"""Base classes for the models.

Two rules that cut across the whole design live here, where they cannot be forgotten:

  * `owner_id` on every domain table and in every unique index — so that a second
    person, a year from now, becomes a new row rather than a migration of everything;
  * time always carries a zone. SQLite does not store one, so the type below forces
    UTC on write and returns an aware datetime on read.
    There are no naive datetimes in the system — they would break travel, late-night
    training and daily targets alike.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, String, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UtcDateTime(TypeDecorator):
    """Stores UTC, returns aware. A naive input is an error, not something to guess at."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "A naive datetime is refused: time must carry a zone. "
                f"Got {value!r}"
            )
        return value.astimezone(dt.UTC)

    def process_result_value(self, value: dt.datetime | None, dialect) -> dt.datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def local_today() -> dt.date:
    """Today in the owner's timezone, not the server's.

    The server runs in UTC; the owner's day ends at local midnight. Late in the evening
    UTC the two disagree, and `date.today()` then answers with yesterday — which is how
    an evening question about today's session got yesterday's plan.
    """
    from app.config import get_settings

    return utcnow().astimezone(get_settings().tz).date()


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    type_annotation_map = {dt.datetime: UtcDateTime}


class IdMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, nullable=False
    )


class OwnedMixin:
    """owner_id is mandatory, and part of every unique index in the domain."""

    owner_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT"), nullable=False, index=True
    )


class DomainBase(Base, IdMixin, OwnedMixin, TimestampMixin):
    __abstract__ = True
