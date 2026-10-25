"""The agent team. Each agent covers one job in the business and hands off to the next
through shared state (prospects → demos → outreach → won → checkout → onboarded client)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from ..models import (AgentAction, AgentConfig, AgentRun, Booking, Call, Client, Contact, EmailDraft, EventLog,
                      KnowledgeGap, Prospect, ProspectSearch, User)
from ..services import agent_builder, billing, http, leads, learning, llm, outreach, provisioning, sitegen, vapi, writer
from .base import Agent, RunContext, aware


def _local_now(ctx: RunContext) -> datetime:
    try:
        return ctx.now.astimezone(ZoneInfo(ctx.agency.timezone or "UTC"))
    except Exception:
        return ctx.now


def _alert(ctx: RunContext, subject: str, body: str, dedupe: str, client_id: int | None = None) -> bool:
    from ..services.calls import alert_agency

    sent = alert_agency(ctx.db, subject, body, client_id=client_id, dedupe_key=dedupe)
    if sent:
        ctx.stats["alerts"] += 1
        ctx.log(f"Alerted you: {subject}")
    return sent


# =================================================================== 1. Prospector

class Prospector(Agent):
    def run(self, ctx: RunContext) -> None:
        targets = ctx.settings["targets"]
        if not targets:
            ctx.summary = "Add the businesses and cities to search in this agent's settings."
            return
        if not leads.configured():
            ctx.summary = "Needs GOOGLE_PLACES_API_KEY."
            return
        retired = {learning.target_key(t) for t in ctx.settings.get("retired") or []}
        live = [t for t in targets if learning.target_key(t) not in retired]
        # Learned order: targets that bring positive replies and paying clients first, with
        # Thompson sampling so untested targets still get tried.
        order = learning.rank_targets(ctx.db, live)
        if order:
            ctx.log("Search order from results so far: " + ", ".join(order[:4]) + (" …" if len(order) > 4 else ""))
        recent = ctx.now - timedelta(days=ctx.settings["research_after_days"])
        searched = 0
        for line in order:
            if searched >= ctx.settings["searches_per_run"]:
                break
            niche, _, location = (part.strip() for part in line.partition("|"))
            if not (niche and location):
                ctx.log(f"Skipped '{line}': write it as type of business | city")
                continue
            done_before = ctx.db.scalars(select(ProspectSearch).where(
                func.lower(ProspectSearch.niche) == niche.lower(), func.lower(ProspectSearch.location) == location.lower(),
                ProspectSearch.created_at >= recent)).first()
            if done_before:
                continue
            s = leads.run_search(ctx.db, niche, location)
            ctx.stats["searches"] += 1
            ctx.stats["new_prospects"] += s.result_count
            ctx.log(f"Searched {niche} in {location}: {s.result_count} new businesses")
            searched += 1
            ctx.commit()
        todo = ctx.db.scalars(select(Prospect).where(Prospect.enriched.is_(False), Prospect.website.is_not(None),
                                                     Prospect.status == "new")
                              .order_by(Prospect.score.desc()).limit(ctx.settings["enrich_top"])).all()
        for p in todo:
            try:
                leads.enrich(p)
                ctx.stats["websites_checked"] += 1
            except Exception as e:
                p.enriched = True
                p.signals = {**(p.signals or {}), "error": str(e)[:200]}
            ctx.commit()
        if not searched and not todo:
            ctx.summary = "Every target was searched recently. Nothing new to do."


# =================================================================== 2. Demo builder

class DemoBuilder(Agent):
    def run(self, ctx: RunContext) -> None:
        st = ctx.settings
        q = select(Prospect).where(Prospect.client_id.is_(None), Prospect.status.in_(["new", "contacted"]),
                                   Prospect.website.is_not(None), Prospect.score >= st["min_score"])
        if st["require_email"]:
            q = q.where(Prospect.email.is_not(None))
        built_today = ctx.db.scalar(select(func.count()).select_from(Client).where(
            Client.status == "demo", Client.created_at >= ctx.now - timedelta(days=1))) or 0
        room = max(0, min(st["max_per_run"], st["max_per_day"] - built_today))
        for p in ctx.db.scalars(q.order_by(Prospect.score.desc()).limit(room)).all():
            if outreach.is_suppressed(ctx.db, p.email):
                continue
            client = provisioning.create_client(ctx.db, ctx.agency, website=p.website, name=p.name, industry=p.niche or "",
                                                business_phone=p.phone or "", status="demo", prospect=p)
            ctx.commit()
            try:
                agent_builder.build_agent(ctx.db, client, client.agent)
            except Exception as e:
                client.agent.build_status, client.agent.build_error = "error", str(e)[:500]
                agent_builder.refresh_prompt(client, client.agent)
                client.agent.first_message = client.agent.first_message or agent_builder.default_first_message(client, client.agent)
            from ..jobs import sync_agent

            sync_agent(ctx.db, client, client.agent)
            if st["build_site"]:
                sitegen.for_client(ctx.db, client)
            ctx.stats["demos_built"] += 1
            ctx.log(f"Built a demo receptionist for {client.name}")
            ctx.commit()
            if st["get_number"] and vapi.configured():
                area = provisioning.area_code_of(p.phone)
                ctx.act("buy_number", f"Get a demo number for {client.name}" + (f" in area code {area}" if area else ""),
                        {"client_id": client.id, "area_code": area}, dedupe=f"number:{client.id}",
                        detail="So the prospect can call a receptionist trained on their own website.",
                        client_id=client.id, prospect_id=p.id)
        if not ctx.stats:
            ctx.summary = "No prospects match yet." if room else "Today's demo limit is reached."


# =================================================================== 3. Outreach

class Outreach(Agent):
    def run(self, ctx: RunContext) -> None:
        st, a = ctx.settings, ctx.agency
        if not a.mailing_address:
            ctx.summary = "Paused: add your mailing address in Settings (required in every cold email)."
            return
        local = _local_now(ctx)
        if st["weekdays_only"] and local.weekday() >= 5:
            ctx.summary = "Weekend. Emails go out on weekdays."
            return
        if not (st["window_start"] <= local.strftime("%H:%M") < st["window_end"]):
            ctx.summary = f"Outside the {st['window_start']}–{st['window_end']} send window."
            return
        room = min(st["max_per_run"], max(0, a.daily_email_cap - outreach.sent_last_24h(ctx.db)))
        if room <= 0:
            ctx.summary = "Today's email cap is used up."
            return
        q = select(Prospect).where(Prospect.email.is_not(None), Prospect.status.in_(["new", "contacted", "demo"]),
                                   Prospect.score >= st["min_score"]).order_by(Prospect.score.desc()).limit(200)
        angles = None  # built on first use: which email angle to write, learned from replies
        for p in ctx.db.scalars(q).all():
            if room <= 0:
                break
            if outreach.is_suppressed(ctx.db, p.email):
                continue
            client = ctx.db.get(Client, p.client_id) if p.client_id else None
            demo_number = next((n.number for n in client.numbers if n.number), None) if client else None
            if st["require_demo"] and not demo_number:
                continue
            if not ctx.db.scalars(select(EmailDraft.id).where(EmailDraft.prospect_id == p.id)).first():
                if angles is None:
                    angles = learning.AngleModel(ctx.db)
                angle = angles.choose(p.niche, demo_number)
                p.angle, p.contacted_with_demo = angle, bool(demo_number)
                ctx.log(f"{p.name}: writing the '{writer.ANGLE_LABELS[angle]}' angle")
                for e in writer.email_sequence(a, p, demo_number, angle=angle):
                    ctx.db.add(EmailDraft(prospect_id=p.id, step=e["step"], send_after_days=e["days"],
                                          subject=e["subject"], body=e["body"]))
                ctx.stats["sequences_written"] += 1
                ctx.commit()
            d = outreach.next_due(ctx.db, p, ctx.now)
            if not d:
                continue
            try:
                outreach.check(ctx.db, a, p, d)
            except outreach.Blocked as e:
                ctx.log(f"Skipped {p.name}: {e}")
                continue
            act = ctx.act("send_email_draft", f"Email {d.step} to {p.name}", {"draft_id": d.id},
                          dedupe=f"email:{d.id}", prospect_id=p.id,
                          detail=f"To: {p.email}\nSubject: {d.subject}\n\n{d.body}")
            if act:
                room -= 1


# =================================================================== 4. Closer + onboarding

class Closer(Agent):
    def run(self, ctx: RunContext) -> None:
        st = ctx.settings
        for p in ctx.db.scalars(select(Prospect).where(Prospect.status == "won")).all():
            client = ctx.db.get(Client, p.client_id) if p.client_id else None
            if not client:
                client = provisioning.create_client(ctx.db, ctx.agency, website=p.website or "", name=p.name,
                                                    industry=p.niche or "", business_phone=p.phone or "", status="demo",
                                                    prospect=p)
                p.status = "won"
                ctx.log(f"Created a client for {p.name}")
                ctx.commit()
            if client.stripe_status in ("active", "trialing"):
                continue
            if not client.owner_email:
                ctx.log(f"{client.name}: add an owner email so the checkout link can be sent")
                continue
            first = ctx.db.scalars(select(AgentAction).where(AgentAction.dedupe_key == f"checkout:{client.id}")).first()
            if not first:
                ctx.act("send_checkout", f"Email {client.name} their checkout link", {"client_id": client.id},
                        dedupe=f"checkout:{client.id}", client_id=client.id, prospect_id=p.id,
                        detail=f"To: {client.owner_email}. Pricing is set on the client's Billing tab.")
                continue
            if first.status != "done":
                continue
            reminders = ctx.db.scalar(select(func.count()).select_from(AgentAction).where(
                AgentAction.kind == "send_checkout_reminder", AgentAction.client_id == client.id)) or 0
            sent_at = aware(first.decided_at or first.created_at)
            due = sent_at + timedelta(days=st["reminder_days"] * (reminders + 1))
            if reminders < st["max_reminders"] and ctx.now >= due:
                ctx.act("send_checkout_reminder", f"Remind {client.name} to finish signing up", {"client_id": client.id},
                        dedupe=f"checkout-reminder:{client.id}:{reminders + 1}", client_id=client.id)
        # onboarding: paid clients without a portal login
        has_login = {u.client_id for u in ctx.db.scalars(select(User).where(User.role == "owner"))}
        for c in ctx.db.scalars(select(Client).where(Client.stripe_status.in_(["active", "trialing"]))).all():
            if c.id in has_login or not c.owner_email:
                continue
            ctx.act("onboard_owner", f"Welcome {c.name} and create their login", {"client_id": c.id},
                    dedupe=f"onboard:{c.id}", client_id=c.id,
                    detail="Sends portal login details and call-forwarding instructions.")


# =================================================================== 5. Quality coach

GROUND_PROMPT = (
    "You answer a caller's question for a small business using ONLY the business information given. "
    'Return {"answerable": bool, "answer": str (one or two sentences a receptionist can say), '
    '"evidence": str (an exact, verbatim quote from the information that supports the answer)}. '
    "If the information doesn't clearly answer it, return answerable false. Never guess."
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


class QualityCoach(Agent):
    def run(self, ctx: RunContext) -> None:
        st = ctx.settings
        gaps = ctx.db.scalars(select(KnowledgeGap).where(KnowledgeGap.status == "open",
                                                         KnowledgeGap.times_asked >= st["min_times_asked"])
                              .order_by(KnowledgeGap.times_asked.desc()).limit(st["max_per_run"])).all()
        for g in gaps:
            client = ctx.db.get(Client, g.client_id)
            if not client or not client.agent:
                continue
            if ctx.already(f"answer:{g.id}") or ctx.already(f"ask-owner:{g.id}"):
                continue
            knowledge = client.agent.knowledge or ""
            drafted = None
            if llm.available() and knowledge:
                try:
                    out = llm.complete_json(GROUND_PROMPT, f"Business information:\n{knowledge[:12000]}\n\nQuestion: {g.question}",
                                            temperature=0)
                    ev = _norm(out.get("evidence", ""))
                    # deterministic grounding check: the quote must really be in the knowledge
                    if out.get("answerable") and out.get("answer") and len(ev) >= 12 and ev in _norm(knowledge):
                        drafted = out
                    else:
                        ctx.log(f"Couldn't ground an answer for '{g.question}'")
                except Exception as e:
                    ctx.log(f"Drafting failed for '{g.question}': {e}")
            if drafted:
                ctx.act("publish_answer", f"Teach {client.name}: {g.question}", {"gap_id": g.id, "answer": drafted["answer"]},
                        dedupe=f"answer:{g.id}", client_id=client.id,
                        detail=f"Answer: {drafted['answer']}\n\nFrom their own info: \"{drafted['evidence']}\"")
            elif st["ask_owner"] and client.owner_email and client.status == "active":
                ctx.act("email_owner", f"Ask {client.name} how to answer: {g.question}",
                        {"client_id": client.id, "subject": f"Callers keep asking: {g.question}",
                         "body": f"Hi {client.owner_name or 'there'},\n\nCallers asked your receptionist \"{g.question}\" "
                                 f"{g.times_asked} times and it didn't have an answer. Type the answer once in your portal "
                                 f"(Business info) and it'll use it on every future call.\n\n{ctx.agency.brand_name}"},
                        dedupe=f"ask-owner:{g.id}", client_id=client.id)
        if not st["coach_calls"] or not llm.available():
            return
        flagged = ctx.db.scalars(select(Call).where(Call.flagged.is_(True), Call.reviewed.is_(False),
                                                    Call.transcript.is_not(None))
                                 .order_by(Call.created_at.desc()).limit(st["max_per_run"])).all()
        for call in flagged:
            if not call.client_id or ctx.already(f"coach:{call.id}"):
                continue
            client = ctx.db.get(Client, call.client_id)
            try:
                out = llm.complete_json(
                    "You coach an AI phone receptionist. Given a call where something went wrong, write ONE short "
                    'instruction that would prevent it next time. Return {"instruction": str, "why": str}. '
                    'If nothing in the receptionist\'s control went wrong, return {"instruction": ""}.',
                    f"Problem: {call.flag_reason}\n\nTranscript:\n{(call.transcript or '')[:8000]}", temperature=0.2)
            except Exception as e:
                ctx.log(f"Coaching failed for call {call.id}: {e}")
                continue
            text = (out.get("instruction") or "").strip()
            if not text:
                continue
            ctx.act("add_instruction", f"New instruction for {client.name}'s receptionist", {"client_id": client.id, "text": text},
                    dedupe=f"coach:{call.id}", client_id=client.id,
                    detail=f"Instruction: {text}\n\nWhy: {out.get('why', '')}\nFrom call {call.id}: {call.flag_reason}")


# =================================================================== 6. Account manager

class AccountManager(Agent):
    def run(self, ctx: RunContext) -> None:
        st = ctx.settings
        week = ctx.now.strftime("%G-W%V")
        silent_since = ctx.now - timedelta(days=st["silent_days"])
        for c in ctx.db.scalars(select(Client).where(Client.status == "active")).all():
            if not c.numbers:
                continue
            last = ctx.db.scalar(select(func.max(Call.created_at)).where(Call.client_id == c.id))
            started = aware(c.created_at)
            if (last is None and started < silent_since) or (last is not None and aware(last) < silent_since):
                _alert(ctx, f"No calls for {c.name} in {st['silent_days']} days",
                       "That usually means call forwarding was switched off or the number changed.",
                       dedupe=f"silent:{c.id}:{week}", client_id=c.id)
                if c.owner_email:
                    number = next((n.number for n in c.numbers if n.number), "your receptionist number")
                    ctx.act("email_owner", f"Check in with {c.name} about call forwarding",
                            {"client_id": c.id, "subject": "Quick check on your call forwarding",
                             "body": f"Hi {c.owner_name or 'there'},\n\nYour receptionist hasn't received a call in "
                                     f"{st['silent_days']} days, which usually means forwarding to {number} got switched off. "
                                     "Could you check your phone's forwarding settings? On most phones it's *72 followed by "
                                     f"the number.\n\n{ctx.agency.sender_name or ctx.agency.brand_name}"},
                            dedupe=f"silent-owner:{c.id}:{week}", client_id=c.id)
                continue
            this_week = ctx.db.scalar(select(func.count()).select_from(Call).where(
                Call.client_id == c.id, Call.created_at >= ctx.now - timedelta(days=7))) or 0
            prior = ctx.db.scalar(select(func.count()).select_from(Call).where(
                Call.client_id == c.id, Call.created_at >= ctx.now - timedelta(days=14),
                Call.created_at < ctx.now - timedelta(days=7))) or 0
            if prior >= 10 and this_week <= prior * (100 - st["drop_pct"]) / 100:
                _alert(ctx, f"Calls for {c.name} fell from {prior} to {this_week} this week",
                       "Worth a check-in: forwarding may be partially off, or their season changed.",
                       dedupe=f"drop:{c.id}:{week}", client_id=c.id)


# =================================================================== 7. Billing guardian

class BillingGuardian(Agent):
    def run(self, ctx: RunContext) -> None:
        if not billing.configured():
            ctx.summary = "Stripe isn't connected."
            return
        from ..services.calls import month_start

        for c in ctx.db.scalars(select(Client).where(Client.stripe_customer_id.is_not(None))).all():
            for call in ctx.db.scalars(select(Call).where(Call.client_id == c.id, Call.usage_reported.is_(False),
                                                          Call.billable_minutes > 0, Call.created_at >= month_start())):
                try:
                    if billing.report_usage(ctx.db, c, call, ctx.agency):
                        ctx.stats["minutes_billed"] += call.billable_minutes
                except http.ProviderError as e:
                    ctx.log(f"Stripe refused usage for {c.name}: {e}")
                    break
            ctx.commit()
            since = aware(c.past_due_since)
            if c.status == "active" and c.stripe_status == "past_due" and since and \
                    ctx.now - since >= timedelta(days=ctx.settings["pause_after_days"]):
                ctx.act("pause_client", f"Pause {c.name}: unpaid for {(ctx.now - since).days} days", {"client_id": c.id},
                        dedupe=f"pause:{c.id}:{since.date().isoformat()}", client_id=c.id,
                        detail="Calls will forward to their transfer number until they pay. Stripe keeps retrying the card.")


# =================================================================== 8. Ops monitor

class OpsMonitor(Agent):
    def run(self, ctx: RunContext) -> None:
        hour = ctx.now.strftime("%Y-%m-%dT%H")
        day = ctx.now.strftime("%Y-%m-%d")
        errors = ctx.db.scalar(select(func.count()).select_from(EventLog).where(
            EventLog.level.in_(["warn", "error"]), EventLog.kind != "alert",
            EventLog.created_at >= ctx.now - timedelta(hours=1))) or 0
        ctx.stats["problems_last_hour"] = errors
        if errors >= ctx.settings["error_threshold"]:
            recent = ctx.db.scalars(select(EventLog).where(EventLog.level.in_(["warn", "error"]), EventLog.kind != "alert")
                                    .order_by(EventLog.created_at.desc()).limit(5)).all()
            _alert(ctx, f"{errors} problems in the last hour", "\n".join(f"- {e.kind}: {e.message}" for e in recent),
                   dedupe=f"ops-errors:{hour}")
        for cfg in ctx.db.scalars(select(AgentConfig).where(AgentConfig.consecutive_failures >= 3)).all():
            _alert(ctx, f"The {cfg.key.replace('_', ' ')} agent keeps failing",
                   f"{cfg.consecutive_failures} failed runs in a row. Last: {cfg.last_summary}", dedupe=f"agent-fail:{cfg.key}:{day}")
        last = ctx.recall("health_checked_at")
        if last and ctx.now - datetime.fromisoformat(last) < timedelta(minutes=ctx.settings["health_every_minutes"]):
            return
        health = {}
        checks = []
        if vapi.configured():
            checks.append(("Vapi", vapi.ping))
        if billing.configured():
            checks.append(("Stripe", billing.ping))
        for name, fn in checks:
            try:
                fn()
                health[name] = "ok"
            except Exception as e:
                health[name] = str(e)[:200]
                _alert(ctx, f"{name} isn't responding", f"Health check failed: {e}", dedupe=f"health:{name}:{day}")
        ctx.remember(health_checked_at=ctx.now.isoformat(), health=health)
        if health:
            ctx.log("Health: " + ", ".join(f"{k} {v}" for k, v in health.items()))


# =================================================================== 9. Daily brief

class DailyBrief(Agent):
    def run(self, ctx: RunContext) -> None:
        local = _local_now(ctx)
        today = local.strftime("%Y-%m-%d")
        if ctx.recall("last_sent") == today and ctx.run.trigger != "manual":
            ctx.summary = "Already sent today."
            return
        if local.hour < ctx.settings["hour"] and ctx.run.trigger != "manual":
            ctx.summary = f"Sends at {ctx.settings['hour']}:00."
            return
        since = ctx.now - timedelta(days=1)
        db = ctx.db

        def count(model, *where):
            return db.scalar(select(func.count()).select_from(model).where(*where)) or 0

        calls = count(Call, Call.created_at >= since)
        booked = count(Booking, Booking.created_at >= since)
        leads_ = count(Contact, Contact.created_at >= since)
        prospects = count(Prospect, Prospect.created_at >= since)
        demos = count(Client, Client.created_at >= since, Client.status == "demo")
        emails = outreach.sent_last_24h(db)
        pending = db.scalars(select(AgentAction).where(AgentAction.status == "pending").order_by(AgentAction.created_at)).all()
        failed_runs = count(AgentRun, AgentRun.started_at >= since, AgentRun.status == "error")
        won = count(Prospect, Prospect.status == "won")
        lines = [
            f"Calls answered: {calls}. Appointments booked: {booked}. New leads and messages: {leads_}.",
            f"New prospects found: {prospects}. Demos built: {demos}. Outreach emails sent: {emails}. Deals marked won: {won}.",
        ]
        if pending:
            lines.append(f"\n{len(pending)} action{'s' if len(pending) != 1 else ''} waiting for your approval:")
            lines += [f"- {a.title}" for a in pending[:15]]
        if failed_runs:
            lines.append(f"\n{failed_runs} agent run{'s' if failed_runs != 1 else ''} failed. Check the Agents page.")
        from ..models import Lesson

        learned = db.scalars(select(Lesson).where(Lesson.changed_at >= since, Lesson.verdict != "early")
                             .order_by(Lesson.changed_at.desc()).limit(6)).all()
        if learned:
            lines.append("\nLearned since yesterday:")
            lines += [f"- {l.statement}" for l in learned]
        from ..config import get_settings

        lines.append(f"\nApprove or review: {get_settings().base_url}/agents")
        body = "\n".join(lines)
        from ..services import messaging

        to = ctx.agency.alert_email or db.scalars(select(User.email).where(User.role == "admin")).first()
        if not to:
            ctx.summary = "No admin email to send the brief to."
            return
        messaging.send_email(to, f"{ctx.agency.brand_name} daily brief: {calls} calls, {len(pending)} approvals waiting",
                             "<p>" + body.replace("\n", "<br>") + "</p>", body)
        ctx.remember(last_sent=today)
        ctx.stats["briefs_sent"] += 1
        ctx.summary = f"Sent to {to}: {calls} calls, {booked} bookings, {len(pending)} approvals waiting."


# =================================================================== 10. Strategist

class Strategist(Agent):
    """Closes the loop: measures every choice the team makes against real results and money,
    records what it learned, and proposes the setting changes the evidence supports."""

    def run(self, ctx: RunContext) -> None:
        st = ctx.settings
        report = learning.evaluate(ctx.db, min_n=max(5, st["min_sample"]), sure=min(0.99, max(0.6, st["confidence"] / 100)))
        changed = learning.sync_lessons(ctx.db, report["lessons"])
        ctx.commit()
        f = report["funnel"]
        ctx.log(f"Looked at {f['leads']} leads: {f['contacted']} emailed, {f['positive']} positive replies, "
                f"{f['won']} won, {f['paying']} paying ({f['mrr'] / 100:,.0f}/mo).")
        for lesson in changed:
            ctx.log(f"Learned: {lesson.statement}")
            ctx.stats["new_lessons"] += 1
        for prop in learning.proposals(ctx.db, report, st):
            if ctx.act(prop["kind"], prop["title"], prop["payload"], dedupe=prop["dedupe"], detail=prop["detail"]):
                ctx.stats["changes_proposed"] += 1
        good = sum(1 for x in report["lessons"] if x["verdict"] in ("works", "earns"))
        bad = sum(1 for x in report["lessons"] if x["verdict"] in ("fails", "loses"))
        early = sum(1 for x in report["lessons"] if x["verdict"] == "early")
        ctx.summary = (f"{good} working, {bad} not working, {early} too early to call"
                       + (f"; {len(changed)} new lesson{'s' if len(changed) != 1 else ''}" if changed else "") + ".")
