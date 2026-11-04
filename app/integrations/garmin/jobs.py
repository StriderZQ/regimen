"""The scheduled Garmin sync, and the alert for when the token dies.

About the alert: it goes to a channel independent of Telegram. If the bot breaks, a
message saying "Garmin disconnected" cannot arrive through the bot — and that is exactly
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.jobs import handler
from app.models.identity import Principal
from app.models.ops import Job

log = logging.getLogger(__name__)

JOB_KIND = "garmin.sync"


def alert(text: str) -> None:
    """Report a failure away from Telegram. With no channel, at least log it loudly."""
    settings = get_settings()
    if not settings.alert_webhook_url:
        log.error("ALERT (no channel configured): %s", text)
        return
    try:
        httpx.post(settings.alert_webhook_url, json={"text": text}, timeout=10.0)
    except httpx.HTTPError as exc:
        log.error("ALERT did not go out (%s): %s", exc, text)


@handler(JOB_KIND)
def run_sync(session: Session, job: Job) -> None:
    from app.integrations.garmin.sync import GarminAuthError, connect, freshness, sync

    owner = session.scalar(select(Principal))
    if owner is None:
        raise RuntimeError("No owner exists — there is nobody to sync for")

    try:
        client = connect()
    except GarminAuthError as exc:
        # Retrying is pointless: nothing changes until a human logs in again
        alert(f"Garmin: access lost. {exc}")
        job.payload = {**job.payload, "needs_relogin": str(exc)}
        return

    report = sync(session, owner.id, client, days_back=job.payload.get("days_back", 3))
    job.payload = {**job.payload, "result": report.summary()}
    log.info("Garmin: %s", report.summary())

    if report.errors:
        alert("Garmin: " + "; ".join(report.errors[:3]))

    stale = [f.kind.value for f in freshness(session, owner.id) if f.stale]
    if stale:
        log.warning("Garmin: data is stale for: %s", ", ".join(stale))
