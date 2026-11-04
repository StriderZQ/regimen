"""A menu for the day: a proposal, never a fact.

The rules here are exactly the ones the owner stated out loud, and their order is fixed:

  1. **What is going bad comes first.** Expiry beats everything else, variety included,
     and the answer says so.
  2. **A meal is a finished dish:** protein + vegetables + fat. Bare protein is not served.
  3. **Variety.** A protein base never repeats two days running. A large stock is not a
     reason to eat something daily; only an expiry date is.
  4. **60/40** in favour of the first meal — a guide, not a law.
  5. **Carbohydrates only on a carb day**, and only as fuel for a session.

A menu **never touches the pantry**. A plan is not a fact: the write-off happens when the
owner says "ate it", and not a second earlier.

A shortfall is never disguised. If the protein at home is more than 20 g below target,
that goes into `warnings` in plain words instead of being spread thinly across portions.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import pantry as pantry_domain
from app.domain.pantry import StockLine
from app.models.catalog import CanonicalProduct, NutritionSource
from app.models.diary import Meal, MealItem
from app.models.policies import Policy, PolicyKind

# Role on the plate, by catalogue category. Values are category names as stored in the DB.
VEGETABLE_CATEGORIES = frozenset(
    {"помидоры", "огурцы", "зелень", "цветная капуста", "лук репчатый", "овощи"}
)
FAT_CATEGORIES = frozenset({"масло растительное", "орехи"})
CARB_CATEGORIES = frozenset({"крупы", "хлеб"})
# Powder is a supplement, not a meal: 450 g of whey is 350 g of protein on paper and
# covers a week's target that never appears on a plate.
SUPPLEMENT_CATEGORIES = frozenset({"добавки"})

BURNING_DAYS = 2          # "going bad" means this many days to the expiry
MAX_PROTEIN_PORTION = 450  # g raw per meal: more than that is not eaten at once
MIN_PROTEIN_PORTION = 70
MAX_FAT_PORTION = 30       # g of oil per meal
VEG_TARGET_FIRST = 280     # g of vegetables in the first meal
VEG_TARGET_SECOND = 200
PROTEIN_SHORTFALL_LOUD = 20  # g: beyond this it is said out loud, not hidden in the plan


@dataclass(frozen=True)
class Dish:
    """A plate item. Always tied to a batch — otherwise there is nothing to eat."""

    batch_id: str
    label: str
    grams: float
    role: str                 # protein | vegetable | fat | carb
    reason: str               # why this one
    kcal: float
    protein_g: float
    fat_g: float
    carbs_g: float
    estimated: bool           # macros not from a label — and that shows in the answer


@dataclass
class MealPlan:
    n: int
    time: str
    dishes: list[Dish] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def kcal(self) -> float:
        return round(sum(d.kcal for d in self.dishes), 1)

    @property
    def protein_g(self) -> float:
        return round(sum(d.protein_g for d in self.dishes), 1)

    @property
    def fat_g(self) -> float:
        return round(sum(d.fat_g for d in self.dishes), 1)

    @property
    def carbs_g(self) -> float:
        return round(sum(d.carbs_g for d in self.dishes), 1)


@dataclass
class Menu:
    date: str
    meals: list[MealPlan]
    targets: dict
    burning: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    carb_day: bool = False

    @property
    def totals(self) -> dict:
        return {
            "kcal": round(sum(m.kcal for m in self.meals), 1),
            "protein_g": round(sum(m.protein_g for m in self.meals), 1),
            "fat_g": round(sum(m.fat_g for m in self.meals), 1),
            "carbs_g": round(sum(m.carbs_g for m in self.meals), 1),
        }


# --- helpers -----------------------------------------------------------------------

def _policy(session: Session, owner_id: str, kind: PolicyKind, now: dt.datetime) -> dict:
    policy = session.scalar(
        select(Policy)
        .where(Policy.owner_id == owner_id, Policy.kind == kind, Policy.active_from <= now)
        .order_by(Policy.active_from.desc())
    )
    if policy is None or (policy.active_to is not None and policy.active_to <= now):
        return {}
    return policy.value


def _low(value, fallback: float) -> float:
    """The lower bound of a target range: the goal we aim at, not a ceiling."""
    if isinstance(value, list | tuple) and value:
        return float(value[0])
    if isinstance(value, int | float):
        return float(value)
    return fallback


def _role(product: CanonicalProduct | None) -> str:
    if product is None:
        return "other"
    if product.category in SUPPLEMENT_CATEGORIES:
        return "supplement"
    if product.is_protein_base:
        return "protein"
    if product.category in FAT_CATEGORIES:
        return "fat"
    if product.category in VEGETABLE_CATEGORIES:
        return "vegetable"
    if product.category in CARB_CATEGORIES:
        return "carb"
    return "other"


def _product(line: StockLine) -> CanonicalProduct | None:
    return line.batch.canonical


def _macros(product: CanonicalProduct | None, grams: float) -> tuple[float, float, float, float]:
    if product is None:
        return (0.0, 0.0, 0.0, 0.0)
    k = grams / 100.0
    return (
        round((product.kcal_100 or 0) * k, 1),
        round((product.protein_100 or 0) * k, 1),
        round((product.fat_100 or 0) * k, 1),
        round((product.carbs_100 or 0) * k, 1),
    )


def _dish(line: StockLine, grams: float, role: str, reason: str) -> Dish:
    product = _product(line)
    kcal, protein, fat, carbs = _macros(product, grams)
    return Dish(
        batch_id=line.batch.id,
        label=line.label,
        grams=round(grams, 1),
        role=role,
        reason=reason,
        kcal=kcal,
        protein_g=protein,
        fat_g=fat,
        carbs_g=carbs,
        estimated=product is None or product.nutrition_source is not NutritionSource.LABEL,
    )


def recent_protein_bases(session: Session, owner_id: str, day: dt.date, back: int = 2) -> dict:
    """Which protein bases were used recently. Key is the slug, value is days ago."""
    days = [(day - dt.timedelta(days=i)).isoformat() for i in range(1, back + 1)]
    rows = session.execute(
        select(CanonicalProduct.slug, Meal.local_date)
        .join(MealItem, MealItem.canonical_id == CanonicalProduct.id)
        .join(Meal, Meal.id == MealItem.meal_id)
        .where(
            Meal.owner_id == owner_id,
            Meal.local_date.in_(days),
            CanonicalProduct.is_protein_base.is_(True),
        )
    ).all()
    seen: dict[str, set[str]] = {}
    for slug, date in rows:
        seen.setdefault(slug, set()).add(date)
    return {slug: len(dates) for slug, dates in seen.items()}


def is_carb_day(day: dt.date, policy: dict) -> bool:
    """A carb day comes from policy. With no policy there are no carbohydrates."""
    text = " ".join(str(v) for v in policy.values()).lower() if policy else ""
    weekday = {0: "понедельник", 1: "вторник", 2: "сред", 3: "четверг",
               4: "пятниц", 5: "суббот", 6: "воскресен"}[day.weekday()]
    return weekday in text


# --- assembly ------------------------------------------------------------------------

def _sort_key(line: StockLine) -> tuple:
    """What is going bad goes first. Then the smallest remainder, to avoid leftovers."""
    days = line.days_left if line.days_left is not None else 999
    return (days, -line.grams_left)


def _pick_protein(
    candidates: list[StockLine], used_slugs: set[str], recent: dict
) -> tuple[StockLine | None, str]:
    burning = [c for c in candidates if c.days_left is not None and c.days_left <= BURNING_DAYS]
    for line in sorted(burning, key=_sort_key):
        product = _product(line)
        if product and product.slug not in used_slugs:
            return line, f"going bad: {line.days_left} d left"

    fresh = sorted(
        (c for c in candidates if c not in burning), key=_sort_key
    )
    # Variety: first what was not eaten yesterday or the day before
    for line in fresh:
        product = _product(line)
        if product and product.slug not in used_slugs and product.slug not in recent:
            return line, "does not repeat the last few days"
    for line in fresh:
        product = _product(line)
        if product and product.slug not in used_slugs and recent.get(product.slug, 0) < 2:
            return line, "had it yesterday — fine today, but not tomorrow"
    for line in sorted(candidates, key=_sort_key):
        product = _product(line)
        if product and product.slug not in used_slugs:
            return line, "no choice left: everything else would be a third day running"
    return None, ""


def _protein_grams(line: StockLine, need_g: float, available: float) -> float:
    product = _product(line)
    per_100 = (product.protein_100 if product else None) or 0
    if per_100 <= 0:
        return min(available, 200)
    grams = need_g / (per_100 / 100.0)
    grams = min(grams, available, MAX_PROTEIN_PORTION)
    grams = max(grams, min(MIN_PROTEIN_PORTION, available))
    # Round DOWN: rounding up pushes the portion past the batch balance, and the plan
    # promises more than the fridge holds.
    snapped = math.floor(grams / 5) * 5
    return float(snapped) if snapped >= 5 else round(min(grams, available), 1)


def propose(
    session: Session,
    owner_id: str,
    *,
    day: dt.date | None = None,
    exclude_batches: set[str] | None = None,
) -> Menu:
    """Assemble a day's menu from what is actually at home."""
    now = dt.datetime.now(dt.UTC)
    day = day or now.date()
    exclude = exclude_batches or set()

    macros = _policy(session, owner_id, PolicyKind.MACROS, now)
    pattern = _policy(session, owner_id, PolicyKind.MEAL_PATTERN, now)
    carbs_policy = _policy(session, owner_id, PolicyKind.CARB_DAYS, now)

    target_protein = _low(macros.get("target_protein_g"), 160)
    target_fat = _low(macros.get("target_fat_g"), 120)
    target_kcal = _low(macros.get("daily_calories"), 2000)
    carb_day = is_carb_day(day, carbs_policy)

    menu = Menu(
        date=day.isoformat(),
        meals=[MealPlan(n=1, time="13:00"), MealPlan(n=2, time="17:30")],
        targets={
            "protein_g": target_protein,
            "fat_g": target_fat,
            "kcal": target_kcal,
            "схема": pattern.get("scheme", "два приёма, ~13:00 и ~17:30"),
        },
        carb_day=carb_day,
    )

    stock = [
        line
        for line in pantry_domain.list_stock(session, owner_id)
        if line.batch.id not in exclude and line.grams_left > 0
    ]
    if not stock:
        menu.warnings.append("The pantry is empty — nothing to propose. Stocktake or shop.")
        return menu

    by_role: dict[str, list[StockLine]] = {}
    for line in stock:
        by_role.setdefault(_role(_product(line)), []).append(line)

    menu.burning = [
        f"{line.label} — {line.days_left} d"
        for line in sorted(stock, key=_sort_key)
        if line.days_left is not None and line.days_left <= BURNING_DAYS
    ]

    recent = recent_protein_bases(session, owner_id, day)
    used_slugs: set[str] = set()
    shares = [0.6, 0.4]

    # The balance is tracked as the menu is built. Without it the same cucumber lands in
    # both meals: exactly the bug that makes stock disagree with the fridge.
    left: dict[str, float] = {line.batch.id: line.grams_left for line in stock}

    for meal, share in zip(menu.meals, shares, strict=True):
        need_protein = target_protein * share

        line, reason = _pick_protein(
            [c for c in by_role.get("protein", []) if left[c.batch.id] >= MIN_PROTEIN_PORTION],
            used_slugs,
            recent,
        )
        if line is not None:
            product = _product(line)
            if product:
                used_slugs.add(product.slug)
            grams = _protein_grams(line, need_protein, left[line.batch.id])
            left[line.batch.id] -= grams
            meal.dishes.append(_dish(line, grams, "protein", reason))
            if line.days_left is not None and line.days_left <= BURNING_DAYS:
                meal.notes.append("Expiry outranks variety — that is why this is here today.")
        else:
            meal.notes.append("No protein base was found for this meal.")

        # Vegetables: what is going bad first again, but no more than two kinds per plate
        veg_target = VEG_TARGET_FIRST if meal.n == 1 else VEG_TARGET_SECOND
        taken = 0.0
        for veg in sorted(by_role.get("vegetable", []), key=_sort_key):
            if taken >= veg_target or len([d for d in meal.dishes if d.role == "vegetable"]) >= 2:
                break
            portion = min(left[veg.batch.id] / 2, veg_target - taken, 200)
            portion = math.floor(portion / 5) * 5
            if portion < 40:
                continue
            left[veg.batch.id] -= portion
            note = (
                f"going bad: {veg.days_left} d"
                if veg.days_left is not None and veg.days_left <= BURNING_DAYS
                else "vegetables for the dish"
            )
            meal.dishes.append(_dish(veg, portion, "vegetable", note))
            taken += portion
        if taken == 0:
            meal.notes.append("No vegetables in stock — the dish comes out incomplete.")

        if carb_day and meal.n == 1:
            carb = next(iter(sorted(by_role.get("carb", []), key=_sort_key)), None)
            if carb is not None:
                carb_grams = min(70, left[carb.batch.id])
                left[carb.batch.id] -= carb_grams
                meal.dishes.append(
                    _dish(carb, carb_grams, "carb",
                          "carb day: fuel for a session, not a way to top up calories")
                )

    # Fat is added last: by then it is clear how much already came with the food
    eaten_fat = sum(m.fat_g for m in menu.meals)
    oils = sorted(by_role.get("fat", []), key=_sort_key)
    if oils:
        for meal, share in zip(menu.meals, shares, strict=True):
            deficit = target_fat * share - meal.fat_g
            if deficit <= 2:
                continue
            oil = next((o for o in oils if left[o.batch.id] >= 3), None)
            if oil is None:
                continue
            grams = round(min(deficit, MAX_FAT_PORTION, left[oil.batch.id]))
            if grams >= 3:
                left[oil.batch.id] -= grams
                meal.dishes.append(_dish(oil, grams, "fat", "topping up fat"))
    elif eaten_fat < target_fat * 0.5:
        menu.warnings.append("No oil in stock — there is nothing to top up fat with.")

    total_protein = menu.totals["protein_g"]
    if total_protein + PROTEIN_SHORTFALL_LOUD < target_protein:
        menu.warnings.append(
            f"Protein comes to {total_protein:.0f} g against a target of {target_protein:.0f} g — "
            f"{target_protein - total_protein:.0f} g short. This is not a plan, this is "
            f"what is at home."
        )

    total_kcal = menu.totals["kcal"]
    if total_kcal + 200 < target_kcal:
        menu.warnings.append(
            f"Calories come to {total_kcal:.0f} against a target of {target_kcal:.0f} — "
            f"there is not that much food at home. Nothing to top up with; buy something."
        )

    if any(d.estimated for m in menu.meals for d in m.dishes):
        menu.notes.append("Some macros are reference values, not labels. That is an estimate.")
    if menu.burning:
        menu.notes.append("Going bad first: " + "; ".join(menu.burning))
    if not carb_day:
        menu.notes.append("Today is not a carb day — the side dish is missing on purpose.")

    return menu
