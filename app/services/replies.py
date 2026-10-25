"""Inbound replies to outreach. Every reply becomes a typed decision (intent + confidence),
not free text: confident decisions are applied, unsure ones are handed to you.

Without this, the only way the system knew someone replied was you changing their status by
hand, so the learning loop starved. Point your inbound-mail provider (Postmark, SendGrid Inbound
Parse, Mailgun routes, a Cloudflare Email Worker...) at /webhooks/inbound-email/<token>."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import EmailDraft, InboundEmail, Prospect, Suppression
from . import eventlog, llm

INTENTS = ("interested", "question", "not_now", "not_interested", "unsubscribe", "auto_reply", "unclear")
POSITIVE = ("interested", "question")
APPLY_AT = 0.75        # confidence needed to change a prospect's status automatically
UNSUBSCRIBE_AT = 0.6   # lower bar: wrongly stopping email is cheap, wrongly continuing is not
FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com", "live.com", "msn.com",
             "proton.me", "protonmail.com", "gmx.com", "yahoo.ca", "rogers.com", "bell.net", "shaw.ca", "sympatico.ca"}

RULES = [
    ("unsubscribe", 0.9, r"\b(unsubscribe|remove me|stop (emailing|contacting|sending)|take me off|do not (contact|email)|don'?t (contact|email) (me|us))\b"),
    ("auto_reply", 0.85, r"\b(out of (the )?office|auto(matic)?[- ]?reply|on vacation|away until|undeliverable|delivery status notification|mailbox is full)\b"),
    ("not_interested", 0.75, r"\b(not interested|no thanks|no thank you|we'?re (all )?good|not for us|we already have|please don'?t)\b"),
    ("not_now", 0.7, r"\b(not right now|maybe later|next (month|year|quarter|season)|reach out (later|again)|circle back|too busy right now)\b"),
    ("question", 0.7, r"\b(how much|what does it cost|pricing|price|how does (it|this) work|can it|does it)\b"),
    ("interested", 0.75, r"\b(interested|sounds good|let'?s (talk|chat)|call me|give me a call|book (a|some) time|set it up|sign (me|us) up|tell me more|send (me )?(the )?(demo|info|details))\b"),
]

CLASSIFY_PROMPT = (
    "Classify a reply to a cold email offering an AI phone receptionist to a local business. "
    'Return {"intent": one of ' + ", ".join(f'"{i}"' for i in INTENTS) + ', "confidence": number 0..1, '
    '"reason": short phrase}. interested = wants to talk, try it or buy; question = asks something before deciding; '
    "not_now = maybe later; not_interested = declines; unsubscribe = asks to stop; auto_reply = out-of-office or bounce; "
    "unclear = can't tell. Be calibrated: use confidence below 0.75 when the reply is ambiguous."
)


def strip_quoted(text: str) -> str:
    """Keep only what the person wrote, not the quoted email below it."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith(">") or re.match(r"^On .{4,120} wrote:$", line) or re.match(r"^-{2,}\s*Original Message", line, re.I):
            break
        if re.match(r"^(From|Sent|To|Subject):\s", line) and out:
            break
        out.append(raw)
    return "\n".join(out).strip()


def classify(text: str) -> tuple[str, float, str]:
    body = strip_quoted(text)[:3000]
    if not body:
        return "unclear", 0.2, "rules"
    if llm.available():
        try:
            out = llm.complete_json(CLASSIFY_PROMPT, body, temperature=0)
            intent, conf = str(out.get("intent", "")).strip().lower(), float(out.get("confidence", 0))
            if intent in INTENTS and 0 <= conf <= 1:
                return intent, conf, "llm"
        except Exception:
            pass
    low = body.lower()
    for intent, conf, pattern in RULES:
        if re.search(pattern, low):
            return intent, conf, "rules"
    return "unclear", 0.3, "rules"


def _domain(value: str | None) -> str:
    if not value:
        return ""
    host = value.split("@")[-1] if "@" in value and "//" not in value else (urlparse(value if "//" in value else "//" + value).hostname or "")
    return host.lower().removeprefix("www.")


def match_prospect(db: Session, from_email: str) -> Prospect | None:
    email = from_email.lower().strip()
    p = db.scalars(select(Prospect).where(func.lower(Prospect.email) == email)).first()
    if p:
        return p
    dom = _domain(email)
    if not dom or dom in FREE_MAIL:
        return None
    # someone else at the same business replied (we emailed info@, the owner answered)
    candidates = [x for x in db.scalars(select(Prospect).where(Prospect.status.in_(["contacted", "demo", "new"]))).all()
                  if _domain(x.email) == dom or _domain(x.website) == dom]
    return candidates[0] if len(candidates) == 1 else None


def _stop_sequence(db: Session, p: Prospect) -> int:
    n = 0
    for d in db.scalars(select(EmailDraft).where(EmailDraft.prospect_id == p.id, EmailDraft.status == "draft")):
        d.status = "skipped"
        n += 1
    return n


def handle(db: Session, *, from_email: str, subject: str = "", text: str = "", message_id: str | None = None) -> InboundEmail:
    from_email = (re.search(r"[\w.+'-]+@[\w-]+(\.[\w-]+)+", from_email or "") or [""])[0].lower()
    if message_id:
        seen = db.scalars(select(InboundEmail).where(InboundEmail.message_id == message_id)).first()
        if seen:
            return seen
    p = match_prospect(db, from_email) if from_email else None
    intent, conf, method = classify(text)
    rec = InboundEmail(from_email=from_email or "unknown", subject=(subject or "")[:300], text=(text or "")[:20000],
                       prospect_id=p.id if p else None, intent=intent, confidence=round(conf, 2), method=method,
                       message_id=(message_id or None) and message_id[:300])
    db.add(rec)
    applied = "No matching prospect; saved for you."
    if p:
        now = datetime.now(timezone.utc)
        if intent != "auto_reply":
            # any human reply ends the automated follow-ups, whatever it says
            stopped = _stop_sequence(db, p)
            p.replied_at = p.replied_at or now
            applied = f"Stopped {stopped} follow-up{'s' if stopped != 1 else ''}."
        if intent == "unsubscribe" and conf >= UNSUBSCRIBE_AT:
            if p.email and not db.scalars(select(Suppression).where(Suppression.email == p.email.lower())).first():
                db.add(Suppression(email=p.email.lower(), reason="replied: unsubscribe"))
            p.status = "lost"
            applied = "Unsubscribed them and stopped all email."
        elif conf >= APPLY_AT and intent in POSITIVE and p.status not in ("won",):
            p.status = "replied"
            applied += " Marked replied."
        elif conf >= APPLY_AT and intent == "not_interested":
            p.status = "lost"
            applied += " Marked lost."
        elif intent == "auto_reply":
            applied = "Auto-reply; the sequence continues."
        else:
            applied += " Needs you: the reply wasn't clear enough to act on."
        needs_you = intent in POSITIVE or (intent in ("unclear", "not_now") or conf < APPLY_AT) and intent != "auto_reply"
        if needs_you:
            from .calls import alert_agency

            snippet = strip_quoted(text)[:600]
            alert_agency(db, f"{p.name} replied ({intent.replace('_', ' ')})",
                         f"From: {from_email}\nSubject: {subject}\n\n{snippet}\n\nWhat happened: {applied}",
                         dedupe_key=f"reply:{message_id or rec.from_email}:{now.date()}")
    rec.applied = applied[:200]
    eventlog.record(db, "reply", f"Reply from {from_email}: {intent} ({conf:.2f}, {method}). {applied}",
                    level="info", prospect_id=p.id if p else None)
    return rec


def parse_provider_payload(data: dict) -> dict:
    """Normalise Postmark / SendGrid / Mailgun / generic payloads to from, subject, text, message_id."""
    def first(*keys):
        for k in keys:
            v = data.get(k)
            if isinstance(v, dict):
                v = v.get("Email") or v.get("email") or v.get("address")
            if isinstance(v, list) and v:
                v = v[0].get("email") if isinstance(v[0], dict) else v[0]
            if v:
                return str(v)
        return ""

    headers = str(data.get("headers") or "")
    mid = first("MessageID", "message_id", "Message-Id", "message-id", "messageId")
    if not mid:
        m = re.search(r"^Message-I[Dd]:\s*(\S+)", headers, re.M)
        mid = m.group(1) if m else ""
    return {
        "from_email": first("FromFull", "from_email", "sender", "From", "from"),
        "subject": first("Subject", "subject"),
        "text": first("StrippedTextReply", "stripped-text", "TextBody", "body-plain", "text", "plain", "body"),
        "message_id": mid or None,
    }
