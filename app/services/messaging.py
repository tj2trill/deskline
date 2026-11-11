"""Outbound SMS (Twilio) and email (console / SMTP / Resend)."""
from __future__ import annotations

import logging
import re
import smtplib
from email.message import EmailMessage
from email.utils import parseaddr

from ..config import get_settings
from . import http

log = logging.getLogger("deskline.messaging")
OUTBOX: list[dict] = []  # last messages in console mode (shown in Settings, used by tests)


def normalize_phone(raw: str | None, default_country: str = "1") -> str | None:
    """Best-effort E.164 for North American numbers; leaves +international intact."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("+"):
        digits = "+" + re.sub(r"\D", "", raw)
        return digits if len(digits) >= 9 else None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+{default_country}{digits}"
    if len(digits) == 11 and digits.startswith(default_country):
        return f"+{digits}"
    return None


def sms_configured() -> bool:
    s = get_settings()
    return bool(s.twilio_account_sid and s.twilio_auth_token and s.twilio_from_number)


def send_sms(to: str, body: str, from_number: str | None = None) -> str:
    s = get_settings()
    to_e164 = normalize_phone(to)
    if not to_e164:
        raise ValueError(f"'{to}' is not a valid phone number")
    if not sms_configured():
        OUTBOX.append({"channel": "sms", "to": to_e164, "body": body})
        log.info("SMS (console) to %s: %s", to_e164, body)
        return "console"
    with http.client(timeout=20, auth=(s.twilio_account_sid, s.twilio_auth_token)) as c:
        r = c.post(f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json",
                   data={"To": to_e164, "From": from_number or s.twilio_from_number, "Body": body[:1500]})
        http.check("Twilio", r)
        return r.json().get("sid", "")


def send_email(to: str, subject: str, html: str, text: str | None = None, reply_to: str | None = None,
               headers: dict | None = None) -> str:
    s = get_settings()
    if not to or "@" not in to or any(ch in to for ch in "\r\n,;<> "):
        raise ValueError(f"'{to}' is not a valid email address")
    subject = re.sub(r"[\r\n]+", " ", subject or "").strip()[:300]
    reply_to = re.sub(r"[\r\n]+", "", reply_to) if reply_to else reply_to
    text = text or re.sub(r"<[^>]+>", "", html)
    provider = s.email_provider.lower()
    if provider == "resend" and s.resend_api_key:
        body = {"from": s.email_from, "to": [to], "subject": subject, "html": html, "text": text}
        if reply_to:
            body["reply_to"] = reply_to
        if headers:
            body["headers"] = headers
        with http.client(timeout=20, headers={"Authorization": f"Bearer {s.resend_api_key}"}) as c:
            r = c.post("https://api.resend.com/emails", json=body)
            http.check("Resend", r)
            return r.json().get("id", "")
    if provider == "smtp" and s.smtp_host:
        msg = EmailMessage()
        msg["From"] = s.email_from
        msg["To"] = to
        msg["Subject"] = subject
        if reply_to:
            msg["Reply-To"] = reply_to
        for k, v in (headers or {}).items():
            msg[k] = v
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
            if s.smtp_starttls:
                smtp.starttls()
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
        return "smtp"
    OUTBOX.append({"channel": "email", "to": to, "subject": subject, "body": text})
    del OUTBOX[:-50]
    log.info("EMAIL (console) to %s: %s", to, subject)
    return "console"


def email_domain(addr: str) -> str:
    return parseaddr(addr)[1].split("@")[-1].lower()
