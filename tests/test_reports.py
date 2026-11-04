"""The reports that arrive in Telegram without being asked for.

What matters here is not prose but that a missing figure is visible as missing, and that
every recommendation carries the number it rests on.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from app.config import get_settings
from app.domain import advice, body, reports
from app.models import Principal
from app.models.base import utcnow
from app.models.garmin import GarminKind, GarminMetric


def _today() -> str:
    return utcnow().astimezone(get_settings().tz).date().isoformat()


def _readiness(session, owner, value, day, level="LOW"):
    session.add(GarminMetric(
        owner_id=owner.id, kind=GarminKind.READINESS, external_id=f"r-{day}",
        local_date=day, fetched_at=utcnow(), value=value,
        details={"level": level, "recovery_factor": "POOR"},
    ))
    session.flush()


def test_missing_data_is_shown_as_missing(session: Session, owner: Principal):
    """An empty line reads as "all fine" — the report must say there is no data."""
    report = reports.morning(session, owner.id)

    assert "нет данных" in report.text
    assert "readiness" in report.missing and "sleep" in report.missing


def test_no_advice_is_given_on_yesterdays_numbers(session: Session, owner: Principal):
    """Advising from stale figures is worse than staying quiet, and it must say why."""
    yesterday = (dt.date.fromisoformat(_today()) - dt.timedelta(days=1)).isoformat()
    _readiness(session, owner, 38, yesterday)

    lines = advice.morning(session, owner.id, today=dt.date.fromisoformat(_today()))

    assert any("не синхронизировался" in line for line in lines)


def test_low_readiness_names_the_figure_it_rests_on(session: Session, owner: Principal):
    """Not "you recovered badly" but the number, so he can check it."""
    today = _today()
    _readiness(session, owner, 38, today)
    session.add(GarminMetric(
        owner_id=owner.id, kind=GarminKind.SLEEP, external_id="s1", local_date=today,
        fetched_at=utcnow(), value=440.0, details={"score": 83},
    ))
    session.flush()

    lines = advice.morning(session, owner.id, today=dt.date.fromisoformat(today))

    assert lines, "Readiness of 38 must produce a recommendation"
    assert any("38" in line for line in lines), "Every line carries its figure"
    assert all(len(line) <= 48 for line in lines), "Lines must fit a phone screen"


def test_protein_shortfall_is_counted_in_scoops(session: Session, owner: Principal):
    """\"Drink protein\" is not advice; \"one scoop, 24 g\" is."""
    from app.domain import diary, rules
    from app.models.policies import PolicyKind

    rules.set_policy(session, owner.id, kind=PolicyKind.MACROS,
                     value={"target_protein_g": [160, 170]})
    diary.log_meal(session, owner.id, portions=[
        diary.Portion(name="Курица", grams=500, kcal=550, protein_g=130),
    ])
    totals = diary.day_totals(session, owner.id)

    lines = advice.evening(session, owner.id, today=dt.date.fromisoformat(_today()),
                           totals=totals)

    assert any("скупа" in line or "скуп" in line for line in lines)
    assert any("30" in line for line in lines), "The gap itself must be stated"


def test_the_tape_is_asked_for_every_fortnight(session: Session, owner: Principal):
    """A month between measurements let three kilograms of lean mass vanish unnoticed."""
    old = (dt.date.fromisoformat(_today()) - dt.timedelta(days=20)).isoformat()
    body.record(session, owner.id, measured_on=old, values={"вес": 86, "живот": 96})

    due = reports.measurement_due(session, owner.id)

    assert due and any("просрочен на 6" in line for line in due)


def test_a_fresh_measurement_stops_the_reminder(session: Session, owner: Principal):
    """Once measured, it must go quiet — a reminder that repeats regardless is ignored."""
    body.record(session, owner.id, measured_on=_today(), values={"вес": 85, "живот": 95})

    assert reports.measurement_due(session, owner.id) == []


# --- when the morning report goes out --------------------------------------------------


def _at(hour: int, minute: int = 0):
    """A local-time moment of today, for deciding whether the morning report is due."""
    import datetime as dt

    tz = get_settings().tz
    return dt.datetime.combine(
        utcnow().astimezone(tz).date(), dt.time(hour, minute), tzinfo=tz
    )


def test_the_morning_report_waits_for_the_night_to_be_published(session, owner):
    """Garmin publishes sleep only after he wakes; a fixed 09:00 often had nothing to say."""
    from app.workers import notify

    assert notify._morning_is_due(_at(7, 30)) is False, "Before the window — never"
    assert notify._morning_is_due(_at(8, 30)) is False, "In the window, night not in yet"


def _publish_the_night(session, owner, *, filled: bool) -> None:
    """Write the three records the morning report is made of.

    `filled=False` reproduces what Garmin actually returns while he is still asleep: all
    three records exist, and every field inside them is null.
    """
    today = utcnow().astimezone(get_settings().tz).date().isoformat()
    summary = {"resting_hr": 56, "body_battery_high": 85, "stress_avg": 24}
    for kind in (GarminKind.SLEEP, GarminKind.READINESS, GarminKind.BODY):
        session.add(GarminMetric(
            owner_id=owner.id, kind=kind, external_id=f"{kind.value}-{today}",
            local_date=today, fetched_at=utcnow(),
            value=38.0 if filled else None,
            details=(summary if kind is GarminKind.BODY else {}) if filled else {},
        ))
    session.commit()


def test_it_goes_as_soon_as_the_whole_night_arrives(session, owner):
    """He wakes, the watch syncs, and the report follows within minutes."""
    from app.workers import notify

    _publish_the_night(session, owner, filled=True)

    assert notify._morning_is_due(_at(8, 20)) is True


def test_empty_records_do_not_count_as_a_published_night(session, owner):
    """Three rows full of nulls once passed for a complete night.

    Garmin creates the day's records the moment it is asked, before there is anything in
    them. The gate checked that the rows existed and let the report through; it went out
    reading "СОН нет данных" while Garmin Connect showed the whole night. His rule was
    that an incomplete report is not sent at all, and this is what broke it.
    """
    from app.workers import notify

    _publish_the_night(session, owner, filled=False)

    assert notify._morning_is_due(_at(8, 20)) is False


def test_a_report_with_blanks_is_never_sent(session, owner):
    """"Готовность 61, сон — нет данных" is noise: the night is what the report is read for."""
    from app.workers import notify

    today = utcnow().astimezone(get_settings().tz).date().isoformat()
    session.add(GarminMetric(
        owner_id=owner.id, kind=GarminKind.READINESS, external_id=f"r-{today}",
        local_date=today, fetched_at=utcnow(), value=61.0, details={"level": "MODERATE"},
    ))
    session.commit()

    assert notify._morning_is_due(_at(9, 0)) is False, (
        "Readiness without sleep is not a morning report"
    )


def test_it_waits_however_long_the_night_takes(session, owner):
    """On a day he sleeps until evening the report arrives then — there is no deadline.

    An incomplete report is worse than a late one, so nothing goes until the night is in.
    """
    from app.workers import notify

    assert notify._morning_is_due(_at(12, 5)) is False
    assert notify._morning_is_due(_at(18, 30)) is False


def test_silence_names_the_fault_instead_of_staying_quiet(session, owner):
    """A morning with no report must not look the same as a morning with nothing to say."""
    from app.domain import reports
    from app.models.base import utcnow
    from app.models.garmin import GarminKind, GarminMetric

    session.add(
        GarminMetric(
            owner_id=owner.id, kind=GarminKind.SLEEP, external_id="stale-night",
            local_date="2026-09-10", value=420.0, fetched_at=utcnow(), details={},
        )
    )
    session.flush()

    report = reports.silence(session, owner.id)
    assert "2026-09-10" in report.text, "it must say how old the data actually is"
    assert "Синхронизируй" in report.text
    assert report.missing == ["morning"]


def test_silence_says_so_when_there_is_no_garmin_data_at_all(session, owner):
    from app.domain import reports

    report = reports.silence(session, owner.id)
    assert "нет вообще" in report.text


def test_a_broken_recovery_clock_does_not_become_an_order_to_rest(session, owner):
    """Readiness low, every physiological part fine — say which one is wrong.

    A jammed GPS can turn an easy 37-minute run into 19 km on the watch, and the
    recovery clock jumped to 95 hours. Readiness fell to 11 while the HRV factor never
    moved off 100. The advice read the score alone and told him to rest, one evening
    after he had been told the number was the artefact.
    """

    from app.domain import advice

    today = utcnow().astimezone(get_settings().tz).date()
    session.add(GarminMetric(
        owner_id=owner.id, kind=GarminKind.READINESS, external_id="r",
        local_date=today.isoformat(), fetched_at=utcnow(), value=11.0,
        details={"level": "POOR", "hrv_pct": 100, "stress_pct": 100,
                 "recovery_pct": 10, "recovery_time_min": 5720},
    ))
    session.add(GarminMetric(
        owner_id=owner.id, kind=GarminKind.SLEEP, external_id="s",
        local_date=today.isoformat(), fetched_at=utcnow(), value=430.0, details={},
    ))
    session.flush()

    lines = " ".join(advice.morning(session, owner.id, today=today))
    assert "отдых" not in lines, "a counter is low, not the man"
    assert "95" in lines and "100" in lines, "name the hours and the HRV factor"


def test_a_genuinely_bad_morning_still_says_rest(session, owner):
    """The escape hatch must not swallow a real collapse."""
    from app.domain import advice

    today = utcnow().astimezone(get_settings().tz).date()
    session.add(GarminMetric(
        owner_id=owner.id, kind=GarminKind.READINESS, external_id="r",
        local_date=today.isoformat(), fetched_at=utcnow(), value=18.0,
        details={"level": "POOR", "hrv_pct": 30, "stress_pct": 40,
                 "recovery_pct": 20, "recovery_time_min": 3000},
    ))
    session.flush()

    lines = " ".join(advice.morning(session, owner.id, today=today))
    assert "отдых" in lines
