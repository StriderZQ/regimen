"""Garmin tests: raw payloads, parsing, freshness, behaviour with a dead token.

There is no live Garmin here and there cannot be — there is one account and login needs
a human. What is tested is everything independent of that: parsing responses,
deduplication, and honesty about freshness.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from app.integrations.garmin import sync as garmin
from app.models import Principal
from app.models.base import utcnow
from app.models.garmin import GarminKind, GarminMetric, GarminRaw

ACTIVITY = {
    "activityId": 20000000001,
    "activityName": "Суббота — ClimbMill",
    "startTimeGMT": "2026-09-12 08:12:00",
    "duration": 3600.0,
    "calories": 650,
    "averageHR": 132,
    "maxHR": 155,
    "aerobicTrainingEffect": 3.2,
    "activityType": {"typeKey": "indoor_climbing"},
}

# The shape is taken from a real Garmin response: the score sits INSIDE dailySleepDTO
# rather than at the top level. The parser used to look in the wrong place.
SLEEP = {
    "dailySleepDTO": {
        "sleepTimeSeconds": 26100,
        "deepSleepSeconds": 4500,
        "remSleepSeconds": 5100,
        "sleepStartTimestampGMT": 1757650000000,
        "sleepScoreFeedback": "POSITIVE_LONG_AND_CALM",
        "sleepScores": {"overall": {"value": 81, "qualifierKey": "GOOD"}},
    },
}


def test_activity_becomes_raw_and_metric(session: Session, owner: Principal):
    report = garmin.SyncReport()
    garmin.import_activities(session, owner.id, [ACTIVITY], report)

    assert report.raw_saved == 1 and report.metrics_saved == 1
    metric = session.query(GarminMetric).one()
    assert metric.title == "Суббота — ClimbMill"
    assert metric.duration_min == 60.0
    assert metric.avg_hr == 132
    assert metric.details["type"] == "indoor_climbing"


def test_raw_payload_is_kept_whole(session: Session, owner: Principal):
    """Garmin's format changes without warning — only the raw payload allows a rebuild."""
    report = garmin.SyncReport()
    garmin.import_activities(session, owner.id, [ACTIVITY], report)
    raw = session.query(GarminRaw).one()
    assert raw.payload == ACTIVITY
    assert raw.kind is GarminKind.ACTIVITY


def test_event_time_and_fetch_time_are_separate(session: Session, owner: Principal):
    """The workout happened yesterday and arrived today: these times must not be confused."""
    report = garmin.SyncReport()
    garmin.import_activities(session, owner.id, [ACTIVITY], report)
    raw = session.query(GarminRaw).one()
    assert raw.event_at.date() == dt.date(2026, 9, 12)
    assert raw.fetched_at > raw.event_at


def test_same_activity_is_not_imported_twice(session: Session, owner: Principal):
    first, second = garmin.SyncReport(), garmin.SyncReport()
    garmin.import_activities(session, owner.id, [ACTIVITY], first)
    garmin.import_activities(session, owner.id, [ACTIVITY], second)

    assert second.raw_saved == 0
    assert second.skipped == 1
    assert session.query(GarminRaw).count() == 1


def test_activity_without_id_is_reported(session: Session, owner: Principal):
    report = garmin.SyncReport()
    garmin.import_activities(session, owner.id, [{"activityName": "без id"}], report)
    assert report.errors and report.raw_saved == 0


def test_sleep_is_parsed_into_minutes(session: Session, owner: Principal):
    report = garmin.SyncReport()
    garmin.import_sleep(session, owner.id, "2026-09-12", SLEEP, report)

    metric = session.query(GarminMetric).one()
    assert metric.value == 435.0
    assert metric.details["deep_min"] == 75.0
    assert metric.details["score"] == 81
    assert metric.details["score_qualifier"] == "GOOD"
    assert metric.details["feedback"] == "POSITIVE_LONG_AND_CALM"


def test_hrv_is_parsed(session: Session, owner: Principal):
    report = garmin.SyncReport()
    garmin.import_hrv(
        session, owner.id, "2026-09-12",
        {"hrvSummary": {"lastNightAvg": 52, "status": "BALANCED"}}, report,
    )
    metric = session.query(GarminMetric).one()
    assert metric.value == 52
    assert metric.details["status"] == "BALANCED"


def test_freshness_says_no_data_rather_than_guessing(session: Session, owner: Principal):
    rows = {f.kind: f for f in garmin.freshness(session, owner.id)}
    assert rows[GarminKind.SLEEP].last_event is None
    assert rows[GarminKind.SLEEP].stale
    assert "no data at all" in rows[GarminKind.SLEEP].describe()


def test_fresh_data_is_not_called_stale(session: Session, owner: Principal):
    session.add(
        GarminMetric(
            owner_id=owner.id, kind=GarminKind.SLEEP, external_id="today",
            local_date=dt.date.today().isoformat(), event_at=utcnow(),
            fetched_at=utcnow(), value=430, details={},
        )
    )
    session.flush()
    row = next(f for f in garmin.freshness(session, owner.id) if f.kind is GarminKind.SLEEP)
    assert not row.stale
    assert "СТАРЫЕ" not in row.describe()


def test_old_data_is_called_old_out_loud(session: Session, owner: Principal):
    """Garmin breaks as a matter of course. "Sleep from Thursday" beats silence."""
    old = utcnow() - dt.timedelta(days=4)
    session.add(
        GarminMetric(
            owner_id=owner.id, kind=GarminKind.SLEEP, external_id="old",
            local_date=old.date().isoformat(), event_at=old, fetched_at=utcnow(),
            value=400, details={},
        )
    )
    session.flush()
    row = next(f for f in garmin.freshness(session, owner.id) if f.kind is GarminKind.SLEEP)
    assert row.stale
    assert "STALE DATA" in row.describe()


def test_missing_tokens_ask_for_a_human(monkeypatch, tmp_path):
    monkeypatch.setattr(garmin, "token_dir", lambda: tmp_path / "нет-папки")
    with pytest.raises(garmin.GarminAuthError, match="garmin-login"):
        garmin.connect()


class _FakeGarmin:
    def __init__(self, *, activities=None, sleep=None, hrv=None, fail=()):
        self._activities = activities or []
        self._sleep = sleep
        self._hrv = hrv
        self._fail = set(fail)

    def get_activities(self, _start, _limit):
        if "activities" in self._fail:
            raise RuntimeError("Garmin отдал 500")
        return self._activities

    def get_sleep_data(self, _day):
        if "sleep" in self._fail:
            raise RuntimeError("сон недоступен")
        return self._sleep

    def get_hrv_data(self, _day):
        return self._hrv

    def get_training_readiness(self, _day):
        return None

    def get_stats_and_body(self, _day):
        return None


def test_sync_collects_everything(session: Session, owner: Principal):
    client = _FakeGarmin(activities=[ACTIVITY], sleep=SLEEP, hrv=None)
    report = garmin.sync(session, owner.id, client, days_back=1)
    assert report.metrics_saved == 2
    assert not report.errors


def test_one_broken_endpoint_does_not_lose_the_rest(session: Session, owner: Principal):
    """Sleep fell over — the activities must still arrive."""
    client = _FakeGarmin(activities=[ACTIVITY], sleep=SLEEP, fail=["sleep"])
    report = garmin.sync(session, owner.id, client, days_back=1)

    assert report.metrics_saved == 1
    assert any("Sleep" in e for e in report.errors)


def test_dead_token_alerts_and_does_not_retry_forever(
    session: Session, owner: Principal, monkeypatch
):
    """Until a human logs in again retrying is pointless — but it must be said."""
    from app.integrations.garmin import jobs as garmin_jobs
    from app.models.ops import Job

    sent: list[str] = []
    monkeypatch.setattr(garmin_jobs, "alert", sent.append)
    monkeypatch.setattr(
        "app.integrations.garmin.sync.connect",
        lambda: (_ for _ in ()).throw(garmin.GarminAuthError("токен мёртв")),
    )

    job = Job(kind=garmin_jobs.JOB_KIND, payload={}, run_after=utcnow())
    session.add(job)
    session.flush()
    garmin_jobs.run_sync(session, job)

    assert sent and "access lost" in sent[0]
    assert "needs_relogin" in job.payload


READINESS = [
    {
        "score": 45, "level": "LOW", "sleepScore": 81,
        "sleepScoreFactorFeedback": "MODERATE", "stressHistoryFactorFeedback": "GOOD",
        "timestamp": "2026-09-12T05:12:00.0",
    }
]

BODY = {
    "totalKilocalories": 2100.0, "activeKilocalories": 600.0, "bmrKilocalories": 1500.0,
    "restingHeartRate": 55, "averageStressLevel": 25, "totalSteps": 7000,
    "bodyBatteryHighestValue": 80, "bodyBatteryLowestValue": 40, "weight": 85200.0,
}


def test_readiness_keeps_the_factors(session: Session, owner: Principal):
    """A score with no reasons is useless: what sagged has to be visible."""
    report = garmin.SyncReport()
    garmin.import_readiness(session, owner.id, "2026-09-12", READINESS, report)

    metric = session.query(GarminMetric).one()
    assert metric.value == 45
    assert metric.details["level"] == "LOW"
    assert metric.details["sleep_score"] == 81
    assert metric.details["stress_factor"] == "GOOD"


def test_readiness_without_a_score_is_skipped(session: Session, owner: Principal):
    report = garmin.SyncReport()
    garmin.import_readiness(session, owner.id, "2026-09-12", [{"level": "LOW"}], report)
    assert session.query(GarminMetric).count() == 0


def test_body_summary_converts_weight_to_kilograms(session: Session, owner: Principal):
    """Garmin returns weight in grams — 85200 is 85.2 kg, not 85 tonnes."""
    report = garmin.SyncReport()
    garmin.import_body(session, owner.id, "2026-09-12", BODY, report)

    metric = session.query(GarminMetric).one()
    assert metric.value == 85.2
    assert metric.calories == 2100.0
    assert metric.details["resting_hr"] == 55
    assert metric.details["steps"] == 7000


def test_sync_collects_readiness_and_body(session: Session, owner: Principal):
    class _Full(_FakeGarmin):
        def get_training_readiness(self, _day):
            return READINESS

        def get_stats_and_body(self, _day):
            return BODY

    report = garmin.sync(session, owner.id, _Full(activities=[], sleep=None), days_back=1)

    kinds = {m.kind for m in session.query(GarminMetric).all()}
    assert GarminKind.READINESS in kinds
    assert GarminKind.BODY in kinds
    assert not report.errors


# --- strength sets ---------------------------------------------------------------------


def _recent_day(days_ago: int = 1) -> str:
    """A date inside the import window.

    `import_strength_sets` drops activities older than `today - days_back`, so a literal
    date in a test stops being imported the moment the calendar walks past it — which is
    exactly how these tests turned red without a line of code changing.
    """
    import datetime as dt

    from app.models.base import utcnow

    return (utcnow() - dt.timedelta(days=days_ago)).date().isoformat()


class _SetsClient:
    """A Garmin stub that answers the two calls the set import makes."""

    def __init__(self, activities, sets):
        self._activities = activities
        self._sets = sets

    def get_activities(self, start, limit):
        return self._activities

    def get_activity_exercise_sets(self, activity_id):
        return self._sets


def test_strength_sets_carry_the_weight_the_summary_hides(session, owner):
    """The activity summary says "3 sets of push-ups"; the weight lives only here.

    Without it "what did I lift last time" has no answer, and progression cannot be
    computed at all.
    """
    from app.domain import training
    from app.integrations.garmin import sync as garmin

    activities = [{
        "activityId": 777, "activityName": "Фулбоди", "duration": 4200,
        "startTimeLocal": f"{_recent_day()} 09:00:00",
        "activityType": {"typeKey": "strength_training"},
    }]
    sets = {"exerciseSets": [
        {"setType": "REST", "duration": 60.0},
        {"setType": "ACTIVE", "duration": 90.0, "repetitionCount": 12, "weight": 50000.0,
         "exercises": [{"category": "LEG_PRESS", "name": "LEG_PRESS", "probability": 99}]},
        {"setType": "ACTIVE", "duration": 88.0, "repetitionCount": 12, "weight": 50000.0,
         "exercises": [{"category": "LEG_PRESS", "name": "LEG_PRESS", "probability": 99}]},
        {"setType": "ACTIVE", "duration": 30.0, "repetitionCount": None, "weight": None,
         "exercises": [{"category": "UNKNOWN", "name": None, "probability": 50}]},
    ]}
    report = garmin.SyncReport()

    garmin.import_strength_sets(session, owner.id, _SetsClient(activities, sets), report)

    exercise = training.find_exercise(session, owner.id, "LEG_PRESS")
    assert exercise is not None, "An unseen Garmin key is added, not dropped"
    hist = training.history(session, owner.id, exercise)
    assert hist.last_date == _recent_day()
    assert hist.best_weight_kg == 50.0, "Grams from Garmin become kilograms here"
    assert hist.best_weight_reps == 12
    assert report.metrics_saved == 2, "Rest blocks and unidentified sets are not sets"


def test_a_second_sync_does_not_double_the_session(session, owner):
    """Syncs repeat after timeouts; a workout must not gain the same sets twice."""
    from app.integrations.garmin import sync as garmin

    activities = [{
        "activityId": 778, "activityName": "Фулбоди", "duration": 3600,
        "startTimeLocal": f"{_recent_day()} 09:00:00",
        "activityType": {"typeKey": "strength_training"},
    }]
    sets = {"exerciseSets": [
        {"setType": "ACTIVE", "duration": 90.0, "repetitionCount": 10, "weight": 25000.0,
         "exercises": [{"category": "SQUAT", "name": "BARBELL_BACK_SQUAT", "probability": 99}]},
    ]}
    client = _SetsClient(activities, sets)

    first, second = garmin.SyncReport(), garmin.SyncReport()
    garmin.import_strength_sets(session, owner.id, client, first)
    garmin.import_strength_sets(session, owner.id, client, second)

    assert first.metrics_saved == 1
    assert second.metrics_saved == 0, "The second pass must add nothing"
    assert second.skipped == 1


def test_a_new_parser_field_reaches_activities_already_imported(session, owner):
    """Adding a field to the parser must fill in what is already stored.

    The raw payload is kept once, and the import used to return early on a known
    activity — so zone minutes and training load stayed blank on every session imported
    before the field existed, and the weekly report showed nothing.
    """
    from sqlalchemy import select

    from app.integrations.garmin import sync as garmin
    from app.models.garmin import GarminKind, GarminMetric

    entry = {
        "activityId": 555, "activityName": "ClimbMill", "duration": 3600,
        "startTimeLocal": "2026-09-12 11:00:00", "calories": 650, "averageHR": 132,
        "activityType": {"typeKey": "indoor_cardio"},
        "hrTimeInZone_1": 300.0, "hrTimeInZone_2": 600.0, "hrTimeInZone_3": 2490.0,
        "activityTrainingLoad": 120.0,
    }

    first = garmin.SyncReport()
    garmin.import_activities(session, owner.id, [entry], first)
    second = garmin.SyncReport()
    garmin.import_activities(session, owner.id, [entry], second)

    metric = session.scalar(
        select(GarminMetric).where(GarminMetric.kind == GarminKind.ACTIVITY)
    )
    zones = (metric.details or {}).get("hr_zones")
    assert zones and zones[2] == 41.5, "Zone minutes must survive a second pass"
    assert (metric.details or {}).get("training_load") == 120.0


def test_a_jammed_gps_does_not_move_the_training_day(session, owner):
    """GPS jamming put a morning run in Lima, with a start time of 03:03.

    The watch takes its timezone from GPS, so a spoofed position rewrites the local
    clock. UTC survives that, and the day is computed from it — otherwise a late
    session would land on the wrong date and the diary would disagree with the diary.
    """
    from sqlalchemy import select

    from app.integrations.garmin import sync as garmin
    from app.models.garmin import GarminKind, GarminMetric

    entry = {
        "activityId": 999, "activityName": "Lima - Воскресенье", "duration": 2100,
        # Garmin's own local time, wrong by five hours because GPS was jammed
        "startTimeLocal": "2026-09-13 03:03:38",
        "startTimeGMT": "2026-09-13 08:03:38",
        "activityType": {"typeKey": "running"}, "distance": 19000.0, "calories": 400,
    }

    garmin.import_activities(session, owner.id, [entry], garmin.SyncReport())

    metric = session.scalar(
        select(GarminMetric).where(GarminMetric.kind == GarminKind.ACTIVITY)
    )
    assert metric.local_date == "2026-09-13", (
        "11:03 local time, whatever the watch believed about its own position"
    )


def test_a_session_records_the_template_it_ran(session, owner):
    """The activity carries the id of the workout that was executed. Without it a session
    shifted to another day cannot be recognised, because the name comes back truncated."""
    from sqlalchemy import select

    from app.domain import training
    from app.integrations.garmin import sync as garmin
    from app.models.training import WorkoutSession

    activities = [{
        "activityId": 901, "activityName": "Пятница — Фулбоди", "duration": 3400,
        "startTimeLocal": f"{_recent_day()} 18:00:00", "workoutId": 1000000003,
        "activityType": {"typeKey": "strength_training"},
    }]
    sets = {"exerciseSets": [
        {"setType": "ACTIVE", "duration": 60.0, "repetitionCount": 12, "weight": 10000.0,
         "exercises": [{"category": "LUNGE", "name": "DUMBBELL_REVERSE_LUNGE",
                        "probability": 99}]},
    ]}
    garmin.import_strength_sets(session, owner.id, _SetsClient(activities, sets),
                                garmin.SyncReport())

    workout = session.scalar(select(WorkoutSession).where(
        WorkoutSession.owner_id == owner.id))
    assert workout.garmin_workout_id == "1000000003"
    assert training.find_exercise(session, owner.id, "DUMBBELL_REVERSE_LUNGE") is not None


def test_a_corrected_set_reaches_the_plan(session, owner):
    """A session filled once was never re-read, so a set corrected on the watch after the
    first sync never landed. Harmless until next week's weight depended on it."""
    from sqlalchemy import select

    from app.integrations.garmin import sync as garmin
    from app.models.training import WorkoutSession

    day = _recent_day()
    activities = [{
        "activityId": 902, "activityName": "Фулбоди", "duration": 3600,
        "startTimeLocal": f"{day} 18:00:00",
        "activityType": {"typeKey": "strength_training"},
    }]
    one = {"exerciseSets": [
        {"setType": "ACTIVE", "duration": 60.0, "repetitionCount": 12, "weight": 25000.0,
         "exercises": [{"category": "SQUAT", "name": "BARBELL_BACK_SQUAT",
                        "probability": 99}]},
    ]}
    two = {"exerciseSets": one["exerciseSets"] + [
        {"setType": "ACTIVE", "duration": 60.0, "repetitionCount": 12, "weight": 25000.0,
         "exercises": [{"category": "SQUAT", "name": "BARBELL_BACK_SQUAT",
                        "probability": 99}]},
    ]}

    garmin.import_strength_sets(session, owner.id, _SetsClient(activities, one),
                                garmin.SyncReport())
    garmin.import_strength_sets(session, owner.id, _SetsClient(activities, two),
                                garmin.SyncReport())

    workout = session.scalar(select(WorkoutSession).where(
        WorkoutSession.owner_id == owner.id))
    assert len(workout.sets) == 2, "The corrected session is re-read, not skipped"
