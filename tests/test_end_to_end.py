"""End-to-end scenarios from the verification section of the plan.

What is checked here is not the code in pieces but the reason it exists: the chain
"bought → in stock → eaten → balance dropped → the menu knows". If something breaks
between layers, ordinary tests miss it and these do not.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from app.domain import diary, menu, pantry, recipes, training
from app.domain.diary import Portion
from app.integrations.silpo import importer
from app.models import (
    BatchSource,
    CanonicalProduct,
    Confidence,
    ExternalKind,
    MealSource,
    Principal,
    StoragePlace,
    StoreItem,
)
from app.models.base import utcnow
from app.models.policies import Policy, PolicyKind

TODAY = dt.date(2026, 9, 13)


def _setup_catalogue(session: Session, owner: Principal) -> dict:
    made = {}
    for slug, name, cat, macros, base in [
        ("kurica", "Куриное филе", "куриная грудка", (110, 23, 1.8, 0), True),
        ("tvorog", "Творог 5%", "творог", (121, 16, 5, 3), True),
        ("ogurcy", "Огурцы", "огурцы", (15, 0.7, 0.1, 2.9), False),
        ("maslo", "Оливковое масло", "масло растительное", (884, 0, 100, 0), False),
    ]:
        kcal, protein, fat, carbs = macros
        product = CanonicalProduct(
            owner_id=owner.id, slug=slug, name=name, category=cat, kcal_100=kcal,
            protein_100=protein, fat_100=fat, carbs_100=carbs, is_protein_base=base,
        )
        session.add(product)
        session.flush()
        made[slug] = product
    session.add(
        Policy(owner_id=owner.id, kind=PolicyKind.MACROS, active_from=utcnow(),
               value={"target_protein_g": [160, 170], "target_fat_g": [125, 135],
                      "daily_calories": [2000, 2200]})
    )
    session.flush()
    return made


def test_receipt_to_pantry_to_meal_to_menu(session: Session, owner: Principal):
    """The full circle: receipt → stock → eaten → balance → the menu sees the new picture."""
    products = _setup_catalogue(session, owner)
    store_item = StoreItem(
        owner_id=owner.id, canonical_id=products["tvorog"].id, store="silpo",
        external_id="716286", name="Творог 5%", pack_grams=350,
    )
    session.add(store_item)
    session.flush()

    # 1. The receipt arrives and creates a batch by itself
    report = importer.ImportReport()
    importer.import_receipt(
        session, owner.id,
        {"id": "R-1", "date": "2026-09-12T16:00:00",
         "items": [{"lager_id": 716286, "name": "Творог 5%", "unit": "350г", "qty": 1,
                    "price": 60.0}]},
        report,
    )
    assert report.batches == 1
    stock = pantry.list_stock(session, owner.id)
    tvorog = next(x for x in stock if "Творог" in x.label)
    assert tvorog.grams_left == 350

    # 2. A repeat import adds nothing
    again = importer.ImportReport()
    importer.import_receipt(
        session, owner.id,
        {"id": "R-1", "date": "2026-09-12T16:00:00", "items": []}, again,
    )
    assert again.receipts_new == 0

    # 3. "Ate 150" — the balance drops rather than zeroing. That was the original pain.
    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Творог", grams=150, batch_id=tvorog.batch.id)],
        source=MealSource.CONFIRMED,
    )
    assert pantry.batch_balance(session, tvorog.batch.id) == 200

    # 4. The menu knows the balance and does not offer the eaten food again in full
    proposal = menu.propose(session, owner.id, day=TODAY)
    planned = sum(
        d.grams for m in proposal.meals for d in m.dishes if d.batch_id == tvorog.batch.id
    )
    assert planned <= 200


def test_eating_out_does_not_touch_stock(session: Session, owner: Principal):
    """"Had lunch in a cafe" — the diary is written, the pantry is untouched."""
    products = _setup_catalogue(session, owner)
    batch = pantry.add_batch(
        session, owner.id, canonical=products["kurica"], label="Куриное филе", grams=600,
        place=StoragePlace.FRIDGE, source=BatchSource.MARKET,
    )
    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Стейк", grams=300, kcal=600, protein_g=60,
                          confidence=Confidence.ESTIMATED)],
        source=MealSource.DICTATED, external=ExternalKind.RESTAURANT,
        external_place="кафе",
    )
    assert pantry.batch_balance(session, batch.id) == 600
    assert diary.day_totals(session, owner.id).protein_g == 60


def test_cooking_moves_stock_from_raw_to_batch(session: Session, owner: Principal):
    """"Made meatballs": the raw input is consumed, the cooked batch created with pieces."""
    products = _setup_catalogue(session, owner)
    raw = pantry.add_batch(
        session, owner.id, canonical=products["kurica"], label="Куриное филе", grams=1000,
        place=StoragePlace.FRIDGE, source=BatchSource.MARKET,
    )
    result = pantry.cook(
        session, owner.id, title="Котлеты куриные",
        inputs=[pantry.CookInput(batch_id=raw.id, grams=1000)],
        output_canonical=products["kurica"], output_grams=850,
        place=StoragePlace.FRIDGE, portions=10,
    )
    assert pantry.batch_balance(session, raw.id) == 0
    assert pantry.batch_balance(session, result.output_batch.id) == 850
    # Cooking loss is not theft: the difference is visible separately
    assert result.run.loss_grams == 150


def test_writeoff_happens_when_he_says_so(session: Session, owner: Principal, identity):
    """Said "write it off" — written off. No pages, no links, no second round."""
    products = _setup_catalogue(session, owner)
    batch = pantry.add_batch(
        session, owner.id, canonical=products["tvorog"], label="Творог", grams=350,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
    )

    pantry.waste(session, owner.id, batch_id=batch.id, grams=100, reason="просрочено")

    assert pantry.batch_balance(session, batch.id) == 250
    # The history reads back: both the purchase and the disposal are visible
    from app.models import MoveKind, StockMove

    kinds = [m.kind for m in session.query(StockMove).filter(
        StockMove.batch_id == batch.id).all()]
    assert MoveKind.PURCHASE in kinds and MoveKind.WASTE in kinds


def test_editing_a_product_does_not_rewrite_history(session: Session, owner: Principal):
    """Editing a product card does not change a recorded day. This is about trusting history."""
    products = _setup_catalogue(session, owner)
    batch = pantry.add_batch(
        session, owner.id, canonical=products["tvorog"], label="Творог", grams=350,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
    )
    diary.log_meal(
        session, owner.id,
        portions=[Portion(name="Творог", grams=100, batch_id=batch.id)],
        source=MealSource.CONFIRMED,
    )
    before = diary.day_totals(session, owner.id).protein_g

    products["tvorog"].protein_100 = 99
    session.flush()

    assert diary.day_totals(session, owner.id).protein_g == before


def test_gym_history_survives_the_whole_chain(session: Session, owner: Principal):
    """Logged a set — history immediately answers what weight to start from."""
    from app.models.training import Exercise

    session.add(Exercise(owner_id=owner.id, slug="squat", name="Приседания со штангой",
                         category="SQUAT", in_program=True))
    session.flush()

    training.log_set(session, owner.id, exercise_name="Приседания со штангой",
                     reps=10, weight_kg=35, day=TODAY)
    found = training.find_exercise(session, owner.id, "приседания со штангой")
    hint = training.history(session, owner.id, found).start_hint
    assert "35 kg × 10" in hint


def test_recipe_knows_what_is_missing(session: Session, owner: Principal):
    """"What can I cook" answers from the pantry, not from how good a recipe looks."""
    from app.models.recipes import Recipe, RecipeIngredient

    products = _setup_catalogue(session, owner)
    pantry.add_batch(
        session, owner.id, canonical=products["kurica"], label="Куриное филе", grams=600,
        place=StoragePlace.FRIDGE, source=BatchSource.MARKET,
    )
    recipe = Recipe(owner_id=owner.id, slug="kurica-zap", title="Курица запечённая",
                    tags=[], notes=[])
    session.add(recipe)
    session.flush()
    session.add_all([
        RecipeIngredient(owner_id=owner.id, recipe_id=recipe.id, name="куриное филе",
                         grams=600, canonical_id=products["kurica"].id),
        RecipeIngredient(owner_id=owner.id, recipe_id=recipe.id, name="соль", grams=5,
                         is_staple=True),
    ])
    session.flush()

    match = recipes.find(session, owner.id, only_cookable=True)
    assert match and match[0].recipe.title == "Курица запечённая"
