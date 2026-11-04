"""Proposing next week from what was actually lifted."""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from app.domain import planning, training
from app.models import Principal


def _session_with(session, owner, day, name, sets):
    for weight, reps in sets:
        training.log_set(
            session, owner.id, exercise_name=name, reps=reps, weight_kg=weight,
            day=day, create_if_unknown=True,
        )

def test_weight_rises_only_after_two_sessions_at_the_target(
    session: Session, owner: Principal
):
    """ACSM: add load once the target reps are met in two consecutive sessions.

    One good session can be sleep or caffeine; adding weight on it turns a plateau into
    a missed rep.
    """
    _session_with(session, owner, dt.date(2026, 9, 4), "Жим ногами",
                  [(45, 12), (45, 12), (45, 12)])
    _session_with(session, owner, dt.date(2026, 9, 11), "Жим ногами",
                  [(45, 12), (45, 12), (45, 12)])

    plan = planning.propose_from(session, owner.id, dt.date(2026, 9, 11))

    assert plan.date == "2026-09-18", "The same weekday, one week on"
    item = next(e for e in plan.exercises if e.name == "Жим ногами")
    assert item.weight_kg == 47.5, "5 % of 45 kg, rounded to plates that exist"
    assert "дважды подряд" in item.reason
    assert item.confidence == "высокая"
    assert "ACSM" in (item.source or ""), "A raise must name what it rests on"


def test_one_good_session_is_not_enough_to_add_weight(session: Session, owner: Principal):
    """The first time the target is reached, the same weight is proposed again."""
    _session_with(session, owner, dt.date(2026, 9, 4), "Жим ногами", [(45, 10), (45, 10)])
    _session_with(session, owner, dt.date(2026, 9, 11), "Жим ногами", [(45, 12), (45, 12)])

    plan = planning.propose_from(session, owner.id, dt.date(2026, 9, 11))

    item = next(e for e in plan.exercises if e.name == "Жим ногами")
    assert item.weight_kg == 45, "Wait for the second session before adding"
    assert "вторая такая тренировка" in item.reason

def test_a_weight_that_moved_inside_the_session_is_repeated_not_raised(
    session: Session, owner: Principal
):
    """8, 8, 10 kg means the last set was a reach, not a completed range."""
    day = dt.date(2026, 9, 11)
    _session_with(session, owner, day, "Выпады с гантелями", [(8, 12), (8, 12), (10, 12)])

    plan = planning.propose_from(session, owner.id, day)

    item = next(e for e in plan.exercises if "ыпад" in e.name)
    assert item.weight_kg == 10, "Repeat the top weight rather than adding to it"
    assert "повторить" in item.reason


def test_dumbbells_move_in_smaller_steps(session: Session, owner: Principal):
    """A 2.5 kg jump on an 8 kg dumbbell is a thirty percent increase — ACSM says 2-10 %."""
    _session_with(session, owner, dt.date(2026, 9, 4), "Подъём на бицепс",
                  [(8, 12), (8, 12), (8, 12)])
    _session_with(session, owner, dt.date(2026, 9, 11), "Подъём на бицепс",
                  [(8, 12), (8, 12), (8, 12)])

    plan = planning.propose_from(session, owner.id, dt.date(2026, 9, 11))

    assert plan.exercises[0].weight_kg == 9.0


def test_bodyweight_work_progresses_by_reps(session: Session, owner: Principal):
    """There is no weight to add to a pull-up — say so instead of inventing one."""
    day = dt.date(2026, 9, 11)
    training.log_set(session, owner.id, exercise_name="Подтягивания", reps=8, day=day,
                     create_if_unknown=True)

    plan = planning.propose_from(session, owner.id, day)

    item = plan.exercises[0]
    assert item.weight_kg is None
    assert "свой вес" in item.reason


def test_nothing_is_proposed_when_nothing_was_done(session: Session, owner: Principal):
    assert planning.propose_from(session, owner.id, dt.date(2026, 9, 11)) is None


def test_the_top_of_the_range_must_be_reached_in_every_set(session, owner):
    """His Friday was 12, 10, 12 against a target of 12. The engine proposed more weight
    because it read the best set. One good set is not the session, and he said so:
    "пока я не выполню 12-12-12, он не должен мне ничего поднимать"."""
    import datetime as dt

    from app.domain import planning
    from app.models.training import (
        Exercise,
        ProgramDay,
        ProgramExercise,
        Progression,
        SetLog,
        WorkoutSession,
    )

    exercise = Exercise(owner_id=owner.id, slug="lunge", name="Выпады",
                        garmin_key="DUMBBELL_REVERSE_LUNGE", category="LUNGE")
    session.add(exercise)
    session.flush()

    # 2026-09-18 and 2026-09-11 are both Fridays — weekday 4.
    day = ProgramDay(owner_id=owner.id, phase="Фаза 1", weekday=4, title="Фулбоди C",
                     blocks=[], is_active=True)
    session.add(day)
    session.flush()
    session.add(ProgramExercise(
        owner_id=owner.id, program_day_id=day.id, exercise_id=exercise.id,
        order=1, sets=3, target_min=10, target_max=12, rest_seconds=150,
        progression=Progression.LOAD, seed_value=10.0,
    ))

    for date, reps in (("2026-09-11", [12, 12, 12]), ("2026-09-18", [12, 10, 12])):
        workout = WorkoutSession(owner_id=owner.id, local_date=date, title="Фулбоди C")
        session.add(workout)
        session.flush()
        for order, count in enumerate(reps, start=1):
            session.add(SetLog(owner_id=owner.id, session_id=workout.id,
                               exercise_id=exercise.id, order=order,
                               reps=count, weight_kg=10.0))
    session.flush()

    plan = planning.propose_from(session, owner.id, dt.date(2026, 9, 18))
    line = plan.exercises[0]

    assert line.weight_kg == 10.0, "A set at 10 against a target of 12 does not earn weight"
    assert "не взята во всех подходах" in line.reason
    assert line.reps == 12, "The watch is told the target, not what he managed"


def test_the_target_in_every_set_twice_running_does_earn_weight(session, owner):
    import datetime as dt

    from app.domain import planning
    from app.models.training import (
        Exercise,
        ProgramDay,
        ProgramExercise,
        Progression,
        SetLog,
        WorkoutSession,
    )

    exercise = Exercise(owner_id=owner.id, slug="lunge", name="Выпады",
                        garmin_key="DUMBBELL_REVERSE_LUNGE", category="LUNGE")
    session.add(exercise)
    session.flush()
    day = ProgramDay(owner_id=owner.id, phase="Фаза 1", weekday=4, title="Фулбоди C",
                     blocks=[], is_active=True)
    session.add(day)
    session.flush()
    session.add(ProgramExercise(
        owner_id=owner.id, program_day_id=day.id, exercise_id=exercise.id,
        order=1, sets=3, target_min=10, target_max=12, rest_seconds=150,
        progression=Progression.LOAD, seed_value=10.0,
    ))

    for date in ("2026-09-11", "2026-09-18"):
        workout = WorkoutSession(owner_id=owner.id, local_date=date, title="Фулбоди C")
        session.add(workout)
        session.flush()
        for order in range(1, 4):
            session.add(SetLog(owner_id=owner.id, session_id=workout.id,
                               exercise_id=exercise.id, order=order,
                               reps=12, weight_kg=10.0))
    session.flush()

    line = planning.propose_from(session, owner.id, dt.date(2026, 9, 18)).exercises[0]

    assert line.weight_kg == 11.0, "Below 15 kg the step is 1 kg, not 2.5"
    assert "во всех подходах дважды подряд" in line.reason


def test_an_absurd_weight_is_refused_out_loud_not_clamped_quietly():
    """The engine takes the heaviest set of a session, so one dictated "450" instead of
    "45" would settle into the plan for ever. A refusal he cannot see is worse than the
    number, because nothing tells him the plan stopped moving."""
    from app.domain import planning

    kept, why = planning.sane(450.0, 45.0, 45.0)
    assert kept == 45.0
    assert why and "пределы разумного" in why

    kept, why = planning.sane(90.0, 45.0, 45.0)
    assert kept == 45.0 and "опечатк" in why, "A double is a typo, not a session"

    kept, why = planning.sane(52.0, 45.0, 45.0)
    assert kept == 45.0, "Above +15 % in one step is not progression"

    kept, why = planning.sane(47.5, 45.0, 45.0)
    assert kept == 47.5 and why is None, "An ordinary step passes untouched"

    kept, why = planning.sane(60.0, 55.0, 20.0)
    assert kept == 55.0, "Sixty kilograms on a history topping out at twenty is a typo"

    kept, why = planning.sane(None, 45.0, 45.0)
    assert kept is None and why is None, "Bodyweight work has nothing to refuse"
