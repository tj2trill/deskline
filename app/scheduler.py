"""In-process periodic jobs: the agent team (every minute) and weekly reports. One web container = one scheduler;
if you scale to several replicas, set SCHEDULER_ENABLED=false on all but one."""
from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import select

from .db import session_scope
from .models import Client
from .services import reports

log = logging.getLogger("deskline.scheduler")
INTERVAL_SECONDS = 60


def tick() -> int:
    from .agents import runner

    try:
        runner.tick()
    except Exception as e:  # the agent runner isolates failures; this is a last line of defence
        log.warning("agent tick failed: %s", e)
    sent = 0
    with session_scope() as db:
        for c in db.scalars(select(Client).where(Client.status == "active", Client.report_weekly.is_(True))):
            if reports.due(c):
                try:
                    sent += int(reports.send_weekly(db, c))
                except Exception as e:  # one bad mailbox must not stop the others
                    log.warning("weekly report for %s failed: %s", c.name, e)
    return sent


async def run_forever() -> None:
    while True:
        try:
            await asyncio.to_thread(tick)
        except Exception as e:
            log.warning("scheduler tick failed: %s", e)
        await asyncio.sleep(INTERVAL_SECONDS)


def enabled() -> bool:
    return os.environ.get("SCHEDULER_ENABLED", "true").lower() not in ("0", "false", "no")
