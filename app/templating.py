from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from sqlalchemy import select

from .config import get_settings
from .db import SessionLocal
from .models import AgencySettings

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def money(cents, decimals: int = 2) -> str:
    try:
        return f"${(cents or 0) / 100:,.{decimals}f}"
    except (TypeError, ValueError):
        return "$0"


def dollars(cents) -> str:
    """Cents → editable dollar string without noise: 29700 → '297', 25 → '0.25'."""
    v = (cents or 0) / 100
    return f"{v:.0f}" if v == int(v) else f"{v:.2f}"


def duration(seconds) -> str:
    s = int(seconds or 0)
    return f"{s // 60}:{s % 60:02d}"


@pass_context
def localtime(ctx, dt: datetime | None, tz: str | None = None, fmt: str = "%b %d, %I:%M %p") -> str:
    """Render in the given zone, else the agency's zone — never raw UTC."""
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    zone = tz or getattr(ctx.get("brand"), "timezone", None) or "UTC"
    try:
        dt = dt.astimezone(ZoneInfo(zone))
    except Exception:
        pass
    out = dt.strftime(fmt).replace(" 0", " ")
    return out[1:] if out.startswith("0") else out


def phone(value: str | None) -> str:
    """+14165550100 → (416) 555-0100; anything else unchanged."""
    if not value:
        return "—"
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10 and (value.startswith("+1") or not value.startswith("+")):
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return value


# One vocabulary for every status in the product: tone → colour, label → words.
TONES = {
    "good": ["booked", "active", "ok", "ready", "won", "positive", "confirmed", "sent", "handled", "paid", "trialing", "answered"],
    "lead": ["lead"],
    "transfer": ["transferred", "replied"],
    "wait": ["message", "callback", "demo", "building", "contacted", "pending", "skipped", "past_due", "normal", "warn"],
    "bad": ["missed", "error", "paused", "churned", "lost", "negative", "cancelled", "canceled", "urgent", "unpaid", "review", "failed"],
}
TONE_OF = {v: t for t, vals in TONES.items() for v in vals}
LABELS = {
    "info": "Answered", "missed": "Missed", "booked": "Booked", "transferred": "Transferred", "message": "Message taken",
    "callback": "Callback", "lead": "New lead", "spam": "Spam", "past_due": "Past due", "trialing": "Trial",
    "skipped": "Local only", "ok": "Synced", "never": "Not synced", "error": "Error", "ready": "Ready", "building": "Building",
    "pending": "Pending", "ringing": "Ringing", "in-progress": "Live", "ended": "Ended", "calcom": "Cal.com",
    "internal": "Built-in", "none": "Off", "vapi": "Vapi", "twilio": "Twilio", "chat": "Website chat", "call": "Phone call",
    "site": "Website",
}


def tone(value) -> str:
    v = str(value or "").lower()
    return v if v in TONES or v == "neutral" else TONE_OF.get(v, "neutral")


def label(value) -> str:
    v = str(value or "")
    return LABELS.get(v.lower(), v.replace("_", " ").replace("-", " ").capitalize() or "—")


_SPEAKERS = {"ai": "agent", "assistant": "agent", "bot": "agent", "agent": "agent",
             "user": "caller", "customer": "caller", "caller": "caller", "human": "caller"}


def turns(transcript: str | None) -> list[dict]:
    """'AI: hi\\nUser: hello' → [{who: agent|caller|system, text}] for the conversation view."""
    out: list[dict] = []
    for raw in (transcript or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        head, sep, rest = line.partition(":")
        who = _SPEAKERS.get(head.strip().lower()) if sep and len(head) <= 12 else None
        if who:
            if out and out[-1]["who"] == who:
                out[-1]["text"] += " " + rest.strip()
            else:
                out.append({"who": who, "text": rest.strip()})
        elif out:
            out[-1]["text"] += " " + line
        else:
            out.append({"who": "system", "text": line})
    return out


def initials(value: str | None) -> str:
    parts = [p for p in (value or "?").replace("@", " ").replace(".", " ").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


from .security import safe_color, safe_url  # noqa: E402

def on_color(value: str | None) -> str:
    """Black or white, whichever reads better on the given brand colour (WCAG relative luminance)."""
    c = safe_color(value).lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    lin = [((v / 255) / 12.92) if (v / 255) <= 0.03928 else (((v / 255) + 0.055) / 1.055) ** 2.4
           for v in (int(c[i:i + 2], 16) for i in (0, 2, 4))]
    lum = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    return "#0f1720" if lum > 0.42 else "#ffffff"


ASSET_V = "7"  # bump when app.css / app.js change so browsers refetch
templates.env.globals["asset_v"] = ASSET_V
templates.env.filters.update(on_color=on_color, safe_url=lambda v: safe_url(v) or "#", color=safe_color, dollars=dollars, money=money, duration=duration, localtime=localtime, phone=phone, tone=tone,
                             label=label, turns=turns, initials=initials)


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append({"kind": kind, "message": message[:600]})


def brand() -> AgencySettings:
    with SessionLocal() as db:
        a = db.scalars(select(AgencySettings)).first()
        if a:
            db.expunge(a)
            return a
    return AgencySettings(brand_name=get_settings().app_name, primary_color="#2563eb")


def nav_counts(user) -> dict:
    """Small attention counters shown in the navigation."""
    if not user:
        return {}
    from sqlalchemy import func

    from .models import Call, Contact

    with SessionLocal() as db:
        q_contacts = select(func.count()).select_from(Contact).where(Contact.status == "new")
        q_review = select(func.count()).select_from(Call).where(Call.flagged.is_(True), Call.reviewed.is_(False))
        if not user.is_agency:
            q_contacts = q_contacts.where(Contact.client_id == user.client_id)
        from .models import AgentAction

        out = {"inbox": db.scalar(q_contacts) or 0}
        if user.is_agency:
            out["review"] = db.scalar(q_review) or 0
            out["approvals"] = db.scalar(select(func.count()).select_from(AgentAction).where(AgentAction.status == "pending")) or 0
        return out


def render(request: Request, _template: str, status_code: int = 200, **ctx):
    ctx.setdefault("brand", brand())
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx.setdefault("counts", nav_counts(ctx["user"]))
    ctx["flashes"] = request.session.pop("flash", []) if "session" in request.scope else []
    ctx["settings"] = get_settings()
    ctx["request"] = request
    return templates.TemplateResponse(request, _template, ctx, status_code=status_code)
