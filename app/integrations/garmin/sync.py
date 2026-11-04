"""Syncing with Garmin: raw payloads, metrics, and honest freshness.

This service is the single owner of the tokens. Garmin lets only one owner refresh them:
any other connector using the same tokens (a desktop script, another MCP server) must be
switched off, or the two will take the access away from each other in turns and break it
silently.

Why freshness is a metric of its own rather than "no data": Garmin breaks regularly after
their own updates, and that is a normal state, not an outage. The system must be able to
say "the last sleep record is from Thursday" instead of passing the night before last off
as last night, or saying nothing.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.base import local_today, utcnow
from app.models.garmin import GarminKind, GarminMetric, GarminRaw
from app.models.training import SetLog

log = logging.getLogger(__name__)

TOKEN_DIRNAME = "garmin-tokens"

# Older than this and the data is called stale out loud. Sleep arrives in the morning,
# activities within an hour of a session; a day with nothing means something broke.
STALE_AFTER = {
    GarminKind.ACTIVITY: dt.timedelta(days=2),
    GarminKind.SLEEP: dt.timedelta(days=1, hours=12),
    GarminKind.HRV: dt.timedelta(days=1, hours=12),
    GarminKind.READINESS: dt.timedelta(days=1, hours=12),
    GarminKind.BODY: dt.timedelta(days=7),
}


class GarminAuthError(Exception):
    """The token is dead. A human fixes it: login and 2FA are closed to automation."""


@dataclass
class SyncReport:
    raw_saved: int = 0
    metrics_saved: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"raw: {self.raw_saved}, metrics: {self.metrics_saved}, "
            f"already had: {self.skipped}, errors: {len(self.errors)}"
        )


@dataclass
class Freshness:
    kind: GarminKind
    last_event: dt.datetime | None
    last_fetch: dt.datetime | None

    @property
    def stale(self) -> bool:
        if self.last_event is None:
            return True
        return utcnow() - self.last_event > STALE_AFTER[self.kind]

    def describe(self) -> str:
        if self.last_event is None:
            return f"{self.kind.value}: no data at all"
        age = utcnow() - self.last_event
        days = age.days
        when = "today" if days == 0 else f"{days} d ago"
        mark = " — STALE DATA, say so out loud" if self.stale else ""
        return f"{self.kind.value}: last event {when}{mark}"


def token_dir() -> Path:
    """Where the tokens live. No need for extra copies of credentials — the path is set."""
    settings = get_settings()
    if settings.garmin_token_dir is not None:
        return settings.garmin_token_dir.expanduser()
    return settings.data_dir / TOKEN_DIRNAME


def connect():
    """A Garmin client on stored tokens. Password and 2FA are not available here."""
    from garminconnect import Garmin

    path = token_dir()
    if not path.exists():
        raise GarminAuthError(
            f"No token directory at {path}. Run `python -m app.cli garmin-login` once — "
            f"the service cannot pass a login and 2FA on its own."
        )
    client = Garmin()
    try:
        client.login(str(path))
    except Exception as exc:  # noqa: BLE001 — the library raises all sorts
        raise GarminAuthError(f"Login with the stored token failed: {exc}") from exc
    return client


# --- storing -----------------------------------------------------------------------

def _store_raw(
    session: Session,
    owner_id: str,
    *,
    kind: GarminKind,
    external_id: str,
    payload: dict,
    event_at: dt.datetime | None,
    local_date: str | None,
    report: SyncReport,
) -> GarminRaw | None:
    existing = session.scalar(
        select(GarminRaw).where(
            GarminRaw.owner_id == owner_id,
            GarminRaw.kind == kind,
            GarminRaw.external_id == external_id,
        )
    )
    if existing is not None:
        if existing.payload == payload:
            report.skipped += 1
            return None
        # A day's record is not finished the first time it appears. Garmin publishes a
        # stub while he is still asleep and fills the night in an hour later; the same
        # happens to the daily summary and to readiness. Treating "seen this id before"
        # as "nothing to do" froze an empty sleep record for a whole day, and the morning
        # report then said "нет данных" while Garmin Connect showed the full night.
        existing.payload = payload
        existing.event_at = event_at or existing.event_at
        existing.local_date = local_date or existing.local_date
        existing.fetched_at = utcnow()
        session.flush()
        report.raw_saved += 1
        return existing

    raw = GarminRaw(
        owner_id=owner_id,
        kind=kind,
        external_id=external_id,
        event_at=event_at,
        local_date=local_date,
        fetched_at=utcnow(),
        payload=payload,
    )
    session.add(raw)
    session.flush()
    report.raw_saved += 1
    return raw


def _store_metric(
    session: Session, owner_id: str, *, kind: GarminKind, external_id: str, **fields
) -> GarminMetric | None:
    existing = session.scalar(
        select(GarminMetric).where(
            GarminMetric.owner_id == owner_id,
            GarminMetric.kind == kind,
            GarminMetric.external_id == external_id,
        )
    )
    if existing is not None:
        # A metric is a mirror of what Garmin holds, so a second pass refreshes it rather
        # than skipping: when a new field is added to the parser — zone minutes, training
        # load — every activity already stored would otherwise stay blank forever, and
        # the reports that read those fields would show nothing with no way to notice.
        details = fields.pop("details", None)
        for key, value in fields.items():
            if value is not None:
                setattr(existing, key, value)
        if details:
            existing.details = {**(existing.details or {}), **details}
        existing.fetched_at = utcnow()
        session.flush()
        return None
    metric = GarminMetric(
        owner_id=owner_id, kind=kind, external_id=external_id,
        fetched_at=utcnow(), **fields
    )
    session.add(metric)
    session.flush()
    return metric


def _moment(value) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, int | float):
        return dt.datetime.fromtimestamp(value / 1000, tz=dt.UTC)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return None


# --- parsing -----------------------------------------------------------------------

def import_activities(
    session: Session, owner_id: str, activities: list[dict], report: SyncReport
) -> None:
    for entry in activities:
        external_id = str(entry.get("activityId") or entry.get("activity_id") or "")
        if not external_id:
            report.errors.append("An activity with no id — skipped")
            continue
        # The day is taken from UTC converted into the owner's timezone, never from
        # Garmin's own local time: the watch trusts GPS for its zone, and jammed GPS put
        # a morning run in Lima at 03:03. UTC survives that; a training day does not
        # move because a satellite lied.
        started = _moment(entry.get("startTimeGMT") or entry.get("startTimeLocal"))
        local_date = (started or utcnow()).astimezone(get_settings().tz).date().isoformat()

        # The raw payload is stored once; the metric is refreshed every pass. Returning
        # early on a known activity was why adding a field to the parser never reached
        # anything already imported.
        _store_raw(
            session, owner_id, kind=GarminKind.ACTIVITY, external_id=external_id,
            payload=entry, event_at=started, local_date=local_date, report=report,
        )

        duration = entry.get("duration")
        metric = _store_metric(
            session, owner_id, kind=GarminKind.ACTIVITY, external_id=external_id,
            event_at=started, local_date=local_date,
            title=entry.get("activityName"),
            duration_min=round(duration / 60, 1) if duration else None,
            calories=entry.get("calories"),
            avg_hr=entry.get("averageHR"),
            max_hr=entry.get("maxHR"),
            value=entry.get("aerobicTrainingEffect"),
            details={
                "type": (entry.get("activityType") or {}).get("typeKey"),
                "distance_m": entry.get("distance"),
                # Minutes per heart-rate zone, as the watch counted them at the time.
                # Garmin never recomputes these when zones change: they are the record
                # of that session, and the daily report reads them rather than the
                # summary, which says nothing about where the work actually sat.
                "hr_zones": [
                    round((entry.get(f"hrTimeInZone_{i}") or 0) / 60, 1) for i in range(1, 6)
                ],
                "training_load": entry.get("activityTrainingLoad"),
                "aerobic_effect": entry.get("aerobicTrainingEffect"),
                "anaerobic_effect": entry.get("anaerobicTrainingEffect"),
            },
        )
        if metric is not None:
            report.metrics_saved += 1


def import_sleep(
    session: Session, owner_id: str, day: str, payload: dict, report: SyncReport
) -> None:
    block = payload.get("dailySleepDTO") or payload
    seconds = block.get("sleepTimeSeconds")
    started = _moment(block.get("sleepStartTimestampGMT"))

    raw = _store_raw(
        session, owner_id, kind=GarminKind.SLEEP, external_id=day, payload=payload,
        event_at=started, local_date=day, report=report,
    )
    if raw is None:
        return
    # The score sits inside dailySleepDTO rather than at the top level: verified against
    # a real response. Check both places — their format moves.
    scores = block.get("sleepScores") or payload.get("sleepScores") or {}
    overall = scores.get("overall") or {}
    metric = _store_metric(
        session, owner_id, kind=GarminKind.SLEEP, external_id=day,
        event_at=started, local_date=day,
        value=round(seconds / 60, 1) if seconds else None,
        details={
            "deep_min": round((block.get("deepSleepSeconds") or 0) / 60, 1),
            "rem_min": round((block.get("remSleepSeconds") or 0) / 60, 1),
            "score": overall.get("value"),
            "score_qualifier": overall.get("qualifierKey"),
            "feedback": block.get("sleepScoreFeedback"),
        },
    )
    if metric is not None:
        report.metrics_saved += 1


def import_hrv(
    session: Session, owner_id: str, day: str, payload: dict, report: SyncReport
) -> None:
    summary = payload.get("hrvSummary") or payload
    raw = _store_raw(
        session, owner_id, kind=GarminKind.HRV, external_id=day, payload=payload,
        event_at=None, local_date=day, report=report,
    )
    if raw is None:
        return
    metric = _store_metric(
        session, owner_id, kind=GarminKind.HRV, external_id=day,
        event_at=None, local_date=day,
        value=summary.get("lastNightAvg"),
        details={"status": summary.get("status"), "baseline": summary.get("baseline")},
    )
    if metric is not None:
        report.metrics_saved += 1


def import_readiness(
    session: Session, owner_id: str, day: str, payload, report: SyncReport
) -> None:
    """Training readiness: the score and what it was made of.

    Garmin returns a list; we take the first entry, which is for the requested day. The
    factors are kept whole: they show what sagged — sleep, stress or load.
    """
    block = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(block, dict) or block.get("score") is None:
        return

    raw = _store_raw(
        session, owner_id, kind=GarminKind.READINESS, external_id=day, payload=block,
        event_at=_moment(block.get("timestamp")), local_date=day, report=report,
    )
    if raw is None:
        return
    metric = _store_metric(
        session, owner_id, kind=GarminKind.READINESS, external_id=day,
        event_at=raw.event_at, local_date=day,
        value=block.get("score"),
        details={
            "level": block.get("level"),
            "sleep_score": block.get("sleepScore"),
            "sleep_factor": block.get("sleepScoreFactorFeedback"),
            "stress_factor": block.get("stressHistoryFactorFeedback"),
            "recovery_factor": block.get("recoveryTimeFactorFeedback"),
            "acute_load_factor": block.get("acuteLoadFactorFeedback"),
            # The percentages, not only the words. A score is an average of four things,
            # and without the parts there is no way to tell "he is wrecked" from "one
            # derived counter is wrong" — which is exactly what a corrupted activity
            # does to it, while heart-rate variability stays untouched at 100.
            "hrv_pct": block.get("hrvFactorPercent"),
            "sleep_pct": block.get("sleepHistoryFactorPercent"),
            "stress_pct": block.get("stressHistoryFactorPercent"),
            "recovery_pct": block.get("recoveryTimeFactorPercent"),
            "recovery_time_min": block.get("recoveryTime"),
            "acute_load": block.get("acuteLoad"),
        },
    )
    if metric is not None:
        report.metrics_saved += 1


def import_body(
    session: Session, owner_id: str, day: str, payload: dict, report: SyncReport
) -> None:
    """The daily summary: expenditure, resting heart rate, stress, weight if weighed."""
    if not isinstance(payload, dict):
        return
    raw = _store_raw(
        session, owner_id, kind=GarminKind.BODY, external_id=day, payload=payload,
        event_at=None, local_date=day, report=report,
    )
    if raw is None:
        return

    weight = payload.get("weight")
    metric = _store_metric(
        session, owner_id, kind=GarminKind.BODY, external_id=day,
        event_at=None, local_date=day,
        calories=payload.get("totalKilocalories"),
        value=round(weight / 1000, 2) if weight else None,  # Garmin returns grams
        details={
            "active_kcal": payload.get("activeKilocalories"),
            "bmr_kcal": payload.get("bmrKilocalories"),
            "resting_hr": payload.get("restingHeartRate"),
            "stress_avg": payload.get("averageStressLevel"),
            "steps": payload.get("totalSteps"),
            "body_battery_high": payload.get("bodyBatteryHighestValue"),
            "body_battery_low": payload.get("bodyBatteryLowestValue"),
        },
    )
    if metric is not None:
        report.metrics_saved += 1


def sync(session: Session, owner_id: str, client, *, days_back: int = 3) -> SyncReport:
    """Fetch activities, sleep and HRV for recent days. One kind failing spares the rest."""
    report = SyncReport()
    today = local_today()

    try:
        activities = client.get_activities(0, 20) or []
        import_activities(session, owner_id, activities, report)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"Activities: {exc}")

    for offset in range(days_back):
        day = (today - dt.timedelta(days=offset)).isoformat()
        try:
            if payload := client.get_sleep_data(day):
                import_sleep(session, owner_id, day, payload, report)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"Sleep {day}: {exc}")
        try:
            if payload := client.get_hrv_data(day):
                import_hrv(session, owner_id, day, payload, report)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"HRV {day}: {exc}")
        try:
            if payload := client.get_training_readiness(day):
                import_readiness(session, owner_id, day, payload, report)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"Readiness {day}: {exc}")
        try:
            if payload := client.get_stats_and_body(day):
                import_body(session, owner_id, day, payload, report)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"Daily summary {day}: {exc}")

    import_strength_sets(session, owner_id, client, report, days_back=max(days_back, 7))

    return report


# --- reading -----------------------------------------------------------------------

def freshness(session: Session, owner_id: str) -> list[Freshness]:
    """How fresh each kind of data is. Asked before any advice is given."""
    result = []
    for kind in GarminKind:
        row = session.execute(
            select(GarminMetric.event_at, GarminMetric.local_date, GarminMetric.fetched_at)
            .where(GarminMetric.owner_id == owner_id, GarminMetric.kind == kind)
            .order_by(GarminMetric.local_date.desc())
            .limit(1)
        ).first()
        if row is None:
            result.append(Freshness(kind=kind, last_event=None, last_fetch=None))
            continue
        event_at, local_date, fetched_at = row
        if event_at is None and local_date:
            event_at = dt.datetime.fromisoformat(local_date).replace(tzinfo=dt.UTC)
        result.append(Freshness(kind=kind, last_event=event_at, last_fetch=fetched_at))
    return result


def latest(
    session: Session, owner_id: str, kind: GarminKind, *, limit: int = 5
) -> list[GarminMetric]:
    return list(
        session.scalars(
            select(GarminMetric)
            .where(GarminMetric.owner_id == owner_id, GarminMetric.kind == kind)
            .order_by(GarminMetric.local_date.desc())
            .limit(limit)
        )
    )

# --- strength sets -------------------------------------------------------------------

# Garmin reports set weight in grams
GRAMS_PER_KG = 1000.0

# Garmin names movements with its own keys, and those keys are what a workout written
# back into the watch must use — so the key is kept on the exercise and only the display
# name is translated. Anything missing here keeps the Garmin key as its name, which is
# ugly but honest: an invented translation would hide which movement it actually was.
GARMIN_EXERCISE_NAMES = {
    "BAND_ASSISTED_PULL_UP": "Подтягивания с резиной",
    "DUMBBELL_REVERSE_LUNGE": "Обратные выпады с гантелями",
    "HYPEREXTENSION": "Гиперэкстензия",
    "FACE_PULL": "Тяга к лицу",
    "HAMSTRING_CURL": "Сгибание ног лёжа",
    "DEAD_BUG": "Мёртвый жук",
    "SUPERMAN_FROM_FLOOR": "Супермен лёжа",
    "LUNGE": "Выпады",
    "SIDE_PLANK": "Боковая планка",
    "PLANK": "Планка",
    "CARDIO": "Кардио-разминка",
    "CORE": "Работа на корпус",
    "UNKNOWN": "Упражнение не опознано часами",
}


def _exercise_for_set(session: Session, owner_id: str, block: dict):
    """The exercise a Garmin set belongs to, created on first sight if unknown.

    Garmin names movements with its own keys — DUMBBELL_REVERSE_LUNGE — and the registry
    already carries a `garmin_key` for exactly this join. A key we have never seen is
    added rather than dropped: losing a set loses the progression that depends on it.
    """
    from app.domain import training

    exercises = block.get("exercises") or []
    key = next((e.get("name") for e in exercises if e.get("name")), None)
    category = next((e.get("category") for e in exercises if e.get("category")), None)
    if key is None and category in (None, "UNKNOWN"):
        return None

    needle = key or category
    found = training.find_exercise(session, owner_id, needle)
    if found is not None:
        if found.garmin_key is None and key:
            found.garmin_key = key
        return found
    label = GARMIN_EXERCISE_NAMES.get(needle) or needle.replace("_", " ").capitalize()
    return training.add_exercise(
        session, owner_id, label, category=category, garmin_key=key,
    )


def import_strength_sets(
    session: Session, owner_id: str, client, report: SyncReport, days_back: int = 7
) -> None:
    """Pull the sets of strength sessions into the training history.

    The activity summary carries no weights — only "3 sets of push-ups" — so the figure
    the owner actually needs, "45 kg for 12", exists nowhere but in this endpoint. That
    is why these are fetched rather than read back from the activity, and why this is not
    a duplicate of what Garmin holds: it is the only shape in which the number is usable.

    Rest blocks are skipped, and so are sets Garmin could not identify: a set with no
    exercise and no reps teaches the progression nothing.
    """
    from app.domain import training
    from app.models.training import SessionSource

    cutoff = (utcnow() - dt.timedelta(days=days_back)).date().isoformat()
    try:
        activities = client.get_activities(0, 20)
    except Exception as exc:  # noqa: BLE001 — one failing kind must not stop the rest
        report.errors.append(f"Strength sets: {exc}")
        return

    for activity in activities:
        started = str(activity.get("startTimeLocal") or "")[:10]
        type_key = ((activity.get("activityType") or {}).get("typeKey") or "")
        if not started or started < cutoff or type_key != "strength_training":
            continue

        activity_id = str(activity.get("activityId"))
        try:
            payload = client.get_activity_exercise_sets(activity_id)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"Sets of {activity_id}: {exc}")
            continue

        blocks = [b for b in (payload.get("exerciseSets") or []) if b.get("setType") == "ACTIVE"]
        if not blocks:
            continue

        workout = training.open_session(
            session, owner_id,
            day=dt.date.fromisoformat(started),
            title=activity.get("activityName"),
            source=SessionSource.GARMIN,
            external_id=activity_id,
            duration_min=round((activity.get("duration") or 0) / 60, 1),
        )
        # The template that was executed, kept so a session can be recognised by type
        # rather than by the date it happened to land on.
        if template := activity.get("workoutId"):
            workout.garmin_workout_id = str(template)

        # A session already filled is re-read only when Garmin now reports a different
        # number of active blocks. Skipping it outright meant a set corrected on the watch
        # after the first sync never landed — a nuisance while nothing depended on it, and
        # a wrong weight in next week's plan once something did.
        if workout.sets and len(workout.sets) == len(blocks):
            report.skipped += 1
            continue
        if workout.sets:
            session.execute(delete(SetLog).where(SetLog.session_id == workout.id))
            # The loaded collection would otherwise stay as it was and the rewritten sets
            # would be invisible to everything holding this object.
            session.expire(workout, ["sets"])
            session.flush()

        written = 0
        for block in blocks:
            exercise = _exercise_for_set(session, owner_id, block)
            reps = block.get("repetitionCount")
            if exercise is None or (reps is None and not block.get("duration")):
                continue
            weight = block.get("weight")
            training.log_set(
                session, owner_id,
                exercise_name=exercise.name,
                reps=reps,
                weight_kg=round(weight / GRAMS_PER_KG, 1) if weight else None,
                duration_s=block.get("duration"),
                workout=workout,
            )
            written += 1
        report.metrics_saved += written
