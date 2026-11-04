"""The food diary: what was eaten, and what it adds up to.

Recording a fact is not a dangerous action and needs no confirmation: it registers what
already happened. The dangerous thing is different — writing off food that was not
eaten. So the write-off follows the entry, and only for items naming a batch.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.domain import pantry
from app.models.base import utcnow
from app.models.catalog import CanonicalProduct
from app.models.diary import (
    Confidence,
    ExternalKind,
    ExternalMeal,
    ItemOrigin,
    Meal,
    MealItem,
    MealSource,
)
from app.models.pantry import Batch, StockMove


class DiaryError(Exception):
    pass


@dataclass
class Portion:
    """What was eaten. From stock or from outside, but always with a weight."""

    name: str
    grams: float
    batch_id: str | None = None
    canonical_id: str | None = None
    recipe_id: str | None = None
    portions: float | None = None
    confidence: Confidence = Confidence.WEIGHED
    # Food eaten out arrives with its macros ready: they cannot be derived from stock
    kcal: float | None = None
    protein_g: float | None = None
    fat_g: float | None = None
    carbs_g: float | None = None
    fiber_g: float | None = None


def local_date_for(moment: dt.datetime) -> str:
    """The local date, not UTC. Dinner at 22:30 belongs to its own day."""
    return moment.astimezone(get_settings().tz).date().isoformat()


MEAL_WINDOW = dt.timedelta(hours=1)


def _meal_within_window(
    session: Session, owner_id: str, day: str, eaten_at: dt.datetime
) -> Meal | None:
    """The meal this food belongs to, if one is already open.

    A meal is a sitting, not a call to the API. Anything recorded within an hour of the
    last one is the same sitting — a second helping, a forgotten salad, a course entered
    separately because it came from a cafe.
    """
    recent = session.scalars(
        select(Meal)
        .where(Meal.owner_id == owner_id, Meal.local_date == day)
        .order_by(Meal.eaten_at.desc())
    ).first()
    if recent is None:
        return None
    return recent if abs(eaten_at - recent.eaten_at) <= MEAL_WINDOW else None


def _macros_from_batch(batch: Batch, grams: float) -> dict[str, float]:
    """Macros come from the batch SNAPSHOT, not from the current product card.

    The snapshot may be empty: the batch was created before the product had any macros.
    Then the card is used — and that is the ONLY case where history looks at current
    values. Otherwise the meal is written as zeros, and the diary shows 43 g of protein
    instead of 86 while looking perfectly confident.

    With neither snapshot nor card we return None, not zero. Zero means "this food has
    no calories", which is a lie you cannot spot in a daily total.
    """
    product = batch.canonical
    per_100 = {
        "kcal": batch.kcal_100 if batch.kcal_100 is not None else (
            product.kcal_100 if product else None),
        "protein_g": batch.protein_100 if batch.protein_100 is not None else (
            product.protein_100 if product else None),
        "fat_g": batch.fat_100 if batch.fat_100 is not None else (
            product.fat_100 if product else None),
        "carbs_g": batch.carbs_100 if batch.carbs_100 is not None else (
            product.carbs_100 if product else None),
    }
    if all(value is None for value in per_100.values()):
        return {"kcal": None, "protein_g": None, "fat_g": None, "carbs_g": None,
                "fiber_g": None}

    factor = grams / 100.0
    return {
        "kcal": round((per_100["kcal"] or 0) * factor, 1),
        "protein_g": round((per_100["protein_g"] or 0) * factor, 1),
        "fat_g": round((per_100["fat_g"] or 0) * factor, 1),
        "carbs_g": round((per_100["carbs_g"] or 0) * factor, 1),
        "fiber_g": 0.0,
    }


def _macros_from_recipe(session: Session, portion: Portion) -> dict[str, float] | None:
    """Macros of something eaten by recipe.

    "Ate 8 syrniki" is not "120 g of something with cottage cheese in it": his syrniki
    have their own numbers, computed from his own ingredients. A reference entry for
    "syrniki in general" is a third higher, and the diary fills with food he never ate.

    Pieces are counted per piece when nobody weighed one.
    """
    from app.models.recipes import Recipe

    recipe = session.get(Recipe, portion.recipe_id)
    if recipe is None:
        return None

    if portion.portions and recipe.per_piece:
        k = float(portion.portions)
        return {
            "kcal": round((recipe.per_piece.get("kcal") or 0) * k, 1),
            "protein_g": round((recipe.per_piece.get("protein_g") or 0) * k, 1),
            "fat_g": round((recipe.per_piece.get("fat_g") or 0) * k, 1),
            "carbs_g": round((recipe.per_piece.get("carbs_g") or 0) * k, 1),
            "fiber_g": 0.0,
        }

    if recipe.kcal_100 is not None and portion.grams:
        k = portion.grams / 100.0
        return {
            "kcal": round((recipe.kcal_100 or 0) * k, 1),
            "protein_g": round((recipe.protein_100 or 0) * k, 1),
            "fat_g": round((recipe.fat_100 or 0) * k, 1),
            "carbs_g": round((recipe.carbs_100 or 0) * k, 1),
            "fiber_g": 0.0,
        }
    return None


def log_meal(
    session: Session,
    owner_id: str,
    *,
    portions: list[Portion],
    eaten_at: dt.datetime | None = None,
    source: MealSource = MealSource.CONFIRMED,
    ordinal: int | None = None,
    note: str | None = None,
    external: ExternalKind | None = None,
    external_place: str | None = None,
    separate: bool = False,
) -> Meal:
    """Record a meal and write off from stock whatever came from there.

    Items with no batch (cafe, delivery, guests) leave stock alone — otherwise it drifts
    negative on every trip.

    One meal is often recorded in several calls: the croissant came from a cafe and the
    rest from the pantry, or the owner remembers the salad a minute later. Each call used
    to create its own meal, so a single lunch appeared as two — twenty-eight seconds
    apart — and every rule that counts meals saw four where there were two.

    So a call within MEAL_WINDOW of the previous meal adds to it instead. `separate=True`
    forces a new one for the rare case of eating twice inside the hour.
    """
    if not portions:
        raise DiaryError("A meal with no items is not recorded")

    eaten_at = eaten_at or utcnow()
    day = local_date_for(eaten_at)

    meal = None if separate else _meal_within_window(session, owner_id, day, eaten_at)

    if meal is None:
        if ordinal is None:
            same_day = session.scalars(
                select(Meal).where(Meal.owner_id == owner_id, Meal.local_date == day)
            ).all()
            ordinal = len(same_day) + 1

        meal = Meal(
            owner_id=owner_id,
            eaten_at=eaten_at,
            local_date=day,
            ordinal=ordinal,
            source=source,
            note=note,
        )
        session.add(meal)
        session.flush()

    for portion in portions:
        if portion.grams <= 0 and not (portion.recipe_id and portion.portions):
            raise DiaryError(
                f"'{portion.name}': needs a weight, or a piece count together with a recipe"
            )

        move_id = None
        canonical_id = portion.canonical_id
        origin = ItemOrigin.EXTERNAL
        # Food from outside with not a single figure is also "unknown", not zero calories
        macros_known = portion.batch_id is not None or any(
            value is not None
            for value in (portion.kcal, portion.protein_g, portion.fat_g, portion.carbs_g)
        )
        macros = {
            "kcal": portion.kcal or 0.0,
            "protein_g": portion.protein_g or 0.0,
            "fat_g": portion.fat_g or 0.0,
            "carbs_g": portion.carbs_g or 0.0,
            "fiber_g": portion.fiber_g or 0.0,
        }

        if portion.batch_id:
            batch = session.get(Batch, portion.batch_id)
            if batch is None or batch.owner_id != owner_id:
                raise DiaryError(f"Batch {portion.batch_id} not found")
            move = pantry.consume(
                session,
                owner_id,
                batch_id=portion.batch_id,
                grams=portion.grams,
                occurred_at=eaten_at,
                meal_id=meal.id,
                weight_source=portion.confidence.value,
            )
            move_id = move.id
            origin = ItemOrigin.PANTRY
            macros = _macros_from_batch(batch, portion.grams)
            if macros["kcal"] is None:
                # Neither snapshot nor card: record the fact of eating, but mark the
                # macros unknown — otherwise they silently become zeros in the day's total
                macros_known = False
                macros = dict.fromkeys(macros, 0.0)
            # The product link comes from the batch: without it the variety rule is blind —
            # food eaten from stock never enters "what was there yesterday".
            canonical_id = canonical_id or batch.canonical_id
        elif portion.recipe_id:
            origin = ItemOrigin.RECIPE
            recipe_macros = _macros_from_recipe(session, portion)
            if recipe_macros is not None:
                macros = recipe_macros
                macros_known = True

        session.add(
            MealItem(
                owner_id=owner_id,
                meal_id=meal.id,
                origin=origin,
                stock_move_id=move_id,
                canonical_id=canonical_id,
                recipe_id=portion.recipe_id,
                name_snapshot=portion.name,
                grams=portion.grams,
                portions=portion.portions,
                confidence=portion.confidence,
                macros_known=macros_known,
                **macros,
            )
        )

    if external is not None:
        session.add(
            ExternalMeal(
                owner_id=owner_id,
                meal_id=meal.id,
                kind=external,
                place=external_place,
                confidence=Confidence.ESTIMATED,
            )
        )

    session.flush()
    recalculate(session, meal)
    return meal


def recalculate(session: Session, meal: Meal) -> Meal:
    """A meal's totals are the sum of its items. Recomputed, never edited by hand."""
    items = session.scalars(select(MealItem).where(MealItem.meal_id == meal.id)).all()
    meal.kcal = round(sum(i.kcal for i in items), 1)
    meal.protein_g = round(sum(i.protein_g for i in items), 1)
    meal.fat_g = round(sum(i.fat_g for i in items), 1)
    meal.carbs_g = round(sum(i.carbs_g for i in items), 1)
    meal.fiber_g = round(sum(i.fiber_g for i in items), 1)
    session.flush()
    return meal


def adjust_item(
    session: Session, owner_id: str, *, item_id: str, actual_grams: float
) -> MealItem:
    """A correction: "I left a hundred grams of the salad".

    The return to stock is a correcting movement, not an undo. The history stays.
    """
    item = session.get(MealItem, item_id)
    if item is None or item.owner_id != owner_id:
        raise DiaryError("Item not found")
    if actual_grams < 0:
        raise DiaryError("Weight cannot be negative")

    delta = round(actual_grams - item.grams, 2)
    if delta == 0:
        return item

    if item.stock_move_id:
        move = session.get(StockMove, item.stock_move_id)
        if move is not None:
            batch = session.get(Batch, move.batch_id)
            pantry.correct(
                session,
                owner_id,
                batch_id=batch.id,
                actual_grams=pantry.batch_balance(session, batch.id) - delta,
                reason=f"correction from a meal: {item.name_snapshot}",
            )
            scale = actual_grams / item.grams if item.grams else 0
            item.kcal = round(item.kcal * scale, 1)
            item.protein_g = round(item.protein_g * scale, 1)
            item.fat_g = round(item.fat_g * scale, 1)
            item.carbs_g = round(item.carbs_g * scale, 1)
            item.fiber_g = round(item.fiber_g * scale, 1)
    else:
        scale = actual_grams / item.grams if item.grams else 0
        item.kcal = round(item.kcal * scale, 1)
        item.protein_g = round(item.protein_g * scale, 1)
        item.fat_g = round(item.fat_g * scale, 1)
        item.carbs_g = round(item.carbs_g * scale, 1)
        item.fiber_g = round(item.fiber_g * scale, 1)

    item.grams = actual_grams
    session.flush()
    recalculate(session, session.get(Meal, item.meal_id))
    return item


@dataclass
class DayTotals:
    date: str
    kcal: float
    protein_g: float
    fat_g: float
    carbs_g: float
    fiber_g: float
    meals: list[Meal]
    # Eaten, but macros unknown. A total without this list is a lie wearing the truth's face
    unknown: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.unknown

    @property
    def warning(self) -> str | None:
        if not self.unknown:
            return None
        return (
            f"Macros are unknown for {len(self.unknown)} items: "
            + ", ".join(self.unknown[:6])
            + ". The total is understated — this is not all the food eaten."
        )


def day_totals(session: Session, owner_id: str, day: str | None = None) -> DayTotals:
    day = day or local_date_for(utcnow())
    meals = list(
        session.scalars(
            select(Meal)
            .where(Meal.owner_id == owner_id, Meal.local_date == day)
            .order_by(Meal.eaten_at)
        ).all()
    )
    return DayTotals(
        date=day,
        kcal=round(sum(m.kcal for m in meals), 1),
        protein_g=round(sum(m.protein_g for m in meals), 1),
        fat_g=round(sum(m.fat_g for m in meals), 1),
        carbs_g=round(sum(m.carbs_g for m in meals), 1),
        fiber_g=round(sum(m.fiber_g for m in meals), 1),
        meals=meals,
        unknown=[
            item.name_snapshot
            for meal in meals
            for item in meal.items
            if not item.macros_known
        ],
    )


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def day_payload(totals: DayTotals) -> dict:
    """A day as every door should report it: totals, the warning, and the meals behind them.

    It lives in the domain rather than inside one adapter. While it sat in the MCP layer,
    the Telegram bot received sums only: asked to list everything eaten today it had
    nothing to list, and asked what day it was it counted the calendar by hand and got it
    wrong. Both doors must answer from the same data.
    """
    day = dt.date.fromisoformat(totals.date)
    return {
        "date": totals.date,
        "weekday": WEEKDAYS[day.weekday()],
        "kcal": totals.kcal,
        "protein_g": totals.protein_g,
        "fat_g": totals.fat_g,
        "carbs_g": totals.carbs_g,
        "meals": len(totals.meals),
        # A total without this warning is a lie wearing the truth's face
        "complete": totals.complete,
        "unknown_macros": totals.unknown,
        "warning": totals.warning,
        "detail": [
            {
                "meal_id": meal.id,
                "eaten_at": meal.eaten_at.isoformat(),
                "kcal": meal.kcal,
                "items": [
                    {
                        "meal_item_id": item.id,
                        "name": item.name_snapshot,
                        "grams": item.grams,
                        "kcal": item.kcal,
                        "protein_g": item.protein_g,
                        "fat_g": item.fat_g,
                        "carbs_g": item.carbs_g,
                        "macros_known": item.macros_known,
                    }
                    for item in meal.items
                ],
            }
            for meal in totals.meals
        ],
    }


def protein_bases_for_day(session: Session, owner_id: str, day: str) -> set[str]:
    """Which protein bases were used — for the rule of no repeats two days running."""
    rows = session.execute(
        select(CanonicalProduct.slug)
        .join(MealItem, MealItem.canonical_id == CanonicalProduct.id)
        .join(Meal, Meal.id == MealItem.meal_id)
        .where(
            Meal.owner_id == owner_id,
            Meal.local_date == day,
            CanonicalProduct.is_protein_base.is_(True),
        )
    ).all()
    return {slug for (slug,) in rows}
