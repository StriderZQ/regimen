"""The SQLite connection.

Four correctness conditions accepted when SQLite was chosen over PostgreSQL:
  1. the database sits on a local volume, never a network filesystem — set by config
  2. transactions stay short: NEVER wait on Anthropic, Garmin, a grocery API or
     Telegram inside one. Network calls happen BEFORE the transaction opens
  3. one service owns writing, and background jobs survive SQLITE_BUSY
  4. backups go through the Online Backup API, not a live file copy (see core/backup.py)
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def _ru_lower(value: str | None) -> str | None:
    return value.lower() if isinstance(value, str) else value


def _configure_connection(dbapi_connection: sqlite3.Connection, _record) -> None:
    """PRAGMAs per connection. WAL is set once, but repeating it is harmless."""
    settings = get_settings()
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    # NORMAL together with WAL: a transaction does not wait for fsync on every commit.
    # The risk is losing the last transactions if the host loses power, which is
    # covered by a copy every 15 minutes.
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
    # Bound WAL growth: long reads must not inflate it without limit.
    cur.execute("PRAGMA wal_autocheckpoint=1000")
    cur.close()

    # SQLite folds case for ASCII only: a LIKE on a lowercased Cyrillic word does not
    # match the same word capitalised. Most names here are non-ASCII, so case folding
    # happens in Python — otherwise searching exercises and products returns nothing.
    dbapi_connection.create_function("ru_lower", 1, _ru_lower, deterministic=True)
    cur.close()


def build_engine(url: str | None = None) -> Engine:
    settings = get_settings()
    engine = create_engine(
        url or settings.database_url,
        future=True,
        # SQLite and threads: one connection per thread. The check is disabled
        # deliberately, because sessions are never reused across threads.
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )
    event.listen(engine, "connect", _configure_connection)
    return engine


engine: Engine = build_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """A short transaction. No network calls inside."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session
