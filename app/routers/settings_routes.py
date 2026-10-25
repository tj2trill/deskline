from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import AgencySettings, EventLog, User
from ..security import admin_user, agency_user, hash_password, verify_password
from ..services import billing, leads, llm, messaging, vapi
from ..templating import flash, render

router = APIRouter()


def agency_settings(db: Session) -> AgencySettings:
    a = db.scalars(select(AgencySettings)).first()
    if not a:
        a = AgencySettings(brand_name=get_settings().app_name)
        db.add(a)
        db.flush()
    return a


def integration_status() -> list[dict]:
    s = get_settings()
    return [
        {"name": "Public URL", "ok": not s.base_url.startswith("http://localhost"),
         "hint": f"BASE_URL={s.base_url} — Vapi and Stripe must reach it over HTTPS (use a tunnel in dev)."},
        {"name": "Vapi (calls)", "ok": vapi.configured(), "hint": "VAPI_API_KEY (private key)"},
        {"name": "Text LLM (builder, chat, writing)", "ok": llm.available(), "hint": f"LLM_BASE_URL={s.llm_base_url} model={s.llm_model}"},
        {"name": "Twilio SMS", "ok": messaging.sms_configured(), "hint": "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER (register A2P 10DLC or toll-free first)"},
        {"name": "Email", "ok": s.email_provider != "console", "hint": f"EMAIL_PROVIDER={s.email_provider} (console just logs)"},
        {"name": "Stripe billing", "ok": billing.configured() and bool(s.stripe_webhook_secret), "hint": "STRIPE_SECRET_KEY + STRIPE_WEBHOOK_SECRET"},
        {"name": "Reply capture", "ok": bool(s.inbound_reply_to), "hint": "INBOUND_REPLY_TO, forwarded to the inbound webhook below. Lets the agents learn from replies."},
        {"name": "Lead finder", "ok": leads.configured(), "hint": "GOOGLE_PLACES_API_KEY with Places API (New)"},
        {"name": "Firecrawl (optional)", "ok": bool(s.firecrawl_api_key), "hint": "Better extraction on JS-heavy sites; built-in crawler otherwise"},
    ]


@router.get("/settings", response_class=HTMLResponse, dependencies=[Depends(agency_user)])
def settings_page(request: Request, db: Session = Depends(get_db)):
    s = get_settings()
    events = db.scalars(select(EventLog).order_by(EventLog.created_at.desc()).limit(40)).all()
    team = db.scalars(select(User).where(User.role.in_(["admin", "staff"]))).all()
    return render(request, "settings.html", agency=agency_settings(db), status=integration_status(), events=events,
                  team=team, outbox=list(reversed(messaging.OUTBOX[-10:])),
                  vapi_webhook=f"{s.base_url}/webhooks/vapi", stripe_webhook=f"{s.base_url}/webhooks/stripe",
                  inbound_webhook=f"{s.base_url}/webhooks/inbound-email/{s.webhook_token}")


@router.post("/settings", dependencies=[Depends(admin_user)])
def settings_save(request: Request, brand_name: str = Form(...), logo_url: str = Form(""), primary_color: str = Form("#2563eb"),
                  website: str = Form(""), support_email: str = Form(""), support_phone: str = Form(""),
                  mailing_address: str = Form(""), sender_name: str = Form(""), default_monthly_price: float = Form(297),
                  default_included_minutes: int = Form(500), default_overage: float = Form(0.25),
                  default_setup_fee: float = Form(0), default_trial_days: int = Form(0), daily_email_cap: int = Form(40),
                  timezone: str = Form("America/Toronto"), alert_email: str = Form(""), margin_alert_pct: int = Form(70),
                  db: Session = Depends(get_db)):
    from zoneinfo import ZoneInfo

    a = agency_settings(db)
    try:
        ZoneInfo(timezone)
        a.timezone = timezone
    except Exception:
        flash(request, f"Unknown timezone '{timezone}' — use an IANA name like America/Toronto.", "error")
    a.alert_email, a.margin_alert_pct = alert_email or None, max(1, min(100, margin_alert_pct))
    a.brand_name, a.logo_url, a.primary_color = brand_name[:120], logo_url or None, primary_color[:20]
    a.website, a.support_email, a.support_phone = website or None, support_email or None, support_phone or None
    a.mailing_address, a.sender_name = mailing_address or None, sender_name or None
    a.default_monthly_price_cents = round(default_monthly_price * 100)
    a.default_included_minutes = max(0, default_included_minutes)
    a.default_overage_cents = round(default_overage * 100)
    a.default_setup_fee_cents = round(default_setup_fee * 100)
    a.default_trial_days, a.daily_email_cap = max(0, default_trial_days), max(1, daily_email_cap)
    db.commit()
    flash(request, "Settings saved.")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/test", dependencies=[Depends(agency_user)])
def settings_test(request: Request, channel: str = Form(...), to: str = Form(...), db: Session = Depends(get_db)):
    brand = agency_settings(db).brand_name
    try:
        if channel == "sms":
            r = messaging.send_sms(to, f"{brand}: test message ✔")
        else:
            r = messaging.send_email(to, f"{brand} test email", "<p>Email delivery works.</p>")
        flash(request, f"Sent ({r}).")
    except Exception as e:
        flash(request, f"Failed: {e}", "error")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/team", dependencies=[Depends(admin_user)])
def team_add(request: Request, email: str = Form(...), name: str = Form(""), role: str = Form("staff"),
             db: Session = Depends(get_db)):
    email = email.strip().lower()
    if db.scalars(select(User).where(User.email == email)).first():
        flash(request, "That email already has a login.", "error")
        return RedirectResponse("/settings", status_code=303)
    pw = secrets.token_urlsafe(9)
    db.add(User(email=email, name=name or None, role="admin" if role == "admin" else "staff", password_hash=hash_password(pw)))
    db.commit()
    flash(request, f"Team login created for {email}. Temporary password (shown once): {pw}")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/team/{uid}/delete", dependencies=[Depends(admin_user)])
def team_delete(request: Request, uid: int, db: Session = Depends(get_db)):
    me = request.session.get("uid")
    u = db.get(User, uid)
    if not u or u.id == me:
        raise HTTPException(400, "Can't remove that user")
    db.delete(u)
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/account/password")
def change_password(request: Request, current: str = Form(...), new: str = Form(...), db: Session = Depends(get_db)):
    from ..security import current_user

    user = current_user(request, db)
    if len(new) < 10 or not verify_password(current, user.password_hash):
        flash(request, "Current password wrong, or new password under 10 characters.", "error")
    else:
        user.password_hash = hash_password(new)
        db.commit()
        flash(request, "Password changed.")
    return RedirectResponse("/portal" if not user.is_agency else "/settings", status_code=303)
