"""The product catalogue, in three levels.

A single flat reference inevitably fills with duplicates: "cottage cheese 5%", "cottage
cheese 5 percent", a brand's own spelling, and a new package of the same thing. Hence:

  canonical_product — the food as the owner names and eats it: "cottage cheese 5%"
  store_item        — one product in a shop: SKU, 350 g package, brand
  item_alias        — a dirty receipt string, matched once and remembered

A receipt line is matched against a `store_item`, while menus and recipes work with
`canonical_product`. Without that split, every new package looks like a new food.

"""

from __future__ import annotations

import enum

from sqlalchemy import Boolean, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import DomainBase


class Unit(enum.StrEnum):
    GRAM = "g"
    PIECE = "pc"
    MILLILITRE = "ml"


class NutritionSource(enum.StrEnum):
    """Where the macros came from. An estimate and a label are different things."""

    LABEL = "label"          # from the shop's label
    RECIPE = "recipe"        # computed from a recipe
    REFERENCE = "reference"  # a reference table value
    ESTIMATED = "estimated"  # the agent's estimate


class CanonicalProduct(DomainBase):
    """A food as a concept: "cottage cheese 5%", "chicken breast", "olive oil"."""

    __tablename__ = "canonical_products"
    __table_args__ = (UniqueConstraint("owner_id", "slug", name="uq_canonical_slug"),)

    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    default_unit: Mapped[Unit] = mapped_column(Enum(Unit), default=Unit.GRAM, nullable=False)

    # Macros per 100 g. For per-piece goods too; piece weight lives in store_item
    kcal_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    protein_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    fat_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    carbs_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    fiber_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    saturated_fat_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    nutrition_source: Mapped[NutritionSource | None] = mapped_column(
        Enum(NutritionSource), nullable=True
    )

    is_protein_base: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # What happens to the food between shop and plate: skin removed, fat trimmed,
    # liquid drained. That is where the divergence from the label comes from
    prep_note: Mapped[str | None] = mapped_column(String(250), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    store_items: Mapped[list[StoreItem]] = relationship(back_populates="canonical")


class StoreItem(DomainBase):
    """One concrete product: package, brand, the shop's article number."""

    __tablename__ = "store_items"
    __table_args__ = (
        UniqueConstraint("owner_id", "store", "external_id", name="uq_store_item_sku"),
    )

    canonical_id: Mapped[str] = mapped_column(
        ForeignKey("canonical_products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    store: Mapped[str] = mapped_column(String(40), nullable=False, default="silpo")
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    slug: Mapped[str | None] = mapped_column(String(200), nullable=True)
    name: Mapped[str] = mapped_column(String(250), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Package weight. Empty for goods sold by weight — that comes from the receipt
    pack_grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    piece_grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_weighted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # The shop declares the product AS SOLD. Chicken is sold with the skin, the skin
    # is removed and thrown away: 166 kcal on the package and 110 on the plate are two
    # honest numbers, and one column cannot hold both.
    kcal_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    protein_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    fat_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    carbs_100: Mapped[float | None] = mapped_column(Float, nullable=True)

    canonical: Mapped[CanonicalProduct] = relationship(back_populates="store_items")


class IngredientAlias(DomainBase):
    """How the owner names a food in a recipe → the catalogue product.

    The same device as receipt lines, for the same reason: only a human can tell that
    "chicken breast" and "chicken fillet" are one thing, while "chicken eggs" is not.
    Ask ONCE, remember forever.

    Name similarity is a hint for asking, never grounds for linking: a wrong macro
    figure costs more than one extra question.
    """

    __tablename__ = "ingredient_aliases"
    __table_args__ = (
        UniqueConstraint("owner_id", "raw_name", name="uq_ingredient_alias"),
    )

    raw_name: Mapped[str] = mapped_column(String(250), nullable=False, index=True)
    canonical_id: Mapped[str] = mapped_column(
        ForeignKey("canonical_products.id", ondelete="CASCADE",
                   name="fk_ingredient_alias_product"),
        nullable=False, index=True,
    )
    # The state of the food in this recipe: raw, boiled, skinless. It changes macros
    state: Mapped[str | None] = mapped_column(String(80), nullable=True)
    hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ItemAlias(DomainBase):
    """A dirty receipt string → a product. Matched once, then remembered.

    A brand's spelling on a receipt and the words in your head are the same food, but
    only a human can know that. The answer is kept here forever.
    """

    __tablename__ = "item_aliases"
    __table_args__ = (UniqueConstraint("owner_id", "store", "raw_name", name="uq_alias_raw"),)

    store: Mapped[str] = mapped_column(String(40), nullable=False, default="silpo")
    raw_name: Mapped[str] = mapped_column(String(250), nullable=False, index=True)
    store_item_id: Mapped[str] = mapped_column(
        ForeignKey("store_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # How many times it fired — shows which matches are alive
    hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
