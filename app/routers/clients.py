"""Agency dashboard: clients, agents, numbers, calls, automations, billing."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import jobs
from ..db import get_db
from ..models import (Booking, Call, ChatSession, Client, Contact, CustomFunction, EventLog, KnowledgeGap,
                      PhoneNumber, Prospect, User, Workflow, slugify)
from ..security import agency_user
from ..services import agent_builder, billing, calls as callsvc, http, messaging, pricing, vapi, workflows
from ..templating import flash, render

router = APIRouter(dependencies=[Depends(agency_user)])
DAYS = agent_builder.DAYS


def _get(db: Session, client_id: int) -> Client:
    c = db.get(Client, client_id)
    if not c:
        raise HTTPException(404, "Client not found")
    return c


def back(client_id: int, tab: str = "overview") -> RedirectResponse:
    return RedirectResponse(f"/clients/{client_id}?tab={tab}", status_code=303)


def unique_client_slug(db: Session, name: str) -> str:
    base, i = slugify(name), 2
    slug = base
    while db.scalars(select(Client).where(Client.slug == slug)).first():
        slug, i = f"{base}-{i}", i + 1
    return slug


def usage_this_month(db: Session, client_id: int | None = None) -> dict:
    start = callsvc.month_start()
    q = select(Call).where(Call.created_at >= start)
    if client_id:
        q = q.where(Call.client_id == client_id)
    rows = db.scalars(q).all()
    by_client: dict[int, dict] = {}
    for c in rows:
        d = by_client.setdefault(c.client_id or 0, {"calls": 0, "minutes": 0, "cost": 0.0, "seconds": 0.0})
        d["calls"] += 1
        d["minutes"] += c.billable_minutes or 0
        d["seconds"] += c.duration_seconds or 0
        d["cost"] += callsvc.estimated_cost_cents(c)
    return by_client


# ------------------------------------------------------------------ dashboard

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    clients = db.scalars(select(Client)).all()
    usage = usage_this_month(db)
    rows, totals = [], {"revenue": 0.0, "cost": 0.0, "calls": 0, "minutes": 0}
    for c in clients:
        u = usage.get(c.id, {"calls": 0, "minutes": 0, "cost": 0.0})
        revenue = pricing.monthly_charge_cents(c, u["minutes"]) if c.status == "active" else 0.0
        rows.append({"client": c, "usage": u, "revenue": revenue, "margin": revenue - u["cost"]})
        totals["revenue"] += revenue
        totals["cost"] += u["cost"]
        totals["calls"] += u["calls"]
        totals["minutes"] += u["minutes"]
    recent = db.scalars(select(Call).order_by(Call.created_at.desc()).limit(10)).all()
    names = {c.id: c.name for c in clients}
    pipeline = dict(db.execute(select(Prospect.status, func.count()).group_by(Prospect.status)).all())
    new_contacts = db.scalar(select(func.count()).select_from(Contact).where(Contact.status == "new")) or 0
    return render(request, "dashboard.html", rows=sorted(rows, key=lambda r: -r["revenue"]), totals=totals,
                  recent=recent, names=names, pipeline=pipeline, new_contacts=new_contacts,
                  active=sum(1 for c in clients if c.status == "active"),
                  demos=sum(1 for c in clients if c.status == "demo"))


# ------------------------------------------------------------------ clients

@router.get("/clients", response_class=HTMLResponse)
def client_list(request: Request, db: Session = Depends(get_db)):
    clients = db.scalars(select(Client).order_by(Client.created_at.desc())).all()
    return render(request, "clients/list.html", clients=clients, usage=usage_this_month(db))


@router.get("/clients/new", response_class=HTMLResponse)
def client_new(request: Request, website: str = "", name: str = "", phone: str = ""):
    return render(request, "clients/new.html", website=website, name=name, phone=phone)


@router.post("/clients")
def client_create(request: Request, bg: BackgroundTasks, website: str = Form(""), name: str = Form(""),
                  industry: str = Form(""), business_phone: str = Form(""), owner_name: str = Form(""),
                  owner_email: str = Form(""), owner_phone: str = Form(""), timezone_: str = Form("America/Toronto", alias="timezone"),
                  agent_name: str = Form("Ava"), status: str = Form("demo"), prospect_id: str = Form(""),
                  db: Session = Depends(get_db)):
    from ..services import provisioning
    from .settings_routes import agency_settings

    prospect = db.get(Prospect, int(prospect_id)) if prospect_id.isdigit() else None
    try:
        client = provisioning.create_client(
            db, agency_settings(db), website=website, name=name, industry=industry, business_phone=business_phone,
            owner_name=owner_name, owner_email=owner_email, owner_phone=owner_phone, timezone=timezone_,
            agent_name=agent_name, status=status, prospect=prospect)
    except ValueError as e:
        flash(request, str(e), "error")
        return RedirectResponse("/clients/new", status_code=303)
    website = client.website
    db.commit()
    if website:
        bg.add_task(jobs.build_and_sync, client.id)
        flash(request, "Reading the website and building the receptionist. This usually takes under a minute.")
    else:
        agent = client.agent
        agent_builder.build_agent(db, client, agent)
        jobs.sync_agent(db, client, agent)
        db.commit()
    return back(client.id, "agent")


@router.get("/clients/{client_id}", response_class=HTMLResponse)
def client_detail(request: Request, client_id: int, tab: str = "overview", db: Session = Depends(get_db)):
    client = _get(db, client_id)
    ctx: dict = {"client": client, "agent": client.agent, "tab": tab, "days": DAYS,
                 "day_names": agent_builder.DAY_NAMES}
    u = usage_this_month(db, client.id).get(client.id, {"calls": 0, "minutes": 0, "cost": 0.0, "seconds": 0})
    ctx["usage"] = u
    ctx["estimate"] = pricing.monthly_charge_cents(client, u["minutes"])
    ctx["pricing_text"] = pricing.describe(client)
    if tab in ("overview", "calls"):
        ctx["calls"] = db.scalars(select(Call).where(Call.client_id == client.id).order_by(Call.created_at.desc())
                                  .limit(100 if tab == "calls" else 8)).all()
    if tab in ("overview", "inbox"):
        ctx["contacts"] = db.scalars(select(Contact).where(Contact.client_id == client.id)
                                     .order_by(Contact.created_at.desc()).limit(100)).all()
        ctx["bookings"] = db.scalars(select(Booking).where(Booking.client_id == client.id)
                                     .order_by(Booking.start_at.desc()).limit(100)).all()
    if tab == "automations":
        ctx["functions"] = db.scalars(select(CustomFunction).where(CustomFunction.client_id == client.id)).all()
        ctx["workflows"] = db.scalars(select(Workflow).where(Workflow.client_id == client.id)).all()
        ctx["triggers"], ctx["ops"], ctx["fields"] = workflows.TRIGGERS, workflows.OPS, workflows.FIELDS
    if tab == "widget":
        ctx["chats"] = db.scalars(select(ChatSession).where(ChatSession.client_id == client.id)
                                  .order_by(ChatSession.updated_at.desc()).limit(20)).all()
        from ..models import Site

        ctx["site"] = db.scalars(select(Site).where(Site.client_id == client.id)).first()
    if tab in ("overview", "agent"):
        ctx["gaps"] = db.scalars(select(KnowledgeGap).where(KnowledgeGap.client_id == client.id, KnowledgeGap.status == "open")
                                 .order_by(KnowledgeGap.times_asked.desc(), KnowledgeGap.created_at.desc()).limit(30)).all()
    if tab == "billing":
        ctx["owners"] = db.scalars(select(User).where(User.client_id == client.id)).all()
        ctx["minutes_used"] = callsvc.minutes_this_month(db, client.id)
    if tab == "overview":
        ctx["events"] = db.scalars(select(EventLog).where(EventLog.client_id == client.id)
                                   .order_by(EventLog.created_at.desc()).limit(8)).all()
    ctx["vapi_ready"] = vapi.configured()
    ctx["stripe_ready"] = billing.configured()
    ctx["tab_counts"] = {
        "inbox": db.scalar(select(func.count()).select_from(Contact).where(Contact.client_id == client.id, Contact.status == "new")) or 0,
        "gaps": db.scalar(select(func.count()).select_from(KnowledgeGap).where(KnowledgeGap.client_id == client.id, KnowledgeGap.status == "open")) or 0,
    }
    return render(request, "clients/detail.html", **ctx)


@router.get("/clients/{client_id}/build-status", response_class=HTMLResponse)
def build_status(request: Request, client_id: int, db: Session = Depends(get_db)):
    client = _get(db, client_id)
    return render(request, "clients/_build_status.html", client=client, agent=client.agent)


@router.post("/clients/{client_id}/info")
def client_info(request: Request, client_id: int, name: str = Form(...), website: str = Form(""),
                industry: str = Form(""), business_phone: str = Form(""), transfer_number: str = Form(""),
                owner_name: str = Form(""), owner_email: str = Form(""), owner_phone: str = Form(""),
                address: str = Form(""), service_area: str = Form(""), timezone_: str = Form("America/Toronto", alias="timezone"),
                status: str = Form("demo"), notes: str = Form(""), db: Session = Depends(get_db)):
    c = _get(db, client_id)
    if transfer_number and not messaging.normalize_phone(transfer_number):
        flash(request, "Transfer number must be a full phone number, e.g. +14165550123.", "error")
        return back(client_id)
    c.name, c.website, c.industry = name[:160], website or None, industry or None
    c.business_phone, c.transfer_number = business_phone or None, messaging.normalize_phone(transfer_number)
    c.owner_name, c.owner_email, c.owner_phone = owner_name or None, owner_email.lower() or None, owner_phone or None
    c.address, c.service_area, c.timezone = address or None, service_area or None, timezone_
    c.status = status if status in ("demo", "active", "paused", "churned") else c.status
    c.notes = notes or None
    _resync(request, db, c)
    return back(client_id)


async def _form(request: Request) -> dict:
    return dict(await request.form())


@router.post("/clients/{client_id}/hours")
async def client_hours(request: Request, client_id: int, db: Session = Depends(get_db)):
    c = _get(db, client_id)
    f = await _form(request)
    hours = {}
    for d in DAYS:
        if f.get(f"{d}_open") and f.get(f"{d}_close") and not f.get(f"{d}_closed"):
            hours[d] = [[f[f"{d}_open"][:5], f[f"{d}_close"][:5]]]
        else:
            hours[d] = []
    c.business_hours = hours
    _resync(request, db, c)
    return back(client_id, "agent")


@router.post("/clients/{client_id}/booking")
def client_booking(request: Request, client_id: int, booking_mode: str = Form("internal"), slot_minutes: int = Form(60),
                   booking_lead_hours: int = Form(2), booking_horizon_days: int = Form(21),
                   calcom_api_key: str = Form(""), calcom_event_type_id: str = Form(""), booking_link: str = Form(""),
                   db: Session = Depends(get_db)):
    c = _get(db, client_id)
    c.booking_mode = booking_mode if booking_mode in ("internal", "calcom", "none") else "internal"
    c.slot_minutes, c.booking_lead_hours, c.booking_horizon_days = max(10, slot_minutes), max(0, booking_lead_hours), max(1, booking_horizon_days)
    if calcom_api_key and not calcom_api_key.startswith("•"):
        c.calcom_api_key = calcom_api_key.strip()
    c.calcom_event_type_id = calcom_event_type_id.strip() or None
    c.booking_link = booking_link.strip() or None
    if c.booking_mode == "calcom" and not (c.calcom_api_key and c.calcom_event_type_id):
        flash(request, "Cal.com mode needs an API key and event type ID.", "error")
    _resync(request, db, c)
    return back(client_id, "agent")


@router.post("/clients/{client_id}/notifications")
def client_notifications(request: Request, client_id: int, notify_email: str = Form(""), notify_sms: str = Form(""),
                         sms_followup: str = Form(""), recording_disclosure: str = Form(""), owner_can_edit: str = Form(""),
                         notify_min_seconds: int = Form(10), db: Session = Depends(get_db)):
    c = _get(db, client_id)
    c.notify_email, c.notify_sms, c.sms_followup = bool(notify_email), bool(notify_sms), bool(sms_followup)
    c.owner_can_edit, c.notify_min_seconds = bool(owner_can_edit), max(0, notify_min_seconds)
    if c.recording_disclosure != bool(recording_disclosure):
        c.recording_disclosure = bool(recording_disclosure)
        if c.agent:
            c.agent.first_message = agent_builder.default_first_message(c, c.agent)
    _resync(request, db, c)
    return back(client_id, "agent")


def _resync(request: Request, db: Session, c: Client) -> None:
    agent = c.agent
    if agent:
        agent_builder.refresh_prompt(c, agent)
        msg = jobs.sync_agent(db, c, agent)
        flash(request, "Saved. " + msg, "ok" if agent.sync_status in ("ok", "skipped") else "error")
    db.commit()


@router.post("/clients/{client_id}/agent")
def agent_save(request: Request, client_id: int, name: str = Form("Ava"), first_message: str = Form(""),
               language: str = Form("en"), voice_provider: str = Form(""), voice_id: str = Form(""),
               llm_model: str = Form(""), custom_instructions: str = Form(""), knowledge: str = Form(""),
               prompt_mode: str = Form("auto"), system_prompt: str = Form(""), db: Session = Depends(get_db)):
    c = _get(db, client_id)
    a = c.agent
    a.name, a.language = name[:60] or "Ava", language or "en"
    a.first_message = first_message.strip() or agent_builder.default_first_message(c, a)
    a.voice_provider, a.voice_id, a.llm_model = voice_provider or None, voice_id or None, llm_model or None
    a.custom_instructions, a.knowledge = custom_instructions or None, knowledge or None
    a.prompt_mode = "manual" if prompt_mode == "manual" else "auto"
    if a.prompt_mode == "manual":
        a.system_prompt = system_prompt
    _resync(request, db, c)
    return back(client_id, "agent")


@router.post("/clients/{client_id}/agent/rebuild")
def agent_rebuild(request: Request, client_id: int, bg: BackgroundTasks, website: str = Form(""),
                  db: Session = Depends(get_db)):
    c = _get(db, client_id)
    if website:
        from ..services import crawler

        c.website = crawler.normalize_url(website)
    if not c.website:
        flash(request, "Add a website first.", "error")
        return back(client_id, "agent")
    c.agent.build_status = "building"
    db.commit()
    bg.add_task(jobs.build_and_sync, c.id)
    flash(request, "Re-reading the website…")
    return back(client_id, "agent")


@router.post("/clients/{client_id}/agent/sync")
def agent_sync(request: Request, client_id: int, db: Session = Depends(get_db)):
    _resync(request, db, _get(db, client_id))
    return back(client_id, "agent")


@router.post("/clients/{client_id}/gaps/{gap_id}")
def gap_resolve(request: Request, client_id: int, gap_id: int, answer: str = Form(""), action: str = Form("answer"),
                db: Session = Depends(get_db)):
    g = db.get(KnowledgeGap, gap_id)
    c = _get(db, client_id)
    if not g or g.client_id != client_id:
        raise HTTPException(404)
    if action == "ignore":
        g.status = "ignored"
        db.commit()
        return back(client_id, "agent")
    if not answer.strip():
        flash(request, "Type the answer first.", "error")
        return back(client_id, "agent")
    g.answer, g.status = answer.strip(), "answered"
    c.agent.learned_faqs = [*(c.agent.learned_faqs or []), {"q": g.question, "a": g.answer}]
    _resync(request, db, c)
    return back(client_id, "agent")


@router.post("/clients/{client_id}/faqs/remove")
def faq_remove(request: Request, client_id: int, index: int = Form(...), db: Session = Depends(get_db)):
    c = _get(db, client_id)
    faqs = list(c.agent.learned_faqs or [])
    if 0 <= index < len(faqs):
        faqs.pop(index)
        c.agent.learned_faqs = faqs
        _resync(request, db, c)
    return back(client_id, "agent")


@router.post("/clients/{client_id}/guardrails")
def client_guardrails(request: Request, client_id: int, monthly_minute_cap: int = Form(0), cap_action: str = Form("alert"),
                      avg_job_value: float = Form(250), report_weekly: str = Form(""), db: Session = Depends(get_db)):
    c = _get(db, client_id)
    c.monthly_minute_cap = max(0, monthly_minute_cap)
    c.cap_action = "forward" if cap_action == "forward" else "alert"
    if c.cap_action == "forward" and not c.transfer_number:
        flash(request, "Cap action 'forward' needs a transfer number (owner's cell) — calls will keep being answered until you add one.", "error")
    c.avg_job_value_cents, c.report_weekly = round(avg_job_value * 100), bool(report_weekly)
    db.commit()
    flash(request, "Guardrails saved.")
    return back(client_id, "billing")


@router.post("/calls/{call_id}/reviewed")
def call_reviewed(call_id: int, db: Session = Depends(get_db)):
    call = db.get(Call, call_id)
    if not call:
        raise HTTPException(404)
    call.reviewed = True
    db.commit()
    return RedirectResponse(f"/calls/{call_id}", status_code=303)


@router.post("/clients/{client_id}/report")
def send_report_now(request: Request, client_id: int, db: Session = Depends(get_db)):
    from ..services import reports

    c = _get(db, client_id)
    try:
        reports.send_weekly(db, c, force=True)
        db.commit()
        flash(request, f"Report sent to {c.owner_email}." if c.owner_email else "No owner email set.")
    except Exception as e:
        flash(request, f"Report failed: {e}", "error")
    return back(client_id, "overview")


# ------------------------------------------------------------------ phone numbers

def _need_assistant(request: Request, db: Session, c: Client) -> str | None:
    a = c.agent
    if not a.vapi_assistant_id:
        jobs.sync_agent(db, c, a)
        db.commit()
    if not a.vapi_assistant_id:
        flash(request, a.sync_error or "Sync the agent with Vapi first.", "error")
    return a.vapi_assistant_id


@router.post("/clients/{client_id}/numbers/buy")
def number_buy(request: Request, client_id: int, area_code: str = Form(""), db: Session = Depends(get_db)):
    from ..services import provisioning

    c = _get(db, client_id)
    try:
        pn = provisioning.provision_number(db, c, area_code.strip() or None)
        db.commit()
    except (http.ProviderError, http.NotConfigured) as e:
        db.commit()  # keep the sync status so the badge explains what's wrong
        flash(request, f"Couldn't get a number: {e}", "error")
        return back(client_id, "phone")
    flash(request, f"Number {pn.number or '(provisioning, refresh in a minute)'} is live and answering.")
    return back(client_id, "phone")


@router.post("/clients/{client_id}/numbers/import")
def number_import(request: Request, client_id: int, number: str = Form(...), db: Session = Depends(get_db)):
    c = _get(db, client_id)
    e164 = messaging.normalize_phone(number)
    if not e164:
        flash(request, "Enter the Twilio number in full, e.g. +14165550123.", "error")
        return back(client_id, "phone")
    aid = _need_assistant(request, db, c)
    if not aid:
        return back(client_id, "phone")
    try:
        data = vapi.import_twilio_number(c, e164)
    except (http.ProviderError, http.NotConfigured) as e:
        flash(request, f"Import failed: {e}", "error")
        return back(client_id, "phone")
    db.add(PhoneNumber(client_id=c.id, agent_id=c.agent.id, number=e164, provider="twilio", vapi_id=data.get("id")))
    db.commit()
    flash(request, f"{e164} now rings the receptionist.")
    return back(client_id, "phone")


@router.post("/clients/{client_id}/numbers/{number_id}/release")
def number_release(request: Request, client_id: int, number_id: int, db: Session = Depends(get_db)):
    n = db.get(PhoneNumber, number_id)
    if not n or n.client_id != client_id:
        raise HTTPException(404)
    if n.vapi_id:
        try:
            vapi.release_number(n.vapi_id)
        except (http.ProviderError, http.NotConfigured) as e:
            flash(request, f"Vapi refused: {e}", "error")
            return back(client_id, "phone")
    db.delete(n)
    db.commit()
    flash(request, "Number released.")
    return back(client_id, "phone")


@router.post("/clients/{client_id}/test-call")
def test_call(request: Request, client_id: int, to: str = Form(...), db: Session = Depends(get_db)):
    c = _get(db, client_id)
    num = next((n for n in c.numbers if n.vapi_id), None)
    e164 = messaging.normalize_phone(to)
    if not (num and c.agent.vapi_assistant_id and e164):
        flash(request, "Needs a synced agent, a phone number, and a valid destination.", "error")
        return back(client_id, "phone")
    try:
        vapi.outbound_test_call(c.agent.vapi_assistant_id, num.vapi_id, e164)
        flash(request, f"Calling {e164} now…")
    except http.ProviderError as e:
        flash(request, f"Call failed: {e}", "error")
    return back(client_id, "phone")


# ------------------------------------------------------------------ calls + inbox

@router.get("/calls", response_class=HTMLResponse)
def all_calls(request: Request, outcome: str = "", db: Session = Depends(get_db)):
    q = select(Call).order_by(Call.created_at.desc()).limit(300)
    if outcome:
        q = q.where(Call.outcome == outcome)
    calls_ = db.scalars(q).all()
    names = {c.id: c.name for c in db.scalars(select(Client))}
    return render(request, "calls/list.html", calls=calls_, names=names, outcome=outcome)


@router.get("/calls/{call_id}", response_class=HTMLResponse)
def call_detail(request: Request, call_id: int, db: Session = Depends(get_db)):
    call = db.get(Call, call_id)
    if not call:
        raise HTTPException(404)
    client = db.get(Client, call.client_id) if call.client_id else None
    contacts = db.scalars(select(Contact).where(Contact.vapi_call_id == call.vapi_call_id)).all()
    bookings = db.scalars(select(Booking).where(Booking.vapi_call_id == call.vapi_call_id)).all()
    return render(request, "calls/detail.html", call=call, client=client, contacts=contacts, bookings=bookings,
                  cost=callsvc.estimated_cost_cents(call), base="/clients")


@router.post("/clients/{client_id}/contacts/{contact_id}/status")
def contact_status(client_id: int, contact_id: int, status: str = Form("handled"), db: Session = Depends(get_db)):
    ct = db.get(Contact, contact_id)
    if ct and ct.client_id == client_id:
        ct.status = status if status in ("new", "handled") else "handled"
        db.commit()
    return back(client_id, "inbox")


@router.post("/clients/{client_id}/bookings/{booking_id}/cancel")
def booking_cancel(client_id: int, booking_id: int, db: Session = Depends(get_db)):
    b = db.get(Booking, booking_id)
    if b and b.client_id == client_id:
        b.status = "cancelled"
        db.commit()
    return back(client_id, "inbox")


# ------------------------------------------------------------------ custom functions + workflows

FN_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,60}$")


@router.post("/clients/{client_id}/functions")
def function_add(request: Request, client_id: int, name: str = Form(...), description: str = Form(...),
                 url: str = Form(...), method: str = Form("POST"), parameters: str = Form(""), headers: str = Form(""),
                 speak_while_running: str = Form(""), db: Session = Depends(get_db)):
    c = _get(db, client_id)
    if not FN_NAME.match(name):
        flash(request, "Function name: letters, numbers, _ or - only.", "error")
        return back(client_id, "automations")
    try:
        params = json.loads(parameters) if parameters.strip() else {"type": "object", "properties": {}}
        hdrs = json.loads(headers) if headers.strip() else {}
        if "properties" not in params:  # allow pasting just the properties map
            params = {"type": "object", "properties": params}
    except json.JSONDecodeError as e:
        flash(request, f"Invalid JSON: {e}", "error")
        return back(client_id, "automations")
    if not url.startswith("https://"):
        flash(request, "Function URL must be https://", "error")
        return back(client_id, "automations")
    db.add(CustomFunction(client_id=c.id, name=name, description=description, url=url, method=method.upper()[:8],
                          parameters=params, headers=hdrs, speak_while_running=speak_while_running or None))
    db.flush()
    _resync(request, db, c)
    return back(client_id, "automations")


@router.post("/clients/{client_id}/functions/{fid}/delete")
def function_delete(request: Request, client_id: int, fid: int, db: Session = Depends(get_db)):
    f = db.get(CustomFunction, fid)
    if f and f.client_id == client_id:
        db.delete(f)
        db.flush()
        _resync(request, db, _get(db, client_id))
    return back(client_id, "automations")


@router.post("/workflows")
def workflow_add(request: Request, name: str = Form(...), trigger: str = Form(...), client_id: str = Form(""),
                 cond_field: str = Form(""), cond_op: str = Form("equals"), cond_value: str = Form(""),
                 action_type: str = Form("sms"), action_to: str = Form(""), action_subject: str = Form(""),
                 action_text: str = Form(""), db: Session = Depends(get_db)):
    if trigger not in workflows.TRIGGERS:
        raise HTTPException(400, "bad trigger")
    conditions = [{"field": cond_field, "op": cond_op, "value": cond_value}] if cond_field else []
    action = {"type": action_type}
    if action_type == "webhook":
        action["url"] = action_to
    else:
        action["to"] = action_to or ("{owner_phone}" if action_type == "sms" else "{owner_email}")
        action["text" if action_type == "sms" else "body"] = action_text
        if action_type == "email":
            action["subject"] = action_subject or "Update from {client_name}"
    cid = int(client_id) if client_id.isdigit() else None
    db.add(Workflow(name=name, trigger=trigger, client_id=cid, conditions=conditions, actions=[action]))
    db.commit()
    flash(request, "Automation saved.")
    return back(cid, "automations") if cid else RedirectResponse("/automations", status_code=303)


@router.post("/workflows/{wid}/toggle")
def workflow_toggle(wid: int, db: Session = Depends(get_db)):
    wf = db.get(Workflow, wid)
    if not wf:
        raise HTTPException(404)
    wf.enabled = not wf.enabled
    db.commit()
    return back(wf.client_id, "automations") if wf.client_id else RedirectResponse("/automations", status_code=303)


@router.post("/workflows/{wid}/delete")
def workflow_delete(wid: int, db: Session = Depends(get_db)):
    wf = db.get(Workflow, wid)
    if not wf:
        raise HTTPException(404)
    cid = wf.client_id
    db.delete(wf)
    db.commit()
    return back(cid, "automations") if cid else RedirectResponse("/automations", status_code=303)


@router.get("/automations", response_class=HTMLResponse)
def automations(request: Request, db: Session = Depends(get_db)):
    flows = db.scalars(select(Workflow).order_by(Workflow.created_at.desc())).all()
    names = {c.id: c.name for c in db.scalars(select(Client))}
    return render(request, "automations.html", workflows=flows, names=names, triggers=workflows.TRIGGERS,
                  ops=workflows.OPS, fields=workflows.FIELDS)


# ------------------------------------------------------------------ billing

def _tiers_from_text(text: str) -> list[dict]:
    tiers = []
    for line in text.strip().splitlines():
        if ":" not in line:
            continue
        up, cents = [x.strip() for x in line.split(":", 1)]
        tiers.append({"up_to": None if up in ("*", "", "inf") else int(up), "cents": float(cents)})
    return tiers


@router.post("/clients/{client_id}/billing")
def billing_save(request: Request, client_id: int, pricing_model: str = Form("flat"), monthly_price: float = Form(0),
                 included_minutes: int = Form(0), overage: float = Form(0), per_minute: float = Form(0),
                 tiers: str = Form(""), setup_fee: float = Form(0), trial_days: int = Form(0),
                 db: Session = Depends(get_db)):
    c = _get(db, client_id)
    c.pricing_model = pricing_model if pricing_model in ("flat", "per_minute", "tiered") else "flat"
    c.monthly_price_cents = round(monthly_price * 100)
    c.included_minutes, c.overage_cents = max(0, included_minutes), round(overage * 100)
    c.per_minute_cents = round(per_minute * 100)
    try:
        c.tiers = _tiers_from_text(tiers) if tiers.strip() else []
    except ValueError:
        flash(request, "Tiers must be lines like 500:0 and *:20 (minutes:cents).", "error")
        return back(client_id, "billing")
    c.setup_fee_cents, c.trial_days = round(setup_fee * 100), max(0, trial_days)
    db.commit()
    flash(request, "Pricing saved: " + pricing.describe(c))
    return back(client_id, "billing")


@router.post("/clients/{client_id}/billing/checkout")
def billing_checkout(request: Request, client_id: int, send: str = Form(""), db: Session = Depends(get_db)):
    from ..services import provisioning
    from .settings_routes import agency_settings

    c = _get(db, client_id)
    try:
        provisioning.send_checkout(db, c, agency_settings(db), email=bool(send))
        db.commit()
    except (http.ProviderError, http.NotConfigured, ValueError) as e:
        db.rollback()
        flash(request, f"Checkout link failed: {e}", "error")
        return back(client_id, "billing")
    flash(request, f"Checkout link emailed to {c.owner_email}." if send else "Checkout link created. Copy it below.")
    return back(client_id, "billing")


@router.post("/clients/{client_id}/billing/resend-usage")
def billing_resend_usage(request: Request, client_id: int, db: Session = Depends(get_db)):
    from .settings_routes import agency_settings

    c = _get(db, client_id)
    sent = 0
    for call in db.scalars(select(Call).where(Call.client_id == c.id, Call.usage_reported.is_(False),
                                              Call.created_at >= callsvc.month_start())):
        try:
            sent += int(billing.report_usage(db, c, call, agency_settings(db)))
        except http.ProviderError as e:
            flash(request, f"Stripe refused usage for call {call.id}: {e}", "error")
            break
    db.commit()
    flash(request, f"Reported {sent} unbilled call(s) to Stripe.")
    return back(client_id, "billing")


@router.post("/clients/{client_id}/owner-login")
def owner_login(request: Request, client_id: int, email: str = Form(...), name: str = Form(""),
                db: Session = Depends(get_db)):
    from ..services import provisioning
    from .settings_routes import agency_settings

    c = _get(db, client_id)
    try:
        pw = provisioning.create_owner_login(db, c, agency_settings(db), email, name)
        db.commit()
    except ValueError as e:
        flash(request, str(e), "error")
        return back(client_id, "billing")
    except Exception as e:
        db.commit()
        flash(request, f"Login created, but the invite email failed ({e}).", "error")
        return back(client_id, "billing")
    flash(request, f"Owner login created and emailed. Temporary password (shown once): {pw}")
    return back(client_id, "billing")


@router.post("/clients/{client_id}/delete")
def client_delete(request: Request, client_id: int, confirm: str = Form(""), db: Session = Depends(get_db)):
    c = _get(db, client_id)
    if confirm.strip() != c.name:
        flash(request, "Type the client's exact name to delete it.", "error")
        return back(client_id, "billing")
    errors = []
    for n in c.numbers:
        if n.vapi_id:
            try:
                vapi.release_number(n.vapi_id)
            except Exception as e:
                errors.append(str(e))
    if c.agent and c.agent.vapi_assistant_id:
        try:
            vapi.delete_assistant(c.agent.vapi_assistant_id)
        except Exception as e:
            errors.append(str(e))
    db.delete(c)
    db.commit()
    flash(request, "Client deleted." + (f" Vapi cleanup warnings: {'; '.join(errors)}" if errors else ""))
    return RedirectResponse("/clients", status_code=303)


@router.get("/billing", response_class=HTMLResponse)
def billing_overview(request: Request, db: Session = Depends(get_db)):
    from ..models import Payment

    clients = db.scalars(select(Client).where(Client.status.in_(["active", "demo", "paused"]))).all()
    usage = usage_this_month(db)
    rows = []
    for c in clients:
        u = usage.get(c.id, {"calls": 0, "minutes": 0, "cost": 0.0})
        est = pricing.monthly_charge_cents(c, u["minutes"]) if c.status == "active" else 0
        rows.append({"client": c, "usage": u, "estimate": est, "margin": est - u["cost"],
                     "underwater": c.status == "active" and u["cost"] > est})
    since = datetime.now(timezone.utc) - timedelta(days=30)
    collected = db.scalar(select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(Payment.created_at >= since)) or 0
    return render(request, "billing.html", rows=rows, collected=collected, stripe_ready=billing.configured())
