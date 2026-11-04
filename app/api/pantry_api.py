"""The pantry and diary API for the bot and the web.

State changes here act immediately and only on the owner's word — the web confirmation
page was removed, along with the pending actions behind it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import require
from app.core.security import Identity, Scope
from app.db import get_session
from app.domain import diary as diary_domain
from app.domain import pantry as pantry_domain
from app.models.diary import Confidence, ExternalKind, MealSource
from app.models.pantry import StoragePlace

router = APIRouter(prefix="/api", tags=["pantry"])


class StockItemOut(BaseModel):
    batch_id: str
    label: str
    grams_left: float
    portions_left: float | None
    place: str
    days_left: int | None
    expires_on: str | None
    expiry_known: bool


@router.get("/pantry", response_model=list[StockItemOut])
def stock(
    place: StoragePlace | None = None,
    identity: Identity = Depends(require(Scope.READ)),
    session: Session = Depends(get_session),
) -> list[StockItemOut]:
    lines = pantry_domain.list_stock(session, identity.principal_id, place=place)
    return [
        StockItemOut(
            batch_id=line.batch.id,
            label=line.label,
            grams_left=line.grams_left,
            portions_left=line.portions_left,
            place=line.batch.place.value,
            days_left=line.days_left,
            expires_on=line.batch.expires_at.date().isoformat() if line.batch.expires_at else None,
            expiry_known=line.batch.expires_at is not None,
        )
        for line in lines
    ]


@router.get("/pantry/expiring", response_model=list[StockItemOut])
def expiring(
    days: int = 2,
    identity: Identity = Depends(require(Scope.READ)),
    session: Session = Depends(get_session),
) -> list[StockItemOut]:
    lines = pantry_domain.expiring(session, identity.principal_id, within_days=days)
    return [
        StockItemOut(
            batch_id=line.batch.id,
            label=line.label,
            grams_left=line.grams_left,
            portions_left=line.portions_left,
            place=line.batch.place.value,
            days_left=line.days_left,
            expires_on=line.batch.expires_at.date().isoformat() if line.batch.expires_at else None,
            expiry_known=True,
        )
        for line in lines
    ]


class PortionIn(BaseModel):
    name: str
    grams: float = Field(gt=0)
    batch_id: str | None = None
    canonical_id: str | None = None
    portions: float | None = None
    confidence: Confidence = Confidence.WEIGHED
    kcal: float | None = None
    protein_g: float | None = None
    fat_g: float | None = None
    carbs_g: float | None = None


class MealIn(BaseModel):
    portions: list[PortionIn]
    source: MealSource = MealSource.CONFIRMED
    note: str | None = None
    external: ExternalKind | None = None
    external_place: str | None = None


class MealOut(BaseModel):
    meal_id: str
    date: str
    kcal: float
    protein_g: float
    fat_g: float
    carbs_g: float


@router.post("/diary/meal", response_model=MealOut)
def log_meal(
    payload: MealIn,
    identity: Identity = Depends(require(Scope.WRITE_FACTS)),
    session: Session = Depends(get_session),
) -> MealOut:
    """Recording a fact. No confirmation needed: the food is already eaten."""
    meal = diary_domain.log_meal(
        session,
        identity.principal_id,
        portions=[
            diary_domain.Portion(
                name=p.name, grams=p.grams, batch_id=p.batch_id,
                canonical_id=p.canonical_id, portions=p.portions,
                confidence=p.confidence, kcal=p.kcal, protein_g=p.protein_g,
                fat_g=p.fat_g, carbs_g=p.carbs_g,
            )
            for p in payload.portions
        ],
        source=payload.source,
        note=payload.note,
        external=payload.external,
        external_place=payload.external_place,
    )
    return MealOut(
        meal_id=meal.id, date=meal.local_date, kcal=meal.kcal,
        protein_g=meal.protein_g, fat_g=meal.fat_g, carbs_g=meal.carbs_g,
    )


@router.get("/diary/today")
def today(
    identity: Identity = Depends(require(Scope.READ)),
    session: Session = Depends(get_session),
) -> dict:
    totals = diary_domain.day_totals(session, identity.principal_id)
    return {
        "date": totals.date,
        "kcal": totals.kcal,
        "protein_g": totals.protein_g,
        "fat_g": totals.fat_g,
        "carbs_g": totals.carbs_g,
        "meals": [
            {"ordinal": m.ordinal, "kcal": m.kcal, "protein_g": m.protein_g}
            for m in totals.meals
        ],
    }


class WriteOffIn(BaseModel):
    batch_id: str
    grams: float = Field(gt=0)
    reason: str | None = None


