"""Grocery receipts.

Receipt lines live in their own table rather than as a list inside an object: they must
be searchable, matchable against products, and resolvable one unknown at a time.

Uniqueness is the pair of shop plus external id. One receipt cannot be entered twice,
even when a sync retries after a timeout.

The receipt URL is deliberately not stored: it is a public link anyone holding it can
open, and accounting does not need it.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import Boolean, Enum, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import DomainBase


class LineStatus(enum.StrEnum):
    """What became of a receipt line."""

    PENDING = "pending"      # waiting to be matched to a product
    MATCHED = "matched"      # matched, batch created
    NOT_FOOD = "not_food"    # cleaning supplies and the like — never enters the pantry
    IGNORED = "ignored"      # the owner said not to record it
    HISTORICAL = "historical"  # from the archive: the purchase happened, but stock is left alone


class Receipt(DomainBase):
    __tablename__ = "receipts"
    __table_args__ = (
        UniqueConstraint("owner_id", "store", "external_id", name="uq_receipt_external"),
        Index("ix_receipts_purchased", "owner_id", "purchased_at"),
    )

    store: Mapped[str] = mapped_column(String(40), default="silpo", nullable=False)
    external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    branch: Mapped[str | None] = mapped_column(String(200), nullable=True)
    purchased_at: Mapped[dt.datetime] = mapped_column(nullable=False)

    total: Mapped[float | None] = mapped_column(Float, nullable=True)
    discount: Mapped[float | None] = mapped_column(Float, nullable=True)
    bonuses: Mapped[float | None] = mapped_column(Float, nullable=True)

    imported_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    lines: Mapped[list[ReceiptLine]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan"
    )


class ReceiptLine(DomainBase):
    __tablename__ = "receipt_lines"
    __table_args__ = (Index("ix_receipt_lines_status", "owner_id", "status"),)

    receipt_id: Mapped[str] = mapped_column(
        ForeignKey("receipts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The shop's article number: exact matching with no guessing from the name
    external_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    raw_name: Mapped[str] = mapped_column(String(250), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    # Grams derived from unit and quantity: "350g" × 1, or "kg" × 0.608
    grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_food: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    status: Mapped[LineStatus] = mapped_column(
        Enum(LineStatus), default=LineStatus.PENDING, nullable=False
    )
    store_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("store_items.id", ondelete="SET NULL"), nullable=True
    )
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    receipt: Mapped[Receipt] = relationship(back_populates="lines")
