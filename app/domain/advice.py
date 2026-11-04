"""The rules behind every recommendation in a report.

Rules rather than a model, for a reason the owner stated plainly: he does not want
different wording every morning, and he does not want thoughts out loud. He wants the
same decision from the same numbers.

The iron constraint here: **every line carries the figure it rests on.** Not "you
recovered badly" but "готовность 42 против 77 вчера". A sentence with no number in it
does not belong in a report, because he cannot check it.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import training
from app.models.garmin import GarminKind, GarminMetric
from app.models.policies import Policy, PolicyKind

MAX_LINES = 5

# A scoop of the whey in the pantry: 30 g of powder, 24 g of protein
SCOOP_PROTEIN_G = 24.0
# Below this the shortfall is not worth a shaker
PROTEIN_SHORTFALL_MIN = 15.0

READINESS_LOW = 50
READINESS_VERY_LOW = 30
READINESS_HIGH = 70
SLEEP_SHORT_MIN = 6 * 60

# Sessions that suffer most from poor recovery: intervals need power the body cannot make
HARD_WORDS = ("vo₂max", "vo2max", "интервал", "порог", "4×4", "4x4")
EASY_WORDS = ("z2", "ходьба", "бег", "велосипед")


def targets(session: Session, owner_id: str) -> dict:
    """The macro targets in force, or an empty dict when none are set."""
    policy = session.scalar(
        select(Policy)
        .where(Policy.owner_id == owner_id, Policy.kind == PolicyKind.MACROS)
        .order_by(Policy.active_from.desc())
    )
    return (policy.value if policy else {}) or {}


def range_text(target) -> str:
    """A target as text: a band, a single number, or a dash when unset."""
    if isinstance(target, list | tuple) and len(target) == 2:
        return f"{target[0]:.0f}–{target[1]:.0f}"
    if isinstance(target, int | float):
        return f"{target:.0f}"
    return "—"


def mark(value: float, target) -> str:
    """Whether a figure sits inside its band, and which way it misses."""
    if isinstance(target, list | tuple) and len(target) == 2:
        if value < target[0]:
            return "↓" if value >= target[0] * 0.85 else "↓↓"
        if value > target[1]:
            return "↑" if value <= target[1] * 1.15 else "↑↑"
        return "✓"
    return ""


def _metric(session: Session, owner_id: str, kind: GarminKind, day: str) -> GarminMetric | None:
    return session.scalar(
        select(GarminMetric).where(
            GarminMetric.owner_id == owner_id,
            GarminMetric.kind == kind,
            GarminMetric.local_date == day,
        )
    )


def _recent(session: Session, owner_id: str, kind: GarminKind, limit: int) -> list[GarminMetric]:
    return list(session.scalars(
        select(GarminMetric)
        .where(GarminMetric.owner_id == owner_id, GarminMetric.kind == kind)
        .order_by(GarminMetric.local_date.desc())
        .limit(limit)
    ).all())


def _is_hard(title: str | None) -> bool:
    lowered = (title or "").lower()
    return any(word in lowered for word in HARD_WORDS)


def _easy_day_this_week(session: Session, owner_id: str, today: dt.date) -> str | None:
    """The nearest day whose session is light enough to swap a hard one into."""
    for ahead in range(1, 4):
        day = today + dt.timedelta(days=ahead)
        plan = training.program_for(session, owner_id, day)
        if plan is None or _is_hard(plan.title):
            continue
        lowered = (plan.title or "").lower()
        if any(word in lowered for word in EASY_WORDS) or "зал" in lowered:
            return f"{day.strftime('%d.%m')} ({plan.title})"
    return None


# A factor at or above this is "fine"; the recovery clock at or below the other is "the
# only thing that is wrong". Garmin reports each part of readiness as a percentage.
FACTOR_FINE = 90
FACTOR_COLLAPSED = 25


def _dragged_by_recovery_clock(readiness) -> tuple[float, float] | None:
    """Is the score low only because of the recovery countdown?

    Returns the hours left and the HRV factor when so, otherwise None. Every part has to
    be present: an absent percentage is not a good one, and guessing here would turn a
    genuinely bad morning into permission to train.
    """
    details = (readiness.details or {}) if readiness else {}
    hrv_pct = details.get("hrv_pct")
    stress_pct = details.get("stress_pct")
    recovery_pct = details.get("recovery_pct")
    minutes = details.get("recovery_time_min")
    if None in (hrv_pct, stress_pct, recovery_pct, minutes):
        return None
    if hrv_pct < FACTOR_FINE or stress_pct < FACTOR_FINE:
        return None
    if recovery_pct > FACTOR_COLLAPSED:
        return None
    return minutes / 60, hrv_pct


def morning(session: Session, owner_id: str, *, today: dt.date) -> list[str]:
    """What today's plan should become, given how the night went."""
    day = today.isoformat()
    lines: list[str] = []

    readiness = _metric(session, owner_id, GarminKind.READINESS, day)
    sleep = _metric(session, owner_id, GarminKind.SLEEP, day)
    hrv = _metric(session, owner_id, GarminKind.HRV, day)
    bodyd = _metric(session, owner_id, GarminKind.BODY, day)

    if readiness is None and sleep is None:
        return [
            "Garmin не синхронизировался за сегодня —",
            "советовать по вчерашним цифрам не буду.",
        ]

    plan = training.program_for(session, owner_id, today)
    plan_title = plan.title if plan else None
    hard_today = _is_hard(plan_title)

    score = readiness.value if readiness else None
    previous = [m for m in _recent(session, owner_id, GarminKind.READINESS, 3)
                if m.local_date < day]
    yesterday = previous[0].value if previous else None

    dragged = _dragged_by_recovery_clock(readiness)
    if score is not None and score < READINESS_LOW and dragged:
        # The score is an average of four parts. When three of them are near perfect and
        # only the recovery clock has collapsed, the body is not the thing that is low —
        # a counter is. A jammed GPS can give one easy run a 95-hour
        # recovery time, and readiness falls to 11 while HRV never moves off 100.
        hours, hrv_pct = dragged
        lines.append(f"Готовность {score:.0f}, но её тянет вниз только")
        lines.append(f"счётчик восстановления: {hours:.0f} ч в запасе.")
        lines.append(f"ВСР {hrv_pct:.0f}/100, стресс в норме — тело не просело.")
        lines.append("Тренируйся по плану, ориентируйся на пульс покоя.")
    elif score is not None and score < READINESS_VERY_LOW:
        lines.append(f"Готовность {score:.0f} — очень низкая.")
        lines.append("Сегодня ходьба или отдых. Тяжёлое не")
        lines.append("переносить на завтра: усталость не уйдёт за ночь.")
    elif score is not None and score < READINESS_LOW:
        was = f" против {yesterday:.0f} вчера" if yesterday else ""
        lines.append(f"Готовность {score:.0f}{was}.")
        if hard_today:
            swap = _easy_day_this_week(session, owner_id, today)
            lines.append("На интервалах не выйдешь на мощность —")
            lines.append(f"предлагаю поменять с {swap}." if swap
                         else "предлагаю сдвинуть интенсивную на день.")
        else:
            lines.append("Тренировку оставляем, но держи нижнюю")
            lines.append("границу зоны и не удлиняй.")
    elif score is not None and score >= READINESS_HIGH and not hard_today:
        lines.append(f"Готовность {score:.0f} — высокая.")
        lines.append("Если хочешь, подтяни интенсивную на сегодня.")

    if sleep is not None and sleep.value and sleep.value < SLEEP_SHORT_MIN:
        hours = sleep.value / 60
        lines.append(f"Сон {hours:.1f} ч — меньше шести, интенсивность вниз.")

    if hrv is not None:
        band = (hrv.details or {}).get("baseline") or {}
        low = band.get("balancedLow")
        if low and hrv.value < low:
            lines.append(f"ВСР {hrv.value:.0f} мс ниже нормы ({low}+) — без интенсивности.")

    if bodyd is not None and (bodyd.details or {}).get("resting_hr"):
        rhr = bodyd.details["resting_hr"]
        week = [m for m in _recent(session, owner_id, GarminKind.BODY, 8)
                if (m.details or {}).get("resting_hr") and m.local_date < day]
        if week:
            average = sum(m.details["resting_hr"] for m in week) / len(week)
            if rhr - average >= 5:
                lines.append(
                    f"Пульс покоя {rhr} против {average:.0f} в среднем — "
                    f"недовосстановление или начало болезни."
                )

    return lines[:MAX_LINES]


def evening(session: Session, owner_id: str, *, today: dt.date, totals) -> list[str]:
    """What the day's food means for tomorrow — and the protein counted, not advised."""
    lines: list[str] = []
    macros = targets(session, owner_id)
    target = macros.get("target_protein_g")

    if isinstance(target, list | tuple) and target and totals.meals:
        low = float(target[0])
        gap = low - totals.protein_g
        if gap >= PROTEIN_SHORTFALL_MIN:
            scoops = round(gap / SCOOP_PROTEIN_G * 2) / 2
            lines.append(f"Белок {totals.protein_g:.0f} при норме от {low:.0f}.")
            lines.append(
                f"Не хватает {gap:.0f} г — это {scoops:g} скупа протеина "
                f"({SCOOP_PROTEIN_G:.0f} г белка в скупе)."
            )

    from app.domain.reports import measurement_due  # circular by design: one owner of the rule

    lines += measurement_due(session, owner_id)

    return lines[:MAX_LINES]


def weekly(session: Session, owner_id: str, *, activities: list, zones: list[float]) -> list[str]:
    """What to change next week, counted from the week that happened."""
    lines: list[str] = []
    total = sum(zones)

    if total:
        easy = (zones[0] + zones[1]) / total * 100
        grey = zones[2] / total * 100
        hard = (zones[3] + zones[4]) / total * 100
        if grey > 15:
            lines.append(f"Z3 {grey:.0f}% при ориентире до 15 —")
            lines.append("серая зона, держи лёгкие дни легче.")
        if easy < 75:
            lines.append(f"Лёгкой работы {easy:.0f}% при ориентире от 75.")
        if hard < 5:
            lines.append(f"Интенсивной {hard:.0f}% — VO₂max не состоялся.")

    week_load = sum((a.details or {}).get("training_load") or 0 for a in activities)
    if week_load:
        lines.append(f"Нагрузка за неделю {week_load:.0f}.")

    return lines[:MAX_LINES]
