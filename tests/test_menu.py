"""Menu tests: the owner's rules checked against numbers, not against wording."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from app.domain import diary, pantry
from app.domain import menu as menu_domain
from app.domain.diary import Portion
from app.models import (
    BatchSource,
    CanonicalProduct,
    MealSource,
    Principal,
    StoragePlace,
)
from app.models.base import utcnow
from app.models.catalog import NutritionSource
from app.models.policies import Policy, PolicyKind

SUNDAY = dt.date(2026, 9, 13)
THURSDAY = dt.date(2026, 9, 17)


def _product(session, owner, slug, name, category, *, protein=20.0, fat=5.0,
             carbs=0.0, kcal=130.0, base=False, source=NutritionSource.LABEL):
    product = CanonicalProduct(
        owner_id=owner.id, slug=slug, name=name, category=category,
        kcal_100=kcal, protein_100=protein, fat_100=fat, carbs_100=carbs,
        is_protein_base=base, nutrition_source=source,
    )
    session.add(product)
    session.flush()
    return product


def _batch(session, owner, product, grams, *, days_left=None, place=StoragePlace.FRIDGE):
    return pantry.add_batch(
        session, owner.id, canonical=product, label=product.name, grams=grams,
        place=place, source=BatchSource.MARKET,
        expires_at=utcnow() + dt.timedelta(days=days_left) if days_left is not None else None,
    )


@pytest.fixture
def targets(session: Session, owner: Principal):
    session.add(
        Policy(
            owner_id=owner.id, kind=PolicyKind.MACROS, active_from=utcnow(),
            value={"target_protein_g": [160, 170], "target_fat_g": [125, 135],
                   "daily_calories": [2000, 2200]},
        )
    )
    session.add(
        Policy(
            owner_id=owner.id, kind=PolicyKind.CARB_DAYS, active_from=utcnow(),
            value={"дни": "четверг и суббота — под тренировку"},
        )
    )
    session.flush()


@pytest.fixture
def full_pantry(session: Session, owner: Principal, targets):
    chicken = _product(session, owner, "kurica", "Куриное филе", "куриная грудка",
                       protein=23, fat=1.8, kcal=110, base=True)
    tvorog = _product(session, owner, "tvorog", "Творог 5%", "творог",
                      protein=16, fat=5, carbs=3, kcal=121, base=True)
    cucumber = _product(session, owner, "ogurcy", "Огурцы", "огурцы",
                        protein=0.7, fat=0.1, carbs=2.9, kcal=15)
    oil = _product(session, owner, "oil", "Оливковое масло", "масло растительное",
                   protein=0, fat=100, carbs=0, kcal=884)
    grain = _product(session, owner, "grechka", "Гречневая крупа", "крупы",
                     protein=13, fat=3.4, carbs=62, kcal=343)
    return {
        "chicken": _batch(session, owner, chicken, 800, days_left=3),
        "tvorog": _batch(session, owner, tvorog, 350, days_left=5),
        "cucumber": _batch(session, owner, cucumber, 600, days_left=6),
        "oil": _batch(session, owner, oil, 700),
        "grain": _batch(session, owner, grain, 900, place=StoragePlace.PANTRY),
    }


def _yesterday_of(day: dt.date) -> dt.datetime:
    """Noon of the previous day in UTC: it lands on yesterday's local date in any zone."""
    return dt.datetime.combine(day - dt.timedelta(days=1), dt.time(12, 0), tzinfo=dt.UTC)


def _all_dishes(menu):
    return [d for meal in menu.meals for d in meal.dishes]


def test_menu_never_promises_more_than_is_left(session, owner, full_pantry):
    """The main check: a plan cannot promise more than the fridge holds."""
    menu = menu_domain.propose(session, owner.id, day=SUNDAY)
    planned: dict[str, float] = {}
    for dish in _all_dishes(menu):
        planned[dish.batch_id] = planned.get(dish.batch_id, 0) + dish.grams
    for batch_id, grams in planned.items():
        assert grams <= pantry.batch_balance(session, batch_id) + 1e-6


def test_menu_does_not_touch_stock(session, owner, full_pantry):
    """A plan is not a fact. After a proposal the pantry must be untouched."""
    before = {b.id: pantry.batch_balance(session, b.id) for b in full_pantry.values()}
    menu_domain.propose(session, owner.id, day=SUNDAY)
    after = {b.id: pantry.batch_balance(session, b.id) for b in full_pantry.values()}
    assert before == after


def test_burning_item_goes_first(session, owner, targets, full_pantry):
    """Expiry beats everything: a product on its last day must reach the plate."""
    burning = _product(session, owner, "tefteli", "Тефтели говяжьи", "партия готовая",
                       protein=17, fat=14, carbs=3, kcal=205, base=True)
    batch = _batch(session, owner, burning, 400, days_left=1)

    menu = menu_domain.propose(session, owner.id, day=SUNDAY)

    assert batch.id in {d.batch_id for d in _all_dishes(menu)}
    assert any("going bad" in d.reason for d in _all_dishes(menu))
    assert any("Тефтели" in line for line in menu.burning)


def test_two_meals_use_different_protein(session, owner, full_pantry):
    """Variety within a day: the same base does not go into both meals."""
    menu = menu_domain.propose(session, owner.id, day=SUNDAY)
    proteins = [
        d.label for meal in menu.meals for d in meal.dishes if d.role == "protein"
    ]
    assert len(proteins) == 2
    assert proteins[0] != proteins[1]


def test_yesterdays_protein_is_avoided(session, owner, full_pantry):
    """Cottage cheese yesterday means it does not lead today, however much there is."""
    tvorog = session.get(type(full_pantry["tvorog"]), full_pantry["tvorog"].id)
    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Творог 5%", grams=200, batch_id=tvorog.id)],
        source=MealSource.CONFIRMED,
        eaten_at=_yesterday_of(SUNDAY),
    )
    menu = menu_domain.propose(session, owner.id, day=SUNDAY)
    first_protein = next(d for d in menu.meals[0].dishes if d.role == "protein")
    assert "Творог" not in first_protein.label


def test_big_stock_is_not_a_reason(session, owner, targets):
    """Stock is not a reason. Yesterday's base does not return just because there is a lot."""
    много = _product(session, owner, "yaica", "Яйца куриные", "куриная грудка",
                     protein=12.6, fat=9.5, carbs=0.7, kcal=143, base=True)
    мало = _product(session, owner, "kurica", "Куриное филе", "куриная грудка",
                    protein=23, fat=1.8, kcal=110, base=True)
    big = _batch(session, owner, много, 3000)
    _batch(session, owner, мало, 400)
    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Яйца", grams=300, batch_id=big.id)],
        source=MealSource.CONFIRMED, eaten_at=_yesterday_of(SUNDAY),
    )
    menu = menu_domain.propose(session, owner.id, day=SUNDAY)
    first_protein = next(d for d in menu.meals[0].dishes if d.role == "protein")
    assert "Яйца" not in first_protein.label


def test_meal_is_complete_dish(session, owner, full_pantry):
    """Not bare protein: the plate must carry vegetables and fat."""
    menu = menu_domain.propose(session, owner.id, day=SUNDAY)
    for meal in menu.meals:
        roles = {d.role for d in meal.dishes}
        assert "protein" in roles
        assert "vegetable" in roles
        assert "fat" in roles


def test_first_meal_is_bigger(session, owner, full_pantry):
    """60/40 in favour of the first meal is a guide, but the direction holds."""
    menu = menu_domain.propose(session, owner.id, day=SUNDAY)
    assert menu.meals[0].protein_g > menu.meals[1].protein_g


def test_no_carbs_on_ordinary_day(session, owner, full_pantry):
    menu = menu_domain.propose(session, owner.id, day=SUNDAY)
    assert not menu.carb_day
    assert all(d.role != "carb" for d in _all_dishes(menu))
    assert any("not a carb day" in n for n in menu.notes)


def test_carbs_appear_on_carb_day(session, owner, full_pantry):
    menu = menu_domain.propose(session, owner.id, day=THURSDAY)
    assert menu.carb_day
    carbs = [d for d in _all_dishes(menu) if d.role == "carb"]
    assert carbs and "fuel for a session" in carbs[0].reason


def test_protein_shortfall_is_said_aloud(session, owner, targets):
    """A shortfall is not spread across portions — it is named as a number."""
    chicken = _product(session, owner, "kurica", "Куриное филе", "куриная грудка",
                       protein=23, fat=1.8, kcal=110, base=True)
    _batch(session, owner, chicken, 200)
    menu = menu_domain.propose(session, owner.id, day=SUNDAY)
    assert any("short" in w for w in menu.warnings)


def test_empty_pantry_says_so(session, owner, targets):
    menu = menu_domain.propose(session, owner.id, day=SUNDAY)
    assert menu.warnings
    assert "empty" in menu.warnings[0]
    assert _all_dishes(menu) == []


def test_missing_vegetables_are_named(session, owner, targets):
    chicken = _product(session, owner, "kurica", "Куриное филе", "куриная грудка",
                       protein=23, fat=1.8, kcal=110, base=True)
    _batch(session, owner, chicken, 900)
    menu = menu_domain.propose(session, owner.id, day=SUNDAY)
    assert any("No vegetables" in n for meal in menu.meals for n in meal.notes)


def test_reference_macros_are_marked_as_estimate(session, owner, targets):
    """A reference value is never passed off as a label."""
    chicken = _product(session, owner, "kurica", "Куриное филе", "куриная грудка",
                       protein=23, base=True, source=NutritionSource.REFERENCE)
    _batch(session, owner, chicken, 600)
    menu = menu_domain.propose(session, owner.id, day=SUNDAY)
    assert any(d.estimated for d in _all_dishes(menu))
    assert any("estimate" in n.lower() for n in menu.notes)


def test_excluded_batch_is_not_offered(session, owner, full_pantry):
    """menu_swap: the owner refused a dish — it does not come back the same day."""
    chicken_id = full_pantry["chicken"].id
    menu = menu_domain.propose(
        session, owner.id, day=SUNDAY, exclude_batches={chicken_id}
    )
    assert chicken_id not in {d.batch_id for d in _all_dishes(menu)}
