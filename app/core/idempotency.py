"""An idempotency key on every incoming command.

Telegram and background jobs will inevitably retry after a timeout. A retry with the
same key must return the original result, not write a second entry into the diary.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.ops import IdempotencyKey

DEFAULT_TTL = dt.timedelta(days=7)


class IdempotencyConflict(Exception):
    """The same key with different content is a caller error, not a retry."""


def request_digest(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def lookup(session: Session, scope: str, key: str, payload: dict) -> dict | None:
    """Return the stored response if this is a retry. None if the command is new."""
    row = session.scalar(
        select(IdempotencyKey).where(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
    )
    if row is None:
        return None
    if row.expires_at <= utcnow():
        session.delete(row)
        return None
    if row.request_hash != request_digest(payload):
        raise IdempotencyConflict(
            f"Key {key} was already used with different content"
        )
    return row.response or {}


def remember(
    session: Session,
    scope: str,
    key: str,
    payload: dict,
    response: dict,
    ttl: dt.timedelta = DEFAULT_TTL,
) -> None:
    session.add(
        IdempotencyKey(
            scope=scope,
            key=key,
            request_hash=request_digest(payload),
            response=response,
            expires_at=utcnow() + ttl,
        )
    )


def purge_expired(session: Session) -> int:
    result = session.execute(
        delete(IdempotencyKey).where(IdempotencyKey.expires_at <= utcnow())
    )
    return result.rowcount or 0
