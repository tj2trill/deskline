"""Live snapshot for the Office view. Every bubble, number and conversation the office
shows comes from here, and everything here comes from real data. Nothing is invented."""
from __future__ import annotations

import hashlib
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (AgencySettings, AgentAction, AgentConfig, AgentRun, Booking, Call, Client, EmailDraft, EventLog,
                      KnowledgeGap, Lesson, Payment, Prospect)
from ..services import billing, leads, learning, llm, outreach, pricing, vapi
from . import actions
from .base import aware, utcnow
from .registry import REGISTRY, TEAM
from .runner import ensure_configs

ROOMS = {
    "prospector": "Research", "demo_builder": "Workshop", "outreach": "Mail room", "closer": "Sales",
    "quality_coach": "Training", "account_manager": "Client lounge", "billing_guardian": "Vault",
    "ops_monitor": "Server room", "daily_brief": "Newsroom", "strategist": "Strategy room",
}


def _id(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:12]


def blocker(db: Session, key: str, cfg: AgentConfig, agency: AgencySettings) -> dict | None:
    """What stops this agent from making money right now, in its own words, with the fix."""
    s = get_settings()
    st = {**{k.name: k.default for k in REGISTRY[key].knobs}, **(cfg.settings or {})}
    email_live = s.email_provider != "console"
    if key == "prospector":
        if not st.get("targets"):
            return {"text": "Tell me what to look for: add a type of business and a city.", "href": "/agents/prospector", "cta": "Add targets"}
        if not leads.configured():
            return {"text": "I need a Google Places key before I can search.", "href": "/settings", "cta": "Open settings"}
    if key == "demo_builder" and st.get("get_number") and not vapi.configured():
        return {"text": "I can build demos, but I need a Vapi key to give them phone numbers.", "href": "/settings", "cta": "Open settings"}
    if key == "outreach":
        if not agency.mailing_address:
            return {"text": "Add your mailing address. The law requires it before I can send cold email.", "href": "/settings", "cta": "Add address"}
        if not email_live:
            return {"text": "My emails only reach the outbox until you connect an email provider.", "href": "/settings", "cta": "Connect email"}
    if key == "closer":
        if not billing.configured():
            return {"text": "Connect Stripe so I can send checkout links.", "href": "/settings", "cta": "Open settings"}
        p = db.scalars(select(Prospect).where(Prospect.status == "won", Prospect.email.is_(None))).first()
        if p:
            return {"text": f"{p.name} is won, but I have no email to send the checkout link to.", "href": f"/prospects/{p.id}", "cta": "Add their email"}
    if key == "quality_coach" and not llm.available():
        return {"text": "Connect an LLM so I can draft answers from each client's information.", "href": "/settings", "cta": "Open settings"}
    if key == "billing_guardian" and not billing.configured():
        return {"text": "Connect Stripe so I can bill minutes.", "href": "/settings", "cta": "Open settings"}
    if key == "strategist" and not s.inbound_reply_to:
        return {"text": "I only learn from replies you mark by hand. Turn on reply capture so every reply counts.",
                "href": "/settings", "cta": "Set up reply capture"}
    if key == "daily_brief" and not email_live:
        return {"text": "Your brief only reaches the outbox until email is connected.", "href": "/settings", "cta": "Connect email"}
    return None


def _money(db: Session, agency: AgencySettings) -> dict:
    from ..services.calls import estimated_cost_cents, month_start

    now = utcnow()
    try:
        tz = ZoneInfo(agency.timezone or "UTC")
    except Exception:
        tz = timezone.utc
    local_midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    billable, cost, mrr = 0.0, 0.0, 0
    for c in db.scalars(select(Client).where(Client.status == "active")).all():
        calls = db.scalars(select(Call).where(Call.client_id == c.id, Call.created_at >= month_start())).all()
        minutes = sum(x.billable_minutes or 0 for x in calls)
        billable += pricing.monthly_charge_cents(c, minutes)
        cost += sum(estimated_cost_cents(x) for x in calls)
        if c.stripe_status in ("active", "trialing"):
            mrr += c.monthly_price_cents or 0
    collected = db.scalar(select(func.coalesce(func.sum(Payment.amount_cents), 0))
                          .where(Payment.created_at >= now - timedelta(days=30))) or 0
    pipeline = dict(db.execute(select(Prospect.status, func.count()).group_by(Prospect.status)).all())
    return {
        "billable_month": round(billable), "cost_month": round(cost), "mrr": mrr, "collected_30d": int(collected),
        "calls_today": db.scalar(select(func.count()).select_from(Call).where(Call.created_at >= local_midnight)) or 0,
        "bookings_today": db.scalar(select(func.count()).select_from(Booking).where(Booking.created_at >= local_midnight)) or 0,
        "emails_today": outreach.sent_last_24h(db),
        "new_prospects_24h": db.scalar(select(func.count()).select_from(Prospect).where(Prospect.created_at >= now - timedelta(days=1))) or 0,
        "pipeline": {k: pipeline.get(k, 0) for k in ("new", "contacted", "demo", "replied", "won", "lost")},
        "active_clients": db.scalar(select(func.count()).select_from(Client).where(Client.status == "active")) or 0,
    }


def _conversations(db: Session, cfgs: dict[str, AgentConfig]) -> list[dict]:
    """Hand-offs between agents, built only from things that actually happened."""
    now = utcnow()
    out: list[dict] = []
    on = {k: c.mode != "off" for k, c in cfgs.items()}

    run = db.scalars(select(AgentRun).where(AgentRun.agent_key == "prospector", AgentRun.status == "ok",
                                            AgentRun.started_at >= now - timedelta(days=1))
                     .order_by(AgentRun.started_at.desc())).first()
    if run and (run.stats or {}).get("new_prospects"):
        n = run.stats["new_prospects"]
        top = db.scalars(select(Prospect).where(Prospect.created_at >= run.started_at - timedelta(minutes=1))
                         .order_by(Prospect.score.desc()).limit(1)).first()
        line = f"Found {n} new business{'es' if n != 1 else ''}." + (f" Best one: {top.name}, score {top.score}." if top else "")
        reply = "I'll build demos for the ones that qualify." if on.get("demo_builder") else "I'm switched off, so they'll wait."
        out.append({"id": _id("p2d", run.id), "from": "prospector", "to": "demo_builder", "lines": [["prospector", line], ["demo_builder", reply]]})

    # a demo the Demo builder made for a prospect still being worked (not a won deal the Closer set up)
    demo = db.scalars(select(Client).join(Prospect, Prospect.client_id == Client.id).where(
        Client.status == "demo", Client.created_at >= now - timedelta(days=2),
        Prospect.status.in_(["new", "contacted", "demo"])).order_by(Client.created_at.desc())).first()
    if demo:
        number = next((n.number for n in demo.numbers if n.number), None)
        if number:
            line = f"{demo.name}'s demo is live. It answers at {number}."
            reply = ("I'll put that number in their first email." if on.get("outreach") else "I'm switched off. Turn me on to email them.")
        else:
            line = f"I built a demo for {demo.name}. It still needs a phone number."
            reply = "I'll wait for the number so they have something to call."
        out.append({"id": _id("d2o", demo.id, bool(number)), "from": "demo_builder", "to": "outreach",
                    "lines": [["demo_builder", line], ["outreach", reply]]})

    replied = db.scalars(select(Prospect).where(Prospect.status == "replied").order_by(Prospect.score.desc())).first()
    if replied:
        out.append({"id": _id("o2c", replied.id), "from": "outreach", "to": "closer",
                    "lines": [["outreach", f"{replied.name} replied. Mark them won when they say yes."],
                              ["closer", "Then I'll send their checkout link."]]})

    for a in db.scalars(select(AgentAction).where(AgentAction.kind == "onboard_owner", AgentAction.status == "done",
                                                  AgentAction.decided_at >= now - timedelta(days=7))).all()[:1]:
        c = db.get(Client, a.client_id)
        if c:
            out.append({"id": _id("c2b", a.id), "from": "closer", "to": "billing_guardian",
                        "lines": [["closer", f"{c.name} signed up and has their login."],
                                  ["billing_guardian", "I'll make sure every minute they use gets billed."]]})

    gap = db.scalars(select(KnowledgeGap).where(KnowledgeGap.status == "open", KnowledgeGap.times_asked >= 2)
                     .order_by(KnowledgeGap.times_asked.desc())).first()
    if gap:
        c = db.get(Client, gap.client_id)
        pending = db.scalars(select(AgentAction).where(AgentAction.dedupe_key == f"answer:{gap.id}")).first()
        reply = ("I drafted an answer from their own info. It's waiting for approval." if pending
                 else "If their info doesn't say, I'll ask the owner.")
        out.append({"id": _id("q2a", gap.id, gap.times_asked), "from": "quality_coach", "to": "account_manager",
                    "lines": [["quality_coach", f"{c.name if c else 'A client'}'s callers asked \"{gap.question}\" {gap.times_asked} times."],
                              ["quality_coach" if not on.get("account_manager") else "account_manager", reply]]})

    # what the Strategist learned recently, told to the agent it affects
    reply_of = {
        "target": ("prospector", {"works": "Good. I'll search there first.", "fails": "Understood. I'll stop once you approve retiring it."}),
        "angle": ("outreach", {"works": "I'll write that angle more often.", "fails": "I'll use it less."}),
        "demo": ("demo_builder", {"works": "Then every lead should get a demo line.", "fails": "Noted. Lines cost money, so that matters."}),
        "client": ("billing_guardian", {"loses": "Its pricing is on the client's Billing tab.", "earns": "I'll keep its minutes billed."}),
        "niche": ("prospector", {"earns": "I'll weight that niche higher when I pick searches.", "loses": "I'll weight that niche lower."}),
        "score": ("outreach", {"works": "I'll favour those leads.", "fails": "I'll skip them once you raise the cutoff."}),
    }
    if cfgs.get("strategist") and cfgs["strategist"].mode != "off":
        recent = db.scalars(select(Lesson).where(Lesson.changed_at >= now - timedelta(days=3), Lesson.verdict != "early",
                                                 Lesson.area.in_(list(reply_of))).order_by(Lesson.changed_at.desc()).limit(2)).all()
        for les in recent:
            to, replies = reply_of[les.area]
            reply = replies.get(les.verdict)
            if reply:
                out.append({"id": _id("learn", les.key, les.verdict), "from": "strategist", "to": to,
                            "lines": [["strategist", les.statement], [to, reply]]})

    problems = db.scalar(select(func.count()).select_from(EventLog).where(
        EventLog.level.in_(["warn", "error"]), EventLog.kind != "alert", EventLog.created_at >= now - timedelta(hours=1))) or 0
    if problems:
        out.append({"id": _id("ops", now.strftime("%Y%m%d%H"), problems), "from": "ops_monitor", "to": "daily_brief",
                    "lines": [["ops_monitor", f"{problems} problem{'s' if problems != 1 else ''} in the last hour. Details are in Settings, Event log."],
                              ["daily_brief", "I'll put it in tomorrow's brief."]]})
    return out[:6]


# What each agent's absence costs, closest to cash first.
OFF_COST = {
    "billing_guardian": "minutes your clients use aren't being billed",
    "closer": "won deals don't get a checkout link",
    "outreach": "nobody new is being emailed",
    "demo_builder": "new leads don't get a demo receptionist",
    "prospector": "no new leads are being found",
    "quality_coach": "unanswered caller questions pile up",
    "account_manager": "quiet or slipping clients go unnoticed",
    "ops_monitor": "outages go unnoticed",
    "daily_brief": "you don't get a morning summary",
    "strategist": "nobody checks which targets, emails and clients actually make money",
}
CASH_ORDER = ["closer", "billing_guardian", "outreach", "demo_builder", "prospector", "strategist", "quality_coach",
              "account_manager", "ops_monitor", "daily_brief"]


def next_step(agents: list[dict], money: dict, enabled: bool, replied: list[dict] | None = None) -> dict:
    """The single most valuable thing you can do in the office right now."""
    by = {a["key"]: a for a in agents}
    if not enabled:
        return {"text": "All agents are paused. Nothing runs and nothing is sent until you resume.", "action": "resume",
                "cta": "Resume agents", "tone": "bad"}
    order = list(CASH_ORDER)
    if not money.get("active_clients"):
        order.remove("billing_guardian")
        order.append("billing_guardian")
    closer = by["closer"]
    if closer["pending_count"] and closer["mode"] != "off":
        return {"text": f"Closer needs your OK: {closer['pending'][0]['title']}.", "agent": "closer", "cta": "Review",
                "tone": "wait"}
    if replied:
        p = replied[0]
        more = f" ({len(replied) - 1} more replied too)" if len(replied) > 1 else ""
        return {"text": f"{p['name']} replied{more}. Talk to them, then mark them won so the Closer can bill them.",
                "desk": True, "cta": "Close deals", "tone": "wait"}
    for k in order:
        a = by[k]
        if a["pending_count"] and a["mode"] != "off":
            n = a["pending_count"]
            more = f" and {n - 1} more" if n > 1 else ""
            return {"text": f"{a['title']} needs your OK: {a['pending'][0]['title']}{more}.", "agent": k,
                    "cta": "Review", "tone": "wait"}
    for k in order:
        a = by[k]
        if a["blocker"] and a["mode"] != "off":
            return {"text": f"{a['title']}: {a['blocker']['text']}", "href": a["blocker"]["href"],
                    "cta": a["blocker"]["cta"], "agent": k, "tone": "bad"}
    for k in order:
        a = by[k]
        if a["status"] == "error":
            return {"text": f"{a['title']} hit an error: {a['summary'].removeprefix('Error: ')}", "agent": k,
                    "cta": "Look", "tone": "bad"}
    for k in order:
        if by[k]["mode"] == "off" and (k != "billing_guardian" or money.get("active_clients")):
            return {"text": f"{by[k]['title']} is switched off, so {OFF_COST[k]}.", "agent": k, "cta": "Turn on",
                    "tone": "wait"}
    return {"text": "Nothing needs you right now. The team is working.", "tone": "good"}


def snapshot(db: Session) -> dict:
    cfgs = ensure_configs(db)
    agency = db.scalars(select(AgencySettings)).first()
    db.commit()
    agents = []
    for a in TEAM:
        cfg = cfgs[a.key]
        last = db.scalars(select(AgentRun).where(AgentRun.agent_key == a.key).order_by(AgentRun.started_at.desc())).first()
        pend = db.scalars(select(AgentAction).where(AgentAction.agent_key == a.key, AgentAction.status == "pending")
                          .order_by(AgentAction.created_at)).all()
        block = blocker(db, a.key, cfg, agency)
        if not agency.agents_enabled or cfg.mode == "off":
            status = "off"
        elif cfg.running_since:
            status = "running"
        elif pend:
            status = "waiting"
        elif cfg.last_status == "error":
            status = "error"
        elif block:
            status = "blocked"
        else:
            status = "idle"
        agents.append({
            "key": a.key, "title": a.title, "role": a.role, "room": ROOMS[a.key], "mode": cfg.mode, "status": status,
            "summary": cfg.last_summary or "", "last_run": aware(cfg.last_run_at).isoformat() if cfg.last_run_at else None,
            "next_run": aware(cfg.next_run_at).isoformat() if cfg.next_run_at else None,
            "log": [e["msg"] for e in (last.log or [])[-4:]] if last else [],
            "blocker": block,
            "pending": [{"id": p.id, "title": p.title, "detail": (p.detail or "")[:1200], "kind": actions.label_of(p.kind)}
                        for p in pend[:8]],
            "pending_count": len(pend),
        })
    money = _money(db, agency)
    replied = [{"id": p.id, "name": p.name, "email": p.email, "score": p.score} for p in db.scalars(
        select(Prospect).where(Prospect.status == "replied").order_by(Prospect.score.desc()).limit(6))]
    ops = cfgs.get("ops_monitor")
    bell = db.scalars(select(EventLog).where(EventLog.kind == "stripe", EventLog.message.in_(
        ["checkout.session.completed", "customer.subscription.created"])).order_by(EventLog.created_at.desc())).first()
    return {
        "now": utcnow().isoformat(),
        "enabled": bool(agency.agents_enabled),
        "brand": agency.brand_name,
        "timezone": agency.timezone,
        "agents": agents,
        "money": money,
        "next_step": next_step(agents, money, bool(agency.agents_enabled), replied),
        "pending_total": sum(x["pending_count"] for x in agents),
        "health": (ops.state or {}).get("health", {}) if ops else {},
        "bell_at": aware(bell.created_at).isoformat() if bell else None,
        "conversations": _conversations(db, cfgs),
        "replied": replied,
        "lessons": learning.top_lessons(db, 6),
        "drafts_waiting": db.scalar(select(func.count()).select_from(EmailDraft).where(EmailDraft.status == "draft")) or 0,
    }


