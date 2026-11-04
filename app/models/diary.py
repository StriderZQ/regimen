"""The food diary.

Two things here are not what they first appear, and both followed a post-mortem:

**Macros are snapshotted on every item.** If an item referenced a product card, editing
that card would rewrite history retroactively. When a recipe is
corrected from six pieces to eight, a reference model would silently recalculate the whole
previous month. So `meal_items` holds a copy of the numbers as written.

**An item may reference nothing at all.** Cafes, delivery, guests, travel — food that was
never in the pantry and never will be. Without this the diary stops being true on the
first trip, and the system starts believing the owner ate nothing.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import Boolean, Enum, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import DomainBase


class MealSource(enum.StrEnum):
    """Where the entry came from — confirming a plan, or dictated from scratch."""

    CONFIRMED = "confirmed"  # the owner confirmed the proposed plan, correcting the delta
    DICTATED = "dictated"    # named the contents himself


class ItemOrigin(enum.StrEnum):
    PANTRY = "pantry"      # from stock, with a movement behind it
    RECIPE = "recipe"      # cooked from a recipe
    EXTERNAL = "external"  # away from home: cafe, delivery, guests, travel


class ExternalKind(enum.StrEnum):
    RESTAURANT = "restaurant"
    DELIVERY = "delivery"
    GUEST = "guest"
    TRAVEL = "travel"


class Confidence(enum.StrEnum):
    """Honesty beats precision: an eyeballed number must be marked as eyeballed."""

    WEIGHED = "weighed"      # on a scale
    TYPICAL = "typical"      # the owner's usual portion
    ESTIMATED = "estimated"  # eyeballed


class Meal(DomainBase):
    """A meal. Time carries a zone: travel and late training must not break the day."""

    __tablename__ = "meals"
    __table_args__ = (Index("ix_meals_owner_eaten", "owner_id", "eaten_at"),)

    eaten_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    # The local date at the moment of eating: the day is counted by this, not by UTC
    local_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source: Mapped[MealSource] = mapped_column(Enum(MealSource), nullable=False)

    kcal: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    protein_g: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    fat_g: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    carbs_g: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    fiber_g: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list[MealItem]] = relationship(
        back_populates="meal", cascade="all, delete-orphan"
    )


class MealItem(DomainBase):
    """A meal item holding a SNAPSHOT of the name and the macros.

    The link to a stock movement is optional: half a plate may come from the old pack,
    half from the new one, and dinner entirely from a cafe.
    """

    __tablename__ = "meal_items"
    __table_args__ = (Index("ix_meal_items_meal", "meal_id"),)

    meal_id: Mapped[str] = mapped_column(
        ForeignKey("meals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    origin: Mapped[ItemOrigin] = mapped_column(Enum(ItemOrigin), nullable=False)

    # Either link may be empty — that is normal, not missing data
    stock_move_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    canonical_id: Mapped[str | None] = mapped_column(
        ForeignKey("canonical_products.id", ondelete="SET NULL"), nullable=True
    )
    recipe_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # A SNAPSHOT, not a reference. Editing a product card leaves history alone
    name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    grams: Mapped[float] = mapped_column(Float, nullable=False)
    portions: Mapped[float | None] = mapped_column(Float, nullable=True)
    kcal: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    protein_g: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    fat_g: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    carbs_g: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    fiber_g: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Macros unknown while the columns hold zero: columns cannot store "don't know".
    # Without this flag the day's total sums the zeros and looks confident — that is how
    # the diary showed 43 g of protein instead of 86 and said nothing about it.
    macros_known: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    confidence: Mapped[Confidence] = mapped_column(
        Enum(Confidence), default=Confidence.WEIGHED, nullable=False
    )

    meal: Mapped[Meal] = relationship(back_populates="items")


class ExternalMeal(DomainBase):
    """Food away from home. Stock is untouched, but the day still adds up honestly."""

    __tablename__ = "external_meals"

    meal_id: Mapped[str] = mapped_column(
        ForeignKey("meals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[ExternalKind] = mapped_column(Enum(ExternalKind), nullable=False)
    place: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # A usual order: "the same as always, there" — so it need not be described again
    saved_as: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    # Macros unknown while the columns hold zero: columns cannot store "don't know".
    # Without this flag the day's total sums the zeros and looks confident — that is how
    # the diary showed 43 g of protein instead of 86 and said nothing about it.
    macros_known: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    confidence: Mapped[Confidence] = mapped_column(
        Enum(Confidence), default=Confidence.ESTIMATED, nullable=False
    )
