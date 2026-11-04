"""Proposing next week's session from the one that just happened.

Progression follows the ACSM position stand on resistance training (Med Sci Sports Exerc,
2009): raise the load by 2-10 % once the target repetitions are reached **in two
consecutive sessions**, the smaller end of that range for upper body and small muscle
groups. One good session is not evidence — it can be sleep, caffeine or a good day, and
adding weight on it is how a plateau turns into a missed rep and a sour shoulder.

The decision stays his. This module computes a proposal, states what it rests on, and
carries a confidence level; nothing reaches the watch until he says so.

A proposal is built from the sets Garmin recorded, not from the plan: what was written
down last week and what he actually lifted are different things, and only the second one
justifies adding weight.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.training import (
    ProgramDay,
    ProgramExercise,
    Progression,
    SetLog,
    WorkoutSession,
)

# Below this weight a 2.5 kg jump is a 30 % increase — dumbbells move in smaller steps
SMALL_WEIGHT_KG = 15.0
BIG_STEP_KG = 2.5
SMALL_STEP_KG = 1.0

# ACSM: 2-10 % per step. The lower bound is used: it is safe in a calorie deficit too,
# where strength is maintained rather than built, and an aggressive jump costs a session.
STEP_SHARE = 0.05

HIGH = "высокая"
MEDIUM = "средняя"
LOW = "низкая"


@dataclass
class ExercisePlan:
    """One exercise of the proposed session, with the reason and how sure it is."""

    name: str
    garmin_key: str | None
    sets: int
    reps: int
    weight_kg: float | None
    reason: str
    confidence: str = MEDIUM
    source: str | None = None
    # Garmin's own exercise category, carried from the exercise rather than derived from
    # the key: splitting DUMBBELL_REVERSE_LUNGE gives "DUMBBELL", which is not a category
    # Garmin knows, and the watch then shows a step with no name and no animation.
    category: str | None = None
    # Seconds per set for work that is held rather than counted — plank, side plank,
    # the vacuum. Garmin records these with a duration and no reps, and an engine that
    # reads only reps cannot see them at all.
    duration_s: float | None = None


@dataclass
class SessionPlan:
    """A session proposed for a date, ready to be shown and then uploaded."""

    date: str
    title: str
    source_date: str
    exercises: list[ExercisePlan] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_text(self) -> list[str]:
        """The proposal as the lines that go into a report."""
        lines = [f"{self.title} · {self.date}"]
        for item in self.exercises:
            weight = f" · {item.weight_kg:g} кг" if item.weight_kg else ""
            lines.append(f"  {item.name}: {item.sets}×{item.reps}{weight}")
        raised = [e for e in self.exercises if e.confidence == HIGH and e.source]
        if raised:
            lines.append(f"  уверенность {raised[0].confidence}: {raised[0].source}")
        lines += [f"  {note}" for note in self.notes]
        return lines


def _step_for(weight: float) -> float:
    """The smallest sensible increment: 5 % rounded to what the rack actually has."""
    raw = weight * STEP_SHARE
    if weight < SMALL_WEIGHT_KG:
        return max(SMALL_STEP_KG, round(raw / SMALL_STEP_KG) * SMALL_STEP_KG)
    return max(BIG_STEP_KG, round(raw / BIG_STEP_KG) * BIG_STEP_KG)


# A step this large is not progression, it is a typo travelling. `max(weights)` means one
# dictated "450" instead of "45" would otherwise settle into the plan for good.
MAX_STEP_SHARE = 1.15
MIN_STEP_SHARE = 0.5
ABSURD_KG = 300.0
# Nothing he trains jumps from novice weights to this in one step.
UNTRAINED_CEILING_KG = 25.0
UNTRAINED_LIMIT_KG = 50.0


def sane(proposed: float | None, previous: float | None, history_max: float | None
         ) -> tuple[float | None, str | None]:
    """Refuse a weight that cannot be right, and say so instead of clamping quietly.

    Returns the weight to use and, when the proposal was refused, the sentence explaining
    it. The refusal travels to the watch and to Telegram: a number that silently failed to
    move is indistinguishable from a number that was never meant to.
    """
    if proposed is None or previous is None:
        return proposed, None
    if proposed <= 0:
        return previous, f"отказ: расчёт дал {proposed:g} кг, оставляю {previous:g}"
    if proposed > ABSURD_KG:
        return previous, f"отказ: {proposed:g} кг выходит за пределы разумного"
    if proposed > previous * MAX_STEP_SHARE:
        return previous, (f"отказ: скачок {previous:g} → {proposed:g} кг больше 15 %, "
                          f"похоже на опечатку в подходе")
    if proposed < previous * MIN_STEP_SHARE:
        return previous, f"отказ: падение {previous:g} → {proposed:g} кг слишком резкое"
    if (history_max or 0) <= UNTRAINED_CEILING_KG and proposed > UNTRAINED_LIMIT_KG:
        return previous, (f"отказ: {proposed:g} кг при истории до "
                          f"{history_max or 0:g} кг")
    return proposed, None


def _sets_by_exercise(session: Session, workout: WorkoutSession) -> dict:
    rows = session.scalars(
        select(SetLog).where(SetLog.session_id == workout.id).order_by(SetLog.order)
    ).all()
    grouped: dict = {}
    for row in rows:
        grouped.setdefault(row.exercise, []).append(row)
    return grouped


def _previous_session_with(
    session: Session, owner_id: str, exercise_id: str, before: str
) -> list[SetLog]:
    """The sets of the last time this exercise was done before a given date."""
    rows = session.scalars(
        select(SetLog)
        .join(WorkoutSession, WorkoutSession.id == SetLog.session_id)
        .where(
            WorkoutSession.owner_id == owner_id,
            SetLog.exercise_id == exercise_id,
            WorkoutSession.local_date < before,
        )
        .order_by(WorkoutSession.local_date.desc())
    ).all()
    if not rows:
        return []
    last_day = session.get(WorkoutSession, rows[0].session_id).local_date
    return [
        r for r in rows
        if session.get(WorkoutSession, r.session_id).local_date == last_day
    ]


def _targets(session: Session, owner_id: str, day: dt.date) -> dict[str, ProgramExercise]:
    """The target range for each exercise of that weekday, keyed by exercise id.

    The same movement carries different targets on different days — standing calf raises
    are 14-16 on Monday and 15-20 on Friday — so the day decides, not the exercise.
    """
    rows = session.scalars(
        select(ProgramExercise)
        .join(ProgramDay, ProgramDay.id == ProgramExercise.program_day_id)
        .where(
            ProgramExercise.owner_id == owner_id,
            ProgramDay.weekday == day.weekday(),
            ProgramDay.is_active.is_(True),
        )
    ).all()
    return {row.exercise_id: row for row in rows}


def _all_sets_at_target(rows, target: int) -> bool:
    """The ACSM condition as he stated it: the top of the range in EVERY working set.

    Reading the best set instead is what produced "10 кг × 12 дважды подряд — норма
    выполнена" for a session of 12, 10, 12. One good set is not the session.
    """
    working = [r for r in rows if r.reps]
    return bool(working) and all((r.reps or 0) >= target for r in working)


def propose_from(session: Session, owner_id: str, day: dt.date) -> SessionPlan | None:
    """Next week's version of the session done on `day`, or None if nothing was done.

    Weight goes up only when the same weight was carried through the whole session and
    the reps did not fall against the previous time — that is the double progression he
    described, and the reason travels with every line so he can disagree with it.
    """
    workout = session.scalar(
        select(WorkoutSession).where(
            WorkoutSession.owner_id == owner_id,
            WorkoutSession.local_date == day.isoformat(),
        )
    )
    if workout is None:
        return None

    grouped = _sets_by_exercise(session, workout)
    if not grouped:
        return None
    targets = _targets(session, owner_id, day)

    plan = SessionPlan(
        date=(day + dt.timedelta(days=7)).isoformat(),
        title=workout.title or "Тренировка",
        source_date=day.isoformat(),
    )

    for exercise, rows in grouped.items():
        # With a composition on record the programme decides what the session is, not
        # whatever Garmin happened to log. That drops two artefacts of the export: the
        # warm-up bike, which arrives as a CARDIO "set" and which the builder supplies
        # itself, and blocks Garmin could not identify.
        if targets and exercise.id not in targets:
            continue

        timed = [r for r in rows if not r.reps and r.duration_s]
        if timed:
            # Held work: progression is by seconds, and the target range it grows towards
            # lives in the program template, which does not carry one yet. Repeating the
            # last duration is the honest answer until it does — inventing a target here
            # would be a number with nothing behind it.
            held = max(r.duration_s for r in timed)
            plan.exercises.append(ExercisePlan(
                exercise.name, exercise.garmin_key, len(timed), 0, None,
                f"повторить {held:g} сек — удержание, прогрессия по времени",
                category=exercise.category,
                duration_s=held,
                confidence=MEDIUM,
                source="ACSM position stand, 2009: при работе со своим весом нагрузка "
                       "растёт временем под нагрузкой и темпом",
            ))
            continue

        working = [r for r in rows if r.reps]
        if not working:
            continue
        reps = max(r.reps for r in working)
        weights = [r.weight_kg for r in working if r.weight_kg]
        if not weights:
            plan.exercises.append(ExercisePlan(
                exercise.name, exercise.garmin_key, len(working), reps, None,
                "свой вес — прогрессия по повторам, не по весу",
                category=exercise.category,
                confidence=HIGH,
                source="ACSM position stand, 2009: при работе со своим весом "
                       "нагрузка растёт повторами и темпом",
            ))
            continue

        top = max(weights)
        held_throughout = len({round(w, 1) for w in weights}) == 1
        previous = _previous_session_with(session, owner_id, exercise.id, day.isoformat())
        previous_weight = max((r.weight_kg or 0) for r in previous) if previous else 0

        target = targets.get(exercise.id)
        if target is not None:
            # The programme knows what he is aiming for, so the condition can be stated
            # properly: the top of the range in every working set, twice running.
            goal = target.target_max
            reps = goal
            met_now = _all_sets_at_target(working, goal)
            met_before = _all_sets_at_target(previous, goal)
            repeated = bool(previous) and previous_weight == top and met_before
            ready = held_throughout and met_now and repeated
        else:
            # No target on record. Falling back to "not fewer reps than last time" is the
            # old behaviour and it is generous — it can raise on a session he did not
            # finish — so it is marked as such rather than passed off as the rule.
            previous_reps = max((r.reps or 0) for r in previous) if previous else 0
            ready = (held_throughout and bool(previous)
                     and previous_weight == top and previous_reps >= reps)

        if ready:
            step = _step_for(top)
            plan.exercises.append(ExercisePlan(
                exercise.name, exercise.garmin_key, len(working), reps,
                round(top + step, 1),
                f"{top:g} кг × {reps} во всех подходах дважды подряд — норма выполнена",
                category=exercise.category,
                confidence=HIGH,
                source="ACSM position stand on progression, 2009: +2-10 % после двух "
                       "подряд тренировок с достигнутой целью",
            ))
        elif held_throughout:
            if target is not None and not _all_sets_at_target(working, reps):
                got = ", ".join(str(r.reps or 0) for r in working)
                why = (f"повторить {top:g} кг: цель {reps} не взята во всех подходах "
                       f"({got})")
            else:
                why = f"повторить {top:g} кг — нужна вторая такая тренировка подряд"
            plan.exercises.append(ExercisePlan(
                exercise.name, exercise.garmin_key, len(working), reps, top,
                why,
                category=exercise.category,
                confidence=HIGH,
                source="ACSM position stand on progression, 2009",
            ))
        else:
            plan.exercises.append(ExercisePlan(
                exercise.name, exercise.garmin_key, len(working), reps, top,
                f"повторить {top:g} кг: вес в подходах менялся",
                category=exercise.category,
                confidence=MEDIUM,
            ))

    if not plan.exercises:
        return None
    return plan


def to_garmin_workout(plan: SessionPlan):
    """Build the Garmin workout object from a proposal.

    Kept as the name the MCP tool and the bot already call. The construction itself lives
    in `workout_builder`, because how a step looks on the watch and how a weight is
    decided are two different subjects that were changing each other by accident.
    """
    from app.domain import workout_builder

    return workout_builder.build(plan)


# Three sessions of a slot, not two. The rule that moves a weight needs two — that is the
# ACSM condition and it does not change — but two numbers are a pair, not a trend: they
# cannot tell a plateau from a climb, or say whether a bad session was the first bad one.
# The third is what the owner reads.
SLOT_HISTORY = 3


def _slot_sessions(
    session: Session, owner_id: str, program_day: ProgramDay, before: str,
    limit: int = SLOT_HISTORY
) -> list[WorkoutSession]:
    """The most recent sessions of this slot, newest first.

    Matched on the template that was executed, not on the weekday. He shifts days — the
    Saturday stair session ran on a Friday — and a date-keyed lookup then silently finds
    nothing and freezes the progression at its seed.
    """
    if not program_day.garmin_workout_id:
        return []
    return list(session.scalars(
        select(WorkoutSession)
        .where(
            WorkoutSession.owner_id == owner_id,
            WorkoutSession.garmin_workout_id == program_day.garmin_workout_id,
            WorkoutSession.local_date < before,
        )
        .order_by(WorkoutSession.local_date.desc())
        .limit(limit)
    ).all())


def _last_weight(session: Session, owner_id: str, exercise_id: str) -> float | None:
    """The heaviest set ever logged for an exercise. Used when a session skipped it."""
    return session.scalar(
        select(func.max(SetLog.weight_kg))
        .join(WorkoutSession, WorkoutSession.id == SetLog.session_id)
        .where(WorkoutSession.owner_id == owner_id, SetLog.exercise_id == exercise_id)
    )


def _best(rows: list[SetLog]) -> float | None:
    """The number a session is judged on: the weight, or the seconds when it is held."""
    if not rows:
        return None
    weights = [r.weight_kg for r in rows if r.weight_kg]
    if weights:
        return max(weights)
    held = [r.duration_s for r in rows if r.duration_s]
    return max(held) if held else None


def _trend(rows, previous_rows, earlier_rows) -> str:
    """Three sessions of a slot, oldest first, as a short tail on the reason.

    Two numbers are a pair; three are a direction. At the rack the useful thing is not
    only today's weight but whether it has been the same weight for a month — which is the
    signal that something other than patience is needed.
    """
    values = [_best(r) for r in (earlier_rows, previous_rows, rows)]
    seen = [v for v in values if v is not None]
    if len(seen) < 3:
        return ""
    if len({round(v, 1) for v in seen}) == 1:
        return f". Три раза подряд {seen[0]:g} — плато"
    return ". Динамика: " + " → ".join(f"{v:g}" for v in seen)


def _line_for(
    session: Session,
    owner_id: str,
    item: ProgramExercise,
    rows: list[SetLog],
    previous_rows: list[SetLog],
    earlier_rows: list[SetLog] | None = None,
) -> ExercisePlan:
    """One line of the plan: what to do, with what, and on what grounds.

    The programme decides the exercise and the target; the history decides the number.
    An exercise the last session skipped keeps whatever it was last done with, because
    progression without a fact is invention — and dropping it would shorten the programme
    without telling anyone, which is how the plank disappeared for a month.
    """
    exercise = item.exercise
    goal = item.target_max
    trend = _trend(rows, previous_rows, earlier_rows or [])
    common = dict(
        name=exercise.name, garmin_key=exercise.garmin_key, sets=item.sets,
        category=exercise.category,
    )

    if item.progression is Progression.TIME:
        held = max((r.duration_s or 0) for r in rows) if rows else 0
        if not held:
            held = _last_weight(session, owner_id, exercise.id) or item.seed_value or goal
        reached_now = bool(rows) and held >= goal
        reached_before = bool(previous_rows) and max(
            (r.duration_s or 0) for r in previous_rows
        ) >= goal
        if reached_now and reached_before:
            # Held work gets longer rather than heavier, and it grows by the same share
            # of itself that a weight would.
            step = max(5, round(goal * STEP_SHARE / 5) * 5)
            return ExercisePlan(
                **common, reps=0, weight_kg=None, duration_s=float(goal + step),
                reason=f"{goal:g} сек во всех подходах дважды подряд — норма выполнена",
                confidence=HIGH,
                source="ACSM position stand, 2009: при работе со своим весом нагрузка "
                       "растёт временем под нагрузкой",
            )
        why = (f"повторить {held:g} сек — удержание{trend}" if rows
               else f"пропущено — повторить {held:g} сек")
        return ExercisePlan(
            **common, reps=0, weight_kg=None, duration_s=float(held), reason=why,
            confidence=MEDIUM if rows else LOW,
        )

    if not rows:
        carried = _last_weight(session, owner_id, exercise.id) or item.seed_value
        held_label = f"{carried:g} кг" if carried else "свой вес"
        return ExercisePlan(
            **common, reps=goal, weight_kg=carried,
            reason=f"пропущено в прошлый раз — тот же {held_label}",
            confidence=LOW,
        )

    weights = [r.weight_kg for r in rows if r.weight_kg]
    met_now = _all_sets_at_target(rows, goal)
    met_before = _all_sets_at_target(previous_rows, goal)

    if not weights:
        # Bodyweight counted in repetitions: the target itself is what moves.
        if met_now and met_before:
            return ExercisePlan(
                **common, reps=goal + 1, weight_kg=None,
                reason=f"{goal} во всех подходах дважды подряд — добавляем повтор",
                confidence=HIGH,
                source="ACSM position stand, 2009: при работе со своим весом нагрузка "
                       "растёт повторами и темпом",
            )
        return ExercisePlan(
            **common, reps=goal, weight_kg=None,
            reason=f"свой вес, цель {goal} — прогрессия по повторам",
            confidence=MEDIUM,
        )

    top = max(weights)
    held_throughout = len({round(w, 1) for w in weights}) == 1
    previous_weight = max((r.weight_kg or 0) for r in previous_rows) if previous_rows else 0
    repeated = bool(previous_rows) and previous_weight == top and met_before

    if held_throughout and met_now and repeated:
        step = _step_for(top)
        # A counterweight makes the movement easier, so progress takes it away. This is
        # the one case where "harder" and "more kilograms" point in opposite directions.
        if item.progression is Progression.ASSIST:
            nxt = max(0.0, round(top - step, 1))
            reason = f"{top:g} кг противовеса × {goal} дважды подряд — снимаем {step:g} кг"
        else:
            nxt = round(top + step, 1)
            reason = f"{top:g} кг × {goal} во всех подходах дважды подряд — норма выполнена"
        nxt, refusal = sane(nxt, top, _last_weight(session, owner_id, exercise.id))
        if refusal:
            return ExercisePlan(
                **common, reps=goal, weight_kg=nxt, reason=refusal, confidence=LOW,
            )
        return ExercisePlan(
            **common, reps=goal, weight_kg=nxt, reason=reason, confidence=HIGH,
            source="ACSM position stand on progression, 2009: +2-10 % после двух подряд "
                   "тренировок с достигнутой целью",
        )

    if not met_now:
        got = ", ".join(str(r.reps or 0) for r in rows if r.reps)
        why = (f"повторить {top:g} кг: цель {goal} не взята во всех подходах "
               f"({got}){trend}")
    elif not held_throughout:
        why = f"повторить {top:g} кг: вес в подходах менялся{trend}"
    else:
        why = f"повторить {top:g} кг — нужна вторая такая тренировка подряд{trend}"
    return ExercisePlan(
        **common, reps=goal, weight_kg=top, reason=why, confidence=HIGH,
        source="ACSM position stand on progression, 2009",
    )


def plan_day(
    session: Session, owner_id: str, program_day: ProgramDay, for_date: dt.date
) -> SessionPlan | None:
    """The session to write for a date, built from the programme and the history.

    The programme is the authority on what the session contains; the history is the
    authority on the numbers. That split is what stops an exercise falling out of the
    week because it was not logged once.
    """
    items = session.scalars(
        select(ProgramExercise)
        .where(
            ProgramExercise.owner_id == owner_id,
            ProgramExercise.program_day_id == program_day.id,
        )
        .order_by(ProgramExercise.order)
    ).all()
    if not items:
        return None

    recent = _slot_sessions(session, owner_id, program_day, for_date.isoformat())
    history = [
        {ex.id: rows for ex, rows in _sets_by_exercise(session, workout).items()}
        for workout in recent
    ]
    by_id = history[0] if history else {}
    before_by_id = history[1] if len(history) > 1 else {}
    earlier_by_id = history[2] if len(history) > 2 else {}

    plan = SessionPlan(
        date=for_date.isoformat(),
        title=program_day.title,
        source_date=recent[0].local_date if recent else "",
        exercises=[
            _line_for(session, owner_id, item,
                      by_id.get(item.exercise_id, []),
                      before_by_id.get(item.exercise_id, []),
                      earlier_by_id.get(item.exercise_id, []))
            for item in items
        ],
    )
    return plan
