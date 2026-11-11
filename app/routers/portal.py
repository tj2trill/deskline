"""White-label portal for business owners: their calls, inbox, bookings, business info, billing."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import jobs
from ..db import get_db
from ..models import Booking, Call, Client, Contact, KnowledgeGap, User
from ..security import current_user
from ..services import agent_builder, billing, http, reports
from ..templating import flash, render

router = APIRouter(prefix="/portal")


def _client(request: Request, db: Session, user: User) -> Client:
    if user.is_agency:
        cid = request.query_params.get("client_id") or request.session.get("preview_client")
        if not cid or not str(cid).isdigit():
            raise HTTPException(400, "Agency users: open the portal from a client page (View as owner).")
        request.session["preview_client"] = int(cid)
        c = db.get(Client, int(cid))
    else:
        c = db.get(Client, user.client_id) if user.client_id else None
    if not c:
        raise HTTPException(404)
    return c


@router.get("", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = _client(request, db, user)
    now = datetime.now(timezone.utc)
    upcoming = db.scalars(select(Booking).where(Booking.client_id == c.id, Booking.status == "confirmed",
                                                Booking.start_at >= now).order_by(Booking.start_at).limit(10)).all()
    recent = db.scalars(select(Call).where(Call.client_id == c.id).order_by(Call.created_at.desc()).limit(10)).all()
    new = db.scalars(select(Contact).where(Contact.client_id == c.id, Contact.status == "new")
                     .order_by(Contact.created_at.desc()).limit(20)).all()
    return render(request, "portal/home.html", c=c, stats=reports.stats(db, c), upcoming=upcoming, recent=recent,
                  new=new, ai_number=next((n.number for n in c.numbers if n.number), None))


@router.get("/calls", response_class=HTMLResponse)
def calls_list(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = _client(request, db, user)
    rows = db.scalars(select(Call).where(Call.client_id == c.id).order_by(Call.created_at.desc()).limit(200)).all()
    return render(request, "portal/calls.html", c=c, calls=rows)


@router.get("/calls/{call_id}", response_class=HTMLResponse)
def call_detail(request: Request, call_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = _client(request, db, user)
    call = db.get(Call, call_id)
    if not call or call.client_id != c.id:
        raise HTTPException(404)
    contacts = db.scalars(select(Contact).where(Contact.vapi_call_id == call.vapi_call_id)).all()
    bookings = db.scalars(select(Booking).where(Booking.vapi_call_id == call.vapi_call_id)).all()
    return render(request, "calls/detail.html", call=call, client=c, contacts=contacts, bookings=bookings,
                  cost=None, base="/portal", portal=True)


@router.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = _client(request, db, user)
    contacts = db.scalars(select(Contact).where(Contact.client_id == c.id).order_by(Contact.created_at.desc()).limit(200)).all()
    bookings = db.scalars(select(Booking).where(Booking.client_id == c.id).order_by(Booking.start_at.desc()).limit(200)).all()
    return render(request, "portal/inbox.html", c=c, contacts=contacts, bookings=bookings)


@router.post("/contacts/{contact_id}/handled")
def contact_handled(request: Request, contact_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = _client(request, db, user)
    ct = db.get(Contact, contact_id)
    if not ct or ct.client_id != c.id:
        raise HTTPException(404)
    ct.status = "handled"
    db.commit()
    return RedirectResponse("/portal/inbox", status_code=303)


@router.get("/business", response_class=HTMLResponse)
def business(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = _client(request, db, user)
    gaps = db.scalars(select(KnowledgeGap).where(KnowledgeGap.client_id == c.id, KnowledgeGap.status == "open")
                      .order_by(KnowledgeGap.times_asked.desc()).limit(20)).all()
    return render(request, "portal/business.html", c=c, agent=c.agent, days=agent_builder.DAYS,
                  day_names=agent_builder.DAY_NAMES, gaps=gaps)


@router.post("/business")
async def business_save(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = _client(request, db, user)
    if not c.owner_can_edit and not user.is_agency:
        raise HTTPException(403, "Editing is managed by your provider.")
    f = dict(await request.form())
    hours = {}
    for d in agent_builder.DAYS:
        if f.get(f"{d}_open") and f.get(f"{d}_close") and not f.get(f"{d}_closed"):
            hours[d] = [[f[f"{d}_open"][:5], f[f"{d}_close"][:5]]]
        else:
            hours[d] = []
    c.business_hours = hours
    if c.agent:
        c.agent.custom_instructions = (f.get("custom_instructions") or "").strip() or None
        learned = list(c.agent.learned_faqs or [])
        for g in db.scalars(select(KnowledgeGap).where(KnowledgeGap.client_id == c.id, KnowledgeGap.status == "open")):
            ans = (f.get(f"gap_{g.id}") or "").strip()
            if ans:
                g.answer, g.status = ans, "answered"
                learned.append({"q": g.question, "a": ans})
        c.agent.learned_faqs = learned
        agent_builder.refresh_prompt(c, c.agent)
        msg = jobs.sync_agent(db, c, c.agent)
    else:
        msg = ""
    db.commit()
    flash(request, "Saved — your receptionist is updated." if "fail" not in msg.lower() else msg,
          "ok" if "fail" not in msg.lower() else "error")
    return RedirectResponse("/portal/business", status_code=303)


@router.post("/billing")
def billing_portal(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = _client(request, db, user)
    try:
        return RedirectResponse(billing.portal_url(c), status_code=303)
    except (ValueError, http.ProviderError, http.NotConfigured) as e:
        flash(request, f"Billing portal unavailable: {e}", "error")
        return RedirectResponse("/portal", status_code=303)
