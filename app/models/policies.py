"""Rules that change faster than the code.

Macro targets, meal pattern, shelf lives and typical weights live in the database with
effective dates, not as constants in code. The reason is simple: cutting, bulking,
illness, a holiday or a new goal change the rules several times a year, and the code

must not be touched for that. Learning lives here too: typical weight and shelf life
move on the owner's observations under the rule of three — one never changes a value.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import JSON, Boolean, Enum, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DomainBase


class PolicyKind(enum.StrEnum):
    MACROS = "macros"            # calories, protein, carbs, fat
    MEAL_PATTERN = "meal_pattern"  # how many meals, at what hours, in what proportion
    CARB_DAYS = "carb_days"      # which days carry carbohydrates
    VARIETY = "variety"          # the variety rules
    SPICES = "spices"            # which spices exist: no weights, no dates
    REVISION = "revision"        # date of the last full stocktake
    COOKING = "cooking"          # the cooking instructions
    COACHING = "coaching"        # how training is planned and progressed


class Policy(DomainBase):
    """A rule with effective dates. History is not rewritten; a new version is added."""

    __tablename__ = "policies"
    __table_args__ = (Index("ix_policies_kind_active", "owner_id", "kind", "active_from"),)

    kind: Mapped[PolicyKind] = mapped_column(Enum(PolicyKind), nullable=False)
    value: Mapped[dict] = mapped_column(JSON, nullable=False)
    active_from: Mapped[dt.datetime] = mapped_column(nullable=False)
    active_to: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)


class ShelfLifeRule(DomainBase):
    """Shelf life by category. A default that the owner's answers move.

    For example, an owner may keep UHT milk 10 days after opening rather than the 3 in
    the general table. Corrections like that matter more than any reference book.
    """

    __tablename__ = "shelf_life_rules"
    __table_args__ = (UniqueConstraint("owner_id", "category", name="uq_shelf_category"),)

    category: Mapped[str] = mapped_column(String(80), nullable=False)
    days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    days_after_opening: Mapped[int | None] = mapped_column(Integer, nullable=True)
    freezable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    freezer_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    days_after_thaw: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Observations of "it kept until tomorrow": three in a row move the date
    observations: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class TypicalWeight(DomainBase):
    """The typical weight of a purchase or a portion.

    The owner dictates without grams — "bought tomatoes, cucumbers, herbs, mince" — and
    the agent fills in the usual weights and shows the table. He corrects only what was
    different, and the correction becomes an observation.

    The rule of three: one observation NEVER changes a value. An agent once saw a
    single day at 148 g of carbs, made it a habit, and the target had to be rolled back.
    """

    __tablename__ = "typical_weights"
    __table_args__ = (UniqueConstraint("owner_id", "kind", "name", name="uq_typical"),)

    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # purchase | portion
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    unit: Mapped[str] = mapped_column(String(10), default="g", nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    observations: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    source_quote: Mapped[str | None] = mapped_column(Text, nullable=True)


class Supplement(DomainBase):
    """Supplements.

    Sports supplements are named. Medication carries a neutral label with no name: the
    name of a drug must not be left in a chat log, because more can be read from it than
    the owner is willing to keep there.
    """

    __tablename__ = "supplements"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_medication: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    schedule: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Reminder(DomainBase):
    """A reminder. Switchable off — the owner asked for that explicitly."""

    __tablename__ = "reminders"

    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    cron: Mapped[str | None] = mapped_column(String(60), nullable=True)
    template: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_sent_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    snoozed_until: Mapped[dt.datetime | None] = mapped_column(nullable=True)


class BodyMeasurement(DomainBase):
    """A body measurement session: girths, weight, and whatever else was measured.

    Values are a dictionary rather than twenty-two columns. The set of what gets measured
    changes — a new tape position gets added months after the first session — and
    a schema migration per new tape position is a bad trade. What matters is the trend of
    a given line, and a dictionary keeps every line that was ever taken.

    Body fat is not stored: it is computed from the numbers on demand, so that correcting
    a mis-measured belly corrects the percentage too, instead of leaving two disagreeing
    figures in the database.
    """

    __tablename__ = "body_measurements"
    __table_args__ = (UniqueConstraint("owner_id", "measured_on", name="uq_measured_on"),)

    measured_on: Mapped[str] = mapped_column(String(10), nullable=False)
    values: Mapped[dict] = mapped_column(JSON, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
