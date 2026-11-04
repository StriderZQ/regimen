"""Diary tests: recording a fact, writing off stock, food eaten out."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from app.config import get_settings
from app.domain import diary, pantry
from app.domain.diary import DiaryError, Portion
from app.models import (
    BatchSource,
    CanonicalProduct,
    Confidence,
    ExternalKind,
    ItemOrigin,
    MealSource,
    Principal,
    StoragePlace,
)
from app.models.base import utcnow


@pytest.fixture
def kuritsa(session: Session, owner: Principal) -> CanonicalProduct:
    product = CanonicalProduct(
        owner_id=owner.id, slug="kurinoe-file", name="Куриное филе",
        category="куриная грудка", kcal_100=110, protein_100=23, fat_100=1.5,
        carbs_100=0, is_protein_base=True,
    )
    session.add(product)
    session.flush()
    return product


@pytest.fixture
def chicken_batch(session: Session, owner: Principal, kuritsa):
    return pantry.add_batch(
        session, owner.id, canonical=kuritsa, label="Куриное филе 608 г", grams=608,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
    )


def test_meal_consumes_from_pantry(session: Session, owner: Principal, chicken_batch):
    """Ate it — stock went down. Exactly the cycle everything is built for."""
    meal = diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Куриное филе", grams=320, batch_id=chicken_batch.id)],
    )
    assert pantry.batch_balance(session, chicken_batch.id) == 288
    assert meal.protein_g == pytest.approx(73.6, abs=0.1)
    assert meal.kcal == pytest.approx(352, abs=1)


def test_macros_come_from_batch_snapshot(
    session: Session, owner: Principal, kuritsa, chicken_batch
):
    """Editing a product card does not change a meal already recorded."""
    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Куриное филе", grams=100, batch_id=chicken_batch.id)],
    )
    kuritsa.kcal_100 = 9999
    session.flush()

    totals = diary.day_totals(session, owner.id)
    assert totals.kcal == pytest.approx(110, abs=1), "История не переписывается задним числом"


def test_meal_out_does_not_touch_pantry(session: Session, owner: Principal, chicken_batch):
    """Lunch in a cafe: the day adds up, the pantry is untouched."""
    before = pantry.batch_balance(session, chicken_batch.id)
    meal = diary.log_meal(
        session, owner.id,
        portions=[Portion(
            name="Мясо и салат в кафе", grams=400, confidence=Confidence.ESTIMATED,
            kcal=620, protein_g=48, fat_g=42, carbs_g=8,
        )],
        source=MealSource.DICTATED,
        external=ExternalKind.RESTAURANT,
        external_place="кафе у дома",
    )
    assert pantry.batch_balance(session, chicken_batch.id) == before
    assert meal.kcal == 620
    assert meal.items[0].origin is ItemOrigin.EXTERNAL
    assert meal.items[0].confidence is Confidence.ESTIMATED


def test_mixed_meal_pantry_plus_external(session: Session, owner: Principal, chicken_batch):
    """Half from stock, half from outside — ordinary life, not an edge case."""
    meal = diary.log_meal(
        session, owner.id,
        portions=[
            Portion(name="Куриное филе", grams=200, batch_id=chicken_batch.id),
            Portion(name="Хлеб у друзей", grams=60, kcal=150, protein_g=5, fat_g=1, carbs_g=30),
        ],
    )
    assert pantry.batch_balance(session, chicken_batch.id) == 408
    assert len(meal.items) == 2
    assert meal.kcal == pytest.approx(220 + 150, abs=1)


def test_adjustment_returns_food_to_pantry(session: Session, owner: Principal, chicken_batch):
    """"Left a hundred grams" — the balance returns and the macros are recomputed."""
    meal = diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Куриное филе", grams=300, batch_id=chicken_batch.id)],
    )
    assert pantry.batch_balance(session, chicken_batch.id) == 308

    diary.adjust_item(session, owner.id, item_id=meal.items[0].id, actual_grams=200)

    assert pantry.batch_balance(session, chicken_batch.id) == 408, "Сто граммов вернулись"
    session.refresh(meal)
    assert meal.protein_g == pytest.approx(46, abs=0.5)


def test_local_date_not_utc(session: Session, owner: Principal, chicken_batch):
    """A late dinner belongs to its own day, not to the next one by UTC."""
    late = dt.datetime(2026, 9, 12, 22, 30, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    meal = diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Куриное филе", grams=100, batch_id=chicken_batch.id)],
        eaten_at=late,
    )
    assert meal.local_date == "2026-09-12"
    assert meal.eaten_at.astimezone(dt.UTC).hour == 19


def test_day_totals_sum_all_meals(session: Session, owner: Principal, chicken_batch):
    """A day's total is a calendar day: lunch and dinner add up whatever the clock says.

    The test used to log "now" and "now plus four hours", so after 20:00 local time the
    second meal crossed midnight and the day honestly split in two. The test was wrong,
    not the product: a day is counted by local date, and that is correct.
    """
    tz = get_settings().tz
    today = utcnow().astimezone(tz).date()
    lunch = dt.datetime.combine(today, dt.time(13, 0), tzinfo=tz)
    dinner = dt.datetime.combine(today, dt.time(17, 30), tzinfo=tz)

    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Филе", grams=100, batch_id=chicken_batch.id)], eaten_at=lunch,
    )
    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Филе", grams=150, batch_id=chicken_batch.id)],
        eaten_at=dinner,
    )
    totals = diary.day_totals(session, owner.id)
    assert len(totals.meals) == 2
    assert totals.protein_g == pytest.approx(57.5, abs=0.5)
    assert [m.ordinal for m in totals.meals] == [1, 2]


def test_empty_meal_rejected(session: Session, owner: Principal):
    with pytest.raises(DiaryError, match="no items is not recorded"):
        diary.log_meal(session, owner.id, portions=[])


def test_protein_bases_tracked_for_variety(
    session: Session, owner: Principal, kuritsa, chicken_batch
):
    """Which base was used today — so tomorrow does not repeat it."""
    diary.log_meal(
        session, owner.id,
        portions=[Portion(
            name="Куриное филе", grams=200, batch_id=chicken_batch.id,
            canonical_id=kuritsa.id,
        )],
    )
    day = diary.local_date_for(utcnow())
    assert diary.protein_bases_for_day(session, owner.id, day) == {"kurinoe-file"}


def test_stock_portion_uses_the_product_when_the_batch_snapshot_is_empty(session, owner):
    """A batch created before its product had macros is no reason to write zero.

    That is how the diary showed 43 g of protein instead of 86: everything eaten from
    stock zeroed out, and the total looked perfectly confident.
    """
    product = CanonicalProduct(
        owner_id=owner.id, slug="tefteli", name="Тефтели говяжьи", category="партия готовая",
        kcal_100=205, protein_100=17, fat_100=14, carbs_100=3, is_protein_base=True,
    )
    session.add(product)
    session.flush()

    batch = pantry.add_batch(
        session, owner.id, canonical=product, label="Тефтели говяжьи", grams=880,
        place=StoragePlace.FREEZER, source=BatchSource.COOKED,
    )
    # An empty snapshot — exactly the state every batch was in after the import
    batch.kcal_100 = batch.protein_100 = batch.fat_100 = batch.carbs_100 = None
    session.flush()

    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Тефтели, 4 шт", grams=176, batch_id=batch.id)],
        source=MealSource.CONFIRMED,
    )

    totals = diary.day_totals(session, owner.id)
    assert totals.protein_g == 29.9
    assert totals.kcal == 360.8
    assert totals.complete
    assert totals.warning is None


def test_unknown_macros_are_flagged_not_zeroed(session, owner):
    """Zero means "this food has no calories". That is a lie invisible in a total."""
    product = CanonicalProduct(
        owner_id=owner.id, slug="zagadka", name="Загадочный продукт", category="прочее",
    )
    session.add(product)
    session.flush()
    batch = pantry.add_batch(
        session, owner.id, canonical=product, label="Загадочный продукт", grams=500,
        place=StoragePlace.FRIDGE, source=BatchSource.MARKET,
    )

    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Загадочный продукт", grams=200, batch_id=batch.id)],
        source=MealSource.CONFIRMED,
    )

    totals = diary.day_totals(session, owner.id)
    assert not totals.complete
    assert "Загадочный продукт" in totals.unknown
    assert "understated" in totals.warning


def test_external_food_without_any_figures_is_also_flagged(session, owner):
    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Что-то в гостях", grams=300)],
        source=MealSource.DICTATED,
    )
    totals = diary.day_totals(session, owner.id)
    assert totals.unknown == ["Что-то в гостях"]


def test_external_food_with_figures_is_not_flagged(session, owner):
    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Круассан с лососем", grams=171, kcal=482, protein_g=24)],
        source=MealSource.DICTATED,
    )
    totals = diary.day_totals(session, owner.id)
    assert totals.complete
    assert totals.protein_g == 24


def test_eating_by_recipe_uses_his_numbers_not_the_reference(session, owner):
    """"Ate 8 syrniki" means his syrniki, not "syrniki in general" from a table.

    The reference entry gives 220 kcal per 100 g; the owner's are 80 per piece. Across eight
    pieces the difference is a third, and it lands straight in the daily total.
    """
    from app.models.recipes import Recipe

    recipe = Recipe(
        owner_id=owner.id, slug="syrniki-mine", title="Сырники, исполнение владелец",
        tags=[], notes=[], yield_pieces=8,
        per_piece={"kcal": 80.0, "protein_g": 8.0, "fat_g": 4.0, "carbs_g": 3.5},
    )
    session.add(recipe)
    session.flush()

    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Сырники, 8 шт", grams=0, recipe_id=recipe.id, portions=8)],
        source=MealSource.CONFIRMED,
    )

    totals = diary.day_totals(session, owner.id)
    assert totals.kcal == 640.0
    assert totals.protein_g == 64.0
    assert totals.complete


def test_eating_part_of_a_recipe_batch_by_weight(session, owner):
    from app.models.recipes import Recipe

    recipe = Recipe(
        owner_id=owner.id, slug="gelato", title="Джелато", tags=[], notes=[],
        kcal_100=174, protein_100=8, fat_100=15, carbs_100=3,
    )
    session.add(recipe)
    session.flush()

    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Джелато", grams=200, recipe_id=recipe.id)],
        source=MealSource.CONFIRMED,
    )

    totals = diary.day_totals(session, owner.id)
    assert totals.kcal == 348
    assert totals.protein_g == 16


def test_portion_needs_weight_or_pieces(session, owner):
    import pytest as pytest_module

    with pytest_module.raises(DiaryError, match="needs a weight, or a piece count"):
        diary.log_meal(session, owner.id,
                       portions=[Portion(name="Что-то", grams=0)],
                       source=MealSource.CONFIRMED)


# --- a meal is a sitting, not a call to the API ----------------------------------------


def test_food_recorded_a_minute_later_joins_the_same_meal(session: Session, owner: Principal,
                                                          chicken_batch):
    """One lunch entered in two calls used to become two meals, 28 seconds apart.

    Every rule that counts meals then saw four where the owner had eaten twice.
    """
    tz = get_settings().tz
    now = dt.datetime.combine(utcnow().astimezone(tz).date(), dt.time(13, 0), tzinfo=tz)
    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Круасан", grams=171, kcal=482, protein_g=24)],
        eaten_at=now, external=ExternalKind.RESTAURANT, external_place="кафе",
    )
    second = diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Филе", grams=200, batch_id=chicken_batch.id)],
        eaten_at=now + dt.timedelta(seconds=30),
    )

    totals = diary.day_totals(session, owner.id)
    assert len(totals.meals) == 1, "Half a minute apart is one sitting, not two meals"
    assert len(second.items) == 2, "Both the cafe item and the pantry item are in it"
    assert second.kcal == pytest.approx(482 + 220, abs=30), "Totals cover the whole sitting"


def test_a_meal_hours_later_is_its_own(session: Session, owner: Principal, chicken_batch):
    """Lunch and dinner must stay apart — the window closes after an hour.

    Both meals are pinned to fixed hours of today: taking "now" and "now plus two hours"
    crosses midnight when the suite runs late, and the second meal lands on another day.
    """
    tz = get_settings().tz
    today = utcnow().astimezone(tz).date()
    lunch = dt.datetime.combine(today, dt.time(13, 0), tzinfo=tz)
    dinner = dt.datetime.combine(today, dt.time(17, 30), tzinfo=tz)
    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Филе", grams=100, batch_id=chicken_batch.id)], eaten_at=lunch,
    )
    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Филе", grams=150, batch_id=chicken_batch.id)],
        eaten_at=dinner,
    )

    assert len(diary.day_totals(session, owner.id).meals) == 2


def test_eating_twice_inside_the_hour_can_be_forced_apart(session: Session, owner: Principal,
                                                          chicken_batch):
    """The window is a default, not a verdict: sometimes it really is a second meal."""
    tz = get_settings().tz
    now = dt.datetime.combine(utcnow().astimezone(tz).date(), dt.time(13, 0), tzinfo=tz)
    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Филе", grams=100, batch_id=chicken_batch.id)], eaten_at=now,
    )
    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Филе", grams=100, batch_id=chicken_batch.id)],
        eaten_at=now + dt.timedelta(minutes=40), separate=True,
    )

    assert len(diary.day_totals(session, owner.id).meals) == 2
