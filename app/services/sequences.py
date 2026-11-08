"""Quote follow-up (+ deposit) and invoice reminders: timed texts that stop the moment a person replies.

Replies become typed decisions (intent + confidence), the same pattern as outreach replies: confident ones
are applied (an explicit "yes, go ahead" accepts the quote), everything else goes to the owner with the
customer's own words. Nothing here ever negotiates price or promises a date on the owner's behalf.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Client, Invoice, Quote, SmsThread
from . import eventlog, llm, messaging, recovery, scheduling

QUOTE_STEPS = [
    "Hi {first}, it's {business}. Just checking that you received the estimate for {desc} ({amount}). "
    "Any questions, just reply here. Reply STOP to opt out.",
    "Hi {first}, any questions about the {desc} estimate? Happy to walk you through it.",
    "Hi {first}, would you like us to check availability for next week for the {desc}? Reply YES and we'll get you on the schedule.",
    "Hi {first}, last check-in from {business} on the {desc} estimate. If now's not the right time, no problem. "
    "Just reply if you'd like to go ahead.",
]
INVOICE_TEXT = {
    "before": "Hi {first}, friendly reminder from {business}: invoice {number} for {amount} is due {due}. {pay}",
    "due": "Hi {first}, invoice {number} from {business} for {amount} is due today. {pay}",
    "late": "Hi {first}, invoice {number} from {business} for {amount} is now {late} days past due. "
            "If you've already paid, thank you, just reply PAID. {pay}",
    "last": "Hi {first}, invoice {number} from {business} for {amount} is {late} days past due. Please pay today, "
            "or reply to set up a payment plan. {pay}",
}

QUOTE_RULES = [  # order matters: a decline or a question beats a stray "yes"
    ("decline", 0.85, r"\b(no thanks|no thank you|not interested|went with (someone|another|a different)|decided not|"
                      r"too (expensive|much|high|pricey)|hired someone|found someone|not going ahead|not proceeding|we'?ll pass|pass for now|"
                      r"don'?t (go ahead|proceed)|hold off)\b"),
    ("hold", 0.8, r"\b(think about|let you know|get back to you|discuss (it )?with|talk (to|with) my|not (yet|sure)|"
                  r"maybe|still deciding|comparing|other quotes)\b"),
    ("decline", 0.8, r"^\s*(no|nope|nah)[.!]*\s*$"),
    # An explicit go-ahead wins even with a question attached ("Yes let's go ahead, can you start Tuesday?").
    ("accept", 0.85, r"\b(let'?s (do it|go ahead|book it|proceed)|go ahead|book (it|me|us) in|we'?ll take it|"
                     r"approved?|accept(ed)?|please proceed|green light)\b"),
    ("question", 0.8, r"\?|\b(how (much|long)|what (about|if|does)|can you|could you|is (it|that|there)|do you|does (it|that))\b"),
    ("accept", 0.8, r"^\s*(yes|yep|yeah|yup|sure|deal)\b|\b(sounds good|i'?m in|works for (me|us))\b"),
    ("interested", 0.75, r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|next week|this week|tomorrow|"
                         r"availability|schedule|start date|when can)\b"),
]
INVOICE_RULES = [
    ("promise", 0.8, r"\b(haven'?t|have not|not yet|didn'?t|did not|can'?t|cannot|will) (yet )?(been )?(paid|pay)\b"),
    ("dispute", 0.8, r"\b(wrong|incorrect|dispute|not (right|correct)|overcharg|didn'?t (finish|complete)|issue with|not happy)\b"),
    ("paid", 0.85, r"\b(paid|sent (it|the|an|payment|e-?transfer|money)|e-?transfer(red)?|already sent|payment (sent|made)|"
                   r"just paid|mailed (a|the)? ?cheque|cheque in the mail)\b"),
    ("promise", 0.75, r"\b(payment plan|installments?|pay (it )?(in|over|on|by|next)|next (week|friday|month)|on payday|"
                      r"by (monday|tuesday|wednesday|thursday|friday|the end))\b"),
]
QUOTE_PROMPT = ('A customer replied by text to a contractor\'s follow-up about a quote. Return {"intent": one of '
                '"accept","decline","question","interested","unclear", "confidence": 0..1}. accept = clearly agrees to go '
                'ahead with the quote; interested = positive but not a clear yes (asks about timing); question = asks '
                'something; decline = says no. Be calibrated.')
INVOICE_PROMPT = ('A customer replied by text to an invoice reminder. Return {"intent": one of "paid","dispute","promise",'
                  '"question","unclear", "confidence": 0..1}. paid = says they already paid; promise = says when they will '
                  'pay or asks for a plan; dispute = disagrees with the invoice.')
APPLY_AT = 0.75


def _classify(text: str, rules: list, prompt: str) -> tuple[str, float]:
    t = (text or "").strip()
    for intent, conf, rx in rules:
        if re.search(rx, t, re.I):
            return intent, conf
    if llm.available() and t:
        try:
            got = llm.complete_json(prompt, t[:1000], temperature=0)
            return str(got.get("intent") or "unclear"), float(got.get("confidence") or 0)
        except Exception:
            pass
    return "unclear", 0.3


def _thread(db: Session, client: Client, kind: str, phone: str, **link) -> SmsThread:
    key = SmsThread.quote_id if kind == "quote" else SmsThread.invoice_id
    t = db.scalars(select(SmsThread).where(SmsThread.client_id == client.id, key == list(link.values())[0])).first()
    if not t:
        rec = db.get(Quote if kind == "quote" else Invoice, list(link.values())[0])
        name = getattr(rec, "customer_name", None)
        t = SmsThread(client_id=client.id, phone=phone, kind=kind, status="texted", data={"name": name} if name else {}, **link)
        db.add(t)
        db.flush()
    return t


# ====================================================================== quotes

def _quote_days(client: Client) -> list[int]:
    days = [int(d) for d in (client.quote_followup_days or [1, 3, 7, 14]) if str(d).lstrip("-").isdigit()]
    return sorted(d for d in days if d > 0)[: len(QUOTE_STEPS)] or [1, 3, 7, 14]


def schedule_quote(client: Client, q: Quote) -> None:
    days = _quote_days(client)
    if q.followups_sent < len(days):
        due = recovery.aware(q.sequence_start) + timedelta(days=days[q.followups_sent])
        q.next_touch_at = recovery.next_window(client, due)
    else:  # final check-in sent: give them a week before calling it
        q.next_touch_at = recovery.aware(q.last_touch_at or recovery.utcnow()) + timedelta(days=7)


def create_quote(db: Session, client: Client, *, amount_cents: int, phone: str | None, customer_name: str | None = None,
                 description: str | None = None, ref: str | None = None, email: str | None = None,
                 sent_at: datetime | None = None, deposit_cents: int | None = None, source: str = "manual") -> Quote:
    now = recovery.utcnow()
    sent_at = recovery.aware(sent_at) or now
    q = Quote(client_id=client.id, ref=(ref or None), customer_name=customer_name, email=email,
              phone=messaging.normalize_phone(phone), description=(description or "your project")[:300],
              amount_cents=max(0, int(amount_cents)), sent_at=sent_at, source=source, status="open",
              followups_sent=0, paused=False,
              deposit_cents=client.default_deposit_cents if deposit_cents is None else deposit_cents)
    age = now - sent_at
    # A quote sent today follows its own clock; an older imported one starts its sequence now.
    q.sequence_start = sent_at if age < timedelta(days=1) else now
    db.add(q)
    if age > timedelta(days=client.quote_max_age_days or 60):
        q.status = "stale"
    elif q.phone:
        schedule_quote(client, q)
    db.flush()
    return q


def run_quotes(db: Session, client: Client, now: datetime) -> int:
    sent = 0
    rows = db.scalars(select(Quote).where(Quote.client_id == client.id, Quote.status == "open", Quote.paused.is_(False),
                                          Quote.next_touch_at.is_not(None), Quote.next_touch_at <= now)).all()
    days = _quote_days(client)
    for q in rows:
        if q.followups_sent >= len(days):
            q.status, q.next_touch_at = "no_response", None
            continue
        if not q.phone or recovery.opted_out(db, client, q.phone):
            q.next_touch_at = None
            continue
        if not recovery.in_window(client, now):
            q.next_touch_at = recovery.next_window(client, now)
            continue
        t = _thread(db, client, "quote", q.phone, quote_id=q.id)
        # The last configured step always gets the "last check-in" wording, however many steps there are.
        i = q.followups_sent
        idx = len(QUOTE_STEPS) - 1 if i == len(days) - 1 else min(i, len(QUOTE_STEPS) - 2)
        body = QUOTE_STEPS[idx].format(first=recovery.first_name(q.customer_name), business=client.name,
                                       desc=q.description, amount=recovery.money(q.amount_cents))
        if i == 0 and "STOP" not in body:
            body += " Reply STOP to opt out."
        if recovery.send(db, client, t, body):
            sent += 1
        q.followups_sent += 1
        q.last_touch_at = now
        schedule_quote(client, q)
    return sent


def quote_reply(db: Session, client: Client, thread: SmsThread, q: Quote | None, text: str) -> None:
    if not q:
        return
    thread.status = "engaged"
    q.replied_at, q.last_reply, q.next_touch_at = recovery.utcnow(), text[:1000], None  # any reply stops the sequence
    if q.status in ("accepted", "deposit_requested", "deposit_paid", "scheduled"):
        recovery.alert_owner(db, client, f"{q.customer_name or q.phone} texted about the {q.description}", text, thread=thread)
        return
    intent, conf = _classify(text, QUOTE_RULES, QUOTE_PROMPT)
    head = f"{recovery.money(q.amount_cents)} quote ({q.description}), {q.customer_name or q.phone}"
    if intent == "accept" and conf >= APPLY_AT:
        accept_quote(db, client, q, via="text", thread=thread)
        recovery.alert_owner(db, client, f"{head}: accepted", f"They replied: \"{text[:400]}\"", thread=thread)
        return
    if intent == "decline" and conf >= APPLY_AT:
        q.status = "declined"
        recovery.send(db, client, thread, "Thanks for letting us know. If anything changes, just reply here.")
        recovery.alert_owner(db, client, f"{head}: declined", f"They replied: \"{text[:400]}\"", thread=thread)
        return
    who = client.owner_name.split(" ")[0] if client.owner_name else "the team"
    recovery.send(db, client, thread, f"Thanks! I've passed this to {who}, who'll get back to you shortly.")
    label = {"interested": "replied, interested", "question": "has a question", "hold": "is still deciding"}.get(intent, "replied")
    recovery.alert_owner(db, client, f"{head}: {label}", f"They replied: \"{text[:400]}\"", thread=thread)


def accept_quote(db: Session, client: Client, q: Quote, *, via: str, thread: SmsThread | None = None) -> None:
    now = recovery.utcnow()
    q.status, q.accepted_at, q.next_touch_at = "accepted", now, None
    if q.followups_sent >= 1:
        recovery.claim(db, client, "quote_recovered", f"quote:{q.id}", q.amount_cents, confirmed=True, fee_eligible=True,
                       note=f"Accepted by {via} after {q.followups_sent} follow-up(s)")
    if not q.phone:
        return
    thread = thread or _thread(db, client, "quote", q.phone, quote_id=q.id)
    if q.deposit_cents and q.deposit_cents > 0:
        pay = (f" Pay here: {client.deposit_link}" if client.deposit_link else
               f" To pay: {client.payment_instructions}." if client.payment_instructions else
               " We'll send payment details shortly.")
        msg = (f"Great, your project with {client.name} is confirmed! A {recovery.money(q.deposit_cents)} deposit "
               f"reserves your spot on the schedule.{pay}")
        recovery.send(db, client, thread, msg)
        q.status, q.deposit_requested_at = "deposit_requested", now
    elif via == "text":
        recovery.send(db, client, thread, f"Great, thank you! {client.name} will be in touch to confirm your start date.")


def set_quote_status(db: Session, client: Client, q: Quote, action: str, when: datetime | None = None) -> None:
    """Owner actions from the dashboard."""
    if action == "accepted":
        accept_quote(db, client, q, via="owner")
    elif action == "deposit_paid":
        q.status, q.deposit_paid_at = "deposit_paid", recovery.utcnow()
    elif action == "scheduled":
        q.status, q.scheduled_for = "scheduled", recovery.aware(when)
    elif action in ("declined", "lost"):
        q.status, q.next_touch_at = action, None
        recovery.void_claim(db, f"quote:{q.id}")
    elif action == "pause":
        q.paused, q.next_touch_at = True, None
    elif action == "resume":
        q.paused = False
        if q.status == "open":
            schedule_quote(client, q)
    else:
        raise ValueError(f"Unknown action {action}")


# ====================================================================== invoices

def _due_local(client: Client, inv: Invoice) -> date:
    return recovery.aware(inv.due_date).astimezone(scheduling.tz_of(client)).date()


def due_from_date(client: Client, d: date) -> datetime:
    return datetime.combine(d, time(0), scheduling.tz_of(client)).astimezone(timezone.utc)


def create_invoice(db: Session, client: Client, *, amount_cents: int, due: date, phone: str | None,
                   customer_name: str | None = None, number: str | None = None, email: str | None = None,
                   payment_link: str | None = None, paid_cents: int = 0, source: str = "manual") -> Invoice:
    inv = Invoice(client_id=client.id, number=number or None, customer_name=customer_name, email=email,
                  phone=messaging.normalize_phone(phone), amount_cents=max(0, int(amount_cents)), paid_cents=paid_cents,
                  due_date=due_from_date(client, due), payment_link=payment_link or None, source=source, reminders_sent=[],
                  status="open", paused=False)
    db.add(inv)
    db.flush()
    return inv


def _days(client: Client) -> list[int]:
    out = sorted({int(d) for d in (client.invoice_reminder_days or [-3, 0, 3, 7, 14]) if str(d).lstrip("-").isdigit()})
    return out or [-3, 0, 3, 7, 14]


def run_invoices(db: Session, client: Client, now: datetime) -> int:
    sent = 0
    today = now.astimezone(scheduling.tz_of(client)).date()
    days = _days(client)
    for inv in db.scalars(select(Invoice).where(Invoice.client_id == client.id, Invoice.status == "open",
                                                Invoice.paused.is_(False), Invoice.escalated_at.is_(None))):
        elapsed = (today - _due_local(client, inv)).days
        if elapsed >= (client.invoice_escalate_days or 21):
            inv.escalated_at = now
            recovery.alert_owner(db, client, f"Invoice {inv.number or inv.id} is {elapsed} days overdue",
                                 f"{inv.customer_name or inv.phone or 'Customer'} still owes {recovery.money(_balance(inv))}. "
                                 "Automated reminders have stopped: a personal call works best from here.")
            continue
        handled = {r["offset"] for r in inv.reminders_sent or []}
        due_now = [o for o in days if o <= elapsed and o not in handled]
        if not due_now or not inv.phone or recovery.opted_out(db, client, inv.phone):
            continue
        if not recovery.in_window(client, now):
            continue
        offset = max(due_now)  # after downtime or a late import: send only the reminder that fits today
        t = _thread(db, client, "invoice", inv.phone, invoice_id=inv.id)
        body = _invoice_text(client, inv, offset, last=offset == max(days))
        ok = recovery.send(db, client, t, body) is not None
        log = list(inv.reminders_sent or [])
        log += [{"offset": o, "sent": ok and o == offset, "at": now.isoformat()} for o in due_now]
        inv.reminders_sent, inv.last_touch_at = log, now
        sent += int(ok)
    return sent


def _balance(inv: Invoice) -> int:
    return max(0, (inv.amount_cents or 0) - (inv.paid_cents or 0))


def _invoice_text(client: Client, inv: Invoice, offset: int, last: bool) -> str:
    pay_url = inv.payment_link or client.deposit_link
    pay = f"Pay here: {pay_url}" if pay_url else (f"To pay: {client.payment_instructions}." if client.payment_instructions else "")
    key = "before" if offset < 0 else "due" if offset == 0 else ("last" if last else "late")
    due_day = _due_local(client, inv)
    text = INVOICE_TEXT[key].format(first=recovery.first_name(inv.customer_name), business=client.name,
                                    number=f"#{inv.number}" if inv.number else "", amount=recovery.money(_balance(inv)),
                                    due=f"{due_day.strftime('%a %b')} {due_day.day}", late=offset, pay=pay).strip()
    text = re.sub(r"\s{2,}", " ", text.replace("invoice  ", "invoice "))
    if not any(r.get("sent") for r in inv.reminders_sent or []):
        text += " Reply STOP to opt out."
    return text


def invoice_reply(db: Session, client: Client, thread: SmsThread, inv: Invoice | None, text: str) -> None:
    if not inv:
        return
    thread.status = "engaged"
    inv.last_reply = text[:1000]
    intent, conf = _classify(text, INVOICE_RULES, INVOICE_PROMPT)
    head = f"Invoice {('#' + inv.number) if inv.number else inv.id} ({recovery.money(_balance(inv))}), {inv.customer_name or inv.phone}"
    who = client.owner_name.split(" ")[0] if client.owner_name else "the team"
    if intent == "paid" and conf >= APPLY_AT:
        inv.customer_says_paid_at, inv.paused = recovery.utcnow(), True
        recovery.send(db, client, thread, "Thank you! We'll confirm once it comes through.")
        recovery.alert_owner(db, client, f"{head}: customer says it's paid",
                             f"They replied: \"{text[:400]}\". Mark it paid in the dashboard once it lands.", thread=thread)
        return
    if intent == "dispute" and conf >= APPLY_AT:
        inv.paused = True
        inv.status = "disputed"
    elif intent == "promise":
        inv.paused = True  # a stated plan deserves a person, not another automated nudge
    recovery.send(db, client, thread, f"Thanks, I've passed this to {who}, who'll get back to you shortly.")
    recovery.alert_owner(db, client, f"{head}: {'disputed' if intent == 'dispute' else 'customer replied'}",
                         f"They replied: \"{text[:400]}\"", thread=thread)


def mark_invoice_paid(db: Session, client: Client, inv: Invoice, paid_cents: int | None = None,
                      when: datetime | None = None) -> None:
    when = recovery.aware(when) or recovery.utcnow()
    inv.paid_cents = inv.amount_cents if paid_cents is None else paid_cents
    inv.status, inv.paid_at = "paid", when
    sent = [r for r in inv.reminders_sent or [] if r.get("sent") and datetime.fromisoformat(r["at"]) <= when]
    overdue_sent = [r for r in sent if r["offset"] > 0]
    if overdue_sent:
        recovery.claim(db, client, "invoice_recovered", f"invoice:{inv.id}", inv.paid_cents, confirmed=True,
                       fee_eligible=True, note=f"Paid after {len(overdue_sent)} overdue reminder(s)")
    elif sent:
        recovery.claim(db, client, "invoice_on_time", f"invoice:{inv.id}", inv.paid_cents, confirmed=True,
                       fee_eligible=False, note="Paid on time after a reminder")


def set_invoice_status(db: Session, client: Client, inv: Invoice, action: str) -> None:
    if action == "paid":
        mark_invoice_paid(db, client, inv)
    elif action == "void":
        inv.status = "void"
        recovery.void_claim(db, f"invoice:{inv.id}")
    elif action == "pause":
        inv.paused = True
    elif action == "resume":
        inv.paused = False
        if inv.status == "disputed":
            inv.status = "open"
    else:
        raise ValueError(f"Unknown action {action}")


# ====================================================================== CSV import

ALIASES = {
    "ref": ("quote", "quote #", "quote number", "estimate", "estimate #", "estimate number", "number", "ref", "#", "id"),
    "number": ("invoice", "invoice #", "invoice number", "invoice no", "number", "ref", "#", "id"),
    "customer_name": ("customer", "customer name", "client", "client name", "name", "contact"),
    "phone": ("phone", "phone number", "mobile", "cell", "customer phone", "telephone"),
    "email": ("email", "e-mail", "customer email"),
    "description": ("description", "job", "job title", "service", "title", "summary", "project", "line items"),
    "amount": ("amount", "total", "quote total", "estimate total", "balance", "balance due", "amount due", "grand total", "price"),
    "date": ("date", "sent", "sent date", "quote date", "estimate date", "created", "issued", "issue date", "invoice date"),
    "due": ("due", "due date", "due on", "payment due"),
    "link": ("payment link", "pay link", "link", "url"),
}


def parse_money(v: str | None) -> int | None:
    s = re.sub(r"[^\d.\-]", "", str(v or ""))
    if not s or s in ("-", "."):
        return None
    try:
        return round(float(s) * 100)
    except ValueError:
        return None


def parse_date(v: str | None) -> date | None:
    s = str(v or "").strip()
    if not s:
        return None
    # Day-first (31/12/2026) is ambiguous with month-first: we read slashes as month/day like QuickBooks
    # Online and Jobber's North American exports. ISO dates are unambiguous and preferred.
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt).date()
        except ValueError:
            continue
    return None


def _mapping(headers: list[str], keys: tuple[str, ...]) -> dict:
    norm = {h: re.sub(r"\s+", " ", (h or "").strip().lower()) for h in headers}
    out = {}
    for key in keys:
        for alias in ALIASES[key]:
            hit = next((h for h, n in norm.items() if n == alias and h not in out.values()), None)
            if hit:
                out[key] = hit
                break
    return out


def import_csv(db: Session, client: Client, raw: bytes | str, kind: str) -> dict:
    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    keys = ("ref", "customer_name", "phone", "email", "description", "amount", "date") if kind == "quote" else \
           ("number", "customer_name", "phone", "email", "amount", "due", "date", "link")
    m = _mapping(headers, keys)
    if "amount" not in m or (kind == "invoice" and "due" not in m):
        need = "an amount column" + (" and a due date column" if kind == "invoice" else "")
        raise ValueError(f"Couldn't find {need}. Columns seen: {', '.join(headers) or 'none'}.")
    model, ref_key = (Quote, "ref") if kind == "quote" else (Invoice, "number")
    existing = {getattr(r, ref_key) for r in db.scalars(select(model).where(model.client_id == client.id))
                if getattr(r, ref_key)}
    result = {"created": 0, "skipped": 0, "errors": []}
    for i, row in enumerate(reader, start=2):
        get = lambda k: (row.get(m[k]) or "").strip() if k in m else ""  # noqa: E731
        amount = parse_money(get("amount"))
        ref = get(ref_key) or None
        if amount is None:
            result["errors"].append(f"Row {i}: no amount")
            continue
        if ref and ref in existing:
            result["skipped"] += 1
            continue
        if kind == "quote":
            sent = parse_date(get("date"))
            create_quote(db, client, amount_cents=amount, phone=get("phone"), customer_name=get("customer_name") or None,
                         description=get("description") or None, ref=ref, email=get("email") or None, source="csv",
                         sent_at=due_from_date(client, sent) if sent else None)
        else:
            due = parse_date(get("due"))
            if not due:
                result["errors"].append(f"Row {i}: unreadable due date '{get('due')}'")
                continue
            create_invoice(db, client, amount_cents=amount, due=due, phone=get("phone"),
                           customer_name=get("customer_name") or None, number=ref, email=get("email") or None,
                           payment_link=get("link") or None, source="csv")
        if ref:
            existing.add(ref)
        result["created"] += 1
    if result["errors"]:
        eventlog.record(db, "recovery", f"{kind} import: {len(result['errors'])} rows skipped", client_id=client.id,
                        level="warn", errors=result["errors"][:20])
    return result
