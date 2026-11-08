"""Twilio voice + SMS plumbing for revenue recovery: webhook signatures, TwiML, numbers.

Recovery numbers talk to Twilio directly (not through Vapi): a missed call needs no AI voice, just a
short spoken line and a text. Keeping these numbers off Vapi also keeps their voice cost near zero.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from xml.sax.saxutils import escape, quoteattr

from ..config import get_settings
from . import http

API = "https://api.twilio.com/2010-04-01"


def configured() -> bool:
    s = get_settings()
    return bool(s.twilio_account_sid and s.twilio_auth_token)


# ------------------------------------------------------------------ webhook signatures

def signature(url: str, params: dict, auth_token: str) -> str:
    """Twilio's X-Twilio-Signature: base64(HMAC-SHA1(token, url + each POST key+value sorted by key))."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


def public_url(path_and_query: str) -> str:
    """The URL exactly as Twilio requested it. Behind a TLS-terminating proxy request.url says http://app:8000,
    so the signature is checked against BASE_URL instead, which is also what we registered with Twilio."""
    return get_settings().base_url.rstrip("/") + path_and_query


def valid_request(path_and_query: str, params: dict, header: str | None) -> bool:
    token = get_settings().twilio_auth_token
    if not (token and header):
        return False
    expected = signature(public_url(path_and_query), params, token)
    return hmac.compare_digest(expected, header)


# ------------------------------------------------------------------ TwiML

def twiml(*verbs: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(verbs) + "</Response>"


def say(text: str) -> str:
    return f'<Say voice="Polly.Joanna">{escape(text)}</Say>'


def hangup() -> str:
    return "<Hangup/>"


def dial_screened(number: str, *, timeout: int, action: str, screen_url: str, caller_id: str | None) -> str:
    """Ring the owner, but make them press 1 before the call connects. Without screening, the owner's
    cell voicemail picks up, Twilio reports the call as answered, and the missed call is never recovered."""
    attrs = f' timeout="{int(timeout)}" action={quoteattr(action)} method="POST" answerOnBridge="true"'
    if caller_id:
        attrs += f" callerId={quoteattr(caller_id)}"
    return f"<Dial{attrs}><Number url={quoteattr(screen_url)} method=\"POST\">{escape(number)}</Number></Dial>"


def gather_digit(action: str, prompt: str, timeout: int = 6) -> str:
    return (f'<Gather numDigits="1" timeout="{int(timeout)}" action={quoteattr(action)} method="POST">'
            f"{say(prompt)}</Gather>")


# ------------------------------------------------------------------ numbers

def _client():
    s = get_settings()
    if not configured():
        raise http.NotConfigured("Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN to buy or connect numbers.")
    return http.client(timeout=20, auth=(s.twilio_account_sid, s.twilio_auth_token)), s.twilio_account_sid


def hook_urls() -> dict:
    base = get_settings().base_url.rstrip("/")
    return {"VoiceUrl": f"{base}/webhooks/twilio/voice", "VoiceMethod": "POST",
            "SmsUrl": f"{base}/webhooks/twilio/sms", "SmsMethod": "POST"}


def buy_number(area_code: str | None, country: str = "CA") -> dict:
    """Search a local SMS+voice number (area code optional) and buy it with our webhooks attached."""
    c, sid = _client()
    with c:
        params = {"SmsEnabled": "true", "VoiceEnabled": "true", "PageSize": "5"}
        if area_code:
            params["AreaCode"] = area_code
        r = c.get(f"{API}/Accounts/{sid}/AvailablePhoneNumbers/{country.upper()}/Local.json", params=params)
        http.check("Twilio", r)
        found = r.json().get("available_phone_numbers") or []
        if not found:
            raise http.ProviderError("Twilio", 404, f"No SMS-capable local numbers available in {area_code or country}.")
        r = c.post(f"{API}/Accounts/{sid}/IncomingPhoneNumbers.json",
                   data={"PhoneNumber": found[0]["phone_number"], **hook_urls()})
        http.check("Twilio", r)
        data = r.json()
    return {"sid": data.get("sid"), "number": data.get("phone_number") or found[0]["phone_number"]}


def connect_number(e164: str) -> dict:
    """Point a number already in the Twilio account at our webhooks (replaces its voice + SMS URLs)."""
    c, sid = _client()
    with c:
        r = c.get(f"{API}/Accounts/{sid}/IncomingPhoneNumbers.json", params={"PhoneNumber": e164})
        http.check("Twilio", r)
        rows = r.json().get("incoming_phone_numbers") or []
        if not rows:
            raise http.ProviderError("Twilio", 404, f"{e164} isn't in this Twilio account.")
        num_sid = rows[0]["sid"]
        r = c.post(f"{API}/Accounts/{sid}/IncomingPhoneNumbers/{num_sid}.json", data=hook_urls())
        http.check("Twilio", r)
    return {"sid": num_sid, "number": e164}
