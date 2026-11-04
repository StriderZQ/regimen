"""The background job queue: retries, deduplication, a failed state.

Not decoration. The receipt and Garmin syncs must survive timeouts, and a message from
Telegram must not be lost while the service is unreachable: it waits here and is applied
when the service returns. That is the answer to "what happens when the server is down" —
the fact is not lost.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import session_scope
from app.models.base import utcnow
from app.models.ops import Job, JobStatus

log = logging.getLogger(__name__)

Handler = Callable[[Session, Job], None]
_handlers: dict[str, Handler] = {}

# The pause before a retry grows: 1, 2, 4, 8, 16 minutes
BACKOFF_BASE = dt.timedelta(minutes=1)
MAX_BACKOFF = dt.timedelta(minutes=30)


def handler(kind: str) -> Callable[[Handler], Handler]:
    def decorator(fn: Handler) -> Handler:
        _handlers[kind] = fn
        return fn

    return decorator


def enqueue(
    session: Session,
    kind: str,
    payload: dict | None = None,
    *,
    dedupe_key: str | None = None,
    run_after: dt.datetime | None = None,
    max_attempts: int = 5,
) -> Job | None:
    """Enqueue a job. Returns None if an identical one is already queued."""
    job = Job(
        kind=kind,
        payload=payload or {},
        dedupe_key=dedupe_key,
        run_after=run_after or utcnow(),
        max_attempts=max_attempts,
    )
    session.add(job)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return None
    return job


def _backoff(attempts: int) -> dt.timedelta:
    delay = BACKOFF_BASE * (2 ** max(0, attempts - 1))
    return min(delay, MAX_BACKOFF)


def claim_next(session: Session) -> Job | None:
    job = session.scalar(
        select(Job)
        .where(Job.status == JobStatus.QUEUED, Job.run_after <= utcnow())
        .order_by(Job.run_after)
        .limit(1)
    )
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.started_at = utcnow()
    job.attempts += 1
    return job


def run_once() -> bool:
    """Take one job and run it. True means there was a job.

    The transaction stays short: the handler works in its own session so network calls
    never hold SQLite's write lock.
    """
    with session_scope() as session:
        job = claim_next(session)
        if job is None:
            return False
        job_id, kind = job.id, job.kind

    fn = _handlers.get(kind)
    if fn is None:
        with session_scope() as session:
            failed = session.get(Job, job_id)
            failed.status = JobStatus.DEAD
            failed.last_error = f"No handler for {kind}"
            failed.finished_at = utcnow()
        log.error("No handler for job %s", kind)
        return True

    try:
        with session_scope() as session:
            fn(session, session.get(Job, job_id))
            done = session.get(Job, job_id)
            done.status = JobStatus.DONE
            done.finished_at = utcnow()
            done.last_error = None
    except Exception as exc:  # noqa: BLE001 — a failing job must not kill the worker
        log.exception("Job %s (%s) failed", job_id, kind)
        with session_scope() as session:
            failed = session.get(Job, job_id)
            if failed.attempts >= failed.max_attempts:
                failed.status = JobStatus.DEAD
                failed.finished_at = utcnow()
            else:
                failed.status = JobStatus.QUEUED
                failed.run_after = utcnow() + _backoff(failed.attempts)
            failed.last_error = f"{type(exc).__name__}: {exc}"[:2000]
    return True


def dead_jobs(session: Session) -> list[Job]:
    """What did not work. The owner is told about it rather than left in the dark."""
    return list(session.scalars(select(Job).where(Job.status == JobStatus.DEAD)).all())
