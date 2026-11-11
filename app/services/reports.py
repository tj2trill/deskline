"""Weekly value report for each business owner — the #1 churn reducer is the
owner seeing what the receptionist did for them."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Booking, Call, Client, Contact
from . import messaging, scheduling


def _after_hours(client: Client, dt: datetime | None) -> bool:
    if not dt:
        return False
    local = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(scheduling.tz_of(client))
    spans = (client.business_hours or {}).get(scheduling.DAYS[local.weekday()]) or []
    hm = local.strftime("%H:%M")
    return not any(a <= hm < b for a, b in spans)


def stats(db: Session, client: Client, days: int = 7) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    calls = db.scalars(select(Call).where(Call.client_id == client.id, Call.created_at >= since)).all()
    answered = [c for c in calls if (c.duration_seconds or 0) >= 10]
    bookings = db.scalars(select(Booking).where(Booking.client_id == client.id, Booking.created_at >= since,
                                                Booking.status == "confirmed")).all()
    contacts = db.scalars(select(Contact).where(Contact.client_id == client.id, Contact.created_at >= since)).all()
    after = [c for c in answered if _after_hours(client, c.started_at or c.created_at)]
    return {
        "days": days, "calls": len(calls), "answered": len(answered), "after_hours": len(after),
        "minutes": sum(c.billable_minutes or 0 for c in calls), "bookings": len(bookings),
        "leads": sum(1 for c in contacts if c.kind == "lead"),
        "messages": sum(1 for c in contacts if c.kind in ("message", "callback")),
        # Only bookings count toward value — no guessing about calls that didn't convert.
        "est_value_cents": len(bookings) * (client.avg_job_value_cents or 0),
    }


def render_html(brand: str, client: Client, s: dict) -> str:
    def row(label, value):
        return f"<tr><td style='padding:6px 12px;color:#555'>{label}</td><td style='padding:6px 12px;font-weight:600'>{value}</td></tr>"

    from ..security import esc

    base = get_settings().base_url
    brand = esc(brand)
    return (f"<h2 style='font-family:sans-serif'>Your receptionist this week — {esc(client.name)}</h2>"
            f"<table style='font-family:sans-serif;border-collapse:collapse'>"
            + row("Calls answered", f"{s['answered']} of {s['calls']}")
            + row("After-hours calls caught", s["after_hours"])
            + row("Appointments booked", s["bookings"])
            + row("New leads captured", s["leads"])
            + row("Messages taken", s["messages"])
            + (row("Est. value of bookings", f"${s['est_value_cents'] / 100:,.0f}") if s["est_value_cents"] else "")
            + "</table>"
            f"<p style='font-family:sans-serif'><a href='{base}/portal'>See every call, recording and transcript</a></p>"
            f"<p style='font-family:sans-serif;color:#888;font-size:12px'>{brand}</p>")


def send_weekly(db: Session, client: Client, force: bool = False) -> bool:
    from .calls import agency

    if not client.owner_email:
        return False
    if not force and client.last_report_at:
        last = client.last_report_at if client.last_report_at.tzinfo else client.last_report_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - last < timedelta(days=6):
            return False
    s = stats(db, client)
    if not force and s["calls"] == 0:
        return False
    brand = agency(db).brand_name
    messaging.send_email(client.owner_email, f"{s['answered']} calls answered for {client.name} this week",
                         render_html(brand, client, s))
    client.last_report_at = datetime.now(timezone.utc)
    return True


def due(client: Client, now: datetime | None = None) -> bool:
    """Monday 8–10am in the client's own timezone."""
    local = (now or datetime.now(timezone.utc)).astimezone(scheduling.tz_of(client))
    return local.weekday() == 0 and 8 <= local.hour < 10
