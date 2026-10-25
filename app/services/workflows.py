"""Tiny automation engine: trigger → conditions → actions (SMS, email, webhook)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import Client, Workflow
from . import crawler, eventlog, http, messaging

TRIGGERS = {  # phrased to read after "When …"
    "call_ended": "a call ends",
    "missed_call": "a call is missed or hung up early",
    "booking_created": "an appointment is booked",
    "message_taken": "a message is taken",
    "lead_captured": "a new lead is captured",
}
OPS = ["equals", "not_equals", "contains", "gt", "lt", "exists"]
FIELDS = ["outcome", "summary", "caller_number", "caller_name", "duration_seconds", "urgency", "service",
          "message", "source", "urgent"]


class _Safe(dict):
    def __missing__(self, key):
        return ""


def render(template: str, ctx: dict) -> str:
    try:
        return (template or "").format_map(_Safe({k: ("" if v is None else v) for k, v in ctx.items()}))
    except (ValueError, IndexError):
        return template or ""


def _cond_ok(cond: dict, ctx: dict) -> bool:
    field, op, val = cond.get("field"), cond.get("op", "equals"), cond.get("value")
    cur = ctx.get(field)
    if op == "exists":
        return cur not in (None, "", [], {})
    if op in ("gt", "lt"):
        try:
            a, b = float(cur or 0), float(val or 0)
        except (TypeError, ValueError):
            return False
        return a > b if op == "gt" else a < b
    a, b = str(cur or "").lower(), str(val or "").lower()
    if op == "contains":
        return b in a
    if op == "not_equals":
        return a != b
    return a == b


def _act(action: dict, ctx: dict) -> None:
    kind = action.get("type")
    if kind == "sms":
        to = render(action.get("to", "{owner_phone}"), ctx)
        messaging.send_sms(to, render(action.get("text", ""), ctx))
    elif kind == "email":
        to = render(action.get("to", "{owner_email}"), ctx)
        body = render(action.get("body", ""), ctx)
        messaging.send_email(to, render(action.get("subject", "Notification"), ctx), body.replace("\n", "<br>"), body)
    elif kind == "webhook":
        url = render(action.get("url", ""), ctx)
        crawler.assert_public_host(url)
        with http.client(timeout=10, follow_redirects=False) as c:
            http.check("Webhook", c.post(url, json={"event": ctx.get("trigger"), "data": ctx}))
    else:
        raise ValueError(f"Unknown action type {kind}")


def run(db: Session, trigger: str, client: Client, ctx: dict) -> int:
    ctx = {**ctx, "trigger": trigger, "client_name": client.name, "client_id": client.id,
           "owner_email": client.owner_email, "owner_phone": client.owner_phone}
    flows = db.scalars(select(Workflow).where(
        Workflow.enabled.is_(True), Workflow.trigger == trigger,
        or_(Workflow.client_id.is_(None), Workflow.client_id == client.id))).all()
    ran = 0
    for wf in flows:
        if not all(_cond_ok(c, ctx) for c in (wf.conditions or [])):
            continue
        try:
            for a in wf.actions or []:
                _act(a, ctx)
            wf.last_error = None
            ran += 1
        except Exception as e:
            wf.last_error = str(e)[:500]
            eventlog.record(db, "workflow", f"'{wf.name}' failed: {e}", client_id=client.id, level="warn")
        wf.run_count = (wf.run_count or 0) + 1
        wf.last_run_at = datetime.now(timezone.utc)
    return ran
