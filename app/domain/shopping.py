"""The shopping list: what is missing against the targets, not "what people usually buy".

Computed from the macro targets and from what is already at home. The list is built
BEFORE looking at promotions: a discount may change the brand or the pack size, but

never the contents. The horizon is given in days — "how many days of protein we want
covered". Beyond that it is mostly vegetables, and stocking up on those is pointless.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.domain import menu as menu_domain
from app.domain import pantry as pantry_domain
from app.models.catalog import CanonicalProduct
from app.models.policies import PolicyKind

# How many days ahead to cover. Vegetables get less: they are not bought in advance
PROTEIN_HORIZON_DAYS = 4
VEGETABLE_HORIZON_DAYS = 3
VEG_PER_DAY_G = 500


@dataclass
class Need:
    what: str
    grams: float
    why: str
    have_grams: float = 0.0


@dataclass
class ShoppingList:
    days: int
    needs: list[Need] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "days": self.days,
            "items": [
                {
                    "what": n.what,
                    "grams": round(n.grams),
                    "have_grams": round(n.have_grams),
                    "why": n.why,
                }
                for n in self.needs
            ],
            "notes": self.notes,
        }


def _role_totals(session: Session, owner_id: str) -> dict[str, float]:
    totals: dict[str, float] = {"protein": 0.0, "vegetable": 0.0, "fat": 0.0, "carb": 0.0}
    protein_grams = 0.0
    for line in pantry_domain.list_stock(session, owner_id):
        product: CanonicalProduct | None = line.batch.canonical
        role = menu_domain._role(product)
        if role in totals:
            totals[role] += line.grams_left
        if role == "protein" and product and product.protein_100:
            protein_grams += line.grams_left * product.protein_100 / 100
    totals["protein_g_in_stock"] = protein_grams
    return totals


def _supplement_protein(session: Session, owner_id: str) -> float:
    """Protein powder is counted apart: it is a supplement to the diet, not the diet."""
    total = 0.0
    for line in pantry_domain.list_stock(session, owner_id):
        product = line.batch.canonical
        if menu_domain._role(product) == "supplement" and product and product.protein_100:
            total += line.grams_left * product.protein_100 / 100
    return total


def deficit(session: Session, owner_id: str, *, days: int = PROTEIN_HORIZON_DAYS) -> ShoppingList:
    """What is missing for `days` ahead at the current targets."""
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    macros = menu_domain._policy(session, owner_id, PolicyKind.MACROS, now)
    target_protein = menu_domain._low(macros.get("target_protein_g"), 160)

    result = ShoppingList(days=days)
    totals = _role_totals(session, owner_id)

    need_protein_g = target_protein * days
    have_protein_g = totals["protein_g_in_stock"]
    if have_protein_g < need_protein_g:
        missing_g = need_protein_g - have_protein_g
        # Converted into product weight at 20 g of protein per 100 g — typical meat,
        # cottage cheese, fish. This is an ESTIMATE, and it is named as one in `why`
        result.needs.append(
            Need(
                what="protein foods",
                grams=missing_g / 0.20,
                have_grams=have_protein_g,
                why=(
                    f"against a target of {target_protein:.0f} g of protein × {days} d, "
                    f"{missing_g:.0f} g is missing; weight estimated at 20 g per 100 g"
                ),
            )
        )

    veg_need = VEG_PER_DAY_G * min(days, VEGETABLE_HORIZON_DAYS)
    if totals["vegetable"] < veg_need:
        result.needs.append(
            Need(
                what="vegetables and herbs",
                grams=veg_need - totals["vegetable"],
                have_grams=totals["vegetable"],
                why=f"{VEG_PER_DAY_G} g a day; not bought ahead — they spoil",
            )
        )

    if totals["fat"] < 200:
        result.needs.append(
            Need(what="cooking fat", grams=500, have_grams=totals["fat"],
                 why="the fat supply is running out")
        )

    supplement_protein = _supplement_protein(session, owner_id)
    if supplement_protein > 50:
        result.notes.append(
            f"There are {supplement_protein:.0f} g of protein in powder, kept out of the "
            f"shopping maths: it is a supplement, not a meal."
        )

    burning = [
        line.label
        for line in pantry_domain.expiring(session, owner_id, within_days=2)
    ]
    if burning:
        result.notes.append(
            "Eat what is going bad before buying more: " + ", ".join(burning[:5])
        )
    if not result.needs:
        result.notes.append("Everything needed is already here — no shop trip required.")
    result.notes.append("Check promotions after the list is built: a discount changes "
                        "the brand and the pack size, not the contents.")
    return result
