"""The daily and weekly reports the owner reads in Telegram.

Assembled by code from the database, never written by the model. That is deliberate and
it is his requirement: the same fields in the same order every day, so a glance finds the
number without reading prose. A model writing these would drift in shape, invent a figure
on a quiet day, and cost tokens for the privilege.

The one place judgement enters is the recommendation block, and even there each line must
carry the figure it rests on: not "you recovered badly" but "readiness 42 against 77".
Rules live in `advice.py`; this module lays out what they return.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.domain import advice, body, diary, training
from app.models.base import utcnow
from app.models.garmin import GarminKind, GarminMetric
from app.models.training import SetLog, WorkoutSession

WEEKDAYS_RU = (
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
)

NO_DATA = "нет данных"


@dataclass
class Report:
    """A report ready to send, plus what it could not find."""

    text: str
    missing: list[str] = field(default_factory=list)


def _local_today() -> dt.date:
    return utcnow().astimezone(get_settings().tz).date()


def _metric(session: Session, owner_id: str, kind: GarminKind, day: str) -> GarminMetric | None:
    return session.scalar(
        select(GarminMetric).where(
            GarminMetric.owner_id == owner_id,
            GarminMetric.kind == kind,
            GarminMetric.local_date == day,
        )
    )


def _latest(session: Session, owner_id: str, kind: GarminKind) -> GarminMetric | None:
    return session.scalar(
        select(GarminMetric)
        .where(GarminMetric.owner_id == owner_id, GarminMetric.kind == kind)
        .order_by(GarminMetric.local_date.desc())
    )


def _staleness(metric: GarminMetric | None, today: dt.date) -> str | None:
    """How old a figure is, in words, or None when it is today's.

    A missing line must never read as "all fine": the report says how old the data is,
    or that there is none.
    """
    if metric is None:
        return NO_DATA
    age = (today - dt.date.fromisoformat(metric.local_date)).days
    if age <= 0:
        return None
    return f"данные за {metric.local_date}, {age} дн. назад"


def _hm(minutes: float | None) -> str:
    if not minutes:
        return NO_DATA
    return f"{int(minutes // 60)}:{int(minutes % 60):02d}"


def silence(session: Session, owner_id: str) -> Report:
    """Why the morning report has not come.

    The morning report waits for the night's data and has no deadline. The hole that
    leaves: when the fault is on this side — a sync that stopped, a dead Garmin token —
    the waiting looks exactly like a quiet morning, and the owner would only find out by
    noticing the absence. That is the wrong way round.

    So silence gets a voice, once, and only when the data is genuinely old: a watch that
    has simply not synced yet is the owner's business, a service that stopped asking is
    the service's.
    """
    today = _local_today()
    freshest = session.scalar(
        select(GarminMetric)
        .where(GarminMetric.owner_id == owner_id)
        .order_by(GarminMetric.local_date.desc(), GarminMetric.created_at.desc())
    )
    lines = ["⚠️  Утреннего отчёта нет"]
    if freshest is None:
        lines.append("Данных Garmin нет вообще.")
    else:
        age = (today - dt.date.fromisoformat(freshest.local_date)).days
        seen = freshest.created_at.astimezone(get_settings().tz)
        lines.append(f"Свежие данные Garmin — за {freshest.local_date}"
                     f"{f', {age} дн. назад' if age > 0 else ''}.")
        lines.append(f"Последняя запись получена в {seen:%H:%M %d.%m}.")
    lines.append("")
    lines.append("Отчёт ждёт сон, готовность и сводку дня. Синхронизируй часы —")
    lines.append("он уйдёт сам, как только данные придут.")
    return Report(text="\n".join(lines), missing=["morning"])


def morning(session: Session, owner_id: str) -> Report:
    """Recovery, sleep, and what today's plan should become in light of them."""
    today = _local_today()
    day = today.isoformat()
    missing: list[str] = []

    readiness = _metric(session, owner_id, GarminKind.READINESS, day) or _latest(
        session, owner_id, GarminKind.READINESS
    )
    sleep = _metric(session, owner_id, GarminKind.SLEEP, day) or _latest(
        session, owner_id, GarminKind.SLEEP
    )
    hrv = _metric(session, owner_id, GarminKind.HRV, day) or _latest(
        session, owner_id, GarminKind.HRV
    )
    bodyd = _metric(session, owner_id, GarminKind.BODY, day) or _latest(
        session, owner_id, GarminKind.BODY
    )

    lines = [f"☀️  {today.strftime('%d.%m')}, {WEEKDAYS_RU[today.weekday()]}", ""]

    stale = _staleness(readiness, today)
    if readiness is None:
        lines.append(f"ГОТОВНОСТЬ   {NO_DATA}")
        missing.append("readiness")
    else:
        level = (readiness.details or {}).get("level", "")
        word = {"LOW": "низкая", "MODERATE": "средняя", "HIGH": "высокая",
                "VERY_HIGH": "очень высокая"}.get(level, level.lower())
        lines.append(f"ГОТОВНОСТЬ   {readiness.value:.0f}/100 — {word}")
        if stale:
            lines.append(f"  {stale}")

    if sleep is None:
        lines.append(f"СОН          {NO_DATA}")
        missing.append("sleep")
    else:
        details = sleep.details or {}
        lines.append(f"СОН          {_hm(sleep.value)} · оценка {details.get('score', '—')}")
        deep, rem = details.get("deep_min"), details.get("rem_min")
        if deep or rem:
            lines.append(f"  глубокий {_hm(deep)} · быстрый {_hm(rem)}")
        if s := _staleness(sleep, today):
            lines.append(f"  {s}")

    if hrv is not None:
        base = (hrv.details or {}).get("baseline") or {}
        band = ""
        if base.get("balancedLow") and base.get("balancedUpper"):
            band = f" · норма {base['balancedLow']}–{base['balancedUpper']}"
        lines.append(f"ВСР          {hrv.value:.0f} мс{band}")
    else:
        missing.append("hrv")

    if bodyd is not None:
        details = bodyd.details or {}
        if details.get("resting_hr"):
            lines.append(f"Пульс покоя  {details['resting_hr']}")
        low, high = details.get("body_battery_low"), details.get("body_battery_high")
        if low and high:
            lines.append(f"Батарея тела {low} → {high}")
        if bodyd.value:
            lines.append(f"Вес          {bodyd.value} кг")

    plan = training.program_for(session, owner_id, today)
    lines += ["", "СЕГОДНЯ ПО ПЛАНУ"]
    lines.append(f"  {plan.title if plan else 'день отдыха'}")

    recommendation = advice.morning(session, owner_id, today=today)
    if recommendation:
        lines += ["", "РЕКОМЕНДАЦИЯ"]
        lines += [f"  {line}" for line in recommendation]

    return Report("\n".join(lines), missing)


def evening(session: Session, owner_id: str) -> Report:
    """What the day came to: food against targets, the session that happened, tomorrow."""
    today = _local_today()
    day = today.isoformat()
    missing: list[str] = []

    totals = diary.day_totals(session, owner_id, day=day)
    macros = advice.targets(session, owner_id)

    lines = [f"🌙  Итог {today.strftime('%d.%m')}, {WEEKDAYS_RU[today.weekday()]}", ""]

    if totals.meals:
        kcal_range = macros.get("daily_calories")
        lines.append(f"ЕДА          {totals.kcal:.0f} ккал · {advice.range_text(kcal_range)}")
        for label, value, target in (
            ("белок", totals.protein_g, macros.get("target_protein_g")),
            ("жир", totals.fat_g, macros.get("target_fat_g")),
            ("углеводы", totals.carbs_g, macros.get("target_net_carbs_g")),
        ):
            mark = advice.mark(value, target)
            lines.append(f"  {label:<10} {value:>5.0f} г / {advice.range_text(target):<9} {mark}")
        lines.append(f"  приёмов    {len(totals.meals)}")
        if not totals.complete:
            lines.append(f"  ⚠ {totals.warning}")
    else:
        lines.append(f"ЕДА          {NO_DATA} за сегодня")
        missing.append("diary")

    workout = session.scalar(
        select(WorkoutSession).where(
            WorkoutSession.owner_id == owner_id, WorkoutSession.local_date == day
        )
    )
    activity = _metric(session, owner_id, GarminKind.ACTIVITY, day)
    if activity is not None:
        details = activity.details or {}
        lines += ["", "ТРЕНИРОВКА"]
        lines.append(f"  {activity.title or 'без названия'}")
        parts = [f"{activity.duration_min:.0f} мин" if activity.duration_min else None,
                 f"{activity.calories:.0f} ккал" if activity.calories else None]
        lines.append("  " + " · ".join(p for p in parts if p))
        zones = details.get("hr_zones")
        if zones:
            lines.append("  " + " · ".join(f"Z{i} {m:.0f}" for i, m in enumerate(zones, 1) if m))
    elif workout is not None:
        sets = session.scalars(select(SetLog).where(SetLog.session_id == workout.id)).all()
        lines += ["", "ТРЕНИРОВКА", f"  {workout.title or 'зал'} · подходов {len(sets)}"]
    else:
        lines += ["", f"ТРЕНИРОВКА   {NO_DATA}"]

    tomorrow = today + dt.timedelta(days=1)
    plan = training.program_for(session, owner_id, tomorrow)
    lines += ["", "ЗАВТРА", f"  {plan.title if plan else 'день отдыха'}"]

    from app.domain import planning

    proposal = planning.propose_from(session, owner_id, today)
    if proposal is not None:
        lines += ["", f"СЛЕДУЮЩАЯ {WEEKDAYS_RU[today.weekday()].upper()}"]
        lines += [f"  {line}" for line in proposal.as_text()[1:]]
        lines += ["", "  Записать в Garmin? Ответь «да»"]

    recommendation = advice.evening(session, owner_id, today=today, totals=totals)
    if recommendation:
        lines += ["", "РЕКОМЕНДАЦИЯ"]
        lines += [f"  {line}" for line in recommendation]

    return Report("\n".join(lines), missing)


def weekly(session: Session, owner_id: str) -> Report:
    """The week as volume, zones and what to change next week."""
    today = _local_today()
    start = today - dt.timedelta(days=6)
    days = [(start + dt.timedelta(days=i)).isoformat() for i in range(7)]

    activities = [
        m for m in session.scalars(
            select(GarminMetric).where(
                GarminMetric.owner_id == owner_id, GarminMetric.kind == GarminKind.ACTIVITY
            )
        ).all()
        if m.local_date in days
    ]

    lines = [f"📊  Неделя {start.strftime('%d.%m')}–{today.strftime('%d.%m')}", ""]
    if not activities:
        lines.append(f"ТРЕНИРОВКИ   {NO_DATA} за неделю")
        return Report("\n".join(lines), ["activities"])

    minutes = sum(a.duration_min or 0 for a in activities)
    kcal = sum(a.calories or 0 for a in activities)
    zone_totals = [0.0] * 5
    for a in activities:
        for i, value in enumerate((a.details or {}).get("hr_zones") or []):
            if i < 5:
                zone_totals[i] += value or 0

    lines += ["ОБЪЁМ", f"  {len(activities)} тренировок · {_hm(minutes)}",
              f"  сожжено     {kcal:.0f} ккал"]

    total_zone = sum(zone_totals)
    if total_zone:
        lines += ["", f"ВРЕМЯ В ЗОНАХ         {total_zone:.0f} мин"]
        for i, value in enumerate(zone_totals, 1):
            if value:
                share = value / total_zone * 100
                bar = "█" * max(1, round(share / 5))
                lines.append(f"  Z{i} {value:>5.0f} мин  {bar:<10} {share:>3.0f}%")

    verdict = advice.weekly(session, owner_id, activities=activities, zones=zone_totals)
    if verdict:
        lines += ["", "НА СЛЕДУЮЩУЮ НЕДЕЛЮ"]
        lines += [f"  {line}" for line in verdict]

    return Report("\n".join(lines), [])


def measurement_due(session: Session, owner_id: str, *, every_days: int = 14) -> list[str]:
    """Whether it is time to take the tape out, and how overdue it is.

    Every fortnight: a week apart measures water and salt, a month apart lets three
    kilograms of lean mass disappear before anyone notices.
    """
    rows = body.history(session, owner_id, limit=1)
    today = _local_today()
    if not rows:
        return ["Замеров тела ещё не было.", "Сделай первый утром, натощак."]
    last = dt.date.fromisoformat(rows[-1].measured_on)
    overdue = (today - last).days - every_days
    if overdue < 0:
        return []
    if overdue == 0:
        return [f"Пора мерить тело: {every_days} дней с {rows[-1].measured_on}."]
    return [
        f"Замер тела просрочен на {overdue} дн.",
        f"Последний — {rows[-1].measured_on}.",
        "Утром, натощак, живот на уровне пупка.",
    ]
