"""The gym: program, exercises, set history, equipment.

The distinction that matters most here:

  * **Program** — what to do. It lives in Garmin; what is here is a copy, and the copy
    always carries a snapshot date: programs change more often than files.
  * **History** — what was actually done. It arrives from Garmin after a session, or is
    dictated in the gym. This is where "what weight do I start with today" comes from.
  * **Equipment** — what the gym actually has. Needed so no exercise is prescribed on a
    machine that is not there, and so a substitute can be found.

Set weight is in kilograms and may be absent: half the work is bodyweight, where a zero
would read as "a barbell weighing nothing".
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    JSON,
    Boolean,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import DomainBase


class Progression(enum.StrEnum):
    """How an exercise is made harder once the target is met.

    The rule is the same in every case — the target twice in a row, then a step — but the
    thing that moves is not. A counterweight goes *down* as he gets stronger, which is the
    one direction a single "increase the load" rule cannot express.
    """

    LOAD = "load"        # barbell, dumbbell, weight stack: add kilograms
    ASSIST = "assist"    # gravitron, assisted dips: take counterweight away
    REPS = "reps"        # bodyweight counted in repetitions
    TIME = "time"        # held: plank, side plank, the vacuum


class SessionSource(enum.StrEnum):
    GARMIN = "garmin"        # exported from the watch
    DICTATED = "dictated"    # dictated in the gym
    IMPORTED = "imported"    # carried over from files


class Exercise(DomainBase):
    """An exercise as a concept. The Garmin key bridges to the set export."""

    __tablename__ = "exercises"
    __table_args__ = (UniqueConstraint("owner_id", "slug", name="uq_exercise_slug"),)

    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    garmin_key: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # In the program now, or derived from it: "did it once" and "do it" are different
    in_program: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Name translated from the Garmin key and not verified against a reference
    name_unverified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    equipment_note: Mapped[str | None] = mapped_column(String(250), nullable=True)
    # How to perform it, in his own shorthand — this is what reaches the watch on the
    # first set of every exercise, and it is the difference between a named step and a
    # coached one. Distinct from `notes`, which records where the exercise came from.
    cue: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkoutSession(DomainBase):
    """A session that happened. Not a plan: only what was done gets in here."""

    __tablename__ = "workout_sessions"
    __table_args__ = (
        UniqueConstraint("owner_id", "external_id", name="uq_session_external"),
        Index("ix_sessions_date", "owner_id", "local_date"),
    )

    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[SessionSource] = mapped_column(
        Enum(SessionSource), default=SessionSource.DICTATED, nullable=False
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    local_date: Mapped[str] = mapped_column(String(10), nullable=False)
    # Which workout template was executed. Garmin puts it on the activity, and it is the
    # only reliable way to tell one session type from another: the activity name is
    # truncated ("5 · Пятница — Фулб") and a shifted day makes the date useless.
    garmin_workout_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    duration_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    sets: Mapped[list[SetLog]] = relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="selectin"
    )


class SetLog(DomainBase):
    """One set. Weight may be empty — that is bodyweight work."""

    __tablename__ = "set_logs"
    __table_args__ = (Index("ix_sets_exercise", "owner_id", "exercise_id"),)

    session_id: Mapped[str] = mapped_column(
        ForeignKey("workout_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    exercise_id: Mapped[str] = mapped_column(
        ForeignKey("exercises.id", ondelete="RESTRICT"), nullable=False
    )
    order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(String(250), nullable=True)

    session: Mapped[WorkoutSession] = relationship(back_populates="sets")
    exercise: Mapped[Exercise] = relationship(lazy="joined")


class ProgramDay(DomainBase):
    """A weekday of the active phase. A copy of the program, always with a snapshot date."""

    __tablename__ = "program_days"
    __table_args__ = (
        UniqueConstraint("owner_id", "phase", "weekday", name="uq_program_day"),
    )

    phase: Mapped[str] = mapped_column(String(80), nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)  # 0 = Monday
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    morning: Mapped[str | None] = mapped_column(String(250), nullable=True)
    evening: Mapped[str | None] = mapped_column(String(250), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(250), nullable=True)
    # Exercise detail lives in Garmin; what is here is what could be carried over
    blocks: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    snapshot_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    source_note: Mapped[str | None] = mapped_column(String(250), nullable=True)
    # The durable Garmin template for this slot. One template per weekday, rewritten in
    # place each night rather than a new workout per date: the id stays valid, the
    # calendar entry stays attached, and nothing ever has to be deleted.
    garmin_workout_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # What kind of session it is, so the right builder is chosen. Only strength can be
    # built today; the rest are named so the job can skip them without guessing.
    kind: Mapped[str] = mapped_column(String(20), default="strength", nullable=False)


class ProgramExercise(DomainBase):
    """What a program day is actually made of.

    Without this the plan can only be derived from what was logged, and an exercise he
    skipped once disappears from the program for good — quietly, because nothing reports
    a session that got shorter. It also holds the target range, which is what the
    progression rule compares against: "the top of the range in every working set" needs
    a range to be the top of.
    """

    __tablename__ = "program_exercises"
    __table_args__ = (
        UniqueConstraint("owner_id", "program_day_id", "exercise_id",
                         name="uq_program_exercise"),
        Index("ix_program_exercise_day", "owner_id", "program_day_id"),
    )

    program_day_id: Mapped[str] = mapped_column(
        ForeignKey("program_days.id", ondelete="CASCADE"), nullable=False
    )
    exercise_id: Mapped[str] = mapped_column(
        ForeignKey("exercises.id", ondelete="RESTRICT"), nullable=False
    )
    order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sets: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    # Repetitions, or seconds when the progression is TIME. One pair of columns rather
    # than two, because a range is a range whichever unit it counts.
    target_min: Mapped[int] = mapped_column(Integer, nullable=False)
    target_max: Mapped[int] = mapped_column(Integer, nullable=False)
    rest_seconds: Mapped[int] = mapped_column(Integer, default=90, nullable=False)
    progression: Mapped[Progression] = mapped_column(
        Enum(Progression), default=Progression.LOAD, nullable=False
    )
    # Where to start when nothing has been logged yet. NULL means bodyweight throughout.
    seed_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(String(250), nullable=True)

    exercise: Mapped[Exercise] = relationship(lazy="joined")


class PlanStatus(enum.StrEnum):
    WRITTEN = "written"
    FAILED = "failed"


class PlannedWorkout(DomainBase):
    """What the service wrote into Garmin for a date, and on what grounds.

    It exists for three jobs. It bounds what may be overwritten — only what this wrote,
    never a workout he built himself. Its digest makes a rerun with no new facts free,
    which matters when the job runs nightly and most nights change nothing. And it answers
    "why 47,5" weeks later, from the database rather than from a chat log.

    A name in Garmin could do none of that: names are editable, guessable, and come back
    truncated.
    """

    __tablename__ = "planned_workouts"
    __table_args__ = (
        UniqueConstraint("owner_id", "local_date", name="uq_planned_date"),
    )

    local_date: Mapped[str] = mapped_column(String(10), nullable=False)
    program_day_id: Mapped[str | None] = mapped_column(
        ForeignKey("program_days.id", ondelete="SET NULL"), nullable=True
    )
    garmin_workout_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The plan as written: every line with its weight and the reason behind it.
    plan: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[PlanStatus] = mapped_column(
        Enum(PlanStatus), default=PlanStatus.WRITTEN, nullable=False
    )
    note: Mapped[str | None] = mapped_column(String(250), nullable=True)


class Equipment(DomainBase):
    """A machine or piece of kit in the gym. Consulted before any substitution."""

    __tablename__ = "equipment"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str | None] = mapped_column(String(80), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(80), nullable=True)
    code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    trains: Mapped[str | None] = mapped_column(String(250), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Identified from a photo but not checked against the nameplate — and that shows in answers
    unverified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
