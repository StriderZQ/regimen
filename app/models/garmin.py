"""Garmin data: the raw payload apart from what was parsed out of it.

The raw response is kept whole and forever. The reason is simple: Garmin changes its
format without warning, and when a parse turns out to be wrong, metrics can only be
rebuilt from the raw form. Discarding it loses history irreversibly.

**Event time and fetch time are different columns.** The workout happened yesterday at
11:12 and arrived today at 04:30. Confusing them is fatal: "what happened" is built on
the first, "how fresh is this" on the second. Answering "data is from Thursday" is more
honest than confidently parsing the night before last as last night.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import JSON, Enum, Float, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DomainBase


class GarminKind(enum.StrEnum):
    ACTIVITY = "activity"
    SLEEP = "sleep"
    HRV = "hrv"
    READINESS = "readiness"
    BODY = "body"


class GarminRaw(DomainBase):
    """Garmin's response as it came. Unique by kind plus external id."""

    __tablename__ = "garmin_raw"
    __table_args__ = (
        UniqueConstraint("owner_id", "kind", "external_id", name="uq_garmin_raw"),
        Index("ix_garmin_raw_event", "owner_id", "kind", "event_at"),
    )

    kind: Mapped[GarminKind] = mapped_column(Enum(GarminKind), nullable=False)
    external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    event_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    local_date: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    fetched_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class GarminMetric(DomainBase):
    """Only what is actually used gets normalised. The rest stays in the raw payload."""

    __tablename__ = "garmin_metrics"
    __table_args__ = (
        UniqueConstraint("owner_id", "kind", "external_id", name="uq_garmin_metric"),
        Index("ix_garmin_metrics_date", "owner_id", "kind", "local_date"),
    )

    kind: Mapped[GarminKind] = mapped_column(Enum(GarminKind), nullable=False)
    external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    event_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    local_date: Mapped[str] = mapped_column(String(10), nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(nullable=False)

    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    duration_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    calories: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_hr: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_hr: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Sleep in minutes, HRV in milliseconds, readiness in Garmin's own points
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
