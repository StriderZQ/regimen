"""The nightly rebuild.

Two of these tests exist to stop a regression rather than to describe a feature: the fake
client raises on `delete_workout`, because nothing here may ever remove a workout the
owner made; and one asserts the worker never imports `anthropic`, because the whole point
of the job is that it runs with no model involved and that is one import away from being
untrue.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.models.training import (
    Exercise,
    PlannedWorkout,
    PlanStatus,
    ProgramDay,
    ProgramExercise,
    Progression,
    SetLog,
    WorkoutSession,
)
from app.workers import gym

SLOT = "1000000003"


class _Client:
    """A Garmin stub that answers the four calls the job makes, and refuses a fifth."""

    def __init__(self, *, fail_update=None):
        self.updated: list[tuple[str, dict]] = []
        self.uploaded: list[dict] = []
        self.scheduled: list[tuple[str, str]] = []
        self._fail_update = fail_update
        self._last: dict | None = None

    def update_workout(self, workout_id, body):
        if self._fail_update:
            raise self._fail_update
        self.updated.append((workout_id, body))
        self._last = body
        return {"workoutId": workout_id}

    def upload_workout(self, body):
        self.uploaded.append(body)
        self._last = body
        return {"workoutId": "999"}

    def schedule_workout(self, workout_id, date):
        self.scheduled.append((str(workout_id), date))

    def get_workout_by_id(self, workout_id):
        return self._last

    def get_scheduled_workouts(self, year, month):
        return [{"date": date, "workoutId": workout_id}
                for workout_id, date in self.scheduled]

    def delete_workout(self, workout_id):  # pragma: no cover - must never be called
        raise AssertionError("The service never deletes a workout")


@pytest.fixture
def monday(session, owner):
    day = ProgramDay(owner_id=owner.id, phase="Фаза 1", weekday=0, title="Фулбоди A",
                     blocks=[], is_active=True, garmin_workout_id=SLOT, kind="strength")
    session.add(day)
    session.flush()
    squat = Exercise(owner_id=owner.id, slug="squat", name="Присед",
                     garmin_key="BARBELL_BACK_SQUAT", category="SQUAT")
    session.add(squat)
    session.flush()
    session.add(ProgramExercise(
        owner_id=owner.id, program_day_id=day.id, exercise_id=squat.id, order=1,
        sets=3, target_min=8, target_max=10, rest_seconds=150,
        progression=Progression.LOAD, seed_value=35.0,
    ))
    workout = WorkoutSession(owner_id=owner.id, local_date="2026-09-14",
                             garmin_workout_id=SLOT, title="Фулбоди A")
    session.add(workout)
    session.flush()
    for order in range(1, 4):
        session.add(SetLog(owner_id=owner.id, session_id=workout.id,
                           exercise_id=squat.id, order=order, reps=10, weight_kg=35.0))
    session.flush()
    return day


def test_the_week_is_written_and_recorded(session, owner, monday):
    client = _Client()

    blocks, failures = gym.plan_week(session, owner.id, client, dt.date(2026, 9, 20))

    assert failures == []
    assert blocks, "A Monday inside the horizon must produce a block"
    assert client.updated and not client.uploaded, "The existing template is rewritten"
    assert client.scheduled == [(SLOT, "2026-09-21")]

    row = session.scalar(select_planned(owner.id))
    assert row.status is PlanStatus.WRITTEN
    assert row.plan[0]["name"] == "Присед"
    assert row.plan[0]["reason"], "The reason is kept, not just the number"


def select_planned(owner_id):
    from sqlalchemy import select

    return select(PlannedWorkout).where(PlannedWorkout.owner_id == owner_id)


def test_a_second_run_with_no_new_facts_touches_nothing(session, owner, monday):
    first = _Client()
    gym.plan_week(session, owner.id, first, dt.date(2026, 9, 20))

    second = _Client()
    blocks, failures = gym.plan_week(session, owner.id, second, dt.date(2026, 9, 20))

    assert not second.updated and not second.uploaded, "An unchanged night costs nothing"
    assert failures == []
    assert blocks and all(not b["changes"] for b in blocks), "Nothing to report"


def test_a_template_deleted_by_hand_is_recreated(session, owner, monday):
    client = _Client(fail_update=RuntimeError("API Error 404 not found"))

    blocks, failures = gym.plan_week(session, owner.id, client, dt.date(2026, 9, 20))

    assert failures == []
    assert client.uploaded, "A missing template is rebuilt rather than reported as broken"
    assert monday.garmin_workout_id == "999", "The new id replaces the dead one"


def test_a_garmin_error_marks_the_day_failed_and_keeps_the_week(session, owner, monday):
    client = _Client(fail_update=RuntimeError("API Error 500"))

    blocks, failures = gym.plan_week(session, owner.id, client, dt.date(2026, 9, 20))

    assert blocks == []
    assert failures and "не записано" in failures[0]
    row = session.scalar(select_planned(owner.id))
    assert row.status is PlanStatus.FAILED
    assert row.note


def test_a_workout_garmin_only_half_accepted_is_not_recorded_as_written(
    session, owner, monday
):
    """Garmin answers 200 for a payload it has partly ignored. Reading it back is the
    only way to find out, and the one that caught this in the reference script."""
    client = _Client()

    def half_accepted(workout_id):
        body = dict(client._last)
        segment = dict(body["workoutSegments"][0])
        segment["workoutSteps"] = [
            {**step, "description": ""} for step in segment["workoutSteps"]
        ]
        body["workoutSegments"] = [segment]
        return body

    client.get_workout_by_id = half_accepted

    blocks, failures = gym.plan_week(session, owner.id, client, dt.date(2026, 9, 20))

    assert blocks == []
    assert failures and "принял не всё" in failures[0]
    assert session.scalar(select_planned(owner.id)).status is PlanStatus.FAILED


def test_slots_are_linked_to_the_templates_he_already_runs(session, owner, monday):
    """Otherwise the first night uploads three new workouts beside his own."""
    monday.garmin_workout_id = None
    session.flush()

    assert gym.link_slots(session, owner.id) == 1
    assert monday.garmin_workout_id == SLOT


def test_the_worker_runs_without_a_model():
    """The whole premise. One careless import makes it false and nothing else would say so."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; import app.workers.gym; "
         "assert 'anthropic' not in sys.modules, sorted(sys.modules)[:0] or 'anthropic imported'"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_a_kind_with_no_builder_is_scheduled_not_rebuilt(session, owner, monday):
    """Cycling, running and the stair machine cannot be computed yet — but he wrote those
    workouts himself, and an empty Tuesday is worse than an unchanged one. The template
    goes in the calendar and its content is never touched."""
    from app.domain import workout_builder

    monday.kind = "cycling"
    session.flush()
    client = _Client()

    blocks, failures = gym.plan_week(session, owner.id, client, dt.date(2026, 9, 20))

    assert "cycling" not in workout_builder.BUILDERS
    assert not client.updated and not client.uploaded, "His workout is left alone"
    assert client.scheduled == [(SLOT, "2026-09-21")], "But the day is not left empty"
    assert failures == [] and any("поставлено" in b["changes"] for b in blocks)


def test_a_day_with_no_template_is_left_alone(session, owner, monday):
    """Nothing to schedule and nothing to invent."""
    monday.kind = "cycling"
    monday.garmin_workout_id = None
    session.flush()
    client = _Client()

    blocks, failures = gym.plan_week(session, owner.id, client, dt.date(2026, 9, 20))

    assert blocks == [] and failures == []
    assert not client.scheduled


def test_the_day_is_classified_by_what_it_says_it_is(session, owner):
    """Weekday is not a kind: he moves days, and the programme changes shape between
    phases. The title is what says whether Thursday is intervals or a gym session."""
    from app.domain.training import kind_from_title

    assert kind_from_title("Зал: Фулбоди A") == "strength"
    assert kind_from_title("Велосипед VO₂max 4×4") == "cycling"
    assert kind_from_title("Ступени с рюкзаком, КАЖДУЮ неделю") == "stairs"
    assert kind_from_title("Бег Z2 30 мин (+ ходьба по желанию)") == "running"
    assert kind_from_title("Отдых") == "rest"
    assert kind_from_title(None) == "strength", "Unrecognised stays buildable-by-default"


def test_a_day_is_never_scheduled_twice(session, owner, monday):
    """Fifteen duplicate workouts reached his watch this way. Updating a workout is
    idempotent; scheduling it is not, so the calendar is asked before it is written to."""
    client = _Client()
    gym.plan_week(session, owner.id, client, dt.date(2026, 9, 20))
    first = list(client.scheduled)

    # A failing day keeps FAILED status, so the next run legitimately retries the write —
    # and must not add a second calendar entry while doing so.
    session.query(PlannedWorkout).delete()
    session.flush()
    gym.plan_week(session, owner.id, client, dt.date(2026, 9, 20))

    assert client.scheduled == first, "The calendar gained an entry on a rewrite"


def test_each_day_is_committed_before_the_next_is_written(session, owner, monday):
    """The job used to run as one transaction. It raised on its last line, the rollback
    took the record of what had already gone to Garmin, and the queue retried five times."""
    client = _Client()

    gym.plan_week(session, owner.id, client, dt.date(2026, 9, 20))

    row = session.scalar(select_planned(owner.id))
    assert row is not None and row.status is PlanStatus.WRITTEN
    # Committed, not merely pending: a rollback after this point must not lose it.
    assert row not in session.new


def test_the_readiness_note_never_costs_the_plan(session, owner):
    """It is decoration, and it took the whole week down once by formatting a model
    object as a number."""
    assert gym._readiness_note(session, owner.id, dt.date(2026, 9, 20)) is None


def test_a_slot_waits_for_its_own_unfinished_session(session, owner, monday):
    """His objection: writing next Monday while this Monday has not been trained is
    putting a number into an equation you already know you will replace."""
    client = _Client()

    # Planning from a Monday he has not trained yet: next Monday must not be written.
    blocks, failures = gym.plan_week(session, owner.id, client, dt.date(2026, 9, 21))

    assert failures == []
    assert client.scheduled == [], "Next Monday waits until this Monday is in the books"


def test_the_day_appears_once_its_predecessor_is_recorded(session, owner, monday):
    from app.models.training import WorkoutSession

    workout = WorkoutSession(owner_id=owner.id, local_date="2026-09-21",
                             garmin_workout_id=SLOT, title="Фулбоди A")
    session.add(workout)
    session.flush()
    client = _Client()

    gym.plan_week(session, owner.id, client, dt.date(2026, 9, 21))

    assert client.scheduled == [(SLOT, "2026-09-28")]


TESTS = "1000000001|RAMP TEST — FTP|0;1000000002|LTHR TEST — порог|2"


def test_no_threshold_tests_are_booked_unless_configured(session, owner, monkeypatch):
    """A workout id is personal: with nothing configured, nothing is scheduled."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "garmin_threshold_tests", "")
    client = _Client()

    assert gym.keep_tests_scheduled(session, owner.id, client, dt.date(2026, 10, 8)) == []
    assert client.scheduled == []


def test_the_next_threshold_test_is_booked_six_weeks_after_the_last(
    session, owner, monkeypatch
):
    """Every zone is computed from a measured threshold, and the measurement goes stale.
    The date is computed from the test actually performed."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "garmin_threshold_tests", TESTS)
    from app.models.training import WorkoutSession

    ramp = WorkoutSession(owner_id=owner.id, local_date="2026-10-07",
                          garmin_workout_id="1000000001", title="RAMP TEST")
    session.add(ramp)
    session.flush()
    client = _Client()

    said = gym.keep_tests_scheduled(session, owner.id, client, dt.date(2026, 10, 8))

    assert ("1000000001", "2026-11-18") in client.scheduled, "Six weeks after 07.10"
    assert any("6 недель" in line for line in said)


def test_a_test_already_on_the_calendar_is_left_alone(session, owner, monkeypatch):
    """Dates get moved by hand. Nothing here argues with a booking that exists."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "garmin_threshold_tests", TESTS)
    client = _Client()
    client.scheduled.append(("1000000001", "2026-11-18"))
    client.scheduled.append(("1000000002", "2026-11-20"))

    said = gym.keep_tests_scheduled(session, owner.id, client, dt.date(2026, 10, 8))

    assert said == []
    assert len(client.scheduled) == 2, "No second copy of a test already booked"


def test_the_message_says_what_moved_and_little_else(session, owner, monday):
    """A full-week listing is too long to read. Tomorrow by name, the weights that
    moved, one line for the rest."""
    blocks = [
        {"date": dt.date(2026, 9, 22), "title": "Велосипед Z2", "changes": []},
        {"date": dt.date(2026, 9, 23), "title": "Фулбоди B",
         "changes": ["Жим ногами: 45 кг → 47,5 кг"]},
        {"date": dt.date(2026, 9, 25), "title": "Фулбоди C", "changes": []},
    ]

    text = gym.compose(blocks, [], [], "Готовность 42 — на план не влияет.",
                       dt.date(2026, 9, 21))

    assert "Завтра · ВТ 22.09 · Велосипед Z2" in text
    assert "Жим ногами: 45 кг → 47,5 кг" in text
    assert "Без изменений: ВТ 22.09, ПТ 25.09" in text
    assert len(text.splitlines()) < 12, "Short enough to read at a glance"
    assert "Фулбоди B" not in text.split("Изменилось:")[0].split("Завтра")[1]


def test_a_failure_is_never_buried(session, owner, monday):
    text = gym.compose([], ["ПТ 25.09: не записано — API Error 500"], [], None,
                       dt.date(2026, 9, 21))

    assert "⚠️" in text and "API Error 500" in text
