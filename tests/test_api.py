"""An end-to-end slice: ask for stock → record a meal → the balance drops.

This is the daily cycle the whole system exists for. If it works, the rest is
scaffolding.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_client
from app.db import SessionLocal
from app.domain import pantry
from app.main import app
from app.models import (
    BatchSource,
    CanonicalProduct,
    ClientKind,
    Principal,
    Scope,
    StoragePlace,
)


@pytest.fixture
def client(session: Session, owner: Principal):
    _c, token = create_client(
        session, principal=owner, name="api-test", kind=ClientKind.TELEGRAM,
        scopes={Scope.READ, Scope.WRITE_FACTS},
    )
    session.commit()

    def override():
        s = SessionLocal()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    from app.db import get_session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {token}"
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def tvorog_batch(session: Session, owner: Principal):
    product = CanonicalProduct(
        owner_id=owner.id, slug="tvorog", name="Творог 5%", category="творог",
        kcal_100=116, protein_100=16, fat_100=5, carbs_100=1.8,
    )
    session.add(product)
    session.flush()
    batch = pantry.add_batch(
        session, owner.id, canonical=product, label="Творог 5%", grams=200,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
    )
    session.commit()
    return batch


def test_health(client):
    assert client.get("/healthz").json()["ok"] is True


def test_unauthenticated_is_refused(session: Session, owner: Principal):
    with TestClient(app) as anon:
        assert anon.get("/api/pantry").status_code == 401


def test_stock_is_visible(client, tvorog_batch):
    rows = client.get("/api/pantry").json()
    assert any(r["label"] == "Творог 5%" and r["grams_left"] == 200 for r in rows)


def test_logging_a_meal_moves_stock(client, tvorog_batch, session: Session):
    """Ate it — stock went down. No confirmation: it has already happened."""
    response = client.post("/api/diary/meal", json={
        "portions": [
            {"name": "Творог 5%", "grams": 150, "batch_id": tvorog_batch.id}
        ]
    })
    assert response.status_code == 200
    assert response.json()["protein_g"] == pytest.approx(24, abs=0.5)

    rows = client.get("/api/pantry").json()
    assert next(r for r in rows if r["batch_id"] == tvorog_batch.id)["grams_left"] == 50


def test_meal_out_does_not_touch_stock(client, tvorog_batch):
    client.post("/api/diary/meal", json={
        "portions": [{
            "name": "Lunch in a cafe", "grams": 400, "confidence": "estimated",
            "kcal": 620, "protein_g": 48, "fat_g": 42, "carbs_g": 8,
        }],
        "source": "dictated",
        "external": "restaurant",
        "external_place": "the cafe round the corner",
    })
    rows = client.get("/api/pantry").json()
    assert next(r for r in rows if r["batch_id"] == tvorog_batch.id)["grams_left"] == 200

    today = client.get("/api/diary/today").json()
    assert today["kcal"] == 620


def test_today_totals(client, tvorog_batch):
    client.post("/api/diary/meal", json={
        "portions": [{"name": "Творог", "grams": 100, "batch_id": tvorog_batch.id}]
    })
    today = client.get("/api/diary/today").json()
    assert today["protein_g"] == pytest.approx(16, abs=0.5)
    assert len(today["meals"]) == 1


def test_healthz_fails_loudly_when_the_database_is_unusable(monkeypatch):
    """Health without a database check once reported "ok" on a service with no schema."""
    from fastapi.testclient import TestClient

    import app.main as main

    class _BrokenSession:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, *_args, **_kwargs):
            raise RuntimeError("no such table: principals")

    monkeypatch.setattr(main, "SessionLocal", lambda: _BrokenSession())
    with TestClient(main.app) as client:
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["ok"] is False
    assert "principals" in response.json()["error"]


def test_healthz_is_ok_on_a_working_database():
    from fastapi.testclient import TestClient

    import app.main as main

    with TestClient(main.app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["ok"] is True
