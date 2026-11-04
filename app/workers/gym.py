"""Writing the next week of training into the watch, every night, without a model.

This module is the reason the whole feature exists: workouts that keep arriving while the
owner is on holiday, with no assistant session open and no agent awake. So it
imports `planning`, `workout_builder` and the Garmin client — and never `anthropic`. A
test asserts that, because the premise is one careless import away from being false.

Three decisions are load-bearing and easy to undo by accident.

**One durable template per program day, rewritten in place.** Not a new workout per date.
`update_workout` keeps the id, so the calendar entry stays attached and nothing is ever
deleted — which means there is no window in which a delete succeeded and an upload did
not, and no way for the service to remove something the owner made himself.

**A digest guards the Garmin call.** Most nights change nothing; a rerun that produces the
same plan must not touch the network at all.

**Every write is read back.** Garmin answers 200 for a payload it has partly ignored, so
the workout is fetched again and checked before the plan is recorded as written.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging

from sqlalchemy import select

from app.core.jobs import handler
from app.db import session_scope
from app.models.ops import Job
from app.models.training import PlannedWorkout, PlanStatus, ProgramDay
from app.workers import notify

log = logging.getLogger(__name__)

JOB_KIND = "gym.plan"

# One minute after the evening report, four before the weekly one, and before most people
# are asleep, so the message is read the same evening. Earlier than the data strictly wants:
# an evening session may not have reached Garmin yet at 22:01. That costs nothing, because
# the job runs every night and rewrites the same seven days — a session that lands late is
# picked up by tomorrow's run, and the plan it affects is a week away.
PLAN_AT = (22, 1)

HORIZON_DAYS = 7


def _digest(plan) -> str:
    """A fingerprint of the plan as written, so an unchanged night costs nothing."""
    body = json.dumps(
        [
            [e.name, e.sets, e.reps, e.weight_kg, e.duration_s, e.reason]
            for e in plan.exercises
        ],
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(body.encode()).hexdigest()


def _as_rows(plan) -> list[dict]:
    return [
        {
            "name": e.name, "sets": e.sets, "reps": e.reps, "weight_kg": e.weight_kg,
            "duration_s": e.duration_s, "reason": e.reason, "confidence": e.confidence,
        }
        for e in plan.exercises
    ]


def _load(entry: dict) -> str:
    if entry["duration_s"]:
        return f"{entry['duration_s']:g} сек"
    if entry["weight_kg"]:
        return f"{entry['weight_kg']:g} кг"
    return "свой вес"


def _line(entry: dict) -> str:
    return (f"  {entry['name'][:26]:<27} {entry['sets']}× {_load(entry):<9} "
            f"{entry['reason']}")


def _changes(before: list[dict] | None, after: list[dict]) -> list[str]:
    """Only what moved since the last time this day was written.

    The message used to spell out every exercise of every day — thirty lines he had to
    read to find the two numbers that had changed. A plan he already has on his watch does
    not need repeating; a weight that moved does.
    """
    was = {row["name"]: row for row in (before or [])}
    moved = []
    for row in after:
        old = was.get(row["name"])
        if old is None:
            moved.append(f"{row['name']}: {_load(row)} — новое")
            continue
        if _load(old) != _load(row):
            moved.append(f"{row['name']}: {_load(old)} → {_load(row)}")
    return moved


def _readiness_note(session, owner_id: str, day: dt.date) -> str | None:
    """His readiness, stated but not acted on — that was his decision, not an oversight.

    Labelled as this morning's on purpose: at 23:10 the figure describes a day already
    over and must not be read as a forecast for tomorrow.
    """
    from app.domain import reports
    from app.models.garmin import GarminKind

    try:
        row = reports._metric(session, owner_id, GarminKind.READINESS, day.isoformat())
        value = row.value if row is not None else None
        if value is None:
            return None
        return f"Готовность сегодня утром {float(value):g} — на план не влияет."
    except Exception:  # noqa: BLE001
        # The whole note is decoration. It cost fifteen duplicate workouts once, because
        # it raised *after* the week had been written to Garmin and took the record of
        # that with it, so the retry wrote everything again.
        log.warning("gym plan: readiness note skipped", exc_info=True)
        return None


def _scheduled_dates(client, day: dt.date, cache: dict) -> set[tuple[str, str]]:
    """What Garmin already has in the calendar for that month, as (date, workout id).

    Scheduling is not idempotent: calling it twice for the same day puts the workout in
    the calendar twice. Updating a workout is idempotent, so only this half needs asking
    first — and a day that keeps failing verification would otherwise add an entry on
    every nightly run for as long as it kept failing.
    """
    key = (day.year, day.month)
    if key in cache:
        return cache[key]
    found: set[tuple[str, str]] = set()
    try:
        payload = client.get_scheduled_workouts(day.year, day.month)
    except Exception:  # noqa: BLE001 — unknown beats wrong: fall through and schedule
        log.warning("gym plan: calendar unreadable for %s", key, exc_info=True)
        cache[key] = found
        return found
    items = payload if isinstance(payload, list) else (payload or {}).get(
        "calendarItems") or []
    for item in items:
        date = item.get("date") or item.get("scheduledDate")
        workout_id = item.get("workoutId")
        if date and workout_id:
            found.add((str(date), str(workout_id)))
    cache[key] = found
    return found


def _waiting_on_an_earlier_session(session, owner_id: str, program_day: ProgramDay,
                                   today: dt.date, for_date: dt.date) -> bool:
    """Is there an unfinished session of this slot before the date being planned?

    His objection, and it is right: writing next Monday while this Monday has not been
    trained is putting a number into an equation you already know you will replace. The
    plan for a slot is only meaningful once the session it is computed from exists — so a
    day waits until its predecessor is in the books, and appears the night after.
    """
    from app.models.training import WorkoutSession

    slot = program_day.garmin_workout_id
    if not slot:
        return False
    day = today
    while day < for_date:
        if day.weekday() == for_date.weekday():
            done = session.scalar(
                select(WorkoutSession).where(
                    WorkoutSession.owner_id == owner_id,
                    WorkoutSession.garmin_workout_id == slot,
                    WorkoutSession.local_date == day.isoformat(),
                )
            )
            if done is None:
                return True
        day += dt.timedelta(days=1)
    return False


def days_to_plan(session, owner_id: str, today: dt.date):
    """The days worth planning, in order — the single answer to "what is next".

    Both the nightly job and the dry run ask this. They used to decide separately, and the
    dry run went on showing a day the job had already learned to withhold.
    """
    from app.models.training import ProgramDay as _Day

    for offset in range(1, HORIZON_DAYS + 1):
        for_date = today + dt.timedelta(days=offset)
        program_day = session.scalar(
            select(_Day).where(
                _Day.owner_id == owner_id,
                _Day.weekday == for_date.weekday(),
                _Day.is_active.is_(True),
            )
        )
        if program_day is None:
            continue
        if _waiting_on_an_earlier_session(session, owner_id, program_day, today, for_date):
            continue
        yield for_date, program_day


def _schedule_only(client, program_day: ProgramDay, for_date: dt.date,
                   calendar: dict) -> str | None:
    """Put an existing template in the calendar without rewriting it.

    For the days the service cannot compute. The workout stays exactly as he built it —
    this only answers "is it on the right date".
    """
    workout_id = program_day.garmin_workout_id
    if not workout_id:
        return None
    if (for_date.isoformat(), str(workout_id)) in _scheduled_dates(
            client, for_date, calendar):
        return {"date": for_date, "title": program_day.title, "changes": []}
    try:
        client.schedule_workout(workout_id, for_date.isoformat())
    except Exception as exc:  # noqa: BLE001
        log.warning("gym plan: %s not scheduled: %s", for_date, exc)
        return None
    calendar.clear()
    return {"date": for_date, "title": program_day.title, "changes": ["поставлено"]}


def compose(blocks: list[dict], failures: list[str], tests: list[str],
            readiness: str | None, today: dt.date) -> str:
    """The message, kept to what the owner would act on.

    Tomorrow's session by name — the detail is already on the watch — then the weights
    that moved, then one line for the days that did not. A message listing every exercise
    of every day takes too long to read to find the two numbers that changed.
    """
    tomorrow = today + dt.timedelta(days=1)
    parts: list[str] = ["🏋️  ПЛАН ОБНОВЛЁН", ""]

    if nxt := next((b for b in blocks if b["date"] == tomorrow), None):
        parts.append(f"Завтра · {_label(tomorrow)} · {nxt['title']}")
        parts.append("")

    moved = [(b, b["changes"]) for b in blocks if b["changes"]]
    if moved:
        parts.append("Изменилось:")
        for block, changes in moved:
            for line in changes:
                parts.append(f"  {_label(block['date'])}  {line}")
        parts.append("")

    quiet = [_label(b["date"]) for b in blocks if not b["changes"]]
    if quiet:
        parts.append(f"Без изменений: {', '.join(quiet)}")

    skipped = sorted({name for b in blocks for name in b.get("skipped") or []})
    if skipped:
        parts.append(f"Не записано: {', '.join(skipped)}")
    if failures:
        parts.append("")
        parts.extend(f"⚠️ {line}" for line in failures)
    if tests:
        parts.extend(tests)
    if readiness:
        parts.append(readiness)
    return "\n".join(parts).rstrip()


def _write_day(session, owner_id: str, client, program_day: ProgramDay,
               for_date: dt.date, calendar: dict | None = None
               ) -> tuple[str | None, str | None]:
    """Build, write and verify one day. Returns (message block, failure)."""
    from app.domain import planning, workout_builder

    plan = planning.plan_day(session, owner_id, program_day, for_date)
    if plan is None:
        return None, None

    digest = _digest(plan)
    existing = session.scalar(
        select(PlannedWorkout).where(
            PlannedWorkout.owner_id == owner_id,
            PlannedWorkout.local_date == for_date.isoformat(),
        )
    )
    previous = list(existing.plan) if existing else None
    if existing and existing.digest == digest and existing.status is PlanStatus.WRITTEN:
        return {"date": for_date, "title": program_day.title, "changes": []}, None

    try:
        workout, skipped = workout_builder.build_for(
            program_day.kind, plan, name=program_day.title
        )
    except workout_builder.NoBuilder:
        return None, f"{_label(for_date)}: {program_day.kind} пока не собирается"
    body = workout.to_dict()

    calendar = {} if calendar is None else calendar
    workout_id = program_day.garmin_workout_id
    try:
        if workout_id:
            client.update_workout(workout_id, body)
        else:
            created = client.upload_workout(body)
            workout_id = str(created.get("workoutId"))
            program_day.garmin_workout_id = workout_id
        if (for_date.isoformat(), str(workout_id)) not in _scheduled_dates(
                client, for_date, calendar):
            client.schedule_workout(workout_id, for_date.isoformat())
    except Exception as exc:  # noqa: BLE001 — one bad day must not cost the week
        if workout_id and _looks_missing(exc):
            created = client.upload_workout(body)
            workout_id = str(created.get("workoutId"))
            program_day.garmin_workout_id = workout_id
            client.schedule_workout(workout_id, for_date.isoformat())
            calendar.clear()
        else:
            _record(session, owner_id, for_date, program_day, workout_id, plan, digest,
                    PlanStatus.FAILED, str(exc)[:200])
            session.commit()
            return None, f"{_label(for_date)}: не записано — {exc}"

    if problem := _verify(client, workout_id):
        _record(session, owner_id, for_date, program_day, workout_id, plan, digest,
                PlanStatus.FAILED, problem)
        session.commit()
        return None, f"{_label(for_date)}: Garmin принял не всё — {problem}"

    _record(session, owner_id, for_date, program_day, workout_id, plan, digest,
            PlanStatus.WRITTEN, None)
    # Committed here, one day at a time, and deliberately not left to the end of the job.
    # A Garmin write cannot be rolled back; the record of it can. When those two came
    # apart — the job raised on its last line, the transaction went back, the queue
    # retried five times — the same three workouts were scheduled five times over.
    session.commit()

    rows = _as_rows(plan)
    return {"date": for_date, "title": program_day.title,
            "changes": _changes(previous, rows), "skipped": skipped}, None


def _looks_missing(exc: Exception) -> bool:
    """A template the owner deleted by hand. Recreated rather than treated as an outage."""
    return "404" in str(exc) or "not found" in str(exc).lower()


def _verify(client, workout_id: str) -> str | None:
    """Read the workout back. Garmin answers 200 for a payload it partly ignored."""
    try:
        back = client.get_workout_by_id(workout_id)
    except Exception as exc:  # noqa: BLE001
        return f"не удалось перечитать: {exc}"

    sport = (back.get("sportType") or {}).get("sportTypeKey")
    if sport != "strength_training":
        return f"тип тренировки вернулся как {sport!r}"
    segments = back.get("workoutSegments") or []
    if not segments:
        return "вернулась без сегментов"
    steps = segments[0].get("workoutSteps") or []
    work = [s for s in steps if (s.get("stepType") or {}).get("stepTypeKey") == "interval"]
    if not work:
        return "вернулась без рабочих шагов"
    if any(not (s.get("description") or "").strip() for s in work):
        return "часть шагов вернулась без описания"
    return None


def _record(session, owner_id, for_date, program_day, workout_id, plan, digest,
            status, note) -> None:
    existing = session.scalar(
        select(PlannedWorkout).where(
            PlannedWorkout.owner_id == owner_id,
            PlannedWorkout.local_date == for_date.isoformat(),
        )
    )
    if existing is None:
        existing = PlannedWorkout(owner_id=owner_id, local_date=for_date.isoformat())
        session.add(existing)
    existing.program_day_id = program_day.id
    existing.garmin_workout_id = workout_id
    existing.plan = _as_rows(plan)
    existing.digest = digest
    existing.status = status
    existing.note = note


WEEKDAY_LABEL = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")


def _label(day: dt.date) -> str:
    return f"{WEEKDAY_LABEL[day.weekday()]} {day.strftime('%d.%m')}"


def plan_week(session, owner_id: str, client, today: dt.date) -> tuple[list[str], list[str]]:
    """Fill the next seven days. Returns the blocks to report and the failures."""
    from app.domain import workout_builder

    blocks: list[str] = []
    failures: list[str] = []
    calendar: dict = {}
    for for_date, program_day in days_to_plan(session, owner_id, today):
        if program_day.kind not in workout_builder.BUILDERS:
            # Cycling, the stair machine and running have no builder yet, but they do
            # have templates he wrote himself. Not being able to compute a session is no
            # reason to leave the day empty: the template is put in the calendar
            # unchanged, and its content is never touched.
            block = _schedule_only(client, program_day, for_date, calendar)
            if block:
                blocks.append(block)
            continue

        block, failure = _write_day(session, owner_id, client, program_day, for_date,
                                    calendar)
        if block:
            blocks.append(block)
        if failure:
            failures.append(failure)
    return blocks, failures


@handler(JOB_KIND)
def run_plan(session, job: Job) -> None:
    """Rebuild the coming week in Garmin and say what changed.

    A dead token alerts off-Telegram and returns without raising: retrying five times
    changes nothing until a human logs in, and the queue would only bury the reason.
    """
    from app.config import get_settings
    from app.integrations.garmin import sync as garmin
    from app.integrations.garmin.jobs import alert
    from app.models.base import utcnow
    from app.models.identity import Principal

    owner = session.scalar(select(Principal))
    if owner is None:
        return

    try:
        client = garmin.connect()
    except garmin.GarminAuthError as exc:
        alert(f"Garmin: план не записан, доступ потерян. {exc}")
        job.payload = {**job.payload, "needs_relogin": True}
        notify.send_message("Тренировки на неделю не записаны: Garmin не пускает. "
                            "Нужен вход руками.")
        return

    today = utcnow().astimezone(get_settings().tz).date()
    link_slots(session, owner.id)
    blocks, failures = plan_week(session, owner.id, client, today)
    tests = keep_tests_scheduled(session, owner.id, client, today)

    if not blocks and not failures and not tests:
        log.info("gym plan: nothing to write")
        return

    if job.payload.get("silent"):
        return

    notify.send_message(compose(blocks, failures, tests,
                                _readiness_note(session, owner.id, today), today))
    job.payload = {**job.payload, "sent": True}


def enqueue_due_plan() -> None:
    """Queue tonight's rebuild once its hour has come.

    Keyed by date alone, so the worker ticking every few minutes — or restarting at
    23:14 — enqueues exactly one job for the night rather than missing it.
    """
    from app.config import get_settings
    from app.core import jobs
    from app.models.base import utcnow

    settings = get_settings()
    now = utcnow().astimezone(settings.tz)
    hour, minute = PLAN_AT
    if now < now.replace(hour=hour, minute=minute, second=0, microsecond=0):
        return
    with session_scope() as session:
        jobs.enqueue(
            session,
            JOB_KIND,
            {"date": now.date().isoformat(),
             "silent": bool(getattr(settings, "gym_plan_silent", False))},
            dedupe_key=f"{JOB_KIND}:{now.date().isoformat()}",
        )


def link_slots(session, owner_id: str) -> int:
    """Attach each program day to the Garmin template he already runs for it.

    Without this the first night would upload three new workouts beside his own and then
    progress from an empty history. The link is taken from what he actually executed on
    that weekday, so the service adopts his templates instead of duplicating them.
    """
    from app.models.training import WorkoutSession

    linked = 0
    days = session.scalars(
        select(ProgramDay).where(
            ProgramDay.owner_id == owner_id,
            ProgramDay.garmin_workout_id.is_(None),
            ProgramDay.is_active.is_(True),
        )
    ).all()
    for day in days:
        recent = session.scalars(
            select(WorkoutSession)
            .where(
                WorkoutSession.owner_id == owner_id,
                WorkoutSession.garmin_workout_id.is_not(None),
            )
            .order_by(WorkoutSession.local_date.desc())
            .limit(40)
        ).all()
        match = next(
            (s for s in recent
             if dt.date.fromisoformat(s.local_date).weekday() == day.weekday),
            None,
        )
        if match:
            day.garmin_workout_id = match.garmin_workout_id
            linked += 1
    return linked


# The thresholds every zone is computed from. They are measured, never estimated, and the
# measurement goes stale: a threshold that rose while the zones stayed put turns aerobic
# work into a walk without anything reporting it. Six weeks is long enough that a test is
# not a tax on the training, short enough that the zones stay true.
TEST_INTERVAL = dt.timedelta(weeks=6)


def threshold_tests() -> tuple[tuple[str, str, int], ...]:
    """The owner's own test workouts in Garmin, from GARMIN_THRESHOLD_TESTS.

    Format: `<workout id>|<label>|<weekday 0-6>`, entries separated by `;`. Empty means
    no tests are kept on the calendar — a workout id is personal and cannot be guessed.
    """
    from app.config import get_settings

    raw = get_settings().garmin_threshold_tests or ""
    tests = []
    for entry in raw.split(";"):
        parts = [part.strip() for part in entry.split("|")]
        if len(parts) != 3 or not parts[0]:
            continue
        try:
            tests.append((parts[0], parts[1], int(parts[2]) % 7))
        except ValueError:
            log.warning("gym plan: bad GARMIN_THRESHOLD_TESTS entry %r", entry)
    return tuple(tests)


def _last_run_of(session, owner_id: str, workout_id: str) -> dt.date | None:
    from app.models.training import WorkoutSession

    row = session.scalar(
        select(WorkoutSession)
        .where(WorkoutSession.owner_id == owner_id,
               WorkoutSession.garmin_workout_id == workout_id)
        .order_by(WorkoutSession.local_date.desc())
        .limit(1)
    )
    return dt.date.fromisoformat(row.local_date) if row else None


def keep_tests_scheduled(session, owner_id: str, client, today: dt.date) -> list[str]:
    """Make sure the next threshold test is always on the calendar.

    Computed from history rather than stored: the next test is six weeks after the last
    one actually performed. Nothing is written while a future test is already booked, so
    this neither duplicates the owner's entries nor argues with a date moved by hand.
    """
    said: list[str] = []
    for workout_id, label, offset in threshold_tests():
        booked = _future_test_date(client, workout_id, today)
        if booked:
            continue
        last = _last_run_of(session, owner_id, workout_id)
        due = (last + TEST_INTERVAL) if last else (today + dt.timedelta(days=7))
        if due <= today:
            # Overdue: put it on the next occurrence of the weekday configured for it.
            due = today + dt.timedelta(days=(offset - today.weekday()) % 7 or 7)
        try:
            client.schedule_workout(workout_id, due.isoformat())
        except Exception as exc:  # noqa: BLE001
            log.warning("gym plan: test %s not scheduled: %s", label, exc)
            continue
        said.append(f"{label}: поставлен на {due.strftime('%d.%m')}"
                    + (f" (6 недель после {last.strftime('%d.%m')})" if last else ""))
    return said


def _future_test_date(client, workout_id: str, today: dt.date) -> dt.date | None:
    """Is this test already booked ahead? Checks the next four months."""
    day = today
    for _ in range(4):
        try:
            payload = client.get_scheduled_workouts(day.year, day.month)
        except Exception:  # noqa: BLE001
            return today  # unknown beats double-booking
        items = payload if isinstance(payload, list) else (payload or {}).get(
            "calendarItems") or []
        for item in items:
            date = item.get("date") or item.get("scheduledDate")
            if str(item.get("workoutId")) == workout_id and date:
                when = dt.date.fromisoformat(date)
                if when >= today:
                    return when
        day = (day.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    return None
