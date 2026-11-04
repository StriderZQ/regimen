"""The owner's recipes.

Not a collection from the internet: only what he cooks himself and has dictated or
Not a collection from the internet: only what the owner cooks himself and has dictated
or corrected gets in. That is why a recipe carries `source` and `uncertainty`: what is

unknown in the calculation (the fat content of mince, the size of a spoon) is visible in
the answer rather than hidden. A recipe is counted as a **batch** — a whole purchased

piece, not a portion — so the yield is stored in grams and in pieces. The version
increments and the old row stays: last week's menu must be explainable by its recipe.
"""

from __future__ import annotations

import enum

from sqlalchemy import (
    JSON,
    Boolean,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import DomainBase


class RecipeSource(enum.StrEnum):
    DICTATED = "dictated"    # dictated by the owner
    PROJECT = "project"      # carried over from the project files
    AGENT = "agent"          # assembled by the agent and confirmed


class Recipe(DomainBase):
    __tablename__ = "recipes"
    __table_args__ = (
        UniqueConstraint("owner_id", "slug", "version", name="uq_recipe_slug_version"),
        Index("ix_recipes_active", "owner_id", "is_active"),
    )

    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[RecipeSource] = mapped_column(
        Enum(RecipeSource), default=RecipeSource.DICTATED, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    method: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    # Batch yield: grams and pieces — the owner counts both ways
    yield_grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    yield_pieces: Mapped[int | None] = mapped_column(Integer, nullable=True)
    piece_grams: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Macros per piece, when a piece's weight is unknown. Syrniki are counted per piece,
    # and nobody weighed one after frying; inventing a weight to produce a "per 100 g"
    # figure would substitute arithmetic for measurement.
    per_piece: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # Macros per 100 g of the finished dish. Per piece is derived from piece_grams
    kcal_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    protein_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    fat_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    carbs_100: Mapped[float | None] = mapped_column(Float, nullable=True)

    # What is unknown in the calculation: fat content of mince, size of a spoon, "a little" sugar
    uncertainty: Mapped[str | None] = mapped_column(Text, nullable=True)

    # A dish can have several executions with different macros. Syrniki with and without
    # sugar are 640 and 500 kcal per batch. Logging what was eaten without asking which
    # one means writing somebody else's number into the diary.
    ask_variant: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    origin_note: Mapped[str | None] = mapped_column(String(250), nullable=True)

    # The finished-dish card in the catalogue: the diary counts what was eaten from it
    dish_product_id: Mapped[str | None] = mapped_column(
        # The constraint must be named: SQLite alters tables by recreating them,
        # and an unnamed one cannot be recreated
        ForeignKey("canonical_products.id", ondelete="SET NULL",
                   name="fk_recipes_dish_product"),
        nullable=True,
    )

    ingredients: Mapped[list[RecipeIngredient]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan", lazy="selectin"
    )


class RecipeIngredient(DomainBase):
    """A batch ingredient. Weights are for the whole batch, not a portion."""

    __tablename__ = "recipe_ingredients"

    recipe_id: Mapped[str] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The catalogue link may be absent: salt and garlic are not kept as products
    canonical_id: Mapped[str | None] = mapped_column(
        ForeignKey("canonical_products.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String(20), default="g", nullable=False)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Small things the dish survives without: salt, pepper, herbs
    is_staple: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # The base product: the one whose kilogram the whole recipe is scaled to.
    # The rule is "every recipe per 1 kg of the base": bought one
    # and a half kilos of mince, multiply by one and a half, and it all adds up.
    is_base: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    note: Mapped[str | None] = mapped_column(String(250), nullable=True)

    recipe: Mapped[Recipe] = relationship(back_populates="ingredients")
