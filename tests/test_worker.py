"""Background process tests: the schedule, resilience, database copies."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.workers import runner


def test_schedule_covers_the_promises(settings):
    """The owner's promise: lose no more than 15 minutes. It must be in the schedule."""
    names = {s.name: s.every_seconds for s in runner.build_schedule()}
    assert names["database copy"] == settings.backup_interval_minutes * 60
    assert names["daily copy"] == 24 * 3600
    assert "grocery receipts" in names


def test_due_and_mark(monkeypatch):
    item = runner.Schedule("test", every_seconds=10, action=lambda: None)
    assert item.due(0.0)
    item.mark(100.0)
    assert not item.due(105.0)
    assert item.due(110.0)


def test_one_broken_task_does_not_stop_the_worker(monkeypatch):
    """One job failing has no right to bring the background process down."""
    calls: list[str] = []

    def boom() -> None:
        calls.append("boom")
        raise RuntimeError("broke")

    def fine() -> None:
        calls.append("fine")

    monkeypatch.setattr(
        runner,
        "build_schedule",
        lambda: [
            runner.Schedule("bad", 0.01, boom),
            runner.Schedule("good", 0.01, fine),
        ],
    )
    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
    monkeypatch.setattr(runner.jobs, "run_once", lambda: False)

    runner.run(stop_after=2)

    assert "boom" in calls
    assert "fine" in calls


def test_backup_failure_is_loud(monkeypatch, tmp_path):
    """A copy that silently failed is the worst kind of breakage: fix it at once."""
    class _Result:
        path = tmp_path / "missing.db.gz"

    monkeypatch.setattr(runner, "create_backup", lambda kind=runner.INTRADAY: _Result())
    monkeypatch.setattr(runner, "prune", lambda kind, keep: 0)

    with pytest.raises(RuntimeError, match="fix this immediately"):
        runner._backup()


def test_backup_writes_and_prunes(monkeypatch):
    seen: dict = {}

    def _prune(kind, keep):
        seen["kind"], seen["keep"] = kind, keep
        return 3

    monkeypatch.setattr(runner, "prune", _prune)
    runner._backup(runner.DAILY)

    assert seen["kind"] == runner.DAILY
    assert seen["keep"] > 0


def test_silpo_sync_is_deduplicated_per_day(session):
    """While the shop is unreachable, the queue must not accumulate identical jobs."""
    from app.models.ops import Job

    runner._enqueue_silpo()
    runner._enqueue_silpo()

    assert session.query(Job).filter(Job.kind == "silpo.sync").count() == 1


def test_missing_offsite_copy_is_said_out_loud(monkeypatch, caplog):
    """No second copy means someone must know. Silence is worse than the absence."""
    import logging

    monkeypatch.setattr(runner.get_settings(), "backup_offsite_command", None, raising=False)
    with caplog.at_level(logging.WARNING):
        runner._copy_offsite()
    assert any("NO OFF-BOX COPY" in r.message for r in caplog.records)


def test_offsite_failure_raises(monkeypatch, tmp_path):
    """A failed upload is an error, not a shrug."""
    import subprocess

    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "kitchen-20260912T000000Z.db.gz").write_bytes(b"x")

    settings = runner.get_settings()
    monkeypatch.setattr(settings, "backup_dir", tmp_path, raising=False)
    monkeypatch.setattr(settings, "backup_offsite_command", "false {path}", raising=False)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "access denied"),
    )
    with pytest.raises(RuntimeError, match="did not go"):
        runner._copy_offsite()


def test_offsite_upload_uses_the_mounted_rclone_config(monkeypatch, tmp_path):
    """The rclone config lives outside the image: without a path the upload looks elsewhere."""
    import subprocess

    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "kitchen-20260912T000000Z.db.gz").write_bytes(b"x")

    settings = runner.get_settings()
    monkeypatch.setattr(settings, "backup_dir", tmp_path, raising=False)
    monkeypatch.setattr(
        settings, "backup_offsite_command", "rclone copy {path} gdrive:backups",
        raising=False,
    )

    seen = {}

    def _run(command, **kwargs):
        seen["command"] = command
        seen["config"] = kwargs.get("env", {}).get("RCLONE_CONFIG")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _run)
    runner._copy_offsite()

    assert "kitchen-20260912T000000Z.db.gz" in seen["command"]
    assert seen["config"].endswith("rclone.conf")
    assert str(settings.data_dir) in seen["config"]


def test_garmin_polling_survives_every_hour_of_the_day(session, monkeypatch):
    """The Garmin schedule must not raise at any local hour.

    It did, and quietly: a constant it referenced had been removed, and the chained
    comparison hid it until the local hour reached six. From then on every tick raised,
    no data was fetched, and the morning report — which waits for that data — never
    fired. Two mornings passed before anyone noticed, because a background job that
    fails only logs.
    """

    from app.models.base import utcnow
    from app.models.ops import Job

    real_utcnow = utcnow
    for hour in range(24):
        moment = real_utcnow().replace(hour=hour, minute=7, second=0, microsecond=0)
        monkeypatch.setattr(runner, "utcnow", lambda m=moment: m)
        runner._enqueue_garmin()

    # Every hour queued something, and identical slots collapsed rather than piling up
    assert session.query(Job).filter(Job.kind == "garmin.sync").count() > 0


def test_garmin_is_polled_often_until_the_morning_report_has_gone(session, monkeypatch):
    """Five-minute polling stops when the report is delivered, not at a fixed hour.

    He may sleep until the evening; the report waits for the night's data with no
    deadline, so the polling that feeds it waits too.
    """
    import datetime as dt

    from app.config import get_settings
    from app.models.ops import Job
    from app.workers import notify

    local = dt.datetime(2026, 9, 14, 9, 7, tzinfo=get_settings().tz)
    monkeypatch.setattr(runner, "utcnow", lambda: local.astimezone(dt.UTC))

    monkeypatch.setattr(notify, "morning_sent", lambda _day: False)
    runner._enqueue_garmin()
    waiting = {j.dedupe_key for j in session.query(Job).filter(Job.kind == "garmin.sync")}

    monkeypatch.setattr(notify, "morning_sent", lambda _day: True)
    runner._enqueue_garmin()
    after = {j.dedupe_key for j in session.query(Job).filter(Job.kind == "garmin.sync")}

    assert len(after - waiting) == 1, "a different slot means a different cadence"
    assert any(key.endswith("-01") for key in waiting), "five-minute slot while waiting"
    assert any(key.endswith("-00") for key in after - waiting), "half-hourly once sent"


def test_the_gym_plan_is_in_the_schedule():
    """A promise that only holds if something actually ticks it."""
    names = {s.name: s.every_seconds for s in runner.build_schedule()}
    assert names["gym plan"] == 3 * 60


def test_the_gym_plan_fires_once_a_night_and_not_before_its_hour(session, monkeypatch):
    """Checked every few minutes rather than scheduled on the minute, so a worker
    restarted after the hour still writes the week. The date key stops it writing twice."""
    from app.models.base import utcnow
    from app.models.ops import Job
    from app.workers import gym

    real_utcnow = utcnow
    base = real_utcnow().astimezone(get_settings().tz)

    def at(hour, minute):
        moment = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        monkeypatch.setattr(gym, "utcnow", lambda m=moment: m, raising=False)
        import app.models.base as base_mod

        monkeypatch.setattr(base_mod, "utcnow", lambda m=moment: m)
        gym.enqueue_due_plan()

    hour, minute = gym.PLAN_AT
    at(hour, minute - 1)
    assert session.query(Job).filter(Job.kind == gym.JOB_KIND).count() == 0

    at(hour, minute)
    at(hour, minute + 13)
    assert session.query(Job).filter(Job.kind == gym.JOB_KIND).count() == 1
