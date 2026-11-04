"""Searching the owner's recipes and judging "can this be cooked right now".

The question is not which recipe is prettier but **what can be made from what is at
home**. So a search returns not a list of titles but three things: what is covered, what
is missing, and how far the batch scales into the stock on hand.

Small things — salt, pepper, garlic, spices — do not count as missing: they are kept as
a presence list, without grams. Blocking a recipe over a pinch of salt is noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain import pantry as pantry_domain
from app.domain.naming import match_score, slugify
from app.domain.pantry import StockLine
from app.models.catalog import CanonicalProduct, IngredientAlias, NutritionSource
from app.models.recipes import Recipe, RecipeIngredient, RecipeSource


@dataclass
class Missing:
    name: str
    grams_needed: float | None
    grams_available: float


@dataclass
class Match:
    """A recipe measured against the pantry."""

    recipe: Recipe
    have: list[str] = field(default_factory=list)
    missing: list[Missing] = field(default_factory=list)
    scale: float = 1.0            # how many times the batch fits the balance
    limiting: str | None = None   # what exactly limits the volume

    @property
    def cookable(self) -> bool:
        return not self.missing and self.scale > 0

    @property
    def ask_variant(self) -> bool:
        return bool(self.recipe.ask_variant)

    @property
    def score(self) -> float:
        """The share of ingredients present. Used to sort "almost possible" recipes."""
        total = len(self.have) + len(self.missing)
        return len(self.have) / total if total else 0.0


def _stock_by_canonical(lines: list[StockLine]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for line in lines:
        product = line.batch.canonical
        if product is None:
            continue
        totals[product.id] = totals.get(product.id, 0.0) + line.grams_left
    return totals


def _name_match(
    session: Session, owner_id: str, ingredient: RecipeIngredient, lines: list[StockLine]
) -> float:
    """The balance for an ingredient not linked to the catalogue.

    Summing everything similar is forbidden: one phrase may resemble both a chicken
    fillet and chicken eggs, and adding the two makes a recipe look possible on food it
    does not contain. Count only when the product is identified unambiguously.
    """
    target, _options = resolve_ingredient(session, owner_id, ingredient.name)
    if target is None:
        return 0.0
    return sum(
        line.grams_left
        for line in lines
        if line.batch.canonical is not None and line.batch.canonical.id == target
    )


def match(session: Session, owner_id: str, recipe: Recipe) -> Match:
    lines = [
        line
        for line in pantry_domain.list_stock(session, owner_id)
        if line.grams_left > 0
    ]
    by_canonical = _stock_by_canonical(lines)

    result = Match(recipe=recipe)
    scales: list[tuple[float, str]] = []

    for ingredient in recipe.ingredients:
        if ingredient.is_staple:
            continue
        available = (
            by_canonical.get(ingredient.canonical_id, 0.0)
            if ingredient.canonical_id
            else _name_match(session, owner_id, ingredient, lines)
        )
        if available <= 0:
            result.missing.append(Missing(ingredient.name, ingredient.grams, 0.0))
            continue
        result.have.append(ingredient.name)
        if ingredient.grams:
            scales.append((available / ingredient.grams, ingredient.name))

    if result.missing:
        # While something is missing, computing the batch volume is meaningless: "fits
        # twice" next to "no mince" reads as "go ahead and cook".
        result.scale = 0.0
        result.limiting = f"missing: {result.missing[0].name}"
    elif scales:
        result.scale, result.limiting = min(scales, key=lambda pair: pair[0])
        result.scale = round(result.scale, 2)
    return result


def find(
    session: Session,
    owner_id: str,
    *,
    query: str | None = None,
    only_cookable: bool = False,
    limit: int = 10,
) -> list[Match]:
    """Recipes for a query, sorted by how well they assemble from stock."""
    stmt = select(Recipe).where(Recipe.owner_id == owner_id, Recipe.is_active.is_(True))
    recipes = list(session.scalars(stmt))

    if query:
        needle = query.lower().strip()
        recipes = [
            r
            for r in recipes
            if needle in r.title.lower()
            or any(needle in str(tag).lower() for tag in r.tags)
            or any(needle in ing.name.lower() for ing in r.ingredients)
        ]

    matches = [match(session, owner_id, recipe) for recipe in recipes]
    if only_cookable:
        matches = [m for m in matches if m.cookable]
    matches.sort(key=lambda m: (m.cookable, m.score, m.scale), reverse=True)
    return matches[:limit]


# How far a candidate must lead the runner-up for a link to be safe.
# Below that gap a human decides: a wrong macro costs more than one extra question.
CONFIDENT = 0.99
CLEAR_GAP = 0.3


def candidates_for(
    session: Session, owner_id: str, name: str, *, limit: int = 5
) -> list[tuple[float, CanonicalProduct]]:
    """Products that might be this ingredient, best first.

    A hint for asking a human, never grounds for linking.
    """
    products = session.scalars(
        select(CanonicalProduct).where(CanonicalProduct.owner_id == owner_id)
    ).all()
    scored = [(match_score(name, p.name), p) for p in products if match_score(name, p.name) > 0]
    scored.sort(key=lambda pair: (-pair[0], pair[1].name))
    return scored[:limit]


def resolve_ingredient(
    session: Session, owner_id: str, name: str
) -> tuple[str | None, list[tuple[float, CanonicalProduct]]]:
    """The product for an ingredient name, plus candidates when there is no answer.

    The order is:

      1. **The personal dictionary.** The owner already said which product this
         ingredient is. Never ask again.
      2. **An undisputed match.** The name matched in full and by a clear margin over
         the runner-up.
      3. **Otherwise link nothing** and return the candidates, so we can ask.

    Similarity alone never becomes a link: one phrase can resemble a chicken fillet and
    chicken eggs equally, and choosing between them is not a job for string comparison.
    string comparison.
    """
    normalized = name.strip().lower()
    alias = session.scalar(
        select(IngredientAlias).where(
            IngredientAlias.owner_id == owner_id,
            func.ru_lower(IngredientAlias.raw_name) == normalized,
        )
    )
    if alias is not None:
        alias.hits += 1
        return alias.canonical_id, []

    options = candidates_for(session, owner_id, name)
    if not options:
        return None, []

    best_score, best = options[0]
    second = options[1][0] if len(options) > 1 else 0.0
    if best_score >= CONFIDENT and (best_score - second) >= CLEAR_GAP:
        return best.id, []
    return None, options


def remember_ingredient(
    session: Session, owner_id: str, name: str, product: CanonicalProduct,
    state: str | None = None,
) -> IngredientAlias:
    """Remember the owner's answer forever and relink every recipe at once."""
    normalized = name.strip()
    alias = session.scalar(
        select(IngredientAlias).where(
            IngredientAlias.owner_id == owner_id,
            func.ru_lower(IngredientAlias.raw_name) == normalized.lower(),
        )
    )
    if alias is None:
        alias = IngredientAlias(
            owner_id=owner_id, raw_name=normalized, canonical_id=product.id, state=state
        )
        session.add(alias)
    else:
        alias.canonical_id = product.id
        alias.state = state or alias.state
    session.flush()

    for ingredient in session.scalars(
        select(RecipeIngredient).where(
            RecipeIngredient.owner_id == owner_id,
            func.ru_lower(RecipeIngredient.name) == normalized.lower(),
        )
    ):
        ingredient.canonical_id = product.id
    session.flush()
    return alias


def _find_product(session: Session, owner_id: str, name: str) -> str | None:
    product_id, _options = resolve_ingredient(session, owner_id, name)
    return product_id


def base_ingredient(recipe: Recipe) -> RecipeIngredient | None:
    """The recipe's base product. Explicitly marked, else the heaviest non-trivial one."""
    marked = next((i for i in recipe.ingredients if i.is_base), None)
    if marked is not None:
        return marked
    weighed = [i for i in recipe.ingredients if i.grams and not i.is_staple]
    return max(weighed, key=lambda i: i.grams) if weighed else None


def scaled(recipe: Recipe, base_grams: float = 1000.0) -> dict:
    """A recipe scaled to the wanted weight of the base product.

    The owner's rule: every recipe is written per 1 kg of the base product. Bought one
    and a half kilos — multiply by one and a half, and it adds up with no arithmetic in
    the head.
    """
    base = base_ingredient(recipe)
    if base is None or not base.grams:
        return {
            "base": None,
            "note": "No base product: not one ingredient carries a weight",
            "ingredients": [
                {"name": i.name, "grams": i.grams, "unit": i.unit, "staple": i.is_staple}
                for i in recipe.ingredients
            ],
        }

    factor = base_grams / base.grams
    return {
        "base": base.name,
        "base_grams": round(base_grams),
        "factor": round(factor, 3),
        "yield_pieces": round(recipe.yield_pieces * factor) if recipe.yield_pieces else None,
        "yield_grams": round(recipe.yield_grams * factor) if recipe.yield_grams else None,
        "ingredients": [
            {
                "name": i.name,
                # Small things scale too: two kilos need twice the salt
                "grams": round(i.grams * factor, 1) if i.grams else None,
                "quantity": round(i.quantity * factor, 2) if i.quantity else None,
                "unit": i.unit,
                "staple": i.is_staple,
                "base": i.is_base or i is base,
                "note": i.note,
            }
            for i in recipe.ingredients
        ],
    }


@dataclass
class ComputedMacros:
    """A dish's macros computed from its ingredients, together with what was missing."""

    grams: float
    kcal: float
    protein_g: float
    fat_g: float
    carbs_g: float
    unknown: list[str] = field(default_factory=list)
    prep_questions: list[str] = field(default_factory=list)
    # What to ask the owner: the ingredient and candidates WITH macros, so he can choose
    questions: list[dict] = field(default_factory=list)
    # How many ingredients were actually counted. Zero means no calculation happened:
    # an empty sum is not "a dish without calories"
    counted: int = 0

    @property
    def complete(self) -> bool:
        return self.counted > 0 and not self.unknown

    def per_100g(self) -> dict:
        if not self.grams:
            return {}
        k = 100.0 / self.grams
        return {
            "kcal": round(self.kcal * k, 1),
            "protein_g": round(self.protein_g * k, 1),
            "fat_g": round(self.fat_g * k, 1),
            "carbs_g": round(self.carbs_g * k, 1),
        }


def compute_macros(session: Session, recipe: Recipe) -> ComputedMacros:
    """Compute a dish's macros from its ingredients.

    Computed from product cards, and a card holds macros **as eaten**: chicken carries
    skinless values, because the owner removes the skin. So trimming losses take care of
    themselves — provided the card was filled in correctly.

    What we do not know, we do not substitute. An ingredient with no card goes into
    `unknown`, and the dish is honestly counted as incomplete.

    The yield comes from `yield_grams` when given: boiling and frying drive off water,
    and dividing the raw sum by the raw weight understates the finished dish.
    """
    result = ComputedMacros(grams=0.0, kcal=0.0, protein_g=0.0, fat_g=0.0, carbs_g=0.0)

    for ingredient in recipe.ingredients:
        if not ingredient.grams:
            if not ingredient.is_staple:
                result.unknown.append(f"{ingredient.name}: no weight given")
            continue

        product = (
            session.get(CanonicalProduct, ingredient.canonical_id)
            if ingredient.canonical_id
            else None
        )
        if product is None or product.kcal_100 is None:
            if not ingredient.is_staple:
                _resolved, options = resolve_ingredient(
                    session, recipe.owner_id, ingredient.name
                )
                if options:
                    result.questions.append(
                        {
                            "ingredient": ingredient.name,
                            "candidates": [
                                {
                                    "product_id": candidate.id,
                                    "name": candidate.name,
                                    "kcal_100": candidate.kcal_100,
                                    "protein_100": candidate.protein_100,
                                }
                                for _score, candidate in options[:5]
                            ],
                        }
                    )
                    result.unknown.append(
                        f"{ingredient.name}: not identified — "
                        + " or ".join(c.name for _s, c in options[:3])
                    )
                else:
                    result.unknown.append(f"{ingredient.name}: no product card")
            continue

        if product.prep_note:
            result.prep_questions.append(f"{product.name}: {product.prep_note}")

        k = ingredient.grams / 100.0
        result.counted += 1
        result.grams += ingredient.grams
        result.kcal += (product.kcal_100 or 0) * k
        result.protein_g += (product.protein_100 or 0) * k
        result.fat_g += (product.fat_100 or 0) * k
        result.carbs_g += (product.carbs_100 or 0) * k

    for attr in ("kcal", "protein_g", "fat_g", "carbs_g"):
        setattr(result, attr, round(getattr(result, attr), 1))

    if recipe.yield_grams:
        # The finished weight is given — count against it, not against the raw sum
        result.grams = float(recipe.yield_grams)
    return result


def publish_to_catalogue(session: Session, owner_id: str, recipe: Recipe) -> CanonicalProduct:
    """Publish a dish as a catalogue product.

    A recipe in the book means the owner cooks it. What he cooks he eats, and what he
    eats must be recordable — so the dish needs a card, and that card must come from the
    recipe rather than from a reference entry for the dish "in general".

    Macros are taken in this order: stated by the cook per 100 g → computed from the
    ingredients → per piece. With none of those the card is created empty, and the diary
    honestly says the macros are unknown.
    """
    product = session.scalar(
        select(CanonicalProduct).where(
            CanonicalProduct.owner_id == owner_id,
            CanonicalProduct.slug == f"dish-{recipe.slug}",
        )
    )
    if product is None:
        product = CanonicalProduct(
            owner_id=owner_id,
            slug=f"dish-{recipe.slug}",
            name=recipe.title,
            category="готовое блюдо",
        )
        session.add(product)

    per_100 = None
    source = None
    if recipe.kcal_100 is not None:
        per_100 = {
            "kcal": recipe.kcal_100, "protein_g": recipe.protein_100,
            "fat_g": recipe.fat_100, "carbs_g": recipe.carbs_100,
        }
        source = NutritionSource.RECIPE
    else:
        computed = compute_macros(session, recipe)
        if computed.complete and computed.grams:
            per_100 = computed.per_100g()
            source = NutritionSource.RECIPE
        elif recipe.per_piece and recipe.yield_pieces and recipe.yield_grams:
            piece = recipe.yield_grams / recipe.yield_pieces
            k = 100.0 / piece
            per_100 = {
                "kcal": round((recipe.per_piece.get("kcal") or 0) * k, 1),
                "protein_g": round((recipe.per_piece.get("protein_g") or 0) * k, 1),
                "fat_g": round((recipe.per_piece.get("fat_g") or 0) * k, 1),
                "carbs_g": round((recipe.per_piece.get("carbs_g") or 0) * k, 1),
            }
            source = NutritionSource.RECIPE

    if per_100:
        product.kcal_100 = per_100.get("kcal")
        product.protein_100 = per_100.get("protein_g")
        product.fat_100 = per_100.get("fat_g")
        product.carbs_100 = per_100.get("carbs_g")
        product.nutrition_source = source
        product.is_protein_base = (per_100.get("protein_g") or 0) >= 10

    product.notes = f"From the recipe '{recipe.title}', version {recipe.version}"
    recipe.dish_product_id = product.id
    session.flush()
    return product


def set_base(session: Session, owner_id: str, recipe_id: str, name: str) -> Recipe | None:
    """Set the base product by hand, when the heaviest ingredient is not it."""
    recipe = get(session, owner_id, recipe_id)
    if recipe is None:
        return None
    needle = name.strip().lower()
    for ingredient in recipe.ingredients:
        ingredient.is_base = needle in ingredient.name.lower()
    session.flush()
    return recipe


def add(
    session: Session,
    owner_id: str,
    *,
    title: str,
    ingredients: list[dict],
    method: str | None = None,
    yield_pieces: int | None = None,
    yield_grams: float | None = None,
    piece_grams: float | None = None,
    tags: list[str] | None = None,
    notes: list[str] | None = None,
    uncertainty: str | None = None,
    source: RecipeSource = RecipeSource.DICTATED,
    supersedes: str | None = None,
    ask_variant: bool = False,
    per_piece: dict | None = None,
    per_100g: dict | None = None,
) -> Recipe:
    """Write a recipe into the book.

    Written ONLY on the owner's explicit instruction. The cook proposes plenty; the book
    keeps what he cooked and approved — otherwise within a month it stops being different
    from a web search.

    Writing the same title again creates a new version instead of overwriting the old:
    last week's recipe must still explain last week's cooking.
    """
    if not title.strip():
        raise ValueError("A recipe must have a title")
    if not ingredients:
        raise ValueError("A recipe with no ingredients cannot be written")

    # A rewritten recipe is still the same recipe: a new version under the old slug,
    # otherwise the book accumulates three copies of one dish under different names
    slug = slugify(supersedes) if supersedes else slugify(title)
    previous = session.scalars(
        select(Recipe)
        .where(Recipe.owner_id == owner_id, Recipe.slug == slug)
        .order_by(Recipe.version.desc())
    ).first()
    if previous is not None:
        previous.is_active = False

    recipe = Recipe(
        owner_id=owner_id,
        slug=slug,
        title=title.strip(),
        source=source,
        version=(previous.version + 1) if previous else 1,
        method=method,
        tags=tags or [],
        notes=notes or [],
        yield_pieces=yield_pieces,
        yield_grams=yield_grams,
        piece_grams=piece_grams,
        uncertainty=uncertainty,
        ask_variant=ask_variant,
        per_piece=per_piece or {},
        # Macros when the cook stated them. We do not compute them from ingredients here:
        # that needs cards for all of them, and unusual ingredients have none
        kcal_100=(per_100g or {}).get("kcal"),
        protein_100=(per_100g or {}).get("protein_g"),
        fat_100=(per_100g or {}).get("fat_g"),
        carbs_100=(per_100g or {}).get("carbs_g"),
    )
    session.add(recipe)
    session.flush()

    heaviest = max(
        (i for i in ingredients if i.get("grams") and not i.get("staple")),
        key=lambda i: i["grams"],
        default=None,
    )
    for item in ingredients:
        name = str(item["name"]).strip()[:200]
        session.add(
            RecipeIngredient(
                owner_id=owner_id,
                recipe_id=recipe.id,
                # Link to the catalogue immediately: without it a recipe cannot find its
                # own products in stock and looks impossible to cook
                canonical_id=_find_product(session, owner_id, name),
                name=name,
                grams=item.get("grams"),
                quantity=item.get("quantity"),
                unit=item.get("unit") or ("g" if item.get("grams") else "pc"),
                is_staple=bool(item.get("staple")),
                is_base=bool(item.get("base")) or (heaviest is not None and item is heaviest),
                note=item.get("note"),
            )
        )
    session.flush()

    # A recipe in the book means he cooks and eats it. With no dish card the diary counts
    # what was eaten from a reference entry for the dish in general — a third off.
    publish_to_catalogue(session, owner_id, recipe)
    return recipe


def get(session: Session, owner_id: str, recipe_id: str) -> Recipe | None:
    recipe = session.get(Recipe, recipe_id)
    if recipe is None or recipe.owner_id != owner_id:
        return None
    return recipe


def portion_macros(recipe: Recipe, *, pieces: float | None = None,
                   grams: float | None = None) -> dict:
    """Macros for a portion. Pieces convert to grams through piece weight, never back.

    When the piece weight is unknown but macros were computed per piece, use those and
    say so: "6 syrniki" is an answer; an invented weight for one is not.
    """
    if grams is None and pieces is not None and not recipe.piece_grams and recipe.per_piece:
        k = float(pieces)
        return {
            "pieces": pieces,
            "grams": None,
            "kcal": round((recipe.per_piece.get("kcal") or 0) * k, 1),
            "protein_g": round((recipe.per_piece.get("protein_g") or 0) * k, 1),
            "fat_g": round((recipe.per_piece.get("fat_g") or 0) * k, 1),
            "carbs_g": round((recipe.per_piece.get("carbs_g") or 0) * k, 1),
            "note": "counted per piece: a piece was never weighed",
            "uncertainty": recipe.uncertainty,
        }
    if grams is None:
        if pieces is None or not recipe.piece_grams:
            return {}
        grams = pieces * recipe.piece_grams
    k = grams / 100.0
    return {
        "grams": round(grams, 1),
        "kcal": round((recipe.kcal_100 or 0) * k, 1),
        "protein_g": round((recipe.protein_100 or 0) * k, 1),
        "fat_g": round((recipe.fat_100 or 0) * k, 1),
        "carbs_g": round((recipe.carbs_100 or 0) * k, 1),
        "uncertainty": recipe.uncertainty,
    }
