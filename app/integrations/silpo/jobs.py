"""The receipt sync as a background job.

Not an agent tool: an agent must not be able to "go to the shop for history". The job runs
on a schedule, survives timeouts through the queue, and reports its failures to the alert
channel rather than the chat — if the bot breaks, you must hear about it another way.

Unknown lines do not disappear: they raise one question for the owner per run, with a
list. The answer becomes an `item_alias` and is never asked about again.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.jobs import handler
from app.models.identity import Principal
from app.models.ops import Job

log = logging.getLogger(__name__)

JOB_KIND = "silpo.sync"


@handler(JOB_KIND)
def run_sync(session: Session, job: Job) -> None:
    """Fetch receipts and create batches. A missing token is work for a human, not a bug."""
    from app.integrations.silpo.client import SilpoAuthError, SilpoClient
    from app.integrations.silpo.importer import sync

    owner = session.scalar(select(Principal))
    if owner is None:
        raise RuntimeError("No owner exists — there is nobody to sync for")

    try:
        client = SilpoClient()
    except SilpoAuthError as exc:
        # Nothing to retry: until a human authorises, the result will not change
        log.warning("Groceries: %s", exc)
        job.payload = {**job.payload, "needs_authorisation": str(exc)}
        return

    with client:
        report = sync(session, owner.id, client, months_back=job.payload.get("months_back", 1))

    log.info("Groceries: %s", report.summary())
    job.payload = {**job.payload, "result": report.summary()}

    if report.pending:
        _ask_about_unknown(session, owner, report.pending)


def _ask_about_unknown(session: Session, owner: Principal, names: list[str]) -> None:
    """Report unknown receipt lines.

    NOT through a pending action: "what product is this?" is a question, not a change
    of state, and there is nothing to confirm. The job used to try creating an action
    of a kind that was never in the allowed list, and died on the first unknown line.
    died on the first unknown line.

    The lines already sit in the `pending` status: the agent sees them through a tool
    and asks when the owner is around.
    """
    unique = list(dict.fromkeys(names))[:20]
    log.info("Groceries: %s lines await an answer: %s", len(unique), ", ".join(unique[:5]))
