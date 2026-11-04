"""The gym: what is on today, what weight to start from, what to substitute, what to log.

The rule that separates this module from a "smart coach": **progression is never applied
automatically.** The owner's double progression is described in his plan — hit the top of
the range in every set, then add weight — but the decision is his. What is computed here
is fact: what he lifted last time, for how many reps, whether the top of the range
was reached. The agent speaks after that, not the table.

A weight of `None` means bodyweight work, not "zero kilograms".
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.base import local_today
from app.models.training import (
    Equipment,
    Exercise,
    ProgramDay,
    SessionSource,
    SetLog,
    WorkoutSession,
)


class TrainingError(Exception):
    """An error to be shown in words, not as a traceback."""


@dataclass
class SetView:
    reps: int | None
    weight_kg: float | None
    duration_s: float | None

    def __str__(self) -> str:
        if self.weight_kg:
            return f"{self.weight_kg:g} kg × {self.reps}"
        if self.reps:
            return f"{self.reps} reps"
        return f"{self.duration_s:.0f} s" if self.duration_s else "—"


@dataclass
class ExerciseHistory:
    """Exercise history: what the owner decides today's weight from."""

    exercise: Exercise
    last_date: str | None = None
    last_sets: list[SetView] | None = None
    best_weight_kg: float | None = None
    best_weight_reps: int | None = None
    sessions_count: int = 0
    newer_sessions_in_garmin: int = 0
    newest_garmin_date: str | None = None

    @property
    def start_hint(self) -> str:
        """Not an instruction, a fact: where he left off last time."""
        if not self.last_sets:
            return "No history for this exercise — pick the weight on the spot."
        body = ", ".join(str(s) for s in self.last_sets)
        if self.best_weight_kg and self.last_sets[0].weight_kg:
            return (
                f"Last time {self.last_date}: {body}. "
                f"Best working set — {self.best_weight_kg:g} kg × {self.best_weight_reps}."
            )
        return f"Last time {self.last_date}: {body}."

    @property
    def freshness_note(self) -> str | None:
        """Whether newer training exists that this history does not know about.

        The set history is filled by a one-off file import; Garmin is not written into
        it. So the answer to "what weight did I use" can be a week old while sounding
        like yesterday. Saying the date is not enough — the gap has to be named.
        """
        if self.newer_sessions_in_garmin:
            return (
                f"Careful: Garmin has {self.newer_sessions_in_garmin} later session(s), "
                f"the most recent on {self.newest_garmin_date}. Their sets are not in this "
                f"history — say so rather than presenting this as the last time."
            )
        return None


# --- program -----------------------------------------------------------------------

def program_for(session: Session, owner_id: str, day: dt.date) -> ProgramDay | None:
    return session.scalar(
        select(ProgramDay).where(
            ProgramDay.owner_id == owner_id,
            ProgramDay.is_active.is_(True),
            ProgramDay.weekday == day.weekday(),
        )
    )


def week(session: Session, owner_id: str) -> list[ProgramDay]:
    return list(
        session.scalars(
            select(ProgramDay)
            .where(ProgramDay.owner_id == owner_id, ProgramDay.is_active.is_(True))
            .order_by(ProgramDay.weekday)
        )
    )


# --- exercises and history ----------------------------------------------------------

def find_exercise(session: Session, owner_id: str, needle: str) -> Exercise | None:
    """Search by name or Garmin key. An exact match outranks a similar one."""
    needle = needle.strip()
    lowered = needle.lower()
    exact = session.scalar(
        select(Exercise).where(
            Exercise.owner_id == owner_id,
            func.ru_lower(Exercise.name) == lowered,
        )
    )
    if exact:
        return exact
    return session.scalar(
        select(Exercise).where(
            Exercise.owner_id == owner_id,
            func.ru_lower(Exercise.name).like(f"%{lowered}%")
            | func.ru_lower(Exercise.garmin_key).like(f"%{lowered}%"),
        )
    )


def history(
    session: Session, owner_id: str, exercise: Exercise
) -> ExerciseHistory:
    rows = list(
        session.execute(
            select(SetLog, WorkoutSession.local_date)
            .join(WorkoutSession, WorkoutSession.id == SetLog.session_id)
            .where(SetLog.owner_id == owner_id, SetLog.exercise_id == exercise.id)
            .order_by(WorkoutSession.local_date.desc(), SetLog.order)
        ).all()
    )
    result = ExerciseHistory(exercise=exercise)
    if not rows:
        return result

    dates = sorted({date for _, date in rows}, reverse=True)
    result.sessions_count = len(dates)
    result.last_date = dates[0]
    result.last_sets = [
        SetView(s.reps, s.weight_kg, s.duration_s) for s, date in rows if date == result.last_date
    ]

    weighted = [s for s, _ in rows if s.weight_kg]
    if weighted:
        best = max(weighted, key=lambda s: (s.weight_kg or 0, s.reps or 0))
        result.best_weight_kg = best.weight_kg
        result.best_weight_reps = best.reps

    _note_newer_garmin_sessions(session, owner_id, result)
    return result


def _note_newer_garmin_sessions(session: Session, owner_id: str, result: ExerciseHistory) -> None:
    """Count strength sessions Garmin knows about and this history does not.

    The history is a one-off import; Garmin keeps the real record. Rather than copying
    theirs into ours — the same training in two places, drifting apart — the answer says
    plainly that newer sessions exist and this is not the last word.
    """
    from app.models.garmin import GarminKind, GarminMetric

    rows = session.scalars(
        select(GarminMetric).where(
            GarminMetric.owner_id == owner_id, GarminMetric.kind == GarminKind.ACTIVITY
        )
    ).all()
    strength = [
        row for row in rows
        if (row.details or {}).get("type") == "strength_training"
        and (result.last_date is None or row.local_date > result.last_date)
    ]
    if not strength:
        return
    result.newer_sessions_in_garmin = len(strength)
    result.newest_garmin_date = max(row.local_date for row in strength)


def recent_sessions(session: Session, owner_id: str, *, limit: int = 5) -> list[WorkoutSession]:
    return list(
        session.scalars(
            select(WorkoutSession)
            .where(WorkoutSession.owner_id == owner_id)
            .order_by(WorkoutSession.local_date.desc())
            .limit(limit)
        )
    )


# --- recording facts ----------------------------------------------------------------

def open_session(
    session: Session,
    owner_id: str,
    *,
    day: dt.date | None = None,
    title: str | None = None,
    source: SessionSource = SessionSource.DICTATED,
    external_id: str | None = None,
    started_at: dt.datetime | None = None,
    duration_min: float | None = None,
) -> WorkoutSession:
    """Find today's session or start one. Sets are written as the gym happens."""
    day = day or local_today()
    local_date = day.isoformat()

    if external_id:
        existing = session.scalar(
            select(WorkoutSession).where(
                WorkoutSession.owner_id == owner_id,
                WorkoutSession.external_id == external_id,
            )
        )
        if existing:
            return existing

    existing = session.scalar(
        select(WorkoutSession).where(
            WorkoutSession.owner_id == owner_id,
            WorkoutSession.local_date == local_date,
            WorkoutSession.source == source,
        )
    )
    if existing:
        return existing

    workout = WorkoutSession(
        owner_id=owner_id,
        local_date=local_date,
        title=title,
        source=source,
        external_id=external_id,
        started_at=started_at,
        duration_min=duration_min,
    )
    session.add(workout)
    session.flush()
    return workout


def add_exercise(
    session: Session, owner_id: str, name: str, *, category: str | None = None,
    garmin_key: str | None = None,
) -> Exercise:
    """Add an exercise to the registry on the owner's word.

    The registry arrived as a one-off import of a file. Everything the gym adds later —
    a new machine, a movement he names his own way — needs a door, or it can never be
    logged.
    """
    from app.domain.naming import slugify

    exercise = Exercise(
        owner_id=owner_id,
        slug=slugify(garmin_key or name),
        name=name.strip(),
        category=category,
        garmin_key=garmin_key,
        notes="Added on the owner's word",
    )
    session.add(exercise)
    session.flush()
    return exercise


def log_set(
    session: Session,
    owner_id: str,
    *,
    exercise_name: str,
    reps: int | None = None,
    weight_kg: float | None = None,
    duration_s: float | None = None,
    day: dt.date | None = None,
    workout: WorkoutSession | None = None,
    note: str | None = None,
    create_if_unknown: bool = False,
    category: str | None = None,
) -> SetLog:
    """Record a set. No confirmation: it registers what happened.

    An unknown name is refused rather than guessed — "жим" must not silently become a
    different press. But refusing forever was worse: the registry came from a one-off
    file import, so a machine the gym added, or a name the owner uses, could never be
    recorded at all. `create_if_unknown` is how he says "yes, add it".
    """
    exercise = find_exercise(session, owner_id, exercise_name)
    if exercise is None:
        if not create_if_unknown:
            raise TrainingError(
                f"The exercise '{exercise_name}' is not in the registry. "
                f"Name it as it appears in the program, or say to add it as new."
            )
        exercise = add_exercise(session, owner_id, exercise_name, category=category)
    if reps is None and duration_s is None:
        raise TrainingError("Needs either reps or a set duration.")

    workout = workout or open_session(session, owner_id, day=day)
    order = session.scalar(
        select(func.coalesce(func.max(SetLog.order), 0)).where(SetLog.session_id == workout.id)
    )
    entry = SetLog(
        owner_id=owner_id,
        session_id=workout.id,
        exercise_id=exercise.id,
        order=(order or 0) + 1,
        reps=reps,
        weight_kg=weight_kg,
        duration_s=duration_s,
        note=note,
    )
    session.add(entry)
    session.flush()
    return entry


# --- equipment ----------------------------------------------------------------------

def equipment(session: Session, owner_id: str, *, needle: str | None = None) -> list[Equipment]:
    stmt = select(Equipment).where(Equipment.owner_id == owner_id)
    if needle:
        like = f"%{needle.strip().lower()}%"
        stmt = stmt.where(
            func.ru_lower(Equipment.name).like(like)
            | func.ru_lower(Equipment.trains).like(like)
            | func.ru_lower(Equipment.kind).like(like)
        )
    return list(session.scalars(stmt.order_by(Equipment.name)))


def alternatives(session: Session, owner_id: str, exercise: Exercise) -> list[Exercise]:
    """What to substitute: exercises of the same category he has already done."""
    if not exercise.category:
        return []
    return list(
        session.scalars(
            select(Exercise).where(
                Exercise.owner_id == owner_id,
                Exercise.category == exercise.category,
                Exercise.id != exercise.id,
            )
        )
    )


# What a day's title says it is. Order matters: "Велосипед VO₂max" is cycling, and the
# stair machine is its own thing because the watch records it as indoor cardio, not as a
# run. A title that matches nothing stays strength, which is the only kind that can be
# built — so an unrecognised day is skipped by the nightly job rather than guessed at.
DAY_KINDS = (
    ("cycling", ("велосипед", "вело", "cycling", "bike")),
    ("stairs", ("ступен", "лестниц", "climbmill", "рюкзак")),
    ("running", ("бег", "пробежк", "running")),
    ("swimming", ("плаван", "swim")),
    ("rest", ("отдых", "восстановлен")),
)


def kind_from_title(title: str | None) -> str:
    """Classify a program day by what its title says, not by the weekday it sits on."""
    lowered = (title or "").lower()
    for kind, needles in DAY_KINDS:
        if any(needle in lowered for needle in needles):
            return kind
    return "strength"
