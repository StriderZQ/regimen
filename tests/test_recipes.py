"""Recipe tests: what can be cooked right now, and what is missing."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.domain import pantry
from app.domain import recipes as recipes_domain
from app.models import BatchSource, CanonicalProduct, Principal, StoragePlace
from app.models.recipes import Recipe, RecipeIngredient, RecipeSource


def _product(session, owner, slug, name, category="прочее"):
    product = CanonicalProduct(
        owner_id=owner.id, slug=slug, name=name, category=category,
        kcal_100=200, protein_100=18, fat_100=14, carbs_100=3,
    )
    session.add(product)
    session.flush()
    return product


def _batch(session, owner, product, grams):
    return pantry.add_batch(
        session, owner.id, canonical=product, label=product.name, grams=grams,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
    )


@pytest.fixture
def tefteli(session: Session, owner: Principal) -> Recipe:
    recipe = Recipe(
        owner_id=owner.id, slug="tefteli", title="Тефтели говяжьи с пармезаном",
        source=RecipeSource.DICTATED, yield_grams=2500, yield_pieces=50, piece_grams=50,
        kcal_100=210.0, protein_100=18.0, fat_100=14.0, carbs_100=4.0,
        uncertainty="Жирность фарша неизвестна", notes=[], tags=["партия"],
    )
    session.add(recipe)
    session.flush()
    return recipe


def _ingredient(session, owner, recipe, name, grams, *, canonical=None, staple=False):
    session.add(
        RecipeIngredient(
            owner_id=owner.id, recipe_id=recipe.id, name=name, grams=grams,
            canonical_id=canonical.id if canonical else None, is_staple=staple,
        )
    )
    session.flush()


def test_missing_ingredient_is_named(session, owner, tefteli):
    _ingredient(session, owner, tefteli, "фарш говяжий", 2000)
    result = recipes_domain.match(session, owner.id, tefteli)
    assert not result.cookable
    assert [m.name for m in result.missing] == ["фарш говяжий"]


def test_staples_do_not_block_a_recipe(session, owner, tefteli):
    """A pinch of salt must not make a recipe impossible."""
    mince = _product(session, owner, "farsh", "Фарш говяжий")
    _batch(session, owner, mince, 2000)
    _ingredient(session, owner, tefteli, "фарш говяжий", 2000, canonical=mince)
    _ingredient(session, owner, tefteli, "соль", 23, staple=True)
    _ingredient(session, owner, tefteli, "чёрный перец", 4, staple=True)

    result = recipes_domain.match(session, owner.id, tefteli)
    assert result.cookable
    assert result.missing == []


def test_scale_shows_how_much_fits(session, owner, tefteli):
    """A 2 kg batch with 4 kg of mince at home — cook a double batch, and say so."""
    mince = _product(session, owner, "farsh", "Фарш говяжий")
    _batch(session, owner, mince, 4000)
    _ingredient(session, owner, tefteli, "фарш говяжий", 2000, canonical=mince)

    result = recipes_domain.match(session, owner.id, tefteli)
    assert result.scale == 2.0
    assert result.limiting == "фарш говяжий"


def test_scale_is_silent_while_something_is_missing(session, owner, tefteli):
    """"Fits twice" next to "no mince" would read as "go ahead and cook"."""
    parsley = _product(session, owner, "petrushka", "Петрушка", category="зелень")
    _batch(session, owner, parsley, 200)
    _ingredient(session, owner, tefteli, "петрушка рубленая", 32, canonical=parsley)
    _ingredient(session, owner, tefteli, "фарш говяжий", 2000)

    result = recipes_domain.match(session, owner.id, tefteli)
    assert result.scale == 0.0
    assert result.limiting is not None and "missing" in result.limiting


def test_partial_stock_limits_the_batch(session, owner, tefteli):
    mince = _product(session, owner, "farsh", "Фарш говяжий")
    _batch(session, owner, mince, 1000)
    _ingredient(session, owner, tefteli, "фарш говяжий", 2000, canonical=mince)

    result = recipes_domain.match(session, owner.id, tefteli)
    assert result.cookable
    assert result.scale == 0.5


def test_find_prefers_cookable(session, owner, tefteli):
    mince = _product(session, owner, "farsh", "Фарш говяжий")
    _batch(session, owner, mince, 2000)
    _ingredient(session, owner, tefteli, "фарш говяжий", 2000, canonical=mince)

    other = Recipe(owner_id=owner.id, slug="syrniki", title="Сырники",
                   source=RecipeSource.DICTATED, tags=[], notes=[])
    session.add(other)
    session.flush()
    _ingredient(session, owner, other, "творог 5%", 350)

    found = recipes_domain.find(session, owner.id)
    assert found[0].recipe.title == "Тефтели говяжьи с пармезаном"
    assert found[0].cookable
    assert not found[1].cookable


def test_find_by_query_matches_ingredient(session, owner, tefteli):
    _ingredient(session, owner, tefteli, "пармезан тёртый", 130)
    assert recipes_domain.find(session, owner.id, query="пармезан")
    assert not recipes_domain.find(session, owner.id, query="лосось")


def test_only_cookable_filters_out_the_rest(session, owner, tefteli):
    _ingredient(session, owner, tefteli, "фарш говяжий", 2000)
    assert recipes_domain.find(session, owner.id, only_cookable=True) == []


def test_portion_macros_from_pieces(session, owner, tefteli):
    """Six meatballs are 300 g, not "six pieces of something"."""
    macros = recipes_domain.portion_macros(tefteli, pieces=6)
    assert macros["grams"] == 300.0
    assert macros["protein_g"] == pytest.approx(54.0, abs=0.5)
    assert "Жирность фарша" in macros["uncertainty"]


def test_portion_macros_need_piece_weight(session, owner):
    """With no piece weight the macros are not invented — the answer comes back empty."""
    recipe = Recipe(owner_id=owner.id, slug="x", title="X", tags=[], notes=[],
                    source=RecipeSource.DICTATED)
    assert recipes_domain.portion_macros(recipe, pieces=3) == {}


def test_foreign_recipe_is_not_returned(session, owner, tefteli):
    other = Principal(name="Кто-то ещё")
    session.add(other)
    session.flush()
    assert recipes_domain.get(session, other.id, tefteli.id) is None
    assert recipes_domain.get(session, owner.id, tefteli.id) is tefteli


# --- the recipe book: writing and scaling to a kilogram -------------------------------

def test_recipe_is_scaled_to_one_kilo_of_the_base(session, owner):
    """The owner's rule: bought one and a half kilos, multiply by one and a half."""
    recipe = recipes_domain.add(
        session, owner.id, title="Тефтели",
        ingredients=[
            {"name": "фарш говяжий", "grams": 2000, "base": True},
            {"name": "сухари", "grams": 130},
            {"name": "соль", "grams": 23, "staple": True},
        ],
        yield_pieces=50,
    )

    view = recipes_domain.scaled(recipe)

    assert view["base"] == "фарш говяжий"
    assert view["factor"] == 0.5
    assert view["yield_pieces"] == 25
    grams = {i["name"]: i["grams"] for i in view["ingredients"]}
    assert grams["фарш говяжий"] == 1000
    assert grams["сухари"] == 65
    # Small things scale too: two kilos need twice the salt
    assert grams["соль"] == 11.5


def test_scaling_to_an_arbitrary_weight(session, owner):
    recipe = recipes_domain.add(
        session, owner.id, title="Курица",
        ingredients=[{"name": "куриное филе", "grams": 500, "base": True},
                     {"name": "масло", "grams": 20}],
    )
    view = recipes_domain.scaled(recipe, base_grams=1500)
    grams = {i["name"]: i["grams"] for i in view["ingredients"]}
    assert grams["куриное филе"] == 1500
    assert grams["масло"] == 60


def test_base_is_the_heaviest_when_not_marked(session, owner):
    recipe = recipes_domain.add(
        session, owner.id, title="Салат",
        ingredients=[{"name": "помидоры", "grams": 300},
                     {"name": "масло", "grams": 30},
                     {"name": "соль", "grams": 50, "staple": True}],
    )
    # Salt outweighs the oil here, but a staple can never be the base product
    assert recipes_domain.base_ingredient(recipe).name == "помидоры"


def test_base_can_be_corrected_by_hand(session, owner):
    recipe = recipes_domain.add(
        session, owner.id, title="Тушёная капуста",
        ingredients=[{"name": "вода", "grams": 800},
                     {"name": "капуста", "grams": 600}],
    )
    assert recipes_domain.base_ingredient(recipe).name == "вода"

    recipes_domain.set_base(session, owner.id, recipe.id, "капуста")
    assert recipes_domain.base_ingredient(recipe).name == "капуста"


def test_recipe_without_weights_says_so_instead_of_guessing(session, owner):
    recipe = recipes_domain.add(
        session, owner.id, title="Салат на глаз",
        ingredients=[{"name": "помидор"}, {"name": "огурец"}],
    )
    view = recipes_domain.scaled(recipe)
    assert view["base"] is None
    assert "No base product" in view["note"]


def test_rewriting_a_recipe_keeps_the_old_version(session, owner):
    """Last week's menu must be explainable by the recipe it was cooked from."""
    first = recipes_domain.add(
        session, owner.id, title="Сырники",
        ingredients=[{"name": "творог", "grams": 350, "base": True}], yield_pieces=6,
    )
    second = recipes_domain.add(
        session, owner.id, title="Сырники",
        ingredients=[{"name": "творог", "grams": 350, "base": True}], yield_pieces=8,
    )

    assert second.version == 2
    assert first.is_active is False
    assert second.is_active is True
    # The current version is what search returns
    found = [m.recipe.id for m in recipes_domain.find(session, owner.id, query="сырник")]
    assert found == [second.id]


def test_empty_recipe_is_refused(session, owner):
    import pytest as pytest_module

    with pytest_module.raises(ValueError, match="no ingredients"):
        recipes_domain.add(session, owner.id, title="Пустой", ingredients=[])
    with pytest_module.raises(ValueError, match="must have a title"):
        recipes_domain.add(session, owner.id, title="  ",
                           ingredients=[{"name": "что-то", "grams": 10}])


def test_per_piece_macros_when_the_piece_was_never_weighed(session, owner):
    """Nobody weighed a syrnik after frying: "6 pieces" is an answer, a guess is not."""
    from app.models.recipes import Recipe

    recipe = Recipe(
        owner_id=owner.id, slug="syrniki-x", title="Сырники", tags=[], notes=[],
        yield_pieces=8, per_piece={"kcal": 80.0, "protein_g": 8.0, "fat_g": 4.0},
    )
    session.add(recipe)
    session.flush()

    macros = recipes_domain.portion_macros(recipe, pieces=6)
    assert macros["kcal"] == 480.0
    assert macros["protein_g"] == 48.0
    assert macros["grams"] is None
    assert "never weighed" in macros["note"]


# --- name matching --------------------------------------------------------------------

def test_recipe_finds_its_products_after_he_answers_once(session, owner):
    """Two phrasings can mean one product, but only the owner knows that.

    Similar names produce a question, not a link. The answer is remembered forever.
    """
    chicken = _product(session, owner, "kurica", "Куриное филе", category="куриная грудка")
    _batch(session, owner, chicken, 800)

    recipe = recipes_domain.add(
        session, owner.id, title="Паргет",
        ingredients=[{"name": "куриная грудка", "grams": 350, "base": True}],
    )
    # Until he answers — no guessing
    assert not recipes_domain.match(session, owner.id, recipe).cookable

    recipes_domain.remember_ingredient(session, owner.id, "куриная грудка", chicken)

    result = recipes_domain.match(session, owner.id, recipe)
    assert result.cookable, f"не нашёл курицу: {[m.name for m in result.missing]}"
    assert result.scale == 2.29  # 800 г курицы на замес из 350 г


def test_answers_are_remembered_across_recipes(session, owner):
    """Answered once, it works everywhere he uses the same name."""
    cabbage = _product(session, owner, "kapusta", "Капуста цветная", category="цветная капуста")
    _batch(session, owner, cabbage, 567)

    first = recipes_domain.add(
        session, owner.id, title="Капуста запечённая",
        ingredients=[{"name": "кочан цветной капусты", "grams": 300, "base": True}],
    )
    recipes_domain.remember_ingredient(session, owner.id, "кочан цветной капусты", cabbage)

    second = recipes_domain.add(
        session, owner.id, title="Капуста тушёная",
        ingredients=[{"name": "кочан цветной капусты", "grams": 400, "base": True}],
    )

    assert second.ingredients[0].canonical_id == cabbage.id
    assert recipes_domain.match(session, owner.id, first).cookable
    assert recipes_domain.match(session, owner.id, second).cookable


def test_different_products_are_not_confused(session, owner):
    """Butter is not olive oil, and substituting one for the other is forbidden."""
    olive = _product(session, owner, "olive", "Оливковое масло для жарки",
                     category="масло растительное")
    _batch(session, owner, olive, 700)

    recipe = recipes_domain.add(
        session, owner.id, title="Паргет",
        ingredients=[{"name": "сливочное масло", "grams": 15, "base": True}],
    )
    result = recipes_domain.match(session, owner.id, recipe)

    assert [m.name for m in result.missing] == ["сливочное масло"]


def test_new_ingredients_link_to_the_catalogue(session, owner):
    """Without a link a recipe cannot see its own products in stock."""
    _product(session, owner, "tvorog", "Творог 5%", category="творог")

    recipe = recipes_domain.add(
        session, owner.id, title="Сырники",
        ingredients=[{"name": "творога 5%", "grams": 350, "base": True},
                     {"name": "неведомая трава", "grams": 5}],
    )
    linked = {i.name: i.canonical_id for i in recipe.ingredients}
    assert linked["творога 5%"] is not None
    assert linked["неведомая трава"] is None


def test_rewritten_recipe_supersedes_the_old_one(session, owner):
    """The cook rewrote the dish — that is a new version, not a second recipe."""
    first = recipes_domain.add(
        session, owner.id, title="Тефтели говяжьи",
        ingredients=[{"name": "фарш", "grams": 2000, "base": True}], yield_pieces=50,
    )
    second = recipes_domain.add(
        session, owner.id, title="Polpette di manzo",
        ingredients=[{"name": "говяжья лопатка", "grams": 1000, "base": True}],
        yield_pieces=27, supersedes="Тефтели говяжьи",
    )

    assert second.version == 2
    assert second.slug == first.slug
    assert first.is_active is False
    titles = [m.recipe.title for m in recipes_domain.find(session, owner.id)]
    assert titles == ["Polpette di manzo"]


# --- dish macros from ingredients, and the dish card ----------------------------------

def test_macros_are_computed_from_the_products_as_eaten(session, owner):
    """Chicken is sold with the skin and eaten without. The card holds "as eaten" —
    so trimming losses take care of themselves, with no separate arithmetic."""
    chicken = _product(session, owner, "kurica", "Куриное филе", category="куриная грудка")
    chicken.kcal_100, chicken.protein_100, chicken.fat_100, chicken.carbs_100 = 110, 23, 1.8, 0
    chicken.prep_note = "продаётся с кожей, кожа снимается"
    butter = _product(session, owner, "butter", "Сливочное масло")
    butter.kcal_100, butter.protein_100, butter.fat_100, butter.carbs_100 = 717, 0.9, 81, 0.1
    session.flush()

    recipe = recipes_domain.add(
        session, owner.id, title="Паргет",
        ingredients=[
            {"name": "куриное филе", "grams": 350, "base": True},
            {"name": "сливочное масло", "grams": 15},
            {"name": "соль", "grams": 3, "staple": True},
        ],
        yield_grams=250,
    )

    computed = recipes_domain.compute_macros(session, recipe)

    assert computed.complete
    assert computed.protein_g == 80.6            # 350 × 23/100 + 15 × 0.9/100
    assert computed.grams == 250                 # вес готового, а не сырого
    assert computed.per_100g()["protein_g"] == 32.2
    # A product with a trimming note raises a question instead of staying quiet
    assert any("кожа" in q for q in computed.prep_questions)


def test_missing_product_card_makes_the_dish_incomplete(session, owner):
    recipe = recipes_domain.add(
        session, owner.id, title="Загадка",
        ingredients=[{"name": "неизвестная трава", "grams": 100, "base": True}],
    )
    computed = recipes_domain.compute_macros(session, recipe)
    assert not computed.complete
    assert "no product card" in computed.unknown[0]


def test_staples_do_not_make_a_dish_incomplete(session, owner):
    rice = _product(session, owner, "ris", "Рис")
    rice.kcal_100, rice.protein_100, rice.fat_100, rice.carbs_100 = 340, 7, 1, 75
    session.flush()

    recipe = recipes_domain.add(
        session, owner.id, title="Рис",
        ingredients=[{"name": "рис", "grams": 100, "base": True},
                     {"name": "соль", "staple": True}],
    )
    assert recipes_domain.compute_macros(session, recipe).complete


def test_recipe_becomes_a_catalogue_product(session, owner):
    """A written recipe is one he cooks. What he cooks he eats. So it needs a card."""
    tvorog = _product(session, owner, "tvorog", "Творог 5%", category="творог")
    tvorog.kcal_100, tvorog.protein_100, tvorog.fat_100, tvorog.carbs_100 = 116, 16, 5, 1.8
    session.flush()

    recipe = recipes_domain.add(
        session, owner.id, title="Сырники мои",
        ingredients=[{"name": "творог 5%", "grams": 350, "base": True}],
        yield_grams=350, yield_pieces=8,
    )
    dish = recipes_domain.publish_to_catalogue(session, owner.id, recipe)

    assert dish.name == "Сырники мои"
    assert dish.category == "готовое блюдо"
    assert dish.protein_100 == 16.0
    assert dish.is_protein_base is True
    assert recipe.dish_product_id == dish.id


def test_dish_card_uses_per_piece_when_that_is_all_there_is(session, owner):
    from app.models.recipes import Recipe

    recipe = Recipe(
        owner_id=owner.id, slug="syrniki-p", title="Сырники", tags=[], notes=[],
        yield_pieces=8, yield_grams=400,
        per_piece={"kcal": 80.0, "protein_g": 8.0, "fat_g": 4.0, "carbs_g": 3.5},
    )
    session.add(recipe)
    session.flush()

    dish = recipes_domain.publish_to_catalogue(session, owner.id, recipe)

    # A 50 g piece means twice as much per 100 g
    assert dish.kcal_100 == 160.0
    assert dish.protein_100 == 16.0


def test_dish_card_stays_empty_rather_than_guessing(session, owner):
    recipe = recipes_domain.add(
        session, owner.id, title="Без цифр",
        ingredients=[{"name": "нечто", "grams": 100, "base": True}],
    )
    dish = recipes_domain.publish_to_catalogue(session, owner.id, recipe)
    assert dish.kcal_100 is None


def test_ambiguous_ingredient_asks_with_macros(session, owner):
    """The question must arrive with the candidates' macros — otherwise there is no choice."""
    filet = _product(session, owner, "kurica", "Куриное филе", category="куриная грудка")
    filet.kcal_100, filet.protein_100 = 110, 23
    eggs = _product(session, owner, "yaica", "Яйца куриные", category="яйца")
    eggs.kcal_100, eggs.protein_100 = 143, 12.6
    session.flush()

    recipe = recipes_domain.add(
        session, owner.id, title="Паргет",
        ingredients=[{"name": "куриная грудка", "grams": 350, "base": True}],
    )

    assert recipe.ingredients[0].canonical_id is None
    computed = recipes_domain.compute_macros(session, recipe)

    assert computed.questions, "должен быть вопрос с кандидатами"
    question = computed.questions[0]
    assert question["ingredient"] == "куриная грудка"
    names = {c["name"] for c in question["candidates"]}
    assert {"Куриное филе", "Яйца куриные"} <= names
    # With macros: without them the choice is blind
    assert all(c["kcal_100"] is not None for c in question["candidates"])


def test_exact_name_links_without_asking(session, owner):
    """The name matched in full — no question needed, this is not a guess."""
    filet = _product(session, owner, "kurica", "Куриное филе", category="куриная грудка")
    filet.kcal_100, filet.protein_100 = 110, 23
    session.flush()

    recipe = recipes_domain.add(
        session, owner.id, title="Паргет",
        ingredients=[{"name": "куриное филе", "grams": 350, "base": True}],
    )
    assert recipe.ingredients[0].canonical_id == filet.id


def test_near_miss_does_not_link_itself(session, owner):
    """Similar is not the same. Half the words in common is not a link."""
    filet = _product(session, owner, "kurica", "Куриное филе", category="куриная грудка")
    filet.kcal_100 = 110
    session.flush()

    recipe = recipes_domain.add(
        session, owner.id, title="Паргет",
        ingredients=[{"name": "куриная грудка", "grams": 350, "base": True}],
    )
    assert recipe.ingredients[0].canonical_id is None


def test_cookability_does_not_sum_lookalike_products(session, owner):
    """Chicken plus eggs do not make a recipe possible: they are different foods."""
    filet = _product(session, owner, "kurica", "Куриное филе", category="куриная грудка")
    filet.kcal_100, filet.protein_100 = 110, 23
    eggs = _product(session, owner, "yaica", "Яйца куриные", category="яйца")
    eggs.kcal_100, eggs.protein_100 = 143, 12.6
    session.flush()
    _batch(session, owner, filet, 200)
    _batch(session, owner, eggs, 1000)

    recipe = recipes_domain.add(
        session, owner.id, title="Паргет",
        ingredients=[{"name": "куриная грудка", "grams": 350, "base": True}],
    )
    result = recipes_domain.match(session, owner.id, recipe)

    # 200 g of chicken and 1000 g of eggs must not look like "enough for 3 batches"
    assert not result.cookable
    assert [m.name for m in result.missing] == ["куриная грудка"]
