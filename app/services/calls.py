"""Everything that happens during and after a conversation: tool execution
(shared by phone + chat), end-of-call processing, owner notifications."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AgencySettings, Agent, Booking, Call, Client, Contact, CustomFunction, KnowledgeGap, PhoneNumber
from . import billing, crawler, eventlog, http, llm, messaging, scheduling, vapi, workflows


def agency(db: Session) -> AgencySettings:
    a = db.scalars(select(AgencySettings)).first()
    if not a:
        a = AgencySettings()
        db.add(a)
        db.flush()
    return a


def resolve_client(db: Session, facts: dict) -> tuple[Client | None, Agent | None]:
    agent = None
    if facts.get("assistant_id"):
        agent = db.scalars(select(Agent).where(Agent.vapi_assistant_id == facts["assistant_id"])).first()
    meta = facts.get("metadata") or {}
    if not agent and str(meta.get("deskline_agent_id", "")).isdigit():
        agent = db.get(Agent, int(meta["deskline_agent_id"]))
    if agent:
        return agent.client, agent
    if facts.get("phone_number_id"):
        num = db.scalars(select(PhoneNumber).where(PhoneNumber.vapi_id == facts["phone_number_id"])).first()
        if num:
            return num.client, num.client.agent
    return None, None


# ------------------------------------------------------------------ tools

def _contact(db, client, kind, args, source, vapi_call_id, caller_number):
    c = Contact(client_id=client.id, kind=kind, name=(args.get("name") or "")[:160] or None,
                phone=args.get("phone") or caller_number, email=(args.get("email") or None),
                details="\n".join(x for x in [args.get("interest"), args.get("message"), args.get("notes")] if x) or None,
                urgency=args.get("urgency") or "normal", source=source, vapi_call_id=vapi_call_id)
    db.add(c)
    db.flush()
    return c


def run_tool(db: Session, client: Client, name: str, args: dict, *, vapi_call_id: str | None = None,
             source: str = "call", caller_number: str | None = None) -> tuple[bool, str]:
    """Returns (ok, text). Text is what the model sees: short, factual, speakable."""
    args = args or {}
    try:
        if name == "check_availability":
            return True, json.dumps(scheduling.check_availability(db, client, args.get("date", "today")))
        if name == "book_appointment":
            phone = args.get("phone") or caller_number
            b = scheduling.book(db, client, args.get("start", ""), args.get("name") or "Caller", phone,
                                args.get("email"), args.get("service"), args.get("notes"), vapi_call_id, source)
            label = scheduling.local_label(client, b.start_at)
            _after_booking(db, client, b, label)
            return True, f"Booked for {label}." + (" A confirmation text was sent." if client.sms_followup and phone else "")
        if name in ("take_message", "capture_lead"):
            kind = "lead" if name == "capture_lead" else ("callback" if args.get("urgency") == "urgent" else "message")
            c = _contact(db, client, kind, args, source, vapi_call_id, caller_number)
            trig = "lead_captured" if kind == "lead" else "message_taken"
            workflows.run(db, trig, client, {"caller_name": c.name, "caller_number": c.phone, "message": c.details,
                                             "urgency": c.urgency, "source": source})
            if c.urgency == "urgent":
                notify_owner(db, client, f"URGENT message from {c.name or c.phone}",
                             f"{c.name or 'A caller'} ({c.phone}) says: {c.details}")
            return True, "Saved. Tell the caller the team will follow up" + (" right away." if c.urgency == "urgent" else " soon.")
        if name == "send_text":
            phone = args.get("phone") or caller_number
            kind = args.get("kind")
            text = {
                "booking_link": client.booking_link and f"Book with {client.name}: {client.booking_link}",
                "address": client.address and f"{client.name} is at {client.address}",
                "website": client.website and f"{client.name}: {client.website}",
                "custom": (args.get("custom_text") or "")[:300] and f"{client.name}: {(args.get('custom_text') or '')[:300]}",
            }.get(kind)
            if not text:
                return False, "That information isn't available to text. Offer to tell them instead."
            messaging.send_sms(phone, text)
            return True, "Text sent."
        if name and name.startswith("x_"):
            return _custom_function(db, client, name[2:], args)
        return False, f"Unknown tool {name}."
    except scheduling.BookingError as e:
        return False, str(e)
    except (http.ProviderError, http.NotConfigured, ValueError) as e:
        eventlog.record(db, "tool", f"{name} failed: {e}", client_id=client.id, level="warn")
        return False, "That system isn't responding right now. Take a message so the team can follow up."


def _custom_function(db, client, fname, args) -> tuple[bool, str]:
    fn = db.scalars(select(CustomFunction).where(CustomFunction.client_id == client.id,
                                                 CustomFunction.name == fname, CustomFunction.enabled.is_(True))).first()
    if not fn:
        return False, "That action isn't available."
    crawler.assert_public_host(fn.url)
    with http.client(timeout=10, follow_redirects=False, headers=fn.headers or {}) as c:
        r = c.get(fn.url, params=args) if fn.method.upper() == "GET" else c.request(fn.method.upper(), fn.url, json=args)
    if r.status_code >= 400:
        return False, f"The action failed (status {r.status_code})."
    return True, r.text[:1500] or "Done."


def _after_booking(db: Session, client: Client, b: Booking, label: str) -> None:
    if client.sms_followup and b.phone:
        try:
            msg = f"You're booked with {client.name} for {label}."
            if client.address:
                msg += f" Address: {client.address}."
            if client.business_phone:
                msg += f" Questions? Call {client.business_phone}."
            messaging.send_sms(b.phone, msg)
        except Exception as e:
            eventlog.record(db, "sms", f"Booking confirmation failed: {e}", client_id=client.id, level="warn")
    notify_owner(db, client, f"New booking: {b.name} — {label}",
                 f"{b.name} ({b.phone or 'no phone'}) booked {b.service or 'an appointment'} for {label}."
                 + (f"\nNotes: {b.notes}" if b.notes else ""))
    workflows.run(db, "booking_created", client, {"caller_name": b.name, "caller_number": b.phone,
                                                  "booking_time": label, "service": b.service, "source": b.source})


# ------------------------------------------------------------------ notifications

def notify_owner(db: Session, client: Client, subject: str, body: str, link: str | None = None) -> None:
    brand = agency(db).brand_name
    html = "<p>" + body.replace("\n", "<br>") + "</p>" + (f'<p><a href="{link}">Open in dashboard</a></p>' if link else "")
    try:
        if client.notify_email and client.owner_email:
            messaging.send_email(client.owner_email, f"[{brand}] {subject}", html, body)
        if client.notify_sms and client.owner_phone:
            messaging.send_sms(client.owner_phone, f"{brand}: {subject}. {body[:240]}")
    except Exception as e:
        eventlog.record(db, "notify", f"Owner notification failed: {e}", client_id=client.id, level="warn")


# ------------------------------------------------------------------ end of call

def _classify(db: Session, call: Call) -> str:
    s = call.structured or {}
    if s.get("outcome") in ("booked", "transferred", "message", "lead", "info", "spam", "missed"):
        return s["outcome"]
    if db.scalars(select(Booking).where(Booking.vapi_call_id == call.vapi_call_id)).first():
        return "booked"
    reason = (call.ended_reason or "").lower()
    if "forward" in reason or "transfer" in reason:
        return "transferred"
    kinds = {c.kind for c in db.scalars(select(Contact).where(Contact.vapi_call_id == call.vapi_call_id))}
    if kinds & {"message", "callback"}:
        return "message"
    if "lead" in kinds:
        return "lead"
    if call.duration_seconds < 10:
        return "missed"
    return "info"


def _analyze(transcript: str) -> dict:
    """Our own post-call analysis when Vapi's is missing (keeps QA working on any provider)."""
    if not (transcript and llm.available()):
        return {}
    try:
        return llm.complete_json(
            "You review receptionist phone calls for a small business. Return "
            '{"summary": 2 sentences for the owner (who called, what they wanted, what was promised),'
            ' "caller_name": str|null, "outcome": one of booked|transferred|message|lead|info|spam|missed,'
            ' "caller_sentiment": positive|neutral|negative, "unanswered_questions": [questions the assistant could not answer],'
            ' "assistant_mistake": bool, "assistant_mistake_reason": str|null}',
            transcript[:12000], temperature=0.1)
    except Exception:
        return {}


def _norm_q(q: str) -> str:
    return " ".join("".join(ch for ch in q.lower() if ch.isalnum() or ch.isspace()).split())[:200]


def record_gaps(db: Session, client: Client, call: Call, questions: list) -> int:
    open_gaps = db.scalars(select(KnowledgeGap).where(KnowledgeGap.client_id == client.id,
                                                      KnowledgeGap.status == "open")).all()
    by_norm = {_norm_q(g.question): g for g in open_gaps}
    added = 0
    for q in questions or []:
        q = str(q).strip()
        if len(q) < 6:
            continue
        key = _norm_q(q)
        if key in by_norm:
            by_norm[key].times_asked += 1
        else:
            g = KnowledgeGap(client_id=client.id, call_id=call.id, question=q[:500])
            db.add(g)
            by_norm[key] = g
            added += 1
    return added


def minutes_this_month(db: Session, client_id: int) -> int:
    from sqlalchemy import func

    return int(db.scalar(select(func.coalesce(func.sum(Call.billable_minutes), 0))
                         .where(Call.client_id == client_id, Call.created_at >= month_start())) or 0)


def alert_agency(db: Session, subject: str, body: str, client_id: int | None = None, dedupe_key: str | None = None) -> bool:
    """Email the agency once per dedupe_key (e.g. one margin alert per client per month)."""
    from ..models import EventLog, User

    if dedupe_key and db.scalars(select(EventLog).where(EventLog.kind == "alert", EventLog.message == dedupe_key)).first():
        return False
    a = agency(db)
    to = a.alert_email or db.scalars(select(User.email).where(User.role == "admin")).first()
    try:
        if to:
            messaging.send_email(to, f"[{a.brand_name} alert] {subject}", "<p>" + body.replace("\n", "<br>") + "</p>", body)
    except Exception as e:
        eventlog.record(db, "notify", f"Agency alert failed: {e}", client_id=client_id, level="warn")
    eventlog.record(db, "alert", dedupe_key or subject, client_id=client_id, level="warn", subject=subject)
    return True


def guardrails(db: Session, client: Client) -> None:
    """Minute cap + margin checks after each call."""
    from . import pricing

    used = minutes_this_month(db, client.id)
    month = month_start().strftime("%Y-%m")
    if client.monthly_minute_cap and used >= client.monthly_minute_cap and not (
            client.cap_tripped_at and _aware(client.cap_tripped_at) >= month_start()):
        client.cap_tripped_at = datetime.now(timezone.utc)
        forwarding = client.cap_action == "forward" and client.transfer_number
        alert_agency(db, f"{client.name} hit its {client.monthly_minute_cap}-minute cap",
                     f"{used} minutes used this month. " + ("New calls now forward to "
                     f"{client.transfer_number} until the cap is raised or the month resets." if forwarding
                     else "The receptionist keeps answering (cap action = alert)."),
                     client_id=client.id, dedupe_key=f"cap:{client.id}:{month}")
    if client.status != "active":
        return
    revenue = pricing.monthly_charge_cents(client, used)
    cost = sum(estimated_cost_cents(c) for c in db.scalars(
        select(Call).where(Call.client_id == client.id, Call.created_at >= month_start())))
    pct = agency(db).margin_alert_pct or 70
    if revenue > 0 and cost >= revenue * pct / 100:
        alert_agency(db, f"Margin warning: {client.name}",
                     f"Provider cost so far this month is ${cost / 100:.2f} vs ${revenue / 100:.2f} billable "
                     f"({cost / revenue * 100:.0f}%). Consider a minute cap or overage pricing.",
                     client_id=client.id, dedupe_key=f"margin:{client.id}:{month}")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def route_inbound(db: Session, msg: dict) -> dict:
    """Answer Vapi's assistant-request (must respond < 7.5 s): which assistant, or forward."""
    facts = vapi.call_facts(msg)
    num = None
    if facts.get("phone_number_id"):
        num = db.scalars(select(PhoneNumber).where(PhoneNumber.vapi_id == facts["phone_number_id"])).first()
    if not num and facts.get("to_number"):
        num = db.scalars(select(PhoneNumber).where(PhoneNumber.number == facts["to_number"])).first()
    if not num:
        return {"error": "Sorry, this number isn't set up yet. Please try again later."}
    client = num.client
    agent = client.agent
    forward = {"destination": {"type": "number", "number": client.transfer_number,
                               "message": "Please hold while I connect you."}} if client.transfer_number else None
    if client.status in ("paused", "churned"):
        return forward or {"error": f"Sorry, {client.name} can't take calls right now. Please call back later."}
    if client.monthly_minute_cap and client.cap_action == "forward" and forward \
            and minutes_this_month(db, client.id) >= client.monthly_minute_cap:
        return forward
    if not (agent and agent.vapi_assistant_id):
        return forward or {"error": "Sorry, we can't take your call right now. Please call back shortly."}
    upsert_call(db, {**facts, "assistant_id": agent.vapi_assistant_id, "status": "ringing"})
    return {"assistantId": agent.vapi_assistant_id}


def upsert_call(db: Session, facts: dict) -> Call | None:
    if not facts.get("vapi_call_id"):
        return None
    client, agent = resolve_client(db, facts)
    call = db.scalars(select(Call).where(Call.vapi_call_id == facts["vapi_call_id"])).first()
    if not call:
        call = Call(vapi_call_id=facts["vapi_call_id"])
        db.add(call)
    call.client_id = client.id if client else call.client_id
    call.agent_id = agent.id if agent else call.agent_id
    for k in ("call_type", "from_number", "to_number", "started_at", "ended_at", "ended_reason", "recording_url",
              "transcript", "summary", "cost_cents", "status"):
        v = facts.get(k)
        if v not in (None, ""):
            setattr(call, k, v)
    if facts.get("duration_seconds"):
        call.duration_seconds = facts["duration_seconds"]
    if facts.get("structured"):
        call.structured = facts["structured"]
    db.flush()
    return call


def process_end_of_call(db: Session, msg: dict) -> Call | None:
    facts = vapi.call_facts(msg)
    call = upsert_call(db, facts)
    if not call:
        return None
    call.status = "ended"
    call.billable_minutes = math.ceil(call.duration_seconds / 60) if call.duration_seconds > 0 else 0
    if call.processed:
        return call  # Vapi retried the webhook: don't double-notify or double-bill
    client = db.get(Client, call.client_id) if call.client_id else None
    if not client:
        eventlog.record(db, "vapi", f"Call {call.vapi_call_id} has no matching client", level="warn",
                        assistant_id=facts.get("assistant_id"))
        call.processed = True
        return call
    s_data = dict(call.structured or {})
    if not call.summary or not s_data:
        mine = _analyze(call.transcript or "")
        call.summary = call.summary or mine.get("summary")
        for k, v in mine.items():
            s_data.setdefault(k, v)
        call.structured = s_data
    call.outcome = _classify(db, call)
    call.sentiment = s_data.get("caller_sentiment")
    reasons = []
    if s_data.get("assistant_mistake"):
        reasons.append(s_data.get("assistant_mistake_reason") or "Assistant mistake")
    if call.sentiment == "negative":
        reasons.append("Caller sounded unhappy")
    if call.duration_seconds >= 20 and call.outcome == "info" and s_data.get("follow_up_needed"):
        reasons.append("Follow-up needed but nothing was captured")
    call.flagged, call.flag_reason = bool(reasons), "; ".join(reasons)[:300] or None
    record_gaps(db, client, call, s_data.get("unanswered_questions") or [])
    # link captured contacts to the call row
    for c in db.scalars(select(Contact).where(Contact.vapi_call_id == call.vapi_call_id)):
        c.call_id = call.id
    s = get_settings()
    link = f"{s.base_url}/portal/calls/{call.id}"
    if call.duration_seconds >= (client.notify_min_seconds or 0) or call.outcome in ("booked", "message", "lead"):
        who = (call.structured or {}).get("caller_name") or call.from_number or "Unknown caller"
        notify_owner(db, client, f"Call from {who} — {call.outcome}",
                     f"Caller: {who} ({call.from_number or 'unknown number'})\n"
                     f"Duration: {int(call.duration_seconds)}s\nOutcome: {call.outcome}\n\n{call.summary or 'No summary.'}",
                     link=link)
    ctx = {"caller_number": call.from_number, "caller_name": (call.structured or {}).get("caller_name"),
           "outcome": call.outcome, "summary": call.summary, "duration_seconds": call.duration_seconds,
           "recording_url": call.recording_url, "urgent": (call.structured or {}).get("urgent"), "call_url": link,
           "source": "call"}
    workflows.run(db, "call_ended", client, ctx)
    if call.outcome == "missed":
        workflows.run(db, "missed_call", client, ctx)
    try:
        billing.report_usage(db, client, call, agency(db))
    except Exception as e:  # never lose the call record over a billing hiccup; it's retryable from the UI
        eventlog.record(db, "billing", f"Usage report failed for call {call.vapi_call_id}: {e}", client_id=client.id, level="warn")
    call.processed = True
    db.flush()
    guardrails(db, client)
    return call


def estimated_cost_cents(call: Call) -> float:
    if call.cost_cents is not None:
        return call.cost_cents
    return (call.duration_seconds / 60.0) * get_settings().est_cost_per_minute_cents


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
