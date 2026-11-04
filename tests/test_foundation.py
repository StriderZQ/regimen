"""Foundation tests: the reason this layer comes first.

Each test guards a mechanism that cannot be added later without rewriting everything.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from app.core.security import (
    AuthError,
    Identity,
    authenticate,
    authenticate_telegram,
    create_client,
)
from app.models import Client, ClientKind, Principal, Scope
from app.models.base import UtcDateTime

# --- time ---------------------------------------------------------------------------

def test_naive_datetime_rejected():
    """A naive datetime must break loudly instead of being stored silently.

    Otherwise travel, late-night training and daily targets drift apart unnoticed.
    """
    with pytest.raises(ValueError, match="naive datetime is refused"):
        UtcDateTime().process_bind_param(dt.datetime(2026, 9, 12, 13, 0), None)


def test_aware_datetime_normalised_to_utc():
    kyiv = dt.timezone(dt.timedelta(hours=3))
    value = dt.datetime(2026, 9, 12, 13, 0, tzinfo=kyiv)
    stored = UtcDateTime().process_bind_param(value, None)
    assert stored.tzinfo == dt.UTC
    assert stored.hour == 10


# --- access -------------------------------------------------------------------------

def test_token_authenticates_and_carries_scopes(session: Session, owner: Principal):
    client, token = create_client(
        session, principal=owner, name="claude", kind=ClientKind.CLAUDE,
        scopes={Scope.READ},
    )
    session.flush()

    ident = authenticate(session, token)
    assert ident.principal_id == owner.id
    assert ident.has(Scope.READ)
    assert not ident.has(Scope.ADMIN)


def test_token_stored_only_as_hash(session: Session, owner: Principal):
    _client, token = create_client(
        session, principal=owner, name="c", kind=ClientKind.CLAUDE, scopes={Scope.READ}
    )
    session.flush()
    stored = session.query(Client).one()
    assert token not in stored.token_hash
    assert len(stored.token_hash) == 64


def test_revoked_client_denied(session: Session, owner: Principal):
    client, token = create_client(
        session, principal=owner, name="c", kind=ClientKind.CLAUDE, scopes={Scope.READ}
    )
    session.flush()
    client.revoked_at = dt.datetime.now(dt.UTC)
    session.flush()
    with pytest.raises(AuthError):
        authenticate(session, token)


def test_admin_scope_implies_everything(owner: Principal):
    ident = Identity(
        principal_id=owner.id, client_id="x", client_kind=ClientKind.WEB,
        scopes=frozenset({Scope.ADMIN}),
    )
    assert ident.has(Scope.WRITE_FACTS)
    ident.require(Scope.READ)


def test_foreign_telegram_chat_rejected(session: Session, owner: Principal):
    create_client(
        session, principal=owner, name="tg", kind=ClientKind.TELEGRAM,
        scopes={Scope.READ}, telegram_chat_id=111,
    )
    session.flush()
    assert authenticate_telegram(session, 111).principal_id == owner.id
    with pytest.raises(AuthError):
        authenticate_telegram(session, 222)


def test_blank_env_values_mean_absent(monkeypatch):
    """An .env template with empty values must not stop the service from starting."""
    from app.config import Settings

    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "  ")

    settings = Settings()

    assert settings.telegram_owner_chat_id is None
    assert settings.anthropic_api_key is None
    assert settings.alert_webhook_url is None


def test_migrations_do_not_let_cascades_eat_child_rows(tmp_path, monkeypatch):
    """SQLite alters tables by recreating them, and a cascade takes the children along.

    That is how every ingredient of every recipe vanished: a migration added a column to
    `recipes`, alembic dropped the old table, and `ON DELETE CASCADE` fired.
    """
    import sqlite3

    db = tmp_path / "probe.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE parent (id TEXT PRIMARY KEY);
        CREATE TABLE child (
            id TEXT PRIMARY KEY,
            parent_id TEXT REFERENCES parent(id) ON DELETE CASCADE
        );
        INSERT INTO parent VALUES ('p1');
        INSERT INTO child VALUES ('c1', 'p1');
        """
    )
    conn.commit()

    def recreate(foreign_keys: str) -> int:
        conn.execute(f"PRAGMA foreign_keys={foreign_keys}")
        conn.executescript(
            """
            CREATE TABLE parent_new (id TEXT PRIMARY KEY, extra TEXT);
            INSERT INTO parent_new (id) SELECT id FROM parent;
            DROP TABLE parent;
            ALTER TABLE parent_new RENAME TO parent;
            """
        )
        conn.commit()
        return conn.execute("SELECT count(*) FROM child").fetchone()[0]

    assert recreate("ON") == 0, "with keys on, the children vanish — that was the loss"

    conn.execute("INSERT INTO child VALUES ('c2', 'p1')")
    conn.commit()
    assert recreate("OFF") == 1, "with keys off, the children stay where they are"
    conn.close()


def test_today_is_the_owners_day_not_the_servers():
    """The server runs in UTC and the owner's day ends at local midnight.

    Between 21:00 UTC and midnight the two disagree, and `date.today()` answers with
    yesterday — which is how an evening question about today's session would fetch
    yesterday's plan, and how a CI run at 21:30 UTC failed while the same code passed
    locally.
    """
    import datetime as dt

    from app.config import get_settings
    from app.models.base import local_today, utcnow

    expected = utcnow().astimezone(get_settings().tz).date()

    assert local_today() == expected
    assert isinstance(local_today(), dt.date)
