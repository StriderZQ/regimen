"""Pantry tests. Each guards a decision settled in argument, not SQLAlchemy mechanics."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from app.domain import pantry
from app.domain.pantry import CookInput, PantryError
from app.models import (
    Batch,
    BatchSource,
    CanonicalProduct,
    ExpirySource,
    MoveKind,
    Principal,
    ShelfLifeRule,
    StockMove,
    StoragePlace,
)
from app.models.base import utcnow


@pytest.fixture
def tvorog(session: Session, owner: Principal) -> CanonicalProduct:
    product = CanonicalProduct(
        owner_id=owner.id,
        slug="tvorog-5",
        name="Творог 5%",
        category="творог",
        kcal_100=116,
        protein_100=16,
        fat_100=5,
        carbs_100=1.8,
        is_protein_base=True,
    )
    session.add(product)
    session.flush()
    return product


@pytest.fixture
def farsh(session: Session, owner: Principal) -> CanonicalProduct:
    product = CanonicalProduct(
        owner_id=owner.id, slug="farsh", name="Фарш говяжий", category="фарш говяжий",
        kcal_100=250, protein_100=17, fat_100=20, carbs_100=0,
    )
    session.add(product)
    session.flush()
    return product


@pytest.fixture
def tefteli(session: Session, owner: Principal) -> CanonicalProduct:
    product = CanonicalProduct(
        owner_id=owner.id, slug="tefteli", name="Тефтели", category="партия готовая",
        kcal_100=212, protein_100=18, fat_100=15, carbs_100=1,
    )
    session.add(product)
    session.flush()
    return product


@pytest.fixture
def shelf_rules(session: Session, owner: Principal) -> None:
    session.add_all([
        ShelfLifeRule(
            owner_id=owner.id, category="творог", days=7, days_after_opening=3, freezable=False
        ),
        ShelfLifeRule(
            owner_id=owner.id, category="фарш говяжий", days=1, days_after_opening=1,
            freezable=True, freezer_days=90,
        ),
        ShelfLifeRule(
            owner_id=owner.id, category="партия готовая", days=4, freezable=True,
            freezer_days=90, days_after_thaw=1,
        ),
    ])
    session.flush()


# --- a balance is computed, never stored ---------------------------------------------

def test_balance_is_sum_of_moves(session: Session, owner: Principal, tvorog):
    batch = pantry.add_batch(
        session, owner.id, canonical=tvorog, label="Творог 350 г", grams=350,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
    )
    assert pantry.batch_balance(session, batch.id) == 350

    pantry.consume(session, owner.id, batch_id=batch.id, grams=150)
    assert pantry.batch_balance(session, batch.id) == 200, "350 g opened, 150 g eaten, 200 g left"

    assert not hasattr(batch, "left_g"), "Остаток не должен храниться полем"


def test_cannot_consume_more_than_available(session: Session, owner: Principal, tvorog):
    """Going negative signals a double entry. That is exactly what happened once."""
    batch = pantry.add_batch(
        session, owner.id, canonical=tvorog, label="Творог", grams=200,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
    )
    with pytest.raises(PantryError, match="double entry"):
        pantry.consume(session, owner.id, batch_id=batch.id, grams=250)
    assert pantry.batch_balance(session, batch.id) == 200


def test_correction_adds_move_not_rewrites(session: Session, owner: Principal, tvorog):
    """A stocktake does not wipe history — it adds a correction."""
    batch = pantry.add_batch(
        session, owner.id, canonical=tvorog, label="Творог", grams=350,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
    )
    pantry.consume(session, owner.id, batch_id=batch.id, grams=150)
    pantry.correct(session, owner.id, batch_id=batch.id, actual_grams=180, reason="ревизия")

    assert pantry.batch_balance(session, batch.id) == 180
    moves = session.query(StockMove).filter_by(batch_id=batch.id).all()
    assert len(moves) == 3, "Приход, списание и поправка — все три видны"
    assert any(m.kind is MoveKind.CORRECTION and m.grams == -20 for m in moves)


def test_empty_batch_hidden_but_not_deleted(session: Session, owner: Principal, tvorog):
    batch = pantry.add_batch(
        session, owner.id, canonical=tvorog, label="Творог", grams=100,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
    )
    pantry.consume(session, owner.id, batch_id=batch.id, grams=100)

    assert pantry.list_stock(session, owner.id) == []
    assert len(pantry.list_stock(session, owner.id, include_empty=True)) == 1
    assert session.get(Batch, batch.id) is not None, "История съеденного не удаляется"


# --- shelf life -----------------------------------------------------------------------

def test_expiry_calculated_from_shelf_life(session: Session, owner: Principal, tvorog, shelf_rules):
    bought = utcnow()
    batch = pantry.add_batch(
        session, owner.id, canonical=tvorog, label="Творог", grams=350,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO, acquired_at=bought,
    )
    assert batch.expiry_source is ExpirySource.CALCULATED
    assert (batch.expires_at - bought).days == 7


def test_unknown_category_gives_honest_unknown(session: Session, owner: Principal, tvorog):
    """With no rule an expiry is not invented — that would be false precision."""
    batch = pantry.add_batch(
        session, owner.id, canonical=tvorog, label="Творог", grams=350,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
    )
    assert batch.expires_at is None
    assert batch.expiry_source is ExpirySource.UNKNOWN


def test_opening_shortens_expiry(session: Session, owner: Principal, tvorog, shelf_rules):
    """An opened pack lives three days, not seven. This is the problem it all started with."""
    bought = utcnow() - dt.timedelta(days=1)
    batch = pantry.add_batch(
        session, owner.id, canonical=tvorog, label="Творог", grams=350,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO, acquired_at=bought,
    )
    before = batch.expires_at
    pantry.open_package(session, owner.id, batch_id=batch.id)
    assert batch.expires_at < before
    assert (batch.expires_at - batch.opened_at).days == 3


def test_confirmed_expiry_survives_recalculation(
    session: Session, owner: Principal, tvorog, shelf_rules
):
    """A date named by the owner beats a computed one: his word outranks the table."""
    batch = pantry.add_batch(
        session, owner.id, canonical=tvorog, label="Творог", grams=350,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
        expires_at=utcnow() + dt.timedelta(days=10), expiry_source=ExpirySource.CONFIRMED,
    )
    stated = batch.expires_at
    pantry.open_package(session, owner.id, batch_id=batch.id)
    assert batch.expires_at == stated


def test_freezer_extends_expiry(session: Session, owner: Principal, farsh, shelf_rules):
    """Mince keeps a day in the fridge and three months in the freezer."""
    batch = pantry.add_batch(
        session, owner.id, canonical=farsh, label="Фарш 2 кг", grams=2000,
        place=StoragePlace.FRIDGE, source=BatchSource.MARKET,
    )
    fridge_expiry = batch.expires_at
    pantry.move_to(session, owner.id, batch_id=batch.id, place=StoragePlace.FREEZER)

    assert batch.frozen_at is not None
    assert batch.expires_at > fridge_expiry + dt.timedelta(days=80)


def test_expiring_lists_only_burning(session: Session, owner: Principal, tvorog, shelf_rules):
    now = utcnow()
    pantry.add_batch(
        session, owner.id, canonical=tvorog, label="Горит", grams=200,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
        expires_at=now + dt.timedelta(days=1), expiry_source=ExpirySource.CONFIRMED,
    )
    pantry.add_batch(
        session, owner.id, canonical=tvorog, label="Не горит", grams=350,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
        expires_at=now + dt.timedelta(days=10), expiry_source=ExpirySource.CONFIRMED,
    )
    burning = pantry.expiring(session, owner.id, within_days=2, now=now)
    assert [line.label for line in burning] == ["Горит"]


def test_fifo_orders_by_expiry(session: Session, owner: Principal, tvorog):
    """Two packs of cottage cheese: the one expiring sooner is eaten first."""
    now = utcnow()
    pantry.add_batch(
        session, owner.id, canonical=tvorog, label="Вторая пачка", grams=350,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
        expires_at=now + dt.timedelta(days=6), expiry_source=ExpirySource.CONFIRMED,
    )
    pantry.add_batch(
        session, owner.id, canonical=tvorog, label="Открытая пачка", grams=200,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
        expires_at=now + dt.timedelta(days=1), expiry_source=ExpirySource.CONFIRMED,
    )
    order = [line.label for line in pantry.fifo_batches(session, owner.id, tvorog.id)]
    assert order == ["Открытая пачка", "Вторая пачка"]


# --- cooking ---------------------------------------------------------------------------

def test_cooking_consumes_inputs_and_creates_output(
    session: Session, owner: Principal, farsh, tefteli, shelf_rules
):
    """A real case: 2 kg of mince becomes a batch of meatballs. Production, not a write-off."""
    mince = pantry.add_batch(
        session, owner.id, canonical=farsh, label="Фарш говяжий 2 кг", grams=2000,
        place=StoragePlace.FRIDGE, source=BatchSource.MARKET,
    )
    result = pantry.cook(
        session, owner.id,
        title="Тефтели говяжьи с пармезаном",
        inputs=[CookInput(batch_id=mince.id, grams=2000)],
        output_canonical=tefteli,
        output_grams=1760,
        place=StoragePlace.FRIDGE,
        portions=40,
        portion_grams=44,
    )

    assert pantry.batch_balance(session, mince.id) == 0, "Сырое списано целиком"
    assert pantry.batch_balance(session, result.output_batch.id) == 1760
    assert result.run.loss_grams == 240, "Усушка зафиксирована, а не потеряна"
    assert result.output_batch.portion_grams == 44
    assert result.consumed[0].preparation_id == result.run.id


def test_cooking_refuses_when_not_enough(session: Session, owner: Principal, farsh, tefteli):
    mince = pantry.add_batch(
        session, owner.id, canonical=farsh, label="Фарш", grams=500,
        place=StoragePlace.FRIDGE, source=BatchSource.MARKET,
    )
    with pytest.raises(PantryError, match="the batch holds"):
        pantry.cook(
            session, owner.id, title="Тефтели",
            inputs=[CookInput(batch_id=mince.id, grams=2000)],
            output_canonical=tefteli, output_grams=1700, place=StoragePlace.FRIDGE,
        )


def test_portions_reported_for_batches(session: Session, owner: Principal, tefteli):
    """The owner counts meatballs in pieces, not grams."""
    batch = pantry.add_batch(
        session, owner.id, canonical=tefteli, label="Тефтели", grams=1056,
        place=StoragePlace.FRIDGE, source=BatchSource.COOKED, portion_grams=44,
    )
    line = next(x for x in pantry.list_stock(session, owner.id) if x.batch.id == batch.id)
    assert line.portions_left == 24


# --- the macro snapshot ------------------------------------------------------------------

def test_batch_keeps_macro_snapshot(session: Session, owner: Principal, tvorog):
    """Editing a product card must not change batches already created."""
    batch = pantry.add_batch(
        session, owner.id, canonical=tvorog, label="Творог", grams=350,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
    )
    tvorog.kcal_100 = 999
    session.flush()
    assert batch.kcal_100 == 116


def test_waste_is_separate_kind(session: Session, owner: Principal, tvorog):
    """What was thrown out is visible separately — otherwise food loss is invisible."""
    batch = pantry.add_batch(
        session, owner.id, canonical=tvorog, label="Творог", grams=200,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
    )
    pantry.waste(session, owner.id, batch_id=batch.id, grams=200, reason="испортился")
    assert pantry.batch_balance(session, batch.id) == 0
    move = session.query(StockMove).filter_by(kind=MoveKind.WASTE).one()
    assert move.reason == "испортился"
