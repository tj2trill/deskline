"""Missed call → text conversation → qualified lead → booked estimate.

A small state machine decides what to ask next and writes every reply from fixed templates; the LLM
(when configured) only *extracts* fields from what the customer typed. That split is deliberate: the
model never promises a price, a time or a service the business didn't offer, and the flow still works
with no LLM at all (each answer fills the field that was just asked for).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Client, Contact, SmsThread
from . import eventlog, http, llm, recovery, scheduling, workflows

EMERGENCY = re.compile(
    r"\b(emergency|sparking|sparks|smoke(?!\s*(detector|alarm)s?)|smoking|burning smell|on fire|fire(?!\s*(alarm|place|pit))|"
    r"flood(ing|ed)?|burst|gushing|no (power|heat|hot water|water)|gas (smell|leak)|smells? (like )?gas|sewage|"
    r"backing up|shock(ed)?|exposed wires?)\b", re.I)
NEGATED = re.compile(r"\b(not|no|isn'?t|nothing|non)[\s-]+(an?\s+|too\s+|that\s+)?(emergency|urgent)\b", re.I)
GAS = re.compile(r"\bgas\b", re.I)
FIRE = re.compile(r"\b(fire|smoke|smoking|sparking|sparks|shock(ed)?|burning)\b", re.I)
WANTS_CALL = re.compile(r"\b(call me|give me a call|can (you|someone) call|rather (talk|call)|phone me|just call)\b", re.I)
SKIP = re.compile(r"^\s*(skip|no|nope|no photo|can'?t|later|n/?a)\b", re.I)
URGENCY_WORDS = [
    ("emergency", re.compile(r"^\s*1\b|\bemergenc", re.I)),
    ("soon", re.compile(r"^\s*2\b|\b(this week|soon|asap|quickly|few days|tomorrow)\b", re.I)),
    ("flexible", re.compile(r"^\s*3\b|\b(flexible|whenever|no rush|not urgent|any ?time|next (week|month))\b", re.I)),
]
FIELDS = ("service", "address", "urgency")


def is_emergency(text: str) -> bool:
    """Deterministic safety net, independent of the LLM. 'Not an emergency' and 'smoke detector install' don't count."""
    return bool(EMERGENCY.search(NEGATED.sub(" ", text or "")))

EXTRACT_PROMPT = (
    "You read text messages from a customer of a home-service business ({industry}). The business last asked the "
    "customer about: {step}. Known so far: {known}. Read the newest customer message and return "
    '{{"service": short description of the job or null, "address": street address or town or null, '
    '"name": the customer\'s name or null, "urgency": "emergency"|"soon"|"flexible"|null, '
    '"slot_choice": 1, 2 or 3 if they picked one of the offered times else null, '
    '"preferred_day": a day or date they asked for or null, "wants_call": true if they would rather talk on the phone, '
    '"skip_photo": true if they decline to send a photo, "question": a question they asked or null}}. '
    "Use only what the customer wrote. Never invent an address or a name."
)


def opener(client: Client, origin: str) -> str:
    if client.textback_opener:
        return client.textback_opener.replace("{business}", client.name)
    if origin == "vapi_missed":
        return f"Hi, it's {client.name}. Looks like we got cut off! What can we help you with? Reply STOP to opt out."
    return (f"Hi, it's {client.name}. Sorry we missed your call! We can help by text right now. "
            "What do you need help with? Reply STOP to opt out.")


def _ask(client: Client, step: str, data: dict) -> str:
    if step == "address":
        return "Got it. What's the address for the job? And your name, if you don't mind."
    if step == "urgency":
        return ("Thanks! How soon do you need someone? Reply 1 if it's an emergency (no power, sparking, leaking), "
                "2 for this week, or 3 if you're flexible.")
    if step == "photos":
        return "If you can, send a photo of the problem so we bring the right parts. Or reply SKIP."
    return "What do you need help with?"


# ------------------------------------------------------------------ extraction

def _rules(step: str, text: str, data: dict) -> dict:
    out: dict = {}
    t = text.strip()
    if WANTS_CALL.search(t):
        out["wants_call"] = True
    if step == "slot":
        m = re.match(r"^\s*(?:#|option\s*)?([1-3])\b", t, re.I)
        if m:
            out["slot_choice"] = int(m[1])
        else:
            try:
                scheduling.resolve_day(t, datetime.now(timezone.utc).date())
                out["preferred_day"] = t
            except scheduling.BookingError:
                pass
        return out
    if step == "urgency" or not data.get("urgency"):
        for level, rx in URGENCY_WORDS:
            if rx.search(NEGATED.sub(" not urgent ", t)):
                out["urgency"] = level
                break
    if step == "photos" and SKIP.match(t):
        out["skip_photo"] = True
    if step in ("service", "address") and t and not out.get("wants_call"):
        out[step] = t[:300]
    if step == "address" and "," in t:
        # We ask for "the address, and your name": split "Priya, 12 Oak St" / "12 Oak St, London, Priya".
        parts = [p.strip() for p in t.split(",") if p.strip()]
        namey = [p for p in parts if not re.search(r"\d", p) and 1 <= len(p.split()) <= 3 and p[:1].isupper()]
        for cand in (parts[0], parts[-1]):
            if cand in namey and len(parts) > 1 and not re.search(r"\b(st|street|rd|road|ave|dr|london|ontario|on)\b", cand, re.I):
                out["name"] = cand
                out["address"] = ", ".join(p for p in parts if p != cand)
                break
    return out


def extract(client: Client, thread: SmsThread, text: str) -> dict:
    data = thread.data or {}
    rules = _rules(thread.step or "service", text, data)
    if not llm.available():
        return rules
    known = {k: data.get(k) for k in ("service", "address", "name", "urgency") if data.get(k)}
    if thread.step == "slot":
        known["offered_times"] = [o["label"] for o in data.get("offered", [])]
    try:
        got = llm.complete_json(EXTRACT_PROMPT.format(industry=client.industry or "home services",
                                                      step=thread.step or "service", known=json.dumps(known)),
                                text[:1500], temperature=0)
    except Exception:  # the deterministic answer is still correct, just less clever
        return rules
    out = {k: v for k, v in got.items() if v not in (None, "", False, [])}
    if out.get("urgency") not in (None, "emergency", "soon", "flexible"):
        out.pop("urgency")
    if out.get("slot_choice") not in (None, 1, 2, 3):
        out.pop("slot_choice")
    return {**rules, **out}  # the model wins where it found something; rules fill the rest


# ------------------------------------------------------------------ the conversation

def handle(db: Session, client: Client, thread: SmsThread, text: str, media: list[str]) -> None:
    data = dict(thread.data or {})
    if media:
        data["photos"] = (data.get("photos") or []) + list(media)
    if thread.status in ("booked", "emergency", "handed_off", "closed"):
        thread.data = data
        _after_done(db, client, thread, text)
        return
    if thread.status in ("texted", "no_reply"):
        thread.status = "engaged"
    urgent_now = is_emergency(text)
    if urgent_now:
        data["urgency"] = "emergency"
    got = extract(client, thread, text) if text else {}
    step = thread.step or "service"
    for k in ("service", "address", "name", "urgency"):
        if got.get(k) and (not data.get(k) or k == step):
            data[k] = str(got[k])[:300]
    if got.get("skip_photo") or (step == "photos" and media):
        data["photo_asked"] = True
    if step == "photos":
        data["photo_asked"] = True
    if got.get("question"):
        data.setdefault("questions", []).append(str(got["question"])[:300])
    thread.data = data

    if got.get("wants_call"):
        return _hand_off(db, client, thread, "asked for a call")
    if data.get("urgency") == "emergency":
        if not data.get("service"):
            data["service"] = text[:300]
            thread.data = data
        return _emergency(db, client, thread, text)
    if step == "slot":
        return _slot_reply(db, client, thread, got)
    nxt = next((f for f in FIELDS if not data.get(f)), None)
    if not nxt and client.ask_photos and not data.get("photo_asked") and not data.get("photos"):
        nxt = "photos"
    if nxt:
        thread.step = nxt
        prefix = "Good question, the team will go over that with you. " if got.get("question") else ""
        recovery.send(db, client, thread, prefix + _ask(client, nxt, data))
        return
    _qualified(db, client, thread)


def _contact(db: Session, client: Client, thread: SmsThread) -> Contact:
    d = thread.data or {}
    if thread.contact_id and (c := db.get(Contact, thread.contact_id)):
        c.details = _summary(d)
        return c
    c = Contact(client_id=client.id, kind="lead", name=d.get("name"), phone=thread.phone, details=_summary(d),
                urgency="urgent" if d.get("urgency") == "emergency" else "normal", source="sms")
    db.add(c)
    db.flush()
    thread.contact_id = c.id
    workflows.run(db, "lead_captured", client, {"caller_name": c.name, "caller_number": c.phone, "message": c.details,
                                                "urgency": c.urgency, "source": "sms"})
    return c


def _summary(d: dict) -> str:
    parts = [d.get("service")]
    if d.get("address"):
        parts.append(f"at {d['address']}")
    if d.get("urgency"):
        parts.append(f"({ {'emergency': 'emergency', 'soon': 'this week', 'flexible': 'flexible'}.get(d['urgency'], d['urgency']) })")
    if d.get("photos"):
        parts.append(f"{len(d['photos'])} photo(s)")
    if d.get("questions"):
        parts.append("Asked: " + "; ".join(d["questions"][-2:]))
    return " ".join(p for p in parts if p)


def _qualified(db: Session, client: Client, thread: SmsThread) -> None:
    thread.status = "qualified"
    _contact(db, client, thread)
    if client.booking_mode == "none":
        return _hand_off(db, client, thread, "qualified", reply=(
            f"Thanks {recovery.first_name(thread.data.get('name'))}! I've passed this to the team at {client.name}. "
            "They'll call you shortly to set up a time."))
    offers = _offers(db, client)
    if not offers:
        return _hand_off(db, client, thread, "no open times", reply=(
            "Thanks! Our schedule is full online right now, so someone from the team will call you to find a time."))
    _offer(db, client, thread, offers, intro="Thanks! We can come out for an estimate:")


def _offers(db: Session, client: Client, day=None) -> list[dict]:
    today = scheduling.now_local(client).date()
    try:
        if day is not None:
            return [{"start": s.strftime("%Y-%m-%dT%H:%M"), "label": f"{s.strftime('%a %b')} {s.day}, {scheduling.fmt_time(s)}"}
                    for s in scheduling._spread(scheduling.slots_for(db, client, day), 3)]
        picks = []
        for i in range(0, min(client.booking_horizon_days or 21, 14) + 1):
            slots = scheduling.slots_for(db, client, today + timedelta(days=i))
            if slots:
                picks.append(slots[0])
            if len(picks) == 3:
                break
        return [{"start": s.strftime("%Y-%m-%dT%H:%M"), "label": f"{s.strftime('%a %b')} {s.day}, {scheduling.fmt_time(s)}"}
                for s in picks]
    except (http.ProviderError, http.NotConfigured, scheduling.BookingError) as e:
        eventlog.record(db, "recovery", f"Couldn't read availability: {e}", client_id=client.id, level="warn")
        return []


def _offer(db: Session, client: Client, thread: SmsThread, offers: list[dict], intro: str) -> None:
    thread.step = "slot"
    thread.data = {**thread.data, "offered": offers}
    lines = "\n".join(f"{i}) {o['label']}" for i, o in enumerate(offers, 1))
    choices = " or ".join(", ".join(str(i) for i in range(1, len(offers) + 1)).rsplit(", ", 1))
    recovery.send(db, client, thread, f"{intro}\n{lines}\nReply {choices}, or tell us a day that works.")


def _slot_reply(db: Session, client: Client, thread: SmsThread, got: dict) -> None:
    d = thread.data or {}
    offers = d.get("offered") or []
    choice = got.get("slot_choice")
    if choice and 1 <= int(choice) <= len(offers):
        return _book(db, client, thread, offers[int(choice) - 1])
    if got.get("preferred_day"):
        try:
            day = scheduling.resolve_day(str(got["preferred_day"]), scheduling.now_local(client).date())
        except scheduling.BookingError:
            day = None
        if day:
            picks = _offers(db, client, day)
            if picks:
                return _offer(db, client, thread, picks, intro=f"Here's what we have on {scheduling.fmt_day(day)}:")
            fallback = _offers(db, client)
            if fallback:
                return _offer(db, client, thread, fallback, intro="We're full that day. The next openings are:")
    recovery.send(db, client, thread, "Sorry, I didn't catch that. Reply with the number of a time above, "
                                      "or tell us a day that works, or say CALL ME.")


def _book(db: Session, client: Client, thread: SmsThread, offer: dict) -> None:
    d = thread.data or {}
    notes = "\n".join(x for x in [f"Address: {d.get('address')}" if d.get("address") else None,
                                  f"Urgency: {d.get('urgency')}" if d.get("urgency") else None,
                                  ("Photos: " + " ".join(d["photos"])) if d.get("photos") else None,
                                  "Booked by text after a missed call" if d.get("origin") != "inbound_text" else
                                  "Booked by text"] if x)
    try:
        b = scheduling.book(db, client, offer["start"], d.get("name") or "Text customer", thread.phone,
                            service=d.get("service"), notes=notes, source="sms")
    except scheduling.BookingError:
        offers = _offers(db, client)
        if offers:
            return _offer(db, client, thread, offers, intro="Sorry, that time was just taken. Here's what's open:")
        return _hand_off(db, client, thread, "booking failed", reply="Sorry, that time was just taken. "
                         "Someone from the team will call you to find another.")
    thread.booking_id, thread.status, thread.step = b.id, "booked", None
    _contact(db, client, thread).status = "handled"
    label = scheduling.local_label(client, b.start_at)
    where = f" at {d['address']}" if d.get("address") else ""
    recovery.send(db, client, thread, f"You're booked for {label}{where}. See you then! Reply here if anything changes.")
    if d.get("origin") in ("missed", "vapi_missed"):
        recovery.claim(db, client, "missed_call_booked", f"thread:{thread.id}", client.avg_job_value_cents or 0,
                       confirmed=False, fee_eligible=False, note=f"{d.get('service') or 'Job'} for {thread.phone}")
    recovery.alert_owner(db, client, f"Missed call recovered: {d.get('name') or thread.phone} booked {label}",
                         f"{d.get('name') or 'A caller'} ({thread.phone}) missed you, then booked by text.\n"
                         f"Job: {_summary(d)}\nWhen: {label}", thread=thread)
    workflows.run(db, "booking_created", client, {"caller_name": b.name, "caller_number": b.phone,
                                                  "booking_time": label, "service": b.service, "source": "sms"})


def _emergency(db: Session, client: Client, thread: SmsThread, text: str) -> None:
    thread.status, thread.step = "emergency", None
    _contact(db, client, thread)
    safety = ""
    if GAS.search(text):
        safety = "If you smell gas, leave the building now and call 911 or your gas utility from outside. "
    elif FIRE.search(text):
        safety = "If there's fire or smoke, or someone has been shocked, call 911 first. "
    who = client.owner_name.split(" ")[0] if client.owner_name else "the team"
    recovery.send(db, client, thread, f"{safety}We've flagged this as urgent and alerted {who}. Expect a call shortly.")
    d = thread.data or {}
    recovery.alert_owner(db, client, f"Emergency from {d.get('name') or thread.phone}",
                         f"Call {thread.phone} now. They wrote: \"{text[:300]}\"\nJob: {_summary(d)}",
                         urgent=True, thread=thread)


def _hand_off(db: Session, client: Client, thread: SmsThread, why: str, reply: str | None = None) -> None:
    thread.status, thread.step = "handed_off", None
    _contact(db, client, thread)
    recovery.send(db, client, thread, reply or f"No problem, someone from {client.name} will call you shortly.")
    d = thread.data or {}
    recovery.alert_owner(db, client, f"Call back {d.get('name') or thread.phone} ({why})",
                         f"{thread.phone}: {_summary(d) or 'no details yet'}", thread=thread)


def _after_done(db: Session, client: Client, thread: SmsThread, text: str) -> None:
    """They texted again after the flow finished: pass it to the owner; acknowledge at most every 10 minutes."""
    recovery.alert_owner(db, client, f"New text from {(thread.data or {}).get('name') or thread.phone}", text[:500], thread=thread)
    last = recovery.aware(thread.last_outbound_at)
    if not last or last < recovery.utcnow() - timedelta(minutes=10):
        recovery.send(db, client, thread, "Thanks, I've passed that on to the team.")


# ------------------------------------------------------------------ scheduler

def nudge_due(db: Session, client: Client, now: datetime) -> int:
    sent = 0
    rows = db.scalars(select(SmsThread).where(SmsThread.client_id == client.id, SmsThread.kind == "missed_call",
                                              SmsThread.status.in_(("texted", "engaged", "qualified")))).all()
    for t in rows:
        created = recovery.aware(t.created_at)
        if t.status == "texted":
            if created < now - timedelta(hours=48):
                t.status = "no_reply"
            elif not t.nudged and recovery.aware(t.last_outbound_at or created) <= now - timedelta(hours=2) \
                    and recovery.in_window(client, now):
                if recovery.send(db, client, t, "Still need a hand? Just reply here and we'll get you booked in."):
                    sent += 1
                t.nudged = True
        elif recovery.aware(t.last_inbound_at or created) < now - timedelta(hours=24):
            # They started, then went quiet: a person should take it from here.
            t.status = "handed_off"
            recovery.alert_owner(db, client, f"Unfinished lead: {(t.data or {}).get('name') or t.phone}",
                                 f"{t.phone} stopped replying mid-conversation. What we know: "
                                 f"{_summary(t.data or {}) or 'nothing yet'}", thread=t)
    return sent
