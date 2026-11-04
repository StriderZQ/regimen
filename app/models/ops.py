"""The machinery that keeps the system dependable: job queue, idempotency, call log.

Confirmations are no longer here. They were meant to guard against the agent acting on
its own, but in practice they made the owner confirm his own words: he would say "I
weighed it, 217", and the system asked him to follow a link and press a button. Three
other things provide the guard, and they remain: nothing is ever deleted (everything is
a movement), a database copy every 15 minutes, and the agent acts only when told to.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    JSON,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TimestampMixin


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"


class Job(Base, IdMixin, TimestampMixin):
    """A background job with retries and deduplication.

    Not decoration: the receipt and Garmin syncs must survive timeouts, and messages from
    Telegram must not be lost while the server is unreachable — they wait here and are
    applied when the service returns.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        # Deduplication: the same job never queues twice
        UniqueConstraint("dedupe_key", name="uq_job_dedupe"),
        Index("ix_jobs_status_run_after", "status", "run_after"),
    )

    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), default=JobStatus.QUEUED, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    run_after: Mapped[dt.datetime] = mapped_column(nullable=False)
    started_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class IdempotencyKey(Base, IdMixin, TimestampMixin):
    """An idempotency key on EVERY incoming command, not only on receipts.

    Telegram and background jobs will inevitably retry after a timeout. A retry with the
    same key returns the original result instead of creating a second record.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("scope", "key", name="uq_idem"),)

    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False, index=True)


class CallLog(Base, IdMixin, TimestampMixin):
    """Who called what, and when.

    Deliberately NOT snapshots of every row: stock movements and the diary already provide
    recoverable history. This is only the trace of a call: who, what, and how it ended.
    """

    __tablename__ = "call_log"
    __table_args__ = (Index("ix_call_log_created", "created_at"),)

    client_id: Mapped[str | None] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tool: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    ok: Mapped[bool] = mapped_column(default=True, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Arguments without anything sensitive: field names, never token values
    args_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
