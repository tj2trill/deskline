"""Availability + booking. 'internal' uses business hours and the bookings table;
'calcom' proxies to Cal.com API v2 so bookings land on the business's real calendar."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Booking, Client
from . import http

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_WORDS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
                 "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
CAL_BASE = "https://api.cal.com/v2"
CAL_SLOTS_VERSION = "2024-09-04"
CAL_BOOKINGS_VERSION = "2026-02-25"


class BookingError(ValueError):
    """A reason the caller should hear (slot taken, outside hours...)."""


def tz_of(client: Client) -> ZoneInfo:
    try:
        return ZoneInfo(client.timezone or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def now_local(client: Client) -> datetime:
    return datetime.now(timezone.utc).astimezone(tz_of(client))


def resolve_day(text: str, today: date) -> date:
    t = (text or "").strip().lower()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    if t in ("", "today", "now", "asap", "soonest", "earliest"):
        return today
    if "day after tomorrow" in t:
        return today + timedelta(days=2)
    if "tomorrow" in t:
        return today + timedelta(days=1)
    for word, wd in sorted(WEEKDAY_WORDS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{word}\b", t):
            delta = (wd - today.weekday()) % 7
            if delta == 0 or "next" in t:
                delta = delta or 7
            return today + timedelta(days=delta)
    m = re.search(r"(\d{1,2})/(\d{1,2})", t)  # 10/28
    if m:
        d = date(today.year, int(m[1]), int(m[2]))
        return d if d >= today else date(today.year + 1, d.month, d.day)
    raise BookingError(f"I couldn't understand the date '{text}'. Ask for a specific day.")


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def fmt_day(d: date | datetime) -> str:
    return d.strftime("%A, %B ") + str(d.day)


def _parse_hm(s: str) -> time:
    h, m = s.split(":")[:2]
    return time(int(h) % 24, int(m))


def _internal_slots(db: Session, client: Client, day: date) -> list[datetime]:
    tz = tz_of(client)
    spans = (client.business_hours or {}).get(DAYS[day.weekday()]) or []
    step = timedelta(minutes=max(10, client.slot_minutes or 60))
    earliest = now_local(client) + timedelta(hours=client.booking_lead_hours or 0)
    day_start = datetime.combine(day, time(0), tz)
    taken = db.scalars(select(Booking).where(
        Booking.client_id == client.id, Booking.status == "confirmed",
        Booking.start_at >= day_start.astimezone(timezone.utc) - timedelta(days=1),
        Booking.start_at < (day_start + timedelta(days=2)).astimezone(timezone.utc))).all()
    busy = [(_aware(b.start_at), _aware(b.end_at)) for b in taken]
    out = []
    for a, b in spans:
        cur = datetime.combine(day, _parse_hm(a), tz)
        end = datetime.combine(day, _parse_hm(b), tz)
        while cur + step <= end:
            if cur >= earliest and not any(cur < e and cur + step > s for s, e in busy):
                out.append(cur)
            cur += step
    return out


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cal_headers(client: Client, version: str) -> dict:
    if not (client.calcom_api_key and client.calcom_event_type_id):
        raise http.NotConfigured("Cal.com API key and event type ID are not set for this client.")
    return {"Authorization": f"Bearer {client.calcom_api_key}", "cal-api-version": version}


def _calcom_slots(client: Client, day: date) -> list[datetime]:
    tz = tz_of(client)
    with http.client(timeout=15, headers=_cal_headers(client, CAL_SLOTS_VERSION)) as c:
        r = c.get(f"{CAL_BASE}/slots", params={"eventTypeId": client.calcom_event_type_id,
                                              "start": day.isoformat(), "end": day.isoformat(),
                                              "timeZone": client.timezone})
        http.check("Cal.com", r)
    data = r.json().get("data") or {}
    out = []
    for slots in data.values():
        for s in slots:
            start = s.get("start") if isinstance(s, dict) else s
            dt = datetime.fromisoformat(str(start).replace("Z", "+00:00")).astimezone(tz)
            if dt.date() == day:
                out.append(dt)
    return sorted(out)


def slots_for(db: Session, client: Client, day: date) -> list[datetime]:
    if client.booking_mode == "calcom":
        return _calcom_slots(client, day)
    return _internal_slots(db, client, day)


def check_availability(db: Session, client: Client, day_text: str) -> dict:
    today = now_local(client).date()
    day = resolve_day(day_text, today)
    horizon = today + timedelta(days=client.booking_horizon_days or 21)
    if day < today:
        raise BookingError("That date has already passed. Ask for an upcoming day.")
    if day > horizon:
        raise BookingError(f"We only book up to {client.booking_horizon_days} days ahead.")
    slots = slots_for(db, client, day)
    result = {"today": fmt_day(today), "requested_day": fmt_day(day), "timezone": client.timezone}
    if slots:
        picks = _spread(slots, 6)
        result["available"] = [{"start": s.strftime("%Y-%m-%dT%H:%M"), "label": fmt_time(s)} for s in picks]
        return result
    # nothing that day → look ahead for the next open day
    for i in range(1, 8):
        nxt = day + timedelta(days=i)
        if nxt > horizon:
            break
        s2 = slots_for(db, client, nxt)
        if s2:
            result["available"] = []
            result["next_open_day"] = fmt_day(nxt)
            result["next_open_slots"] = [{"start": s.strftime("%Y-%m-%dT%H:%M"), "label": fmt_time(s)} for s in _spread(s2, 4)]
            return result
    result["available"] = []
    result["note"] = "No openings soon. Offer to take a message for a callback."
    return result


def _spread(slots: list[datetime], n: int) -> list[datetime]:
    if len(slots) <= n:
        return slots
    step = len(slots) / n
    return [slots[int(i * step)] for i in range(n)]


def book(db: Session, client: Client, start_text: str, name: str, phone: str | None, email: str | None = None,
         service: str | None = None, notes: str | None = None, vapi_call_id: str | None = None,
         source: str = "call") -> Booking:
    tz = tz_of(client)
    try:
        start = datetime.fromisoformat(start_text.strip()[:16])
    except ValueError as e:
        raise BookingError("Use the exact start value from check_availability.") from e
    start = start.replace(tzinfo=tz) if start.tzinfo is None else start.astimezone(tz)
    end = start + timedelta(minutes=client.slot_minutes or 60)
    open_slots = slots_for(db, client, start.date())
    if not any(abs((s - start).total_seconds()) < 60 for s in open_slots):
        raise BookingError("That time is no longer available. Check availability again and offer other times.")
    booking = Booking(client_id=client.id, vapi_call_id=vapi_call_id, source=source, name=name, phone=phone,
                      email=email, service=service, notes=notes, start_at=start.astimezone(timezone.utc),
                      end_at=end.astimezone(timezone.utc), provider=client.booking_mode)
    if client.booking_mode == "calcom":
        attendee = {"name": name, "timeZone": client.timezone}
        if email:
            attendee["email"] = email
        if phone:
            attendee["phoneNumber"] = phone
        body = {"start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "eventTypeId": int(client.calcom_event_type_id), "attendee": attendee,
                "metadata": {"source": "deskline", "service": (service or "")[:400], "phone": phone or ""}}
        if notes:
            body["bookingFieldsResponses"] = {"notes": notes[:1000]}
        with http.client(timeout=20, headers=_cal_headers(client, CAL_BOOKINGS_VERSION)) as c:
            r = c.post(f"{CAL_BASE}/bookings", json=body)
        if r.status_code >= 400:
            detail = r.text[:300]
            raise BookingError(f"The calendar rejected the booking ({detail}). If it needs an email, ask for one and try again.")
        data = r.json().get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        booking.external_id = data.get("uid")
    db.add(booking)
    db.flush()
    return booking


def local_label(client: Client, dt_utc: datetime) -> str:
    local = _aware(dt_utc).astimezone(tz_of(client))
    return f"{fmt_day(local)} at {fmt_time(local)}"
