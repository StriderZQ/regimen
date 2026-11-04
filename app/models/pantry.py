"""The pantry: batches, movements, cooking.

The central decision of the whole system: **a balance is never stored as a number**. It
is computed as the sum of movements. There is deliberately no `left_g` column — it was
overwriting a balance is exactly what lets two agents wipe each other's pantries with
nobody noticing.
The consequences this buys:
  * a mistake is fixed by adding a movement, not by rewriting history
  * you can see where a figure came from: purchase, eaten, thrown out, correction
  * nothing is lost in a crash — movements are atomic
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import Enum, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import DomainBase
from app.models.catalog import CanonicalProduct


class StoragePlace(enum.StrEnum):
    """Shelf life follows the place. Mince keeps a day in the fridge, three months frozen."""

    FRIDGE = "fridge"
    FREEZER = "freezer"
    PANTRY = "pantry"


class BatchSource(enum.StrEnum):
    SILPO = "silpo"    # from a receipt, exact weight
    MARKET = "market"  # from a market, dictated
    COOKED = "cooked"  # cooked at home


class ExpirySource(enum.StrEnum):
    """A date cannot come from rules alone — that would manufacture false precision."""

    LABEL = "label"            # off the package
    CONFIRMED = "confirmed"    # the owner said so
    CALCULATED = "calculated"  # derived from shelf-life rules
    UNKNOWN = "unknown"        # unknown, and said so plainly


class MoveKind(enum.StrEnum):
    PURCHASE = "purchase"        # + incoming
    CONSUMPTION = "consumption"  # − eaten
    WASTE = "waste"              # − thrown out
    CORRECTION = "correction"    # ± correction after a stocktake
    PRODUCTION_IN = "prod_in"    # − went into cooking
    PRODUCTION_OUT = "prod_out"  # + came out of cooking


class Batch(DomainBase):
    """A batch in the pantry: one package, or one cooked dish."""

    __tablename__ = "batches"
    __table_args__ = (
        Index("ix_batches_owner_place", "owner_id", "place"),
        Index("ix_batches_expires", "owner_id", "expires_at"),
    )

    store_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("store_items.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # Cooked food has no shop item — then the product is referenced directly
    canonical_id: Mapped[str] = mapped_column(
        ForeignKey("canonical_products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False)

    place: Mapped[StoragePlace] = mapped_column(Enum(StoragePlace), nullable=False)
    source: Mapped[BatchSource] = mapped_column(Enum(BatchSource), nullable=False)

    acquired_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    opened_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    frozen_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    expiry_source: Mapped[ExpirySource] = mapped_column(
        Enum(ExpirySource), default=ExpirySource.UNKNOWN, nullable=False
    )

    # For per-piece goods: how many pieces, and what one weighs
    portion_grams: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Macros snapshotted when the batch was created: editing a product card must not
    # rewrite the past
    kcal_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    protein_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    fat_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    carbs_100: Mapped[float | None] = mapped_column(Float, nullable=True)

    price_per_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    receipt_line_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # A batch's product is needed in almost every read of the pantry: menus compute
    # macros, variety looks at the slug. Without the relationship that is N queries.
    canonical: Mapped[CanonicalProduct] = relationship(lazy="joined")

    moves: Mapped[list[StockMove]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class StockMove(DomainBase):
    """A movement. A batch's balance is the sum of these rows, and nothing else."""

    __tablename__ = "stock_moves"
    __table_args__ = (
        Index("ix_moves_batch", "batch_id", "occurred_at"),
        Index("ix_moves_owner_kind", "owner_id", "kind"),
    )

    batch_id: Mapped[str] = mapped_column(
        ForeignKey("batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[MoveKind] = mapped_column(Enum(MoveKind), nullable=False)
    # Positive is incoming, negative is outgoing. The sign carries meaning
    grams: Mapped[float] = mapped_column(Float, nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(nullable=False)

    # Where the weight figure came from
    weight_source: Mapped[str] = mapped_column(String(20), default="confirmed", nullable=False)
    meal_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    preparation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    batch: Mapped[Batch] = relationship(back_populates="moves")


class PreparationRun(DomainBase):
    """Cooking as a production operation, not as a write-off.

    "Make meatballs" means consuming 2 kg of mince, parmesan and breadcrumbs, producing a
    batch of 47 pieces, and recording the loss. One movement cannot express that: many
    inputs, a different output, and weight is not conserved.
    """

    __tablename__ = "preparation_runs"

    recipe_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    output_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("batches.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    cooked_at: Mapped[dt.datetime] = mapped_column(nullable=False)

    input_grams: Mapped[float] = mapped_column(Float, nullable=False)
    output_grams: Mapped[float] = mapped_column(Float, nullable=False)
    output_portions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Cooking losses: evaporation, what sticks to the pan, what stays in the bowl
    loss_grams: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
