"""The pantry: what is here, what is going bad, what to write off, what to cook.

A balance is never stored as a number — it is the sum of movements. Every function here
either reads, or appends a movement; none of them rewrites the past.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.catalog import CanonicalProduct, StoreItem
from app.models.pantry import (
    Batch,
    BatchSource,
    ExpirySource,
    MoveKind,
    PreparationRun,
    StockMove,
    StoragePlace,
)
from app.models.policies import ShelfLifeRule


class PantryError(Exception):
    pass


@dataclass
class StockLine:
    """A pantry line with its computed balance."""

    batch: Batch
    grams_left: float
    portions_left: float | None
    days_left: int | None
    is_expired: bool

    @property
    def label(self) -> str:
        return self.batch.label


# --- reading ----------------------------------------------------------------------

def batch_balance(session: Session, batch_id: str) -> float:
    """A batch balance is the sum of its movements. There is no other source of truth."""
    total = session.scalar(
        select(func.coalesce(func.sum(StockMove.grams), 0.0)).where(
            StockMove.batch_id == batch_id
        )
    )
    return round(float(total or 0.0), 2)


def _balances(session: Session, owner_id: str) -> dict[str, float]:
    rows = session.execute(
        select(StockMove.batch_id, func.sum(StockMove.grams))
        .where(StockMove.owner_id == owner_id)
        .group_by(StockMove.batch_id)
    ).all()
    return {batch_id: round(float(total or 0.0), 2) for batch_id, total in rows}


def list_stock(
    session: Session,
    owner_id: str,
    *,
    place: StoragePlace | None = None,
    include_empty: bool = False,
    now: dt.datetime | None = None,
) -> list[StockLine]:
    now = now or utcnow()
    balances = _balances(session, owner_id)

    query = select(Batch).where(Batch.owner_id == owner_id)
    if place is not None:
        query = query.where(Batch.place == place)

    lines: list[StockLine] = []
    for batch in session.scalars(query).all():
        grams = balances.get(batch.id, 0.0)
        if grams <= 0 and not include_empty:
            continue
        days_left = None
        expired = False
        if batch.expires_at is not None:
            delta = batch.expires_at - now
            days_left = delta.days
            expired = delta.total_seconds() < 0
        portions = None
        if batch.portion_grams:
            portions = round(grams / batch.portion_grams, 1)
        lines.append(
            StockLine(
                batch=batch,
                grams_left=grams,
                portions_left=portions,
                days_left=days_left,
                is_expired=expired,
            )
        )

    # What is going bad first: nearer expiry ranks higher. No date sorts to the end
    lines.sort(key=lambda line: (line.days_left is None, line.days_left or 0))
    return lines


def expiring(
    session: Session, owner_id: str, *, within_days: int = 2, now: dt.datetime | None = None
) -> list[StockLine]:
    """What is going bad. The first thing the agent must look at before any menu."""
    now = now or utcnow()
    return [
        line
        for line in list_stock(session, owner_id, now=now)
        if line.days_left is not None and line.days_left <= within_days
    ]


def fifo_batches(session: Session, owner_id: str, canonical_id: str) -> list[StockLine]:
    """Batches of the same food are spent by date: the earliest expiry is eaten first."""
    lines = [
        line
        for line in list_stock(session, owner_id)
        if line.batch.canonical_id == canonical_id
    ]
    lines.sort(key=lambda line: (line.batch.expires_at is None, line.batch.expires_at or utcnow()))
    return lines


# --- shelf life --------------------------------------------------------------------

def resolve_expiry(
    session: Session,
    owner_id: str,
    *,
    category: str,
    place: StoragePlace,
    acquired_at: dt.datetime,
    opened_at: dt.datetime | None = None,
    frozen_at: dt.datetime | None = None,
) -> tuple[dt.datetime | None, ExpirySource]:
    """Compute an expiry from the rules. With no rule, return "unknown" honestly.

    Inventing a date is forbidden: it manufactures precision where there is none.
    """
    rule = session.scalar(
        select(ShelfLifeRule).where(
            ShelfLifeRule.owner_id == owner_id, ShelfLifeRule.category == category
        )
    )
    if rule is None:
        return None, ExpirySource.UNKNOWN

    candidates: list[dt.datetime] = []
    if rule.days:
        candidates.append(acquired_at + dt.timedelta(days=rule.days))
    if opened_at and rule.days_after_opening:
        candidates.append(opened_at + dt.timedelta(days=rule.days_after_opening))
    if place is StoragePlace.FREEZER and rule.freezable and rule.freezer_days:
        base = frozen_at or acquired_at
        # The freezer extends the date rather than shortening it: it wins over the rest
        candidates = [base + dt.timedelta(days=rule.freezer_days)]

    if not candidates:
        return None, ExpirySource.UNKNOWN
    return min(candidates), ExpirySource.CALCULATED


# --- writing ----------------------------------------------------------------------

def add_batch(
    session: Session,
    owner_id: str,
    *,
    canonical: CanonicalProduct,
    label: str,
    grams: float,
    place: StoragePlace,
    source: BatchSource,
    store_item: StoreItem | None = None,
    acquired_at: dt.datetime | None = None,
    expires_at: dt.datetime | None = None,
    expiry_source: ExpirySource | None = None,
    portion_grams: float | None = None,
    weight_source: str = "confirmed",
    price_per_kg: float | None = None,
    receipt_line_id: str | None = None,
    note: str | None = None,
) -> Batch:
    """Create a batch and its incoming movement at once. A batch without one cannot exist."""
    if grams <= 0:
        raise PantryError("An incoming amount must be positive")

    acquired_at = acquired_at or utcnow()
    if expires_at is None and expiry_source is None:
        expires_at, expiry_source = resolve_expiry(
            session,
            owner_id,
            category=canonical.category,
            place=place,
            acquired_at=acquired_at,
        )

    batch = Batch(
        owner_id=owner_id,
        canonical_id=canonical.id,
        store_item_id=store_item.id if store_item else None,
        label=label,
        place=place,
        source=source,
        acquired_at=acquired_at,
        expires_at=expires_at,
        expiry_source=expiry_source or ExpirySource.UNKNOWN,
        portion_grams=portion_grams,
        kcal_100=canonical.kcal_100,
        protein_100=canonical.protein_100,
        fat_100=canonical.fat_100,
        carbs_100=canonical.carbs_100,
        price_per_kg=price_per_kg,
        receipt_line_id=receipt_line_id,
        note=note,
    )
    session.add(batch)
    session.flush()

    session.add(
        StockMove(
            owner_id=owner_id,
            batch_id=batch.id,
            kind=MoveKind.PURCHASE,
            grams=grams,
            occurred_at=acquired_at,
            weight_source=weight_source,
        )
    )
    session.flush()
    return batch


def consume(
    session: Session,
    owner_id: str,
    *,
    batch_id: str,
    grams: float,
    occurred_at: dt.datetime | None = None,
    meal_id: str | None = None,
    weight_source: str = "confirmed",
    allow_overdraft: bool = False,
) -> StockMove:
    """Write off what was eaten. Going negative is refused — it signals a double entry."""
    if grams <= 0:
        raise PantryError("A write-off must be a positive number")

    available = batch_balance(session, batch_id)
    if grams > available and not allow_overdraft:
        raise PantryError(
            f"The batch holds {available:.0f} g and {grams:.0f} g was requested. "
            "This looks like a double entry — a stocktake is needed"
        )

    move = StockMove(
        owner_id=owner_id,
        batch_id=batch_id,
        kind=MoveKind.CONSUMPTION,
        grams=-abs(grams),
        occurred_at=occurred_at or utcnow(),
        meal_id=meal_id,
        weight_source=weight_source,
    )
    session.add(move)
    session.flush()
    return move


def waste(
    session: Session, owner_id: str, *, batch_id: str, grams: float, reason: str | None = None
) -> StockMove:
    """Thrown out. A movement of its own kind: it shows how much food was lost."""
    move = StockMove(
        owner_id=owner_id,
        batch_id=batch_id,
        kind=MoveKind.WASTE,
        grams=-abs(grams),
        occurred_at=utcnow(),
        reason=reason,
    )
    session.add(move)
    session.flush()
    return move


def correct(
    session: Session, owner_id: str, *, batch_id: str, actual_grams: float, reason: str
) -> StockMove:
    """A stocktake: bring the balance to what the human sees in the fridge.

    Not an edit of old movements but a new correcting one. The history stays readable.
    """
    current = batch_balance(session, batch_id)
    delta = round(actual_grams - current, 2)
    move = StockMove(
        owner_id=owner_id,
        batch_id=batch_id,
        kind=MoveKind.CORRECTION,
        grams=delta,
        occurred_at=utcnow(),
        weight_source="confirmed",
        reason=reason,
    )
    session.add(move)
    session.flush()
    return move


def open_package(
    session: Session, owner_id: str, *, batch_id: str, opened_at: dt.datetime | None = None
) -> Batch:
    """Mark a package as opened: an open package has a different shelf life."""
    batch = session.get(Batch, batch_id)
    if batch is None or batch.owner_id != owner_id:
        raise PantryError("Batch not found")

    batch.opened_at = opened_at or utcnow()
    canonical = session.get(CanonicalProduct, batch.canonical_id)
    expires_at, source = resolve_expiry(
        session,
        owner_id,
        category=canonical.category,
        place=batch.place,
        acquired_at=batch.acquired_at,
        opened_at=batch.opened_at,
        frozen_at=batch.frozen_at,
    )
    # A date named by a human outranks a computed one
    if expires_at and batch.expiry_source is not ExpirySource.CONFIRMED:
        batch.expires_at = expires_at
        batch.expiry_source = source
    session.flush()
    return batch


def move_to(
    session: Session, owner_id: str, *, batch_id: str, place: StoragePlace
) -> Batch:
    """Move between fridge, freezer and cupboard — the expiry is recomputed."""
    batch = session.get(Batch, batch_id)
    if batch is None or batch.owner_id != owner_id:
        raise PantryError("Batch not found")

    batch.place = place
    if place is StoragePlace.FREEZER and batch.frozen_at is None:
        batch.frozen_at = utcnow()

    canonical = session.get(CanonicalProduct, batch.canonical_id)
    expires_at, source = resolve_expiry(
        session,
        owner_id,
        category=canonical.category,
        place=place,
        acquired_at=batch.acquired_at,
        opened_at=batch.opened_at,
        frozen_at=batch.frozen_at,
    )
    if expires_at and batch.expiry_source is not ExpirySource.CONFIRMED:
        batch.expires_at = expires_at
        batch.expiry_source = source
    session.flush()
    return batch


@dataclass
class CookInput:
    batch_id: str
    grams: float


@dataclass
class CookResult:
    run: PreparationRun
    output_batch: Batch
    consumed: list[StockMove] = field(default_factory=list)


def cook(
    session: Session,
    owner_id: str,
    *,
    title: str,
    inputs: list[CookInput],
    output_canonical: CanonicalProduct,
    output_grams: float,
    place: StoragePlace,
    portions: int | None = None,
    portion_grams: float | None = None,
    recipe_id: str | None = None,
    cooked_at: dt.datetime | None = None,
    expires_at: dt.datetime | None = None,
    note: str | None = None,
) -> CookResult:
    """Cooking: consume the inputs, create the output, record the loss.

    Neither a write-off nor a purchase. The owner cooks a whole purchased piece: took
    1.5 kg, cooked 1.5 kg. The output is lighter, and the difference is loss, not theft.
    """
    if not inputs:
        raise PantryError("Cooking with no input products is impossible")
    if output_grams <= 0:
        raise PantryError("The output must be positive")

    cooked_at = cooked_at or utcnow()
    total_in = 0.0
    consumed: list[StockMove] = []

    for item in inputs:
        available = batch_balance(session, item.batch_id)
        if item.grams > available:
            batch = session.get(Batch, item.batch_id)
            raise PantryError(
                f"'{batch.label if batch else item.batch_id}': the batch holds {available:.0f} g, "
                f"cooking asks for {item.grams:.0f} g"
            )
        move = StockMove(
            owner_id=owner_id,
            batch_id=item.batch_id,
            kind=MoveKind.PRODUCTION_IN,
            grams=-abs(item.grams),
            occurred_at=cooked_at,
        )
        session.add(move)
        consumed.append(move)
        total_in += item.grams

    output = Batch(
        owner_id=owner_id,
        canonical_id=output_canonical.id,
        label=title,
        place=place,
        source=BatchSource.COOKED,
        acquired_at=cooked_at,
        expires_at=expires_at,
        expiry_source=ExpirySource.CONFIRMED if expires_at else ExpirySource.UNKNOWN,
        portion_grams=portion_grams,
        kcal_100=output_canonical.kcal_100,
        protein_100=output_canonical.protein_100,
        fat_100=output_canonical.fat_100,
        carbs_100=output_canonical.carbs_100,
        note=note,
    )
    session.add(output)
    session.flush()

    session.add(
        StockMove(
            owner_id=owner_id,
            batch_id=output.id,
            kind=MoveKind.PRODUCTION_OUT,
            grams=output_grams,
            occurred_at=cooked_at,
            preparation_id=None,
        )
    )

    run = PreparationRun(
        owner_id=owner_id,
        recipe_id=recipe_id,
        output_batch_id=output.id,
        title=title,
        cooked_at=cooked_at,
        input_grams=round(total_in, 2),
        output_grams=round(output_grams, 2),
        output_portions=portions,
        loss_grams=round(max(0.0, total_in - output_grams), 2),
        note=note,
    )
    session.add(run)
    session.flush()

    for move in consumed:
        move.preparation_id = run.id
    session.flush()

    return CookResult(run=run, output_batch=output, consumed=consumed)
