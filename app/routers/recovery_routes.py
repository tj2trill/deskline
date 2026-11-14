"""Revenue recovery screens: the owner's dashboard (portal) and the agency's setup tab."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Client, Invoice, PhoneNumber, Quote, RecoveryCall, SmsMessage, SmsThread, User, new_token
from ..security import agency_user, current_user
from ..services import http, messaging, recovery, sequences, twilio
from ..templating import flash, render
from .portal import _client as portal_client

router = APIRouter()
BASE = "/portal/recovery"


def _cents(v: str | None) -> int:
    return sequences.parse_money(v) or 0


def _go(path: str = "", anchor: str = "") -> RedirectResponse:
    return RedirectResponse(f"{BASE}{path}" + (f"#{anchor}" if anchor else ""), status_code=303)


# ====================================================================== owner portal

@router.get(BASE, response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = portal_client(request, db, user)
    threads = db.scalars(select(SmsThread).where(SmsThread.client_id == c.id)
                         .order_by(SmsThread.updated_at.desc()).limit(30)).all()
    quotes = db.scalars(select(Quote).where(Quote.client_id == c.id).order_by(Quote.created_at.desc()).limit(100)).all()
    invoices = db.scalars(select(Invoice).where(Invoice.client_id == c.id).order_by(Invoice.due_date.desc()).limit(100)).all()
    return render(request, "portal/recovery.html", c=c, s=recovery.stats(db, c), threads=threads, quotes=quotes,
                  invoices=invoices, jobs=recovery.unconfirmed_bookings(db, c), number=recovery.recovery_number(c),
                  now=recovery.utcnow(), aware=recovery.aware, today=date.today().isoformat())


@router.get(BASE + "/threads/{thread_id}", response_class=HTMLResponse)
def thread_detail(request: Request, thread_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = portal_client(request, db, user)
    t = db.get(SmsThread, thread_id)
    if not t or t.client_id != c.id:
        raise HTTPException(404)
    msgs = db.scalars(select(SmsMessage).where(SmsMessage.thread_id == t.id).order_by(SmsMessage.created_at, SmsMessage.id)).all()
    return render(request, "portal/recovery_thread.html", c=c, t=t, msgs=msgs,
                  quote=db.get(Quote, t.quote_id) if t.quote_id else None,
                  invoice=db.get(Invoice, t.invoice_id) if t.invoice_id else None,
                  opted_out=recovery.opted_out(db, c, t.phone))


@router.post(BASE + "/threads/{thread_id}/reply")
def thread_reply(request: Request, thread_id: int, body: str = Form(...), db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    c = portal_client(request, db, user)
    t = db.get(SmsThread, thread_id)
    if not t or t.client_id != c.id:
        raise HTTPException(404)
    if not body.strip():
        return _go(f"/threads/{t.id}")
    ok = recovery.owner_reply(db, c, t, body)
    db.commit()
    flash(request, "Sent. Automated texts on this conversation are now off." if ok else
          "Couldn't send: the customer opted out or the number rejected it.", "ok" if ok else "error")
    return _go(f"/threads/{t.id}")


@router.post(BASE + "/quotes")
def quote_create(request: Request, amount: str = Form(...), phone: str = Form(""), customer_name: str = Form(""),
                 description: str = Form(""), ref: str = Form(""), deposit: str = Form(""), sent_on: str = Form(""),
                 db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = portal_client(request, db, user)
    cents = _cents(amount)
    if cents <= 0:
        flash(request, "Enter the quote amount.", "error")
        return _go("", "quotes")
    if not messaging.normalize_phone(phone):
        flash(request, "Add the customer's mobile number so we can follow up.", "error")
        return _go("", "quotes")
    sent = sequences.parse_date(sent_on)
    q = sequences.create_quote(db, c, amount_cents=cents, phone=phone, customer_name=customer_name.strip() or None,
                               description=description.strip() or None, ref=ref.strip() or None,
                               deposit_cents=_cents(deposit) if deposit.strip() else None,
                               sent_at=sequences.due_from_date(c, sent) if sent else None)
    db.commit()
    when = q.next_touch_at and q.next_touch_at.astimezone(recovery.scheduling.tz_of(c))
    flash(request, f"Quote added. First follow-up {when.strftime('%a %b %d, %I:%M %p') if when else 'not scheduled'}.")
    return _go("", "quotes")


@router.post(BASE + "/quotes/import")
async def quote_import(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    return await _import(request, db, user, file, "quote")


@router.post(BASE + "/invoices/import")
async def invoice_import(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db),
                         user: User = Depends(current_user)):
    return await _import(request, db, user, file, "invoice")


async def _import(request, db, user, file: UploadFile, kind: str):
    c = portal_client(request, db, user)
    raw = await file.read()
    if len(raw) > 2_000_000:
        flash(request, "That file is over 2 MB. Export a shorter date range.", "error")
        return _go("", kind + "s")
    try:
        r = sequences.import_csv(db, c, raw, kind)
        db.commit()
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "error")
        return _go("", kind + "s")
    msg = f"Imported {r['created']} {kind}s"
    if r["skipped"]:
        msg += f", skipped {r['skipped']} already here"
    if r["errors"]:
        msg += f", {len(r['errors'])} rows unreadable ({'; '.join(r['errors'][:3])})"
    flash(request, msg + ".", "ok" if not r["errors"] else "warn")
    return _go("", kind + "s")


@router.post(BASE + "/quotes/{quote_id}/{action}")
def quote_action(request: Request, quote_id: int, action: str, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    c = portal_client(request, db, user)
    q = db.get(Quote, quote_id)
    if not q or q.client_id != c.id:
        raise HTTPException(404)
    try:
        sequences.set_quote_status(db, c, q, action)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return _go("", "quotes")


@router.post(BASE + "/invoices")
def invoice_create(request: Request, amount: str = Form(...), due: str = Form(...), phone: str = Form(""),
                   customer_name: str = Form(""), number: str = Form(""), payment_link: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = portal_client(request, db, user)
    cents, due_d = _cents(amount), sequences.parse_date(due)
    if cents <= 0 or not due_d:
        flash(request, "Enter the amount and the due date.", "error")
        return _go("", "invoices")
    if not messaging.normalize_phone(phone):
        flash(request, "Add the customer's mobile number so we can send reminders.", "error")
        return _go("", "invoices")
    link = payment_link.strip()
    if link and not link.startswith("https://"):
        flash(request, "Payment links must start with https://", "error")
        return _go("", "invoices")
    sequences.create_invoice(db, c, amount_cents=cents, due=due_d, phone=phone, customer_name=customer_name.strip() or None,
                             number=number.strip() or None, payment_link=link or None)
    db.commit()
    flash(request, "Invoice added. Reminders go out on schedule until it's marked paid.")
    return _go("", "invoices")


@router.post(BASE + "/invoices/{invoice_id}/{action}")
def invoice_action(request: Request, invoice_id: int, action: str, db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    c = portal_client(request, db, user)
    inv = db.get(Invoice, invoice_id)
    if not inv or inv.client_id != c.id:
        raise HTTPException(404)
    try:
        sequences.set_invoice_status(db, c, inv, action)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return _go("", "invoices")


@router.post(BASE + "/jobs/{event_id}")
def job_value(request: Request, event_id: int, value: str = Form(""), db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    c = portal_client(request, db, user)
    try:
        recovery.confirm_job_value(db, c, event_id, _cents(value))
    except ValueError as e:
        raise HTTPException(404) from e
    db.commit()
    return _go("", "jobs")


# ====================================================================== agency setup (client → Recovery tab)

agency = APIRouter(prefix="/clients/{client_id}/recovery", dependencies=[Depends(agency_user)])


def _get(db: Session, client_id: int) -> Client:
    c = db.get(Client, client_id)
    if not c:
        raise HTTPException(404)
    return c


def _back(client_id: int) -> RedirectResponse:
    return RedirectResponse(f"/clients/{client_id}?tab=recovery", status_code=303)


def _int_list(raw: str, fallback: list[int]) -> list[int]:
    out = []
    for part in (raw or "").replace(" ", "").split(","):
        if part.lstrip("-").isdigit():
            out.append(int(part))
    return sorted(set(out)) or fallback


@agency.post("")
async def save(request: Request, client_id: int, db: Session = Depends(get_db)):
    c = _get(db, client_id)
    f = dict(await request.form())
    c.recovery_enabled = bool(f.get("recovery_enabled"))
    c.missed_call_mode = f.get("missed_call_mode") if f.get("missed_call_mode") in ("forward", "ring") else "forward"
    c.ring_seconds = max(10, min(60, int(f.get("ring_seconds") or 20)))
    c.textback_opener = (f.get("textback_opener") or "").strip() or None
    c.ask_photos = bool(f.get("ask_photos"))
    c.quote_followup_days = [d for d in _int_list(f.get("quote_followup_days", ""), [1, 3, 7, 14]) if d > 0][:4]
    c.invoice_reminder_days = _int_list(f.get("invoice_reminder_days", ""), [-3, 0, 3, 7, 14])
    c.invoice_escalate_days = max(1, int(f.get("invoice_escalate_days") or 21))
    c.default_deposit_cents = _cents(f.get("default_deposit"))
    link = (f.get("deposit_link") or "").strip()
    c.deposit_link = link if link.startswith("https://") else None
    c.payment_instructions = (f.get("payment_instructions") or "").strip()[:300] or None
    for k in ("send_window_start", "send_window_end"):
        v = (f.get(k) or "").strip()[:5]
        if len(v) == 5 and v[2] == ":":
            setattr(c, k, v)
    try:
        c.performance_fee_pct = max(0.0, min(50.0, float(f.get("performance_fee_pct") or 0)))
        c.avg_job_value_cents = _cents(f.get("avg_job_value")) or c.avg_job_value_cents
    except ValueError:
        pass
    if c.missed_call_mode == "ring" and not (c.transfer_number or c.owner_phone):
        flash(request, "Ring mode needs the owner's cell (Info → transfer number or owner phone).", "error")
    db.commit()
    flash(request, "Recovery settings saved.")
    return _back(client_id)


@agency.post("/number/buy")
def number_buy(request: Request, client_id: int, area_code: str = Form(""), country: str = Form("CA"),
               db: Session = Depends(get_db)):
    c = _get(db, client_id)
    if recovery.recovery_number(c):
        flash(request, "This client already has a recovery number.", "error")
        return _back(client_id)
    try:
        got = twilio.buy_number(area_code.strip() or None, country if country in ("CA", "US") else "CA")
    except (http.ProviderError, http.NotConfigured) as e:
        flash(request, f"Couldn't get a number: {e}", "error")
        return _back(client_id)
    db.add(PhoneNumber(client_id=c.id, number=got["number"], provider="twilio", purpose="recovery",
                       twilio_sid=got["sid"], label="Recovery"))
    db.commit()
    flash(request, f"Recovery number {got['number']} is ready.")
    return _back(client_id)


@agency.post("/number/connect")
def number_connect(request: Request, client_id: int, number: str = Form(...), db: Session = Depends(get_db)):
    c = _get(db, client_id)
    e164 = messaging.normalize_phone(number)
    if not e164:
        flash(request, "Enter the number in full, e.g. +15195550123.", "error")
        return _back(client_id)
    if db.scalars(select(PhoneNumber).where(PhoneNumber.number == e164)).first():
        flash(request, f"{e164} is already used by a client (receptionist numbers can't double as recovery numbers).", "error")
        return _back(client_id)
    try:
        got = twilio.connect_number(e164)
    except (http.ProviderError, http.NotConfigured) as e:
        flash(request, f"Couldn't connect it: {e}", "error")
        return _back(client_id)
    db.add(PhoneNumber(client_id=c.id, number=e164, provider="twilio", purpose="recovery", twilio_sid=got["sid"],
                       label="Recovery"))
    db.commit()
    flash(request, f"{e164} now sends missed calls and texts to recovery.")
    return _back(client_id)


@agency.post("/number/{number_id}/remove")
def number_remove(request: Request, client_id: int, number_id: int, db: Session = Depends(get_db)):
    n = db.get(PhoneNumber, number_id)
    if not n or n.client_id != client_id or n.purpose != "recovery":
        raise HTTPException(404)
    db.delete(n)
    db.commit()
    flash(request, "Number detached. It's still in your Twilio account: release it there if you don't need it.")
    return _back(client_id)


@agency.post("/simulate/call")
def simulate_call(request: Request, client_id: int, phone: str = Form(...), db: Session = Depends(get_db)):
    """Demo the flow in front of a prospect without a live number: a fake missed call from `phone`."""
    c = _get(db, client_id)
    sid = "SIM" + new_token(10)
    db.add(RecoveryCall(client_id=c.id, call_sid=sid, from_number=phone, mode="sim", answered=False, dial_status="sim"))
    db.flush()
    t = recovery.start_missed_call(db, c, phone, origin="missed", call_sid=sid)
    db.commit()
    flash(request, "Missed call simulated. The opener was texted." if t else "That number is invalid or opted out.",
          "ok" if t else "error")
    return _back(client_id)


@agency.post("/simulate/text")
def simulate_text(request: Request, client_id: int, phone: str = Form(...), body: str = Form(...),
                  db: Session = Depends(get_db)):
    c = _get(db, client_id)
    recovery.handle_inbound(db, c, phone, body, [], None)
    db.commit()
    return _back(client_id)
