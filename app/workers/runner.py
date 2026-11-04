"""The background process: job queue, schedule, database copies.

One process for all background work — a second writer to SQLite is unnecessary and
harmful. Inside, things are separated in time rather than across threads: jobs are short,
and the rare long ones (syncs) go through the queue with retries.

The schedule lives here rather than in cron: that way a job has deduplication, retries
and a visible failed state, instead of a silent line in a system log.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass

from app.config import get_settings
from app.core import jobs
from app.core.backup import DAILY, INTRADAY, MONTHLY, create_backup, prune
from app.db import session_scope

# Imported to register handlers: without it the queue does not know who does what
from app.integrations.garmin import jobs as garmin_jobs  # noqa: F401
from app.integrations.silpo import jobs as silpo_jobs  # noqa: F401
from app.models.base import utcnow
from app.workers import gym, notify

log = logging.getLogger(__name__)

TICK_SECONDS = 5


@dataclass
class Schedule:
    """Periodic work. Holds only the moment of the next run."""

    name: str
    every_seconds: float
    action: object
    next_run: float = 0.0

    def due(self, now: float) -> bool:
        return now >= self.next_run

    def mark(self, now: float) -> None:
        self.next_run = now + self.every_seconds


def _backup(kind: str = INTRADAY) -> None:
    settings = get_settings()
    result = create_backup(kind)
    keep = {
        INTRADAY: settings.backup_keep_intraday,
        DAILY: settings.backup_keep_daily,
        MONTHLY: settings.backup_keep_monthly,
    }[kind]
    removed = prune(kind, keep)
    log.info("Database copy (%s): %s, old ones removed: %s", kind, result.path.name, removed)
    if not result.path.exists():
        raise RuntimeError("The copy was not created — fix this immediately")


def _daily_backup() -> None:
    """A daily copy outlives the intraday ones and survives a week-old mistake."""
    _backup(DAILY)
    _copy_offsite()


def _copy_offsite() -> None:
    """Take a copy off the box. One host is one point of failure, and disks die whole."""
    settings = get_settings()
    latest = sorted(
        (settings.backup_dir / DAILY).glob("kitchen-*.db*"), reverse=True
    )
    if not latest:
        log.error("No off-box copy: no local daily copies were found")
        return
    if not settings.backup_offsite_command:
        log.warning(
            "THERE IS NO OFF-BOX COPY: backup_offsite_command is not set. "
            "While that holds, losing the disk means losing everything."
        )
        return

    command = settings.backup_offsite_command.replace("{path}", str(latest[0]))
    result = subprocess.run(  # noqa: S602 — the command is set by the owner in .env
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=600,
        # The rclone config sits on the data volume next to the service's other tokens:
        # the volume belongs to the container user, the host's home directory does not
        env={**os.environ, "RCLONE_CONFIG": str(settings.data_dir / "rclone.conf")},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"The off-box copy did not go ({result.returncode}): {result.stderr[:300]}"
        )
    log.info("Copy taken off the box: %s", latest[0].name)


def _enqueue_silpo() -> None:
    with session_scope() as session:
        # Deduplicated by day: two syncs a day are pointless, and the queue must not
        # accumulate identical jobs while the shop is unreachable
        jobs.enqueue(
            session,
            silpo_jobs.JOB_KIND,
            {"months_back": 1},
            dedupe_key=f"silpo-{utcnow().date().isoformat()}",
        )


def _enqueue_garmin() -> None:
    """Ask Garmin for new data — every half hour, and every five while the night is awaited.

    The night is published when he wakes and the watch syncs, and the morning report
    waits for it. Half-hourly polling would make that report up to thirty minutes late
    for no reason, so from 06:00 local the interval drops to five minutes and stays there
    until the report has actually been delivered — not until some hour of the morning.
    The report has no deadline, and neither has the polling that feeds it: on a day he
    sleeps until the evening, both simply wait.
    """
    from app.config import get_settings

    now = utcnow()
    local = now.astimezone(get_settings().tz)
    waiting_for_the_night = (
        local.hour >= notify.MORNING_WINDOW_FROM[0]
        and not notify.morning_sent(local.date().isoformat())
    )
    if waiting_for_the_night:
        slot = local.strftime("%Y-%m-%dT%H-") + f"{local.minute // 5:02d}"
    else:
        slot = now.strftime("%Y-%m-%dT%H") + ("-30" if now.minute >= 30 else "-00")
    with session_scope() as session:
        jobs.enqueue(
            session, garmin_jobs.JOB_KIND, {"days_back": 3}, dedupe_key=f"garmin-{slot}"
        )


def build_schedule() -> list[Schedule]:
    settings = get_settings()
    return [
        Schedule("database copy", settings.backup_interval_minutes * 60, _backup),
        Schedule("daily copy", 24 * 3600, _daily_backup),
        Schedule("grocery receipts", 6 * 3600, _enqueue_silpo),
        # Ticked every five minutes; the job itself decides how often to actually ask
        Schedule("Garmin", 5 * 60, _enqueue_garmin),
        Schedule("reminders", 15 * 60, notify.enqueue_due),
        # Checked every five minutes rather than scheduled at the hour: a worker
        # restarted at 9:03 must still send the morning report, not skip the day.
        Schedule("reports", 3 * 60, notify.enqueue_due_reports),
        # Same three minutes and the same reason: a worker restarted at 23:14 must still
        # write the week, not skip the night.
        Schedule("gym plan", 3 * 60, gym.enqueue_due_plan),
    ]


def run(stop_after: int | None = None) -> None:
    """The main loop. `stop_after` is for tests; in production it runs forever."""
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # The Telegram request URL contains the token: it does not belong in a log
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    schedule = build_schedule()
    running = {"value": True}

    def _stop(_sig, _frame) -> None:
        log.info("Stop signal received — finishing the current job")
        running["value"] = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    ticks = 0
    log.info("Worker started: %s", ", ".join(s.name for s in schedule)),
    while running["value"]:
        now = time.monotonic()
        for item in schedule:
            if not item.due(now):
                continue
            try:
                item.action()
            except Exception:  # noqa: BLE001 — one failing job must not kill the worker
                log.exception("Job '%s' did not complete", item.name)
            finally:
                item.mark(time.monotonic())

        # The queue is polled more often than the schedule: Telegram messages must not wait
        try:
            while jobs.run_once():
                pass
        except Exception:  # noqa: BLE001
            log.exception("The job queue broke on this pass")

        ticks += 1
        if stop_after is not None and ticks >= stop_after:
            break
        time.sleep(TICK_SECONDS)

    log.info("Worker stopped")


if __name__ == "__main__":
    run()
