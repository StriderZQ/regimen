"""Supplements and reminders.

Two rules here matter more than the mechanics.

**Medication is stored without a name.** Medical data never reaches the server — the
owner's decision, enforced by code rather than by promise: the domain substitutes a
neutral label on write. Even if a name arrives in the call it never reaches the database,
and never appears in the chat. Sports supplements are named; they are not medication.

**Everything can be switched off.** A reminder that cannot be silenced gets ignored
wholesale within a week — together with the ones that mattered.

The schedule uses a minimal syntax instead of cron: `HH:MM` daily, or `HH:MM` with
weekday abbreviations. Nobody here reads cron, and it is easy to get wrong.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.base import utcnow
from app.models.policies import Reminder, Supplement

MEDICATION_LABEL = "scheduled dose"

# Both spellings are accepted: existing schedules are stored as text and must keep
# parsing after the code was translated.
WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6,
}


class ReminderError(Exception):
    """An error meant to be shown in words."""


@dataclass
class Due:
    reminder: Reminder
    text: str


# --- supplements ---------------------------------------------------------------------

def add_supplement(
    session: Session,
    owner_id: str,
    *,
    name: str,
    is_medication: bool = False,
    schedule: str | None = None,
    note: str | None = None,
) -> Supplement:
    """Add a supplement. For medication the name is NOT stored — only the label."""
    supplement = Supplement(
        owner_id=owner_id,
        name=MEDICATION_LABEL if is_medication else name.strip(),
        is_medication=is_medication,
        schedule=schedule,
        # A note on medication is not stored either: it usually contains exactly what
        # the owner keeps on his own machine
        note=None if is_medication else note,
    )
    session.add(supplement)
    session.flush()
    return supplement


def supplements(session: Session, owner_id: str) -> list[Supplement]:
    return list(
        session.scalars(
            select(Supplement).where(Supplement.owner_id == owner_id).order_by(Supplement.name)
        )
    )


# --- reminders ------------------------------------------------------------------------

def add_reminder(
    session: Session,
    owner_id: str,
    *,
    kind: str,
    template: str,
    schedule: str | None = None,
    enabled: bool = True,
) -> Reminder:
    if schedule and not _parse_schedule(schedule):
        raise ReminderError(
            f"Could not read the schedule '{schedule}'. Use 'HH:MM' or 'HH:MM mon,wed,fri'."
        )
    reminder = Reminder(
        owner_id=owner_id, kind=kind, cron=schedule, template=template, enabled=enabled
    )
    session.add(reminder)
    session.flush()
    return reminder


def set_enabled(session: Session, owner_id: str, kind: str, enabled: bool) -> Reminder:
    reminder = session.scalar(
        select(Reminder).where(Reminder.owner_id == owner_id, Reminder.kind == kind)
    )
    if reminder is None:
        raise ReminderError(f"There is no '{kind}' reminder")
    reminder.enabled = enabled
    session.flush()
    return reminder


def snooze(session: Session, owner_id: str, kind: str, *, minutes: int = 60) -> Reminder:
    """Snooze. Does not switch it off — it simply stays quiet until the given time."""
    reminder = session.scalar(
        select(Reminder).where(Reminder.owner_id == owner_id, Reminder.kind == kind)
    )
    if reminder is None:
        raise ReminderError(f"There is no '{kind}' reminder")
    reminder.snoozed_until = utcnow() + dt.timedelta(minutes=minutes)
    session.flush()
    return reminder


def acknowledge(session: Session, owner_id: str, kind: str) -> Reminder:
    """"Taken" / "done": the reminder goes quiet until its next occurrence."""
    reminder = session.scalar(
        select(Reminder).where(Reminder.owner_id == owner_id, Reminder.kind == kind)
    )
    if reminder is None:
        raise ReminderError(f"There is no '{kind}' reminder")
    reminder.last_sent_at = utcnow()
    reminder.snoozed_until = None
    session.flush()
    return reminder


def _parse_schedule(schedule: str) -> tuple[dt.time, set[int]] | None:
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(.*)$", schedule)
    if not match:
        return None
    hour, minute, rest = int(match.group(1)), int(match.group(2)), match.group(3).strip()
    if hour > 23 or minute > 59:
        return None
    if not rest:
        return dt.time(hour, minute), set(range(7))
    days = {WEEKDAYS[d.strip()] for d in rest.split(",") if d.strip() in WEEKDAYS}
    return (dt.time(hour, minute), days) if days else None


def due(session: Session, owner_id: str, *, now: dt.datetime | None = None) -> list[Due]:
    """Which reminders are due. Switched-off and snoozed ones stay silent."""
    now = now or utcnow()
    local = now.astimezone(get_settings().tz)
    result: list[Due] = []

    for reminder in session.scalars(
        select(Reminder).where(Reminder.owner_id == owner_id, Reminder.enabled.is_(True))
    ):
        if reminder.snoozed_until is not None and reminder.snoozed_until > now:
            continue
        if reminder.cron:
            parsed = _parse_schedule(reminder.cron)
            if parsed is None:
                continue
            at_time, days = parsed
            if local.weekday() not in days:
                continue
            if local.time() < at_time:
                continue
        if reminder.last_sent_at is not None:
            sent_local = reminder.last_sent_at.astimezone(get_settings().tz)
            if sent_local.date() == local.date():
                continue
        result.append(Due(reminder=reminder, text=reminder.template))
    return result


def mark_sent(session: Session, reminder: Reminder, *, at: dt.datetime | None = None) -> None:
    reminder.last_sent_at = at or utcnow()
    session.flush()
