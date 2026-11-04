from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# Configuration before the app is imported: every test session gets its own temp database
_tmp = tempfile.mkdtemp(prefix="kag-tests-")
os.environ["DATA_DIR"] = _tmp
os.environ["BACKUP_DIR"] = str(Path(_tmp) / "backups")
os.environ["ENVIRONMENT"] = "test"
# The suite was written against a UTC+2/+3 zone with European DST; any such zone will do
os.environ["DISPLAY_TIMEZONE"] = "Europe/Helsinki"
os.environ["BODY_HEIGHT_CM"] = "180"

from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.security import create_client  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.models import Base, ClientKind, Principal, Scope  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema() -> Iterator[None]:
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def session() -> Iterator[Session]:
    """A clean database per test: test order must never change the result."""
    with SessionLocal() as s:
        for table in reversed(Base.metadata.sorted_tables):
            s.execute(table.delete())
        s.commit()
        yield s
        s.rollback()


@pytest.fixture
def owner(session: Session) -> Principal:
    principal = Principal(name="owner")
    session.add(principal)
    session.flush()
    return principal


@pytest.fixture
def identity(session: Session, owner: Principal):
    ident, _token = create_client(
        session,
        principal=owner,
        name="test-claude",
        kind=ClientKind.CLAUDE,
        scopes={Scope.READ, Scope.WRITE_FACTS},
    )
    session.flush()
    from app.core.security import Identity

    return Identity(
        principal_id=owner.id,
        client_id=ident.id,
        client_kind=ClientKind.CLAUDE,
        scopes=frozenset({Scope.READ, Scope.WRITE_FACTS}),
    )


@pytest.fixture
def settings():
    return get_settings()
