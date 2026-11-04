"""What the watch receives.

Every assertion here stands for a defect that reached the gym floor once: a workout that
would not construct at all, steps with no name because the category was guessed, sets
merged into one block with no comment, no warm-up, and held exercises silently dropped
because they carry seconds instead of repetitions.
"""

from __future__ import annotations

import pytest

from app.domain import workout_builder
from app.domain.planning import ExercisePlan, SessionPlan


def _plan(*exercises: ExercisePlan) -> SessionPlan:
    return SessionPlan(
        date="2026-09-26", title="Фулбоди C · Пт", source_date="2026-09-18",
        exercises=list(exercises),
    )


def _loaded(**kwargs) -> ExercisePlan:
    base = dict(
        name="Обратные выпады с гантелями", garmin_key="DUMBBELL_REVERSE_LUNGE",
        sets=3, reps=12, weight_kg=10.0, reason="повторить 10 кг: вес в подходах менялся",
        category="LUNGE",
    )
    base.update(kwargs)
    return ExercisePlan(**base)


def _steps(plan: SessionPlan) -> list[dict]:
    workout, _ = workout_builder.build(plan)
    return workout.to_dict()["workoutSegments"][0]["workoutSteps"]


def test_the_workout_constructs_at_all():
    """It did not. The DTO omitted two required fields and raised before any upload."""
    workout, skipped = workout_builder.build(_plan(_loaded()))
    body = workout.to_dict()

    assert body["sportType"]["sportTypeKey"] == "strength_training"
    assert body["estimatedDurationInSecs"] > 0
    assert body["workoutSegments"], "Garmin refuses a workout with no segment"
    assert skipped == []


def test_warmup_opens_and_cooldown_closes():
    steps = _steps(_plan(_loaded()))

    assert steps[0]["stepType"]["stepTypeKey"] == "warmup"
    assert steps[0]["endConditionValue"] == workout_builder.WARMUP_SECONDS
    assert steps[-1]["stepType"]["stepTypeKey"] == "cooldown"
    assert steps[-1]["endConditionValue"] == workout_builder.COOLDOWN_SECONDS


def test_every_set_is_its_own_step_with_its_own_comment():
    """The repeat group collapses sets under one description. He reads them at the rack."""
    steps = _steps(_plan(_loaded(sets=3)))
    work = [s for s in steps if s["stepType"]["stepTypeKey"] == "interval"]

    assert len(work) == 3
    assert all(s["description"].strip() for s in steps), "A step with no text is a blank screen"
    assert "1/3" in work[0]["description"]
    assert "3/3" in work[2]["description"]


def test_the_reason_for_the_weight_travels_onto_the_watch():
    steps = _steps(_plan(_loaded(reason="10 кг × 12 дважды подряд — норма выполнена")))
    first = next(s for s in steps if s["stepType"]["stepTypeKey"] == "interval")

    assert "дважды подряд" in first["description"]


def test_the_category_comes_from_the_exercise_not_from_the_key():
    """Splitting DUMBBELL_REVERSE_LUNGE gives DUMBBELL, which Garmin does not know."""
    steps = _steps(_plan(_loaded()))
    work = [s for s in steps if s["stepType"]["stepTypeKey"] == "interval"]

    assert {s["category"] for s in work} == {"LUNGE"}


def test_every_category_written_is_one_garmin_knows():
    from garminconnect.exercises import CATEGORIES

    steps = _steps(_plan(_loaded(), _loaded(name="Планка", garmin_key="PLANK", category="PLANK")))

    for step in steps:
        if step.get("category"):
            assert step["category"] in CATEGORIES


def test_an_unknown_category_is_refused_rather_than_sent():
    with pytest.raises(workout_builder.UnknownCategory):
        workout_builder.build(_plan(_loaded(category="DUMBBELL")))


def test_weight_is_written_in_kilograms():
    """Workouts he trained from carry 8.0 for 8 kg. A thousandfold error is not subtle."""
    steps = _steps(_plan(_loaded(weight_kg=47.5)))
    first = next(s for s in steps if s["stepType"]["stepTypeKey"] == "interval")

    assert first["weightValue"] == 47.5
    assert first["weightUnit"]["unitKey"] == "kilogram"


def test_held_work_is_written_as_seconds_not_repetitions():
    """Plank and the vacuum carry a duration and no reps; an engine reading only reps
    cannot see them, and they vanished from every proposal for a month."""
    steps = _steps(_plan(_loaded(
        name="Боковая планка", garmin_key="SIDE_PLANK", category="PLANK",
        reps=0, weight_kg=None, duration_s=80.0,
        reason="повторить 80 сек — удержание",
    )))
    work = [s for s in steps if s["stepType"]["stepTypeKey"] == "interval"]

    assert all(s["endCondition"]["conditionTypeKey"] == "time" for s in work)
    assert all(s["endConditionValue"] == 80.0 for s in work)
    assert "сек" in work[0]["description"]


def test_an_exercise_with_no_garmin_key_is_written_by_category():
    """The vacuum has no key. Dropping it shortens the session without saying so."""
    steps = _steps(_plan(_loaded(
        name="Вакуум живота", garmin_key=None, category="CORE",
        reps=0, weight_kg=None, duration_s=20.0, sets=5,
    )))
    work = [s for s in steps if s["stepType"]["stepTypeKey"] == "interval"]

    assert len(work) == 5
    assert all(s["category"] == "CORE" for s in work)
    assert all(s["exerciseName"] == "" for s in work)


def test_step_orders_are_unique_and_increasing():
    """Colliding orders are accepted by the API and then rendered in the wrong sequence."""
    steps = _steps(_plan(_loaded(), _loaded(name="Планка", garmin_key="PLANK",
                                            category="PLANK", sets=3)))
    orders = [s["stepOrder"] for s in steps]

    assert orders == sorted(orders)
    assert len(set(orders)) == len(orders)


def test_rest_separates_the_sets_and_names_what_comes_next():
    steps = _steps(_plan(
        _loaded(),
        _loaded(name="Боковая планка", garmin_key="SIDE_PLANK", category="PLANK"),
    ))
    rests = [s for s in steps if s["stepType"]["stepTypeKey"] == "rest"]

    assert rests, "Without a rest step the watch runs straight into the next set"
    assert any("Далее: Боковая планка" in s["description"] for s in rests)
    assert steps[-1]["stepType"]["stepTypeKey"] == "cooldown"
    assert steps[-2]["stepType"]["stepTypeKey"] != "rest", (
        "The cool-down ends the session; a rest before it reads as 'more is coming'"
    )


def test_a_description_never_exceeds_what_garmin_stores():
    long_reason = "причина " * 200
    steps = _steps(_plan(_loaded(reason=long_reason, source=long_reason)))

    assert all(len(s["description"]) <= workout_builder.MAX_DESCRIPTION for s in steps)


def test_garmins_own_placeholder_is_skipped_not_raised_on():
    """UNKNOWN is what Garmin calls a set it could not identify, and it arrives through
    the activity import. One such row must not cost the whole session."""
    workout, skipped = workout_builder.build(_plan(
        _loaded(),
        _loaded(name="UNKNOWN", garmin_key=None, category="UNKNOWN"),
    ))
    steps = workout.to_dict()["workoutSegments"][0]["workoutSteps"]

    assert skipped == ["UNKNOWN"]
    assert all(s.get("category") != "UNKNOWN" for s in steps)
