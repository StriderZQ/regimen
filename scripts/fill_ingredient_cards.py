#!/usr/bin/env python
"""Create product cards for recipe ingredients that have none.

A recipe in the book means the owner cooks it. So its ingredients are products he uses,
even when they are not in the fridge today. That is what a catalogue is for: it describes
products, not balances.

Values come from the reference table and are marked as reference. A label or a shop
declaration always outranks them, and this script never touches those.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.domain.naming import names_match, slugify  # noqa: E402
from app.models import CanonicalProduct, NutritionSource, Principal  # noqa: E402
from app.models.recipes import Recipe, RecipeIngredient  # noqa: E402


def main(reference_path: str) -> int:
    table = json.loads(Path(reference_path).read_text())["значения"]

    with session_scope() as session:
        owner = session.scalar(select(Principal))
        products = list(
            session.scalars(
                select(CanonicalProduct).where(CanonicalProduct.owner_id == owner.id)
            )
        )
        active = {
            r.id
            for r in session.scalars(
                select(Recipe).where(
                    Recipe.owner_id == owner.id, Recipe.is_active.is_(True)
                )
            )
        }

        created = 0
        skipped = []
        for ingredient in session.scalars(
            select(RecipeIngredient).where(RecipeIngredient.owner_id == owner.id)
        ):
            if ingredient.recipe_id not in active or ingredient.canonical_id:
                continue
            if ingredient.is_staple and not ingredient.grams:
                continue

            row = next(
                (r for r in table if names_match(ingredient.name, r["ключ"])), None
            )
            if row is None:
                skipped.append(ingredient.name)
                continue

            existing = next(
                (p for p in products if names_match(p.name, row["ключ"])), None
            )
            if existing is None:
                existing = CanonicalProduct(
                    owner_id=owner.id,
                    slug=slugify(row["ключ"]),
                    name=row["ключ"].capitalize(),
                    category="ингредиент",
                    kcal_100=row["kcal"],
                    protein_100=row["protein"],
                    fat_100=row["fat"],
                    carbs_100=row["carbs"],
                    fiber_100=row.get("fiber"),
                    nutrition_source=NutritionSource.REFERENCE,
                )
                session.add(existing)
                session.flush()
                products.append(existing)
                created += 1

            ingredient.canonical_id = existing.id

        session.flush()
        print(f"  cards created: {created}")
        if skipped:
            print(f"  with no reference value ({len(skipped)}): "
                  f"{', '.join(sorted(set(skipped))[:10])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "data/reference/nutrition.json"))
