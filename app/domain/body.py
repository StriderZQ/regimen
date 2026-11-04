"""Body measurements: girths over time, and what can honestly be derived from them.

Weight alone says little during a cut. The scale can drop while the waist stays put, or
come back by a kilogram while the belly grows and lean mass does not move. Only the tape
shows which of those happened.

Body fat is computed, never stored. The US Navy girth method is sensitive to exactly
where the belly was measured, so a corrected measurement must correct the percentage
with it — two disagreeing figures in one database is worse than no figure.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.policies import BodyMeasurement

# The tape positions the service knows, in measuring order
KNOWN_LINES = (
    "вес", "шея", "плечи", "грудь", "талия", "живот",
    "бёдра_таз", "бёдра_посадка", "бёдра_широкое",
    "бицепс_левый", "бицепс_правый", "предплечье_левое", "предплечье_правое",
    "запястье_левое", "запястье_правое", "бедро_левое", "бедро_правое",
    "икра_левая", "икра_правая", "щиколотка_левая", "щиколотка_правая", "голова",
)

def _height_cm() -> float | None:
    """The owner's height, from BODY_HEIGHT_CM. Without it body fat is not computed."""
    from app.config import get_settings

    return get_settings().body_height_cm


class BodyError(Exception):
    """An error to be shown in words."""


@dataclass
class Derived:
    """What the girths imply. An estimate, and labelled as one wherever it is shown."""

    body_fat_pct: float | None = None
    fat_mass_kg: float | None = None
    lean_mass_kg: float | None = None
    waist_to_height: float | None = None
    belly_minus_waist: float | None = None


def body_fat(values: dict, height_cm: float | None = None) -> float | None:
    """US Navy girth method for men, computed from belly and neck.

    The belly at navel level is used rather than the narrow waist, consistently: mixing
    the two would break the trend line. Accuracy is ±3-4 percentage points — the trend
    matters, the absolute value does not. With no height configured there is no figure.
    """
    height_cm = height_cm or _height_cm()
    if not height_cm:
        return None
    belly = values.get("живот")
    neck = values.get("шея")
    if not belly or not neck or belly <= neck:
        return None
    value = 495 / (
        1.0324
        - 0.19077 * math.log10(belly - neck)
        + 0.15456 * math.log10(height_cm)
    ) - 450
    return round(value, 1)


def derive(values: dict, height_cm: float | None = None) -> Derived:
    """Everything computable from one measurement session."""
    height_cm = height_cm or _height_cm()
    result = Derived(body_fat_pct=body_fat(values, height_cm))
    weight = values.get("вес")
    if result.body_fat_pct is not None and weight:
        result.fat_mass_kg = round(weight * result.body_fat_pct / 100, 1)
        result.lean_mass_kg = round(weight - result.fat_mass_kg, 1)
    if values.get("талия") and height_cm:
        result.waist_to_height = round(values["талия"] / height_cm, 3)
    if values.get("живот") and values.get("талия"):
        result.belly_minus_waist = round(values["живот"] - values["талия"], 1)
    return result


def record(
    session: Session, owner_id: str, *, measured_on: str | dt.date, values: dict,
    note: str | None = None,
) -> BodyMeasurement:
    """Store a measurement session. Measuring the same day again replaces it.

    Re-measuring a line the same morning is a correction, not a second session: a tape
    read twice does not mean the body changed twice.
    """
    day = measured_on.isoformat() if isinstance(measured_on, dt.date) else str(measured_on)
    clean = {k: float(v) for k, v in values.items() if v is not None}
    if not clean:
        raise BodyError("A measurement with no values is not recorded")

    existing = session.scalar(
        select(BodyMeasurement).where(
            BodyMeasurement.owner_id == owner_id, BodyMeasurement.measured_on == day
        )
    )
    if existing is not None:
        existing.values = {**existing.values, **clean}
        if note:
            existing.note = note
        session.flush()
        return existing

    entry = BodyMeasurement(owner_id=owner_id, measured_on=day, values=clean, note=note)
    session.add(entry)
    session.flush()
    return entry


def history(session: Session, owner_id: str, *, limit: int = 12) -> list[BodyMeasurement]:
    """Measurement sessions, newest last — the order a trend is read in."""
    rows = session.scalars(
        select(BodyMeasurement)
        .where(BodyMeasurement.owner_id == owner_id)
        .order_by(BodyMeasurement.measured_on.desc())
    ).all()
    return list(reversed(rows[:limit]))


def trend(session: Session, owner_id: str, *, lines: tuple[str, ...] | None = None) -> dict:
    """Each line across sessions, plus the change since the first and the previous one.

    Returned as data rather than prose: what it means is for the agent to say, and it
    should say it from these numbers rather than from memory.
    """
    rows = history(session, owner_id)
    if not rows:
        return {"sessions": [], "lines": {}, "note": "Замеров пока нет"}

    wanted = lines or KNOWN_LINES
    out: dict[str, list] = {}
    for line in wanted:
        series = [(r.measured_on, r.values.get(line)) for r in rows if r.values.get(line)]
        if series:
            out[line] = series

    derived = {r.measured_on: derive(r.values) for r in rows}
    return {
        "sessions": [r.measured_on for r in rows],
        "lines": out,
        "derived": {
            day: {
                "body_fat_pct": d.body_fat_pct,
                "fat_mass_kg": d.fat_mass_kg,
                "lean_mass_kg": d.lean_mass_kg,
                "waist_to_height": d.waist_to_height,
                "belly_minus_waist": d.belly_minus_waist,
            }
            for day, d in derived.items()
        },
        "note": (
            "Процент жира — оценка методом окружностей US Navy, ±3-4 пункта. "
            "Считается от живота на уровне пупка: смотреть на тренд, не на число."
        ),
    }
