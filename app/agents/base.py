"""Agent framework.

An agent is a fixed routine (`run`) that reads the business state, does internal work
directly, and routes anything that spends money or contacts a person through `ctx.act`.
`act` either queues the action for approval ("Ask me first") or executes it now
("Autopilot"). Every action has a dedupe key, so re-running an agent never repeats work
and a rejected action is never proposed again.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AgencySettings, AgentAction, AgentConfig, AgentRun

MODES = {"off": "Off", "approve": "Ask me first", "auto": "Autopilot"}


@dataclass
class Knob:
    name: str
    label: str
    type: str  # int | bool | lines | text | time
    default: Any
    hint: str = ""


@dataclass
class Agent:
    key: str
    title: str
    role: str
    icon: str = "bot"
    default_mode: str = "approve"
    interval_minutes: int = 60
    knobs: list[Knob] = field(default_factory=list)
    acts: list[str] = field(default_factory=list)  # action kinds it may take (shown in the UI)

    def run(self, ctx: RunContext) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class RunContext:
    def __init__(self, db: Session, agent: Agent, config: AgentConfig, run: AgentRun):
        self.db, self.agent, self.config, self.run = db, agent, config, run
        self.mode = config.mode
        self.agency = db.scalars(select(AgencySettings)).first()
        self.stats: Counter = Counter()
        self.summary: str | None = None
        self.now = utcnow()
        stored = config.settings or {}
        self.settings = {k.name: _coerce(k, stored.get(k.name, k.default)) for k in agent.knobs}

    # ---- logging -------------------------------------------------------
    def log(self, message: str) -> None:
        entries = list(self.run.log or [])
        entries.append({"t": utcnow().isoformat(timespec="seconds"), "msg": message[:500]})
        self.run.log = entries[-200:]

    def commit(self) -> None:
        self.run.stats = dict(self.stats)
        self.db.commit()

    # ---- memory ----------------------------------------------------------
    def remember(self, **values) -> None:
        self.config.state = {**(self.config.state or {}), **values}

    def recall(self, key: str, default=None):
        return (self.config.state or {}).get(key, default)

    # ---- actions -----------------------------------------------------------
    def already(self, dedupe: str) -> bool:
        return self.db.scalars(select(AgentAction.id).where(AgentAction.dedupe_key == dedupe)).first() is not None

    def act(self, kind: str, title: str, payload: dict, *, dedupe: str, detail: str | None = None,
            client_id: int | None = None, prospect_id: int | None = None) -> AgentAction | None:
        """Queue (Ask me first) or execute (Autopilot) an outside-world action, once per dedupe key."""
        from . import actions

        if self.already(dedupe):
            return None
        action = AgentAction(agent_key=self.agent.key, run_id=self.run.id, kind=kind, title=title[:300],
                             detail=detail, payload=payload, dedupe_key=dedupe[:160],
                             client_id=client_id, prospect_id=prospect_id)
        self.db.add(action)
        self.commit()
        if self.mode == "auto":
            actions.execute(self.db, action, decided_by=f"autopilot:{self.agent.key}")
            self.stats["done" if action.status == "done" else "failed"] += 1
            self.log(f"{'Did' if action.status == 'done' else 'Failed'}: {title}" + (f" ({action.result})" if action.result else ""))
        else:
            self.stats["queued"] += 1
            self.log(f"Asked for approval: {title}")
        return action


def _coerce(knob: Knob, value):
    try:
        if knob.type == "int":
            return int(value)
        if knob.type == "bool":
            return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "on", "yes")
        if knob.type == "lines":
            if isinstance(value, list):
                return [str(v).strip() for v in value if str(v).strip()]
            return [ln.strip() for ln in str(value or "").splitlines() if ln.strip()]
        return str(value or "")
    except (TypeError, ValueError):
        return knob.default
