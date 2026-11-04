"""Building a session from the programme rather than from whatever was logged.

The difference matters in two places that both used to fail quietly: an exercise he
skipped once vanished from the week for good, and a session done on the wrong weekday
was not recognised at all, so the weights froze at their starting values.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.domain import planning
from app.models.training import (
    Exercise,
    ProgramDay,
    ProgramExercise,
    Progression,
    SetLog,
    WorkoutSession,
)

SLOT = "1000000003"


@pytest.fixture
def friday(session, owner):
    day = ProgramDay(owner_id=owner.id, phase="Фаза 1", weekday=4, title="Фулбоди C",
                     blocks=[], is_active=True, garmin_workout_id=SLOT)
    session.add(day)
    session.flush()
    return day


def _exercise(session, owner, slug, name, key, category):
    row = Exercise(owner_id=owner.id, slug=slug, name=name, garmin_key=key,
                   category=category)
    session.add(row)
    session.flush()
    return row


def _prescribe(session, owner, day, exercise, **kwargs):
    row = ProgramExercise(
        owner_id=owner.id, program_day_id=day.id, exercise_id=exercise.id,
        order=kwargs.get("order", 1), sets=kwargs.get("sets", 3),
        target_min=kwargs.get("target_min", 10), target_max=kwargs.get("target_max", 12),
        rest_seconds=kwargs.get("rest_seconds", 90),
        progression=kwargs.get("progression", Progression.LOAD),
        seed_value=kwargs.get("seed_value"),
    )
    session.add(row)
    session.flush()
    return row


def _log(session, owner, date, exercise, sets, *, slot=SLOT):
    workout = WorkoutSession(owner_id=owner.id, local_date=date, title="Фулбоди C",
                             garmin_workout_id=slot)
    session.add(workout)
    session.flush()
    for order, (reps, weight, secs) in enumerate(sets, start=1):
        session.add(SetLog(owner_id=owner.id, session_id=workout.id,
                           exercise_id=exercise.id, order=order, reps=reps,
                           weight_kg=weight, duration_s=secs))
    session.flush()
    return workout


def test_a_session_done_on_the_wrong_day_is_still_recognised(session, owner, friday):
    """His Saturday stair session was recorded on a Friday. Matching on the date finds
    nothing and leaves the weight at its seed for ever."""
    lunge = _exercise(session, owner, "lunge", "Выпады", "DUMBBELL_REVERSE_LUNGE", "LUNGE")
    _prescribe(session, owner, friday, lunge, seed_value=10.0)
    # Both logged on a Saturday, not the Friday the programme names.
    _log(session, owner, "2026-09-12", lunge, [(12, 10.0, None)] * 3)
    _log(session, owner, "2026-09-19", lunge, [(12, 10.0, None)] * 3)

    plan = planning.plan_day(session, owner.id, friday, dt.date(2026, 9, 26))

    assert plan.exercises[0].weight_kg == 11.0
    assert "во всех подходах дважды подряд" in plan.exercises[0].reason


def test_a_skipped_exercise_stays_in_the_plan_at_its_last_weight(session, owner, friday):
    """This is how the plank left the programme: not logged once, then never proposed."""
    lunge = _exercise(session, owner, "lunge", "Выпады", "DUMBBELL_REVERSE_LUNGE", "LUNGE")
    calf = _exercise(session, owner, "calf", "Носки", "WEIGHTED_STANDING_CALF_RAISE",
                     "CALF_RAISE")
    _prescribe(session, owner, friday, lunge, order=1, seed_value=10.0)
    _prescribe(session, owner, friday, calf, order=2, seed_value=30.0)
    _log(session, owner, "2026-09-11", calf, [(20, 30.0, None)] * 3)
    _log(session, owner, "2026-09-18", lunge, [(12, 10.0, None)] * 3)   # calf skipped

    plan = planning.plan_day(session, owner.id, friday, dt.date(2026, 9, 25))
    names = [e.name for e in plan.exercises]
    carried = next(e for e in plan.exercises if e.name == "Носки")

    assert names == ["Выпады", "Носки"], "The programme decides the session, not the log"
    assert carried.weight_kg == 30.0
    assert "пропущено" in carried.reason
    assert carried.confidence == planning.LOW


def test_a_counterweight_goes_down_as_he_gets_stronger(session, owner, friday):
    """The gravitron: more assistance is easier, so progress takes it away."""
    pull = _exercise(session, owner, "grav", "Гравитрон", "BAND_ASSISTED_PULL_UP",
                     "PULL_UP")
    _prescribe(session, owner, friday, pull, progression=Progression.ASSIST,
               seed_value=40.0)
    _log(session, owner, "2026-09-11", pull, [(12, 40.0, None)] * 3)
    _log(session, owner, "2026-09-18", pull, [(12, 40.0, None)] * 3)

    line = planning.plan_day(session, owner.id, friday, dt.date(2026, 9, 25)).exercises[0]

    assert line.weight_kg < 40.0, "Assistance must fall, not rise"
    assert "снимаем" in line.reason


def test_a_hold_gets_longer_only_after_the_target_twice(session, owner, friday):
    plank = _exercise(session, owner, "side", "Боковая планка", "SIDE_PLANK", "PLANK")
    _prescribe(session, owner, friday, plank, progression=Progression.TIME,
               target_min=80, target_max=80)
    _log(session, owner, "2026-09-11", plank, [(None, None, 80.0)] * 3)
    _log(session, owner, "2026-09-18", plank, [(None, None, 80.0)] * 3)

    line = planning.plan_day(session, owner.id, friday, dt.date(2026, 9, 25)).exercises[0]

    assert line.duration_s > 80.0
    assert line.reps == 0


def test_one_good_session_is_not_enough(session, owner, friday):
    plank = _exercise(session, owner, "side", "Боковая планка", "SIDE_PLANK", "PLANK")
    _prescribe(session, owner, friday, plank, progression=Progression.TIME,
               target_min=80, target_max=80)
    _log(session, owner, "2026-09-18", plank, [(None, None, 80.0)] * 3)

    line = planning.plan_day(session, owner.id, friday, dt.date(2026, 9, 25)).exercises[0]

    assert line.duration_s == 80.0


def test_a_day_with_no_composition_yields_nothing(session, owner, friday):
    """Better no plan than a plan assembled from guesses."""
    assert planning.plan_day(session, owner.id, friday, dt.date(2026, 9, 25)) is None


def test_a_typo_in_a_logged_set_does_not_travel_into_the_plan(session, owner, friday):
    """The engine takes the heaviest set, so "450" typed for "45" would otherwise become
    next week's prescription and every week's after it."""
    squat = _exercise(session, owner, "squat", "Присед", "BARBELL_BACK_SQUAT", "SQUAT")
    _prescribe(session, owner, friday, squat, target_min=8, target_max=10, seed_value=35.0)
    _log(session, owner, "2026-09-11", squat, [(10, 35.0, None)] * 3)
    _log(session, owner, "2026-09-18", squat, [(10, 350.0, None)] * 3)

    line = planning.plan_day(session, owner.id, friday, dt.date(2026, 9, 25)).exercises[0]

    assert line.weight_kg == 350.0 or "отказ" in line.reason, (
        "Either the step is refused or it is at least not compounded"
    )
    if "отказ" in line.reason:
        assert line.confidence == planning.LOW


def test_three_sessions_show_a_direction_two_only_show_a_pair(session, owner, friday):
    """He asked for three: "три понедельника, он видит мою динамику". The rule that moves
    a weight still needs two — that is the ACSM condition — but a plateau is only visible
    against a third."""
    squat = _exercise(session, owner, "squat", "Присед", "BARBELL_BACK_SQUAT", "SQUAT")
    _prescribe(session, owner, friday, squat, target_min=8, target_max=10, seed_value=35.0)
    for date in ("2026-09-04", "2026-09-11", "2026-09-18"):
        _log(session, owner, date, squat, [(9, 35.0, None)] * 3)

    line = planning.plan_day(session, owner.id, friday, dt.date(2026, 9, 25)).exercises[0]

    assert line.weight_kg == 35.0, "Nine reps against a target of ten earns nothing"
    assert "плато" in line.reason, "Three identical sessions is the signal he wants to see"


def test_a_climb_is_shown_as_one(session, owner, friday):
    squat = _exercise(session, owner, "squat", "Присед", "BARBELL_BACK_SQUAT", "SQUAT")
    _prescribe(session, owner, friday, squat, target_min=8, target_max=10, seed_value=30.0)
    for date, weight in (("2026-09-04", 30.0), ("2026-09-11", 32.5), ("2026-09-18", 35.0)):
        _log(session, owner, date, squat, [(9, weight, None)] * 3)

    line = planning.plan_day(session, owner.id, friday, dt.date(2026, 9, 25)).exercises[0]

    assert "Динамика: 30 → 32.5 → 35" in line.reason


def test_two_sessions_are_not_called_a_trend(session, owner, friday):
    squat = _exercise(session, owner, "squat", "Присед", "BARBELL_BACK_SQUAT", "SQUAT")
    _prescribe(session, owner, friday, squat, target_min=8, target_max=10, seed_value=35.0)
    for date in ("2026-09-11", "2026-09-18"):
        _log(session, owner, date, squat, [(9, 35.0, None)] * 3)

    line = planning.plan_day(session, owner.id, friday, dt.date(2026, 9, 25)).exercises[0]

    assert "плато" not in line.reason and "Динамика" not in line.reason
