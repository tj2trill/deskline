"""Revenue recovery core: sending texts, routing replies, the claims ledger, the dashboard numbers.

Three leaks, one pipeline:  call → quote → job → invoice → payment
  textback.py   missed call → text conversation → qualified lead → booked estimate
  sequences.py  unanswered quote → follow-ups → accepted → deposit;  invoice → reminders → paid

Attribution rules (what counts as "recovered", and what a performance fee may be charged on):
  * Missed call → booked: the call went unanswered, we texted, the customer replied and booked through the
    text conversation. Counted at the client's average job value as an *estimate*; it becomes confirmed (and
    fee-eligible) only when the owner enters the real job value.
  * Quote recovered: the quote was accepted after at least one automated follow-up had been sent. Confirmed
    at the quote's own amount. Voided if the quote later falls through.
  * Invoice recovered: paid after it went overdue *and* after at least one overdue reminder was sent.
    Paid on time after a pre-due reminder counts as "collected on time": shown, never fee-eligible, because
    many of those customers would have paid anyway.
Every claim is one row in recovery_events keyed by what it is about, so nothing is ever counted twice.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (Client, Invoice, PhoneNumber, Quote, RecoveryCall, RecoveryEvent, SmsMessage, SmsOptOut,
                      SmsThread)
from . import eventlog, http, messaging, scheduling

STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "arret", "arrêt"}
START_WORDS = {"start", "unstop", "yes"}
HELP_WORDS = {"help"}
DEDUPE_HOURS = 24          # one opener per caller per day, however many times they redial
ACTIVE = ("texted", "engaged", "qualified")  # missed-call threads still being worked by the system


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    return dt if (dt is None or dt.tzinfo) else dt.replace(tzinfo=timezone.utc)


def recovery_number(client: Client) -> PhoneNumber | None:
    return next((n for n in client.recovery_numbers if n.number), None)


def client_for_number(db: Session, to_number: str | None) -> Client | None:
    e164 = messaging.normalize_phone(to_number)
    if not e164:
        return None
    num = db.scalars(select(PhoneNumber).where(PhoneNumber.number == e164, PhoneNumber.purpose == "recovery")).first()
    return db.get(Client, num.client_id) if num else None


def first_name(name: str | None) -> str:
    return (name or "").strip().split(" ")[0] or "there"


def money(cents: int) -> str:
    v = (cents or 0) / 100
    return f"${v:,.0f}" if v == int(v) else f"${v:,.2f}"


# ------------------------------------------------------------------ opt-outs + sending

def opted_out(db: Session, client: Client, phone: str) -> bool:
    return db.scalars(select(SmsOptOut).where(SmsOptOut.client_id == client.id, SmsOptOut.phone == phone)).first() is not None


def opt_out(db: Session, client: Client, phone: str) -> None:
    if not opted_out(db, client, phone):
        db.add(SmsOptOut(client_id=client.id, phone=phone))
    for t in db.scalars(select(SmsThread).where(SmsThread.client_id == client.id, SmsThread.phone == phone)):
        t.status = "opted_out"
    for q in db.scalars(select(Quote).where(Quote.client_id == client.id, Quote.phone == phone, Quote.next_touch_at.is_not(None))):
        q.next_touch_at = None
    for inv in db.scalars(select(Invoice).where(Invoice.client_id == client.id, Invoice.phone == phone, Invoice.status == "open")):
        inv.paused = True
    eventlog.record(db, "recovery", f"{phone} opted out of texts", client_id=client.id)


def send(db: Session, client: Client, thread: SmsThread, body: str) -> SmsMessage | None:
    """Text a customer from the client's recovery number and log it. Never raises: a failed text is
    recorded on the thread (and in the event log) so one bad number can't stop a sequence run."""
    if opted_out(db, client, thread.phone):
        return None
    num = recovery_number(client)
    msg = SmsMessage(client_id=client.id, thread_id=thread.id, direction="out", body=body)
    try:
        sid = messaging.send_sms(thread.phone, body, from_number=num.number if num else None)
        msg.sid = sid if sid and sid != "console" else None
        msg.status = "console" if sid == "console" else "sent"
    except http.ProviderError as e:
        msg.status, msg.error = "failed", str(e)[:300]
        if "21610" in e.detail or "21610" in str(e):  # Twilio: recipient replied STOP on this number
            opt_out(db, client, thread.phone)
        eventlog.record(db, "sms", f"Text to {thread.phone} failed: {e}", client_id=client.id, level="warn")
    except ValueError as e:
        msg.status, msg.error = "failed", str(e)[:300]
    db.add(msg)
    thread.last_outbound_at = utcnow()
    db.flush()
    return msg if msg.status != "failed" else None


def alert_owner(db: Session, client: Client, subject: str, body: str, *, urgent: bool = False,
                thread: SmsThread | None = None) -> None:
    """Email (and text, when the owner asked for texts or it's urgent) the business owner."""
    from ..config import get_settings
    from .calls import notify_owner

    link = f"{get_settings().base_url}/portal/recovery" + (f"/threads/{thread.id}" if thread else "")
    notify_owner(db, client, subject, body, link=link)
    if urgent and client.owner_phone and not client.notify_sms:
        try:
            num = recovery_number(client)
            messaging.send_sms(client.owner_phone, f"URGENT: {subject}. {body[:300]}", from_number=num.number if num else None)
        except Exception as e:
            eventlog.record(db, "notify", f"Urgent owner text failed: {e}", client_id=client.id, level="warn")


# ------------------------------------------------------------------ send window (follow-ups only)

def _hm(s: str | None, default: str) -> time:
    try:
        h, m = (s or default).split(":")[:2]
        return time(int(h) % 24, int(m) % 60)
    except ValueError:
        h, m = default.split(":")
        return time(int(h), int(m))


def in_window(client: Client, now: datetime | None = None) -> bool:
    local = (now or utcnow()).astimezone(scheduling.tz_of(client))
    return _hm(client.send_window_start, "08:30") <= local.time() < _hm(client.send_window_end, "19:30")


def next_window(client: Client, after: datetime) -> datetime:
    """The earliest moment at/after `after` that is inside the client's send window (local time)."""
    tz = scheduling.tz_of(client)
    local = after.astimezone(tz)
    start, end = _hm(client.send_window_start, "08:30"), _hm(client.send_window_end, "19:30")
    if local.time() < start:
        local = datetime.combine(local.date(), start, tz)
    elif local.time() >= end:
        local = datetime.combine(local.date() + timedelta(days=1), start, tz)
    return local.astimezone(timezone.utc)


# ------------------------------------------------------------------ ledger

def claim(db: Session, client: Client, kind: str, ref: str, amount_cents: int, *, confirmed: bool,
          fee_eligible: bool, note: str | None = None) -> RecoveryEvent:
    ev = db.scalars(select(RecoveryEvent).where(RecoveryEvent.ref == ref)).first()
    if ev:
        ev.amount_cents, ev.confirmed, ev.fee_eligible, ev.void = amount_cents, confirmed, fee_eligible, False
        ev.note = note or ev.note
        return ev
    ev = RecoveryEvent(client_id=client.id, kind=kind, ref=ref, amount_cents=amount_cents, confirmed=confirmed,
                       fee_eligible=fee_eligible, note=note)
    db.add(ev)
    db.flush()
    return ev


def void_claim(db: Session, ref: str) -> None:
    ev = db.scalars(select(RecoveryEvent).where(RecoveryEvent.ref == ref)).first()
    if ev:
        ev.void = True


# ------------------------------------------------------------------ threads

def open_thread_for(db: Session, client: Client, phone: str) -> SmsThread | None:
    """The conversation a reply belongs to: the one we most recently texted this person from."""
    rows = db.scalars(select(SmsThread).where(SmsThread.client_id == client.id, SmsThread.phone == phone,
                                              SmsThread.status.not_in(("opted_out",)))).all()
    if not rows:
        return None
    return max(rows, key=lambda t: aware(t.last_outbound_at or t.created_at))


def start_missed_call(db: Session, client: Client, caller: str | None, *, origin: str = "missed",
                      call_sid: str | None = None) -> SmsThread | None:
    """A call went unanswered: open (or reuse) a text conversation and send the opener."""
    from . import textback

    phone = messaging.normalize_phone(caller)
    if not phone:
        eventlog.record(db, "recovery", f"Missed call from a hidden or invalid number ({caller or 'unknown'})",
                        client_id=client.id)
        return None
    rc = db.scalars(select(RecoveryCall).where(RecoveryCall.call_sid == call_sid)).first() if call_sid else None
    if opted_out(db, client, phone):
        return None
    since = utcnow() - timedelta(hours=DEDUPE_HOURS)
    recent = db.scalars(select(SmsThread).where(SmsThread.client_id == client.id, SmsThread.phone == phone,
                                                SmsThread.kind == "missed_call", SmsThread.created_at >= since)).first()
    if recent:  # they're redialling: one conversation, no second opener
        if rc:
            rc.thread_id = recent.id
        recent.data = {**(recent.data or {}), "calls": int((recent.data or {}).get("calls", 1)) + 1}
        return recent
    thread = SmsThread(client_id=client.id, phone=phone, kind="missed_call", status="texted", step="service",
                       data={"origin": origin, "calls": 1})
    db.add(thread)
    db.flush()
    if rc:
        rc.thread_id = thread.id
    send(db, client, thread, textback.opener(client, origin))
    return thread


def handle_inbound(db: Session, client: Client, from_number: str, body: str, media: list[str] | None = None,
                   sid: str | None = None) -> SmsThread | None:
    """A customer texted the recovery number. Keywords first (compliance), then the right conversation."""
    from . import sequences, textback

    phone = messaging.normalize_phone(from_number)
    if not phone:
        return None
    if sid and db.scalars(select(SmsMessage).where(SmsMessage.sid == sid)).first():
        return None  # Twilio retried the webhook
    text = (body or "").strip()
    word = re.sub(r"[^\w]", "", text.lower())
    thread = open_thread_for(db, client, phone)
    if word in STOP_WORDS:
        _log_in(db, client, thread, text, media, sid)
        opt_out(db, client, phone)  # Twilio sends the carrier-required confirmation itself
        return thread
    if opted_out(db, client, phone):
        if word in START_WORDS:
            db.query(SmsOptOut).filter(SmsOptOut.client_id == client.id, SmsOptOut.phone == phone).delete()
            eventlog.record(db, "recovery", f"{phone} opted back in", client_id=client.id)
        _log_in(db, client, thread, text, media, sid)
        return thread
    if word in HELP_WORDS:
        _log_in(db, client, thread, text, media, sid)
        return thread
    stale = thread is not None and thread.kind == "missed_call" and thread.status in ("closed", "no_reply") \
        and aware(thread.updated_at) < utcnow() - timedelta(days=7)
    if thread is None or stale:
        # A new conversation: they texted the number directly (e.g. it's on the website or truck).
        thread = SmsThread(client_id=client.id, phone=phone, kind="missed_call", status="engaged", step="service",
                           data={"origin": "inbound_text", "calls": 0})
        db.add(thread)
        db.flush()
    _log_in(db, client, thread, text, media, sid)
    thread.last_inbound_at = utcnow()
    if thread.status in ("handed_off",):
        alert_owner(db, client, f"New text from {thread.data.get('name') or phone}", text, thread=thread)
        return thread
    if thread.kind == "quote" and thread.quote_id:
        sequences.quote_reply(db, client, thread, db.get(Quote, thread.quote_id), text)
    elif thread.kind == "invoice" and thread.invoice_id:
        sequences.invoice_reply(db, client, thread, db.get(Invoice, thread.invoice_id), text)
    else:
        textback.handle(db, client, thread, text, media or [])
    return thread


def _log_in(db, client, thread, text, media, sid):
    db.add(SmsMessage(client_id=client.id, thread_id=thread.id if thread else None, direction="in", body=text,
                      media=list(media or []), sid=sid, status="received"))
    db.flush()


def owner_reply(db: Session, client: Client, thread: SmsThread, body: str) -> SmsMessage | None:
    """The owner texts the customer from the dashboard. A human took over: automation on this thread stops."""
    msg = send(db, client, thread, body.strip()[:1000])
    if thread.kind == "missed_call" and thread.status in ACTIVE:
        thread.status = "handed_off"
    if thread.quote_id:
        q = db.get(Quote, thread.quote_id)
        if q:
            q.paused, q.next_touch_at = True, None
    if thread.invoice_id:
        inv = db.get(Invoice, thread.invoice_id)
        if inv:
            inv.paused = True
    return msg


# ------------------------------------------------------------------ numbers for the dashboard

def month_start(client: Client, now: datetime | None = None) -> datetime:
    local = (now or utcnow()).astimezone(scheduling.tz_of(client))
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def stats(db: Session, client: Client, since: datetime | None = None) -> dict:
    since = since or month_start(client)
    calls = db.scalars(select(RecoveryCall).where(RecoveryCall.client_id == client.id, RecoveryCall.created_at >= since)).all()
    threads = db.scalars(select(SmsThread).where(SmsThread.client_id == client.id, SmsThread.kind == "missed_call",
                                                 SmsThread.created_at >= since)).all()
    missed_threads = [t for t in threads if (t.data or {}).get("origin") in ("missed", "vapi_missed")]
    events = db.scalars(select(RecoveryEvent).where(RecoveryEvent.client_id == client.id, RecoveryEvent.void.is_(False),
                                                    RecoveryEvent.created_at >= since)).all()
    now = utcnow()
    quotes_open = db.scalars(select(Quote).where(Quote.client_id == client.id, Quote.status == "open")).all()
    overdue = [i for i in db.scalars(select(Invoice).where(Invoice.client_id == client.id, Invoice.status == "open"))
               if aware(i.due_date) < now]

    def total(kinds, **flags):
        return sum(e.amount_cents for e in events if e.kind in kinds and all(getattr(e, k) == v for k, v in flags.items()))

    vapi_missed = sum(1 for t in missed_threads if t.data.get("origin") == "vapi_missed")
    return {
        "since": since,
        "calls": len(calls), "missed_calls": sum(1 for c in calls if not c.answered) + vapi_missed,
        "texted": len(missed_threads),
        "engaged": sum(1 for t in missed_threads if t.last_inbound_at),
        "qualified": sum(1 for t in missed_threads if t.status in ("qualified", "booked", "emergency", "handed_off")),
        "booked": sum(1 for t in missed_threads if t.status == "booked"),
        "emergencies": sum(1 for t in missed_threads if t.status == "emergency"),
        "missed_value_est": total(("missed_call_booked",), confirmed=False),
        "missed_value_confirmed": total(("missed_call_booked",), confirmed=True),
        "quotes_open": len(quotes_open), "quotes_open_cents": sum(q.amount_cents for q in quotes_open),
        "quotes_recovered": sum(1 for e in events if e.kind == "quote_recovered"),
        "quotes_recovered_cents": total(("quote_recovered",)),
        "overdue_count": len(overdue), "overdue_cents": sum(i.amount_cents - (i.paid_cents or 0) for i in overdue),
        "invoices_recovered_cents": total(("invoice_recovered",)),
        "invoices_on_time_cents": total(("invoice_on_time",)),
        "recovered_confirmed": sum(e.amount_cents for e in events if e.confirmed and e.kind != "invoice_on_time"),
        "recovered_estimated": total(("missed_call_booked",), confirmed=False),
        "fee_base": sum(e.amount_cents for e in events if e.fee_eligible and e.confirmed),
        "fee_cents": round(sum(e.amount_cents for e in events if e.fee_eligible and e.confirmed)
                           * (client.performance_fee_pct or 0) / 100),
    }


def unconfirmed_bookings(db: Session, client: Client) -> list[tuple[RecoveryEvent, SmsThread | None]]:
    rows = db.scalars(select(RecoveryEvent).where(RecoveryEvent.client_id == client.id, RecoveryEvent.void.is_(False),
                                                  RecoveryEvent.kind == "missed_call_booked",
                                                  RecoveryEvent.confirmed.is_(False))
                      .order_by(RecoveryEvent.created_at.desc()).limit(50)).all()
    out = []
    for ev in rows:
        tid = int(ev.ref.split(":")[1]) if ev.ref.startswith("thread:") else None
        out.append((ev, db.get(SmsThread, tid) if tid else None))
    return out


def confirm_job_value(db: Session, client: Client, event_id: int, amount_cents: int) -> RecoveryEvent:
    ev = db.get(RecoveryEvent, event_id)
    if not ev or ev.client_id != client.id:
        raise ValueError("Not found")
    if amount_cents <= 0:  # the booking didn't turn into a job: no claim
        ev.void = True
    else:
        ev.amount_cents, ev.confirmed, ev.fee_eligible = amount_cents, True, True
    return ev


def counts(db: Session, client: Client) -> dict:
    return {
        "threads": db.scalar(select(func.count()).select_from(SmsThread).where(SmsThread.client_id == client.id)) or 0,
        "quotes": db.scalar(select(func.count()).select_from(Quote).where(Quote.client_id == client.id)) or 0,
        "invoices": db.scalar(select(func.count()).select_from(Invoice).where(Invoice.client_id == client.id)) or 0,
    }


# ------------------------------------------------------------------ scheduler

def tick(db: Session, now: datetime | None = None) -> dict:
    """Every minute: nudge silent missed-call threads, send due quote follow-ups and invoice reminders.
    Each client runs in its own savepoint so one client's bad data can't block the others."""
    from . import sequences, textback

    now = now or utcnow()
    done = {"nudges": 0, "quotes": 0, "invoices": 0}
    for client in db.scalars(select(Client).where(Client.recovery_enabled.is_(True), Client.status.in_(("active", "demo")))):
        try:
            with db.begin_nested():
                done["nudges"] += textback.nudge_due(db, client, now)
                done["quotes"] += sequences.run_quotes(db, client, now)
                done["invoices"] += sequences.run_invoices(db, client, now)
        except Exception as e:
            eventlog.record(db, "recovery", f"Recovery run failed: {e}", client_id=client.id, level="error")
    return done
