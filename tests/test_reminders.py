"""Supplement and reminder tests.

The most important test here is about medication: their names never appear on the server.
That is the owner's decision, enforced by code rather than by an agent's care.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from app.domain import reminders
from app.domain.reminders import MEDICATION_LABEL, ReminderError
from app.models import Principal
from app.models.base import utcnow
from app.models.policies import Supplement


def test_sport_supplement_is_named(session: Session, owner: Principal):
    item = reminders.add_supplement(
        session, owner.id, name="Креатин моногидрат", schedule="утро", note="5 г"
    )
    assert item.name == "Креатин моногидрат"
    assert item.note == "5 г"


def test_medication_name_never_reaches_the_database(session: Session, owner: Principal):
    """A drug name reveals more than the owner keeps on the server."""
    hidden_name = "Препарат-Тест 10 мг"
    reminders.add_supplement(
        session, owner.id, name=hidden_name, is_medication=True,
        note="назначение врача, показатели крови",
    )
    stored = session.query(Supplement).one()

    assert stored.name == MEDICATION_LABEL
    assert stored.note is None
    assert hidden_name.lower() not in stored.name.lower()


def test_medication_label_is_neutral_in_listing(session: Session, owner: Principal):
    reminders.add_supplement(session, owner.id, name="что-то", is_medication=True)
    names = [s.name for s in reminders.supplements(session, owner.id)]
    assert names == [MEDICATION_LABEL]


# --- schedules ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("schedule", "valid"),
    [("08:30", True), ("08:30 пн,ср,пт", True), ("25:00", False),
     ("каждый день", False), ("08:30 юп", False)],
)
def test_schedule_syntax(session, owner, schedule, valid):
    if valid:
        assert reminders.add_reminder(
            session, owner.id, kind=f"k-{schedule}", template="текст", schedule=schedule
        )
    else:
        with pytest.raises(ReminderError, match="Could not read the schedule"):
            reminders.add_reminder(
                session, owner.id, kind="k", template="текст", schedule=schedule
            )


def test_due_respects_time_of_day(session: Session, owner: Principal, settings):
    reminders.add_reminder(
        session, owner.id, kind="creatine", template="Креатин", schedule="20:00"
    )
    morning = dt.datetime(2026, 9, 14, 6, 0, tzinfo=dt.UTC)
    evening = dt.datetime(2026, 9, 14, 18, 30, tzinfo=dt.UTC)

    assert reminders.due(session, owner.id, now=morning) == []
    assert [d.reminder.kind for d in reminders.due(session, owner.id, now=evening)] == ["creatine"]


def test_due_respects_weekdays(session: Session, owner: Principal):
    reminders.add_reminder(
        session, owner.id, kind="gym", template="Зал", schedule="09:00 пн,ср,пт"
    )
    tuesday = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)
    monday = dt.datetime(2026, 9, 14, 12, 0, tzinfo=dt.UTC)

    assert reminders.due(session, owner.id, now=tuesday) == []
    assert reminders.due(session, owner.id, now=monday)


def test_disabled_reminder_is_silent(session: Session, owner: Principal):
    """A reminder that cannot be switched off starts being ignored entirely."""
    reminders.add_reminder(session, owner.id, kind="water", template="Вода")
    reminders.set_enabled(session, owner.id, "water", False)
    assert reminders.due(session, owner.id) == []

    reminders.set_enabled(session, owner.id, "water", True)
    assert reminders.due(session, owner.id)


def test_snooze_is_not_a_switch_off(session: Session, owner: Principal):
    reminders.add_reminder(session, owner.id, kind="water", template="Вода")
    item = reminders.snooze(session, owner.id, "water", minutes=30)

    assert item.enabled is True
    assert reminders.due(session, owner.id) == []
    later = utcnow() + dt.timedelta(minutes=31)
    assert reminders.due(session, owner.id, now=later)


def test_acknowledged_reminder_is_silent_until_next_day(session: Session, owner: Principal):
    reminders.add_reminder(session, owner.id, kind="creatine", template="Креатин")
    reminders.acknowledge(session, owner.id, "creatine")

    assert reminders.due(session, owner.id) == []
    tomorrow = utcnow() + dt.timedelta(days=1)
    assert reminders.due(session, owner.id, now=tomorrow)


def test_unknown_reminder_says_so(session: Session, owner: Principal):
    with pytest.raises(ReminderError, match="There is no"):
        reminders.acknowledge(session, owner.id, "выдуманное")


def test_failed_delivery_is_not_marked_as_sent(session: Session, owner: Principal, monkeypatch):
    """An undelivered reminder does not count as sent — otherwise it disappears."""
    from app.models.ops import Job
    from app.workers import notify

    reminders.add_reminder(session, owner.id, kind="water", template="Вода")
    monkeypatch.setattr(notify, "send_message", lambda _text: False)

    job = Job(kind=notify.JOB_KIND, payload={}, run_after=utcnow())
    session.add(job)
    session.flush()
    notify.run_reminders(session, job)

    assert job.payload["sent"] == 0
    assert reminders.due(session, owner.id)  # всё ещё ждёт отправки


def test_successful_delivery_marks_it_sent(session: Session, owner: Principal, monkeypatch):
    from app.models.ops import Job
    from app.workers import notify

    reminders.add_reminder(session, owner.id, kind="water", template="Вода")
    monkeypatch.setattr(notify, "send_message", lambda _text: True)

    job = Job(kind=notify.JOB_KIND, payload={}, run_after=utcnow())
    session.add(job)
    session.flush()
    notify.run_reminders(session, job)

    assert job.payload["sent"] == 1
    assert reminders.due(session, owner.id) == []
