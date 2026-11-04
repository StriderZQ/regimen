"""Sending reminders to Telegram.

Deliberately separate from the bot: the bot answers messages, this is the service taking
initiative. Sending goes through a direct API call so the background process need not
start a second bot instance and a second writer to the database.

An undelivered reminder does not count as sent: `last_sent_at` is set only after success,
otherwise the next pass stays quiet and the owner concludes there are no reminders at
all.
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.core.jobs import handler
from app.db import session_scope
from app.domain import reminders as reminders_domain
from app.models.identity import Principal
from app.models.ops import Job

log = logging.getLogger(__name__)

JOB_KIND = "reminder.send"


def send_message(text: str) -> bool:
    """Send to the owner. False means it did not go, which is not a job done."""
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_owner_chat_id:
        log.warning("Telegram not configured — reminder not sent: %s", text[:60])
        return False
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_owner_chat_id, "text": text},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        log.error("Reminder did not go out: %s", exc)
        return False
    if response.status_code >= 400:
        log.error("Telegram refused: %s %s", response.status_code, response.text[:200])
        return False
    return True


@handler(JOB_KIND)
def run_reminders(session, job: Job) -> None:
    owner = session.scalar(select(Principal))
    if owner is None:
        return
    sent = 0
    for item in reminders_domain.due(session, owner.id):
        if send_message(item.text):
            reminders_domain.mark_sent(session, item.reminder)
            sent += 1
    if sent:
        log.info("Reminders sent: %s", sent)
    job.payload = {**job.payload, "sent": sent}


def enqueue_due() -> None:
    """Queue a pass over the reminders. Deduplicated by quarter hour."""
    from app.core import jobs
    from app.models.base import utcnow

    with session_scope() as session:
        now = utcnow()
        slot = now.strftime("%Y-%m-%dT%H") + f"-{now.minute // 15}"
        jobs.enqueue(session, JOB_KIND, {}, dedupe_key=f"reminders-{slot}")


REPORT_KIND = "report.send"

# When each report goes out, local time
REPORT_TIMES = {
    "evening": (22, 0),
    "weekly": (22, 5),  # Sunday only, five minutes after the evening one
}

# The morning report waits for the night, not for a clock. Garmin publishes sleep, HRV
# and readiness only after he wakes and the watch syncs — so from 06:00 the scheduler
# looks every few minutes and sends the moment everything is in, whether that is seven
# in the morning or six in the evening on a day off. There is deliberately no deadline:
# a report with "нет данных" where the night should be is noise, and his instruction was
# to wait as long as it takes.
MORNING_WINDOW_FROM = (6, 0)

# When a silent morning stops being patience and starts being a fault worth naming. Not a
# deadline for the report — that still has none — but the hour past which "no data yet"
# deserves one line saying so, if the data is genuinely stale.
SILENCE_NOTICE_AT = (12, 0)
# Below this the watch simply has not synced yet, which is his business, not the service's
SILENCE_AFTER_HOURS = 6


@handler(REPORT_KIND)
def run_reports(session, job: Job) -> None:
    """Send whichever report is due and has not gone out today.

    Sent state lives in the job payload keyed by date, so a retry after a failed send
    does not produce a second copy, and a worker restart does not lose the day.
    """
    from app.domain import reports

    owner = session.scalar(select(Principal))
    if owner is None:
        return

    which = job.payload.get("which")
    builder = {"morning": reports.morning, "evening": reports.evening,
               "weekly": reports.weekly, "silence": reports.silence}.get(which)
    if builder is None:
        log.error("Unknown report %s", which)
        return

    report = builder(session, owner.id)
    if send_message(report.text):
        job.payload = {**job.payload, "sent": True, "missing": report.missing}
        log.info("Report %s sent, missing: %s", which, report.missing or "nothing")
    else:
        # Not marked sent: the next pass tries again. A report nobody received is not
        # a report, and silence here reads as "the day was fine".
        job.payload = {**job.payload, "sent": False}
        log.error("Report %s did not go out", which)



def _night_is_analysed(day: str) -> bool:
    """Whether Garmin has published everything the morning report is made of.

    All three are required — sleep, readiness and the daily summary that carries body
    battery and resting pulse. A report with "нет данных" where the night should be is
    not worth sending: the night is the half he reads it for.
    """
    from sqlalchemy import select

    from app.models import Principal
    from app.models.garmin import GarminKind, GarminMetric

    with session_scope() as session:
        owner = session.scalar(select(Principal))
        if owner is None:
            return False
        kinds = {GarminKind.SLEEP, GarminKind.READINESS, GarminKind.BODY}
        rows = {
            m.kind: m for m in session.scalars(
                select(GarminMetric).where(
                    GarminMetric.owner_id == owner.id,
                    GarminMetric.local_date == day,
                    GarminMetric.kind.in_(kinds),
                )
            ).all()
        }
        if not kinds.issubset(rows):
            return False
        # The presence of a row is not the presence of data, and this gate used to check
        # only the first. Garmin writes an empty record for the day the moment it is
        # asked, so all three kinds existed while every field inside them was null — and
        # the report went out saying "нет данных" against a Garmin Connect full of numbers.
        if rows[GarminKind.SLEEP].value is None:
            return False
        if rows[GarminKind.READINESS].value is None:
            return False
        summary = rows[GarminKind.BODY].details or {}
        return any(
            summary.get(field) is not None
            for field in ("resting_hr", "body_battery_high", "stress_avg")
        )


def morning_sent(day: str) -> bool:
    """Has today's morning report actually gone out?

    Used to decide how hard to poll Garmin. It reads the job's own record of delivery
    rather than a clock, because the report has no deadline: on a day he sleeps until
    the evening, the answer stays False and the polling stays frequent until it is True.
    """
    with session_scope() as session:
        job = session.scalar(
            select(Job).where(Job.dedupe_key == f"{REPORT_KIND}:morning:{day}")
        )
        return bool(job and job.payload.get("sent"))


def _silence_is_worth_naming(now) -> bool:
    """Past midday, with no morning report and Garmin data that is genuinely old.

    Both halves matter. Without the hour it would fire on every ordinary morning before
    the watch syncs; without the staleness check it would blame the service on a day he
    simply slept in. Only the intersection is a fault worth a message.
    """
    from app.models.garmin import GarminMetric

    notice_at = now.replace(hour=SILENCE_NOTICE_AT[0], minute=SILENCE_NOTICE_AT[1],
                            second=0, microsecond=0)
    if now < notice_at or morning_sent(now.date().isoformat()):
        return False

    with session_scope() as session:
        owner = session.scalar(select(Principal))
        if owner is None:
            return False
        freshest = session.scalar(
            select(GarminMetric)
            .where(GarminMetric.owner_id == owner.id)
            .order_by(GarminMetric.created_at.desc())
        )
    if freshest is None:
        return True
    from app.models.base import utcnow as _utcnow

    age_hours = (_utcnow() - freshest.created_at).total_seconds() / 3600
    return age_hours >= SILENCE_AFTER_HOURS


def _morning_is_due(now) -> bool:
    """After 06:00 local, and only once Garmin has published the whole night.

    No deadline: on a day he sleeps until evening the report simply arrives then. An
    incomplete one would be worse than a late one.
    """
    start = now.replace(hour=MORNING_WINDOW_FROM[0], minute=MORNING_WINDOW_FROM[1],
                        second=0, microsecond=0)
    if now < start:
        return False
    return _night_is_analysed(now.date().isoformat())


def enqueue_due_reports() -> None:
    """Queue the reports whose hour has come, once per day each.

    The worker ticks more often than this; the idempotency key is the date plus the
    report name, so ticking twice inside the same minute enqueues nothing extra.
    """
    from app.config import get_settings
    from app.core import jobs
    from app.models.base import utcnow

    now = utcnow().astimezone(get_settings().tz)

    if _morning_is_due(now):
        with session_scope() as session:
            jobs.enqueue(
                session,
                REPORT_KIND,
                {"which": "morning", "date": now.date().isoformat()},
                dedupe_key=f"{REPORT_KIND}:morning:{now.date().isoformat()}",
            )

    elif _silence_is_worth_naming(now):
        with session_scope() as session:
            jobs.enqueue(
                session,
                REPORT_KIND,
                {"which": "silence", "date": now.date().isoformat()},
                dedupe_key=f"{REPORT_KIND}:silence:{now.date().isoformat()}",
            )

    for which, (hour, minute) in REPORT_TIMES.items():
        if which == "weekly" and now.weekday() != 6:
            continue
        due_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < due_at:
            continue
        with session_scope() as session:
            jobs.enqueue(
                session,
                REPORT_KIND,
                {"which": which, "date": now.date().isoformat()},
                dedupe_key=f"{REPORT_KIND}:{which}:{now.date().isoformat()}",
            )
