"""Runs due agents, one at a time, each in its own session, with a lock and
failure isolation: one agent crashing never stops the others."""
from __future__ import annotations

import logging
import traceback
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import AgencySettings, AgentAction, AgentConfig, AgentRun
from . import actions
from .base import RunContext, aware, utcnow
from .registry import REGISTRY

log = logging.getLogger("deskline.agents")
STALE_LOCK = timedelta(minutes=30)
MAX_RETRIES_PER_TICK = 20


def ensure_configs(db: Session) -> dict[str, AgentConfig]:
    existing = {c.key: c for c in db.scalars(select(AgentConfig))}
    for key, agent in REGISTRY.items():
        if key not in existing:
            cfg = AgentConfig(key=key, mode=agent.default_mode, interval_minutes=agent.interval_minutes, settings={})
            db.add(cfg)
            existing[key] = cfg
    db.flush()
    return existing


def is_due(cfg: AgentConfig, now) -> bool:
    if cfg.mode == "off":
        return False
    nxt = aware(cfg.next_run_at)
    return nxt is None or nxt <= now


def run_agent(key: str, trigger: str = "schedule") -> AgentRun | None:
    agent = REGISTRY[key]
    db = SessionLocal()
    try:
        cfg = ensure_configs(db)[key]
        now = utcnow()
        if cfg.running_since and now - aware(cfg.running_since) < STALE_LOCK:
            return None  # another run is in progress
        cfg.running_since = now
        run = AgentRun(agent_key=key, trigger=trigger, status="running", log=[], stats={})
        db.add(run)
        db.commit()
        ctx = RunContext(db, agent, cfg, run)
        try:
            agent.run(ctx)
            run.status = "ok"
            cfg.consecutive_failures = 0
        except Exception as e:
            db.rollback()
            run = db.get(AgentRun, run.id)
            cfg = db.get(AgentConfig, cfg.id)
            ctx.run, ctx.config = run, cfg
            run.status = "error"
            ctx.log(f"Stopped with an error: {e}")
            ctx.log(traceback.format_exc(limit=3)[-480:])
            ctx.summary = f"Error: {e}"[:300]
            cfg.consecutive_failures = (cfg.consecutive_failures or 0) + 1
            log.exception("agent %s failed", key)
        run.stats = dict(ctx.stats)
        run.summary = ctx.summary or _auto_summary(ctx)
        run.finished_at = utcnow()
        cfg.last_run_at, cfg.last_status, cfg.last_summary = run.finished_at, run.status, run.summary
        cfg.next_run_at = run.finished_at + timedelta(minutes=max(1, cfg.interval_minutes or agent.interval_minutes))
        cfg.running_since = None
        db.commit()
        return run
    finally:
        db.close()


def _auto_summary(ctx: RunContext) -> str:
    if not ctx.stats:
        return "Nothing to do."
    return ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in ctx.stats.items())


def retry_waiting(db: Session) -> int:
    """Re-run approved actions that were deferred (e.g. waiting for tomorrow's email cap)."""
    done = 0
    for a in db.scalars(select(AgentAction).where(AgentAction.status == "approved").order_by(AgentAction.created_at)
                        .limit(MAX_RETRIES_PER_TICK)):
        actions.execute(db, a, a.decided_by or "approval")
        done += a.status == "done"
    return done


def tick() -> list[str]:
    """Called by the scheduler every minute."""
    ran = []
    with SessionLocal() as db:
        agency = db.scalars(select(AgencySettings)).first()
        if not agency or not agency.agents_enabled:
            return ran
        configs = ensure_configs(db)
        db.commit()
        now = utcnow()
        due = [k for k, c in configs.items() if is_due(c, now)]
        retry_waiting(db)
    for key in due:
        try:
            if run_agent(key):
                ran.append(key)
        except Exception:  # never let one agent stop the loop
            log.exception("runner failed for %s", key)
    return ran


def decide(db: Session, action_id: int, approve: bool, who: str) -> AgentAction:
    action = db.get(AgentAction, action_id)
    if not action or action.status not in ("pending", "failed"):
        return action
    if approve:
        action.status = "approved"
        db.commit()
        return actions.execute(db, action, decided_by=who)
    action.status, action.decided_by, action.decided_at = "rejected", who, utcnow()
    db.commit()
    return action
