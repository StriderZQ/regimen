"""Gym tests: the program, set history, recording facts, substituting an exercise."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from app.domain import training
from app.domain.training import TrainingError
from app.models import Principal
from app.models.training import (
    Equipment,
    Exercise,
    ProgramDay,
    SessionSource,
    SetLog,
    WorkoutSession,
)

MONDAY = dt.date(2026, 9, 14)
SUNDAY = dt.date(2026, 9, 13)


@pytest.fixture
def squat(session: Session, owner: Principal) -> Exercise:
    exercise = Exercise(
        owner_id=owner.id, slug="barbell-back-squat", name="Приседания со штангой",
        garmin_key="BARBELL_BACK_SQUAT", category="SQUAT", in_program=True,
    )
    session.add(exercise)
    session.flush()
    return exercise


@pytest.fixture
def program(session: Session, owner: Principal):
    session.add_all(
        [
            ProgramDay(owner_id=owner.id, phase="Фаза 1", weekday=0, title="Зал: Фулбоди A",
                       evening="Зал: Фулбоди A", purpose="Сила", blocks=[],
                       snapshot_date="2026-09-07"),
            ProgramDay(owner_id=owner.id, phase="Фаза 1", weekday=6, title="Бег Z2 30 мин",
                       morning="Бег Z2 30 мин", purpose="Беговая база", blocks=[]),
        ]
    )
    session.flush()


def _log_session(session, owner, exercise, date, sets, source=SessionSource.GARMIN):
    workout = WorkoutSession(owner_id=owner.id, local_date=date, source=source,
                             title="Силовая", external_id=f"g-{date}")
    session.add(workout)
    session.flush()
    for i, (reps, weight) in enumerate(sets, start=1):
        session.add(SetLog(owner_id=owner.id, session_id=workout.id,
                           exercise_id=exercise.id, order=i, reps=reps, weight_kg=weight))
    session.flush()
    return workout


def test_program_answers_what_is_today(session, owner, program):
    assert training.program_for(session, owner.id, MONDAY).title == "Зал: Фулбоди A"
    assert training.program_for(session, owner.id, SUNDAY).morning == "Бег Z2 30 мин"


def test_program_day_carries_snapshot_date(session, owner, program):
    """The program lives in Garmin: a copy must say which date it was taken."""
    day = training.program_for(session, owner.id, MONDAY)
    assert day.snapshot_date == "2026-09-07"


def test_rest_day_is_absence_not_error(session, owner, program):
    assert training.program_for(session, owner.id, dt.date(2026, 9, 15)) is None


def test_history_says_where_he_stopped(session, owner, squat):
    _log_session(session, owner, squat, "2026-09-01", [(10, 20.0), (10, 30.0)])
    _log_session(session, owner, squat, "2026-09-07", [(10, 30.0), (9, 35.0)])

    result = training.history(session, owner.id, squat)

    assert result.sessions_count == 2
    assert result.last_date == "2026-09-07"
    assert result.best_weight_kg == 35.0
    assert "35 kg × 9" in result.start_hint


def test_history_does_not_prescribe(session, owner, squat):
    """Progression is the owner's call. The hint states a fact, not an instruction."""
    _log_session(session, owner, squat, "2026-09-07", [(12, 30.0), (12, 30.0), (12, 30.0)])
    hint = training.history(session, owner.id, squat).start_hint
    assert "Last time" in hint
    assert "добавь" not in hint.lower()


def test_bodyweight_set_is_not_zero_kilograms(session, owner):
    plank = Exercise(owner_id=owner.id, slug="plank", name="Планка", category="PLANK")
    session.add(plank)
    session.flush()
    workout = WorkoutSession(owner_id=owner.id, local_date="2026-09-07",
                             source=SessionSource.GARMIN, external_id="g-1")
    session.add(workout)
    session.flush()
    session.add(SetLog(owner_id=owner.id, session_id=workout.id, exercise_id=plank.id,
                       order=1, duration_s=50))
    session.flush()

    result = training.history(session, owner.id, plank)
    assert result.best_weight_kg is None
    assert "50 s" in result.start_hint


def test_unknown_exercise_is_empty_history_not_a_guess(session, owner, squat):
    result = training.history(session, owner.id, squat)
    assert result.last_sets is None
    assert "No history" in result.start_hint


def test_log_set_writes_the_fact_immediately(session, owner, squat):
    """A set is a fact, not a proposal: it needs no confirmation."""
    entry = training.log_set(session, owner.id, exercise_name="Приседания со штангой",
                             reps=10, weight_kg=40, day=MONDAY)
    assert entry.order == 1
    second = training.log_set(session, owner.id, exercise_name="приседания со штангой",
                              reps=9, weight_kg=40, day=MONDAY)
    assert second.order == 2
    assert second.session_id == entry.session_id


def test_log_set_refuses_unknown_exercise(session, owner):
    with pytest.raises(TrainingError, match="not in the registry"):
        training.log_set(session, owner.id, exercise_name="Жим лёжа на луне", reps=10)


def test_log_set_refuses_empty_set(session, owner, squat):
    with pytest.raises(TrainingError, match="reps"):
        training.log_set(session, owner.id, exercise_name="Приседания со штангой")


def test_find_prefers_exact_name(session, owner, squat):
    session.add(Exercise(owner_id=owner.id, slug="squat-jump", name="Приседания с выпрыгиванием",
                         category="SQUAT"))
    session.flush()
    found = training.find_exercise(session, owner.id, "Приседания со штангой")
    assert found.id == squat.id


def test_find_by_garmin_key(session, owner, squat):
    assert training.find_exercise(session, owner.id, "BARBELL_BACK_SQUAT").id == squat.id


def test_alternatives_come_from_same_category(session, owner, squat):
    session.add_all([
        Exercise(owner_id=owner.id, slug="leg-press", name="Жим ногами", category="SQUAT"),
        Exercise(owner_id=owner.id, slug="plank", name="Планка", category="PLANK"),
    ])
    session.flush()
    names = [e.name for e in training.alternatives(session, owner.id, squat)]
    assert names == ["Жим ногами"]


def test_equipment_search_and_unverified_flag(session, owner):
    session.add_all([
        Equipment(owner_id=owner.id, name="Shoulder Press FT555", trains="дельты, трицепс",
                  unverified=True),
        Equipment(owner_id=owner.id, name="Leg Extension", trains="квадрицепс"),
    ])
    session.flush()
    found = training.equipment(session, owner.id, needle="дельт")
    assert [e.name for e in found] == ["Shoulder Press FT555"]
    assert found[0].unverified is True


def test_garmin_session_is_not_imported_twice(session, owner, squat):
    first = training.open_session(session, owner.id, day=MONDAY, external_id="activity-1",
                                  source=SessionSource.GARMIN)
    again = training.open_session(session, owner.id, day=MONDAY, external_id="activity-1",
                                  source=SessionSource.GARMIN)
    assert first.id == again.id


def test_an_unknown_exercise_can_be_added_on_his_word(session: Session, owner: Principal):
    """Refusing to guess was right; refusing forever was not.

    The registry came from a one-off file import, so a machine the gym added later could
    never be logged at all — the answer said "or we add a new one" and nothing could.
    """
    with pytest.raises(TrainingError, match="not in the registry"):
        training.log_set(session, owner.id, exercise_name="ClimbMill", duration_s=3984)

    entry = training.log_set(
        session, owner.id, exercise_name="ClimbMill", duration_s=3984,
        create_if_unknown=True, category="кардио",
    )

    assert entry.exercise.name == "ClimbMill"
    assert training.find_exercise(session, owner.id, "ClimbMill") is not None, (
        "Added once, it must be findable next time without asking again"
    )


def test_history_admits_when_garmin_knows_more(session: Session, owner: Principal):
    """A one-off import goes stale silently: the weight sounds like yesterday's.

    Rather than copying Garmin's sessions into our own table — the same training kept
    twice and drifting — the answer says newer sessions exist elsewhere.
    """
    from app.models.base import utcnow
    from app.models.garmin import GarminKind, GarminMetric

    exercise = training.add_exercise(session, owner.id, "Жим лёжа", category="грудь")
    training.log_set(
        session, owner.id, exercise_name="Жим лёжа", reps=10, weight_kg=60,
        day=dt.date(2026, 9, 9),
    )
    session.add(GarminMetric(
        owner_id=owner.id, kind=GarminKind.ACTIVITY, external_id="a1",
        local_date="2026-09-11", fetched_at=utcnow(), title="Фулбоди",
        details={"type": "strength_training"},
    ))
    session.flush()

    hist = training.history(session, owner.id, exercise)

    assert hist.last_date == "2026-09-09"
    assert hist.newer_sessions_in_garmin == 1
    assert "2026-09-11" in (hist.freshness_note or ""), (
        "The gap must be named, or a week-old weight passes as the last time"
    )


def test_plank_does_not_match_side_plank(session, owner):
    """PLANK is a substring of SIDE_PLANK. A fuzzy match put the side plank into Monday,
    a day that does not contain it — silently, because nothing reports a wrong exercise."""
    from app.migration.training_files import _match_exercise
    from app.models.training import Exercise

    session.add(Exercise(owner_id=owner.id, slug="side-plank", name="Боковая планка",
                         garmin_key="SIDE_PLANK", category="PLANK"))
    session.add(Exercise(owner_id=owner.id, slug="plank", name="Планка",
                         garmin_key="PLANK", category="PLANK"))
    session.flush()

    found = _match_exercise(session, owner, {"garmin_key": "PLANK", "title": "ПЛАНКА",
                                             "category": "PLANK"})

    assert found.garmin_key == "PLANK"


def test_keyless_work_matches_the_placeholder_that_has_the_history(session, owner):
    """The vacuum has no Garmin key and is logged under a category with no name. Several
    such rows exist; the real one is the one the sets point at."""
    from app.migration.training_files import _match_exercise
    from app.models.training import Exercise, SetLog, WorkoutSession

    empty = Exercise(owner_id=owner.id, slug="core-empty", name="(только категория CORE)",
                     garmin_key=None, category="CORE")
    used = Exercise(owner_id=owner.id, slug="core", name="CORE",
                    garmin_key=None, category="CORE")
    session.add_all([empty, used])
    session.flush()
    workout = WorkoutSession(owner_id=owner.id, local_date="2026-09-07")
    session.add(workout)
    session.flush()
    session.add(SetLog(owner_id=owner.id, session_id=workout.id, exercise_id=used.id,
                       order=1, duration_s=20.0))
    session.flush()

    found = _match_exercise(session, owner, {"garmin_key": None, "title": "ВАКУУМ ЖИВОТА",
                                             "category": "CORE"})

    assert found.id == used.id


def test_an_exercise_that_is_not_in_the_registry_is_reported_not_invented(session, owner):
    """A guessed Garmin key becomes a different movement on the watch without saying so."""
    from app.migration.training_files import _match_exercise

    assert _match_exercise(session, owner, {"garmin_key": "NOT_A_REAL_KEY",
                                            "title": "Ничего", "category": None}) is None


def test_a_name_match_is_exact_not_a_substring(session, owner):
    """Exercises logged before their Garmin key was known carry no key, so the name is
    the only handle. It must be equality: "планка" must not find "боковая планка"."""
    from app.migration.training_files import _match_exercise
    from app.models.training import Exercise

    session.add(Exercise(owner_id=owner.id, slug="side", name="Боковая планка",
                         garmin_key=None, category="PLANK"))
    session.add(Exercise(owner_id=owner.id, slug="plank", name="Планка",
                         garmin_key=None, category="PLANK"))
    session.flush()

    found = _match_exercise(session, owner, {"garmin_key": "PLANK", "title": "ПЛАНКА",
                                             "category": "PLANK"})

    assert found.name == "Планка"
