"""Client lifecycle actions shared by the dashboard and the agents:
create a client, give it a number, send a checkout link, onboard the owner."""
from __future__ import annotations

import re
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AgencySettings, Agent, Client, PhoneNumber, Prospect, User, slugify
from ..security import esc, hash_password
from . import billing, crawler, http, messaging, pricing, vapi


def unique_client_slug(db: Session, name: str) -> str:
    base, i = slugify(name), 2
    slug = base
    while db.scalars(select(Client).where(Client.slug == slug)).first():
        slug, i = f"{base}-{i}", i + 1
    return slug


def create_client(db: Session, agency: AgencySettings, *, website: str = "", name: str = "", industry: str = "",
                  business_phone: str = "", owner_name: str = "", owner_email: str = "", owner_phone: str = "",
                  timezone: str = "America/Toronto", agent_name: str = "Ava", status: str = "demo",
                  prospect: Prospect | None = None) -> Client:
    from ..security import safe_url

    website = safe_url(crawler.normalize_url(website)) if website.strip() else ""
    if not (website or name.strip()):
        raise ValueError("Add a website or a business name.")
    display = name.strip() or re.sub(r"^https?://(www\.)?", "", website).split("/")[0]
    client = Client(name=display[:160], slug=unique_client_slug(db, display), website=website or None,
                    industry=industry or None, business_phone=business_phone or None, owner_name=owner_name or None,
                    owner_email=owner_email.strip().lower() or None, owner_phone=owner_phone or None,
                    timezone=timezone or "America/Toronto", status=status if status in ("demo", "active") else "demo",
                    monthly_price_cents=agency.default_monthly_price_cents, included_minutes=agency.default_included_minutes,
                    overage_cents=agency.default_overage_cents, setup_fee_cents=agency.default_setup_fee_cents,
                    trial_days=agency.default_trial_days)
    db.add(client)
    db.flush()
    db.add(Agent(client_id=client.id, name=(agent_name or "Ava")[:60], build_status="building" if website else "ready"))
    if prospect:
        prospect.client_id, prospect.status = client.id, ("demo" if prospect.status in ("new", "contacted") else prospect.status)
        if not client.owner_email and prospect.email:
            client.owner_email = prospect.email.lower()
    db.flush()
    return client


def area_code_of(phone: str | None) -> str | None:
    e164 = messaging.normalize_phone(phone)
    return e164[2:5] if e164 and e164.startswith("+1") else None


def provision_number(db: Session, client: Client, area_code: str | None = None) -> PhoneNumber:
    """Get a Vapi number routed to this client (assistant must be synced first)."""
    from ..jobs import sync_agent

    agent = client.agent
    if not agent.vapi_assistant_id:
        sync_agent(db, client, agent)
    if not agent.vapi_assistant_id:
        raise http.NotConfigured(agent.sync_error or "Sync the agent with Vapi first.")
    data = vapi.buy_number(client, area_code)
    num = data.get("number")
    if not num and data.get("id"):
        try:
            num = vapi.get_number(data["id"]).get("number")
        except http.ProviderError:
            pass
    pn = PhoneNumber(client_id=client.id, agent_id=agent.id, number=num, provider="vapi", vapi_id=data.get("id"))
    db.add(pn)
    db.flush()
    return pn


def send_checkout(db: Session, client: Client, agency: AgencySettings, email: bool = True) -> str:
    url = billing.create_checkout(db, client, agency)
    if email:
        if not client.owner_email:
            raise ValueError("The client has no owner email to send the checkout link to.")
        messaging.send_email(client.owner_email, f"Activate your AI receptionist with {agency.brand_name}",
                             f"<p>Hi {esc(client.owner_name or 'there')},</p><p>Your receptionist for {esc(client.name)} is ready. "
                             f"Plan: {esc(pricing.describe(client))}.</p><p><a href=\"{esc(url)}\">Start your subscription</a></p>")
    return url


def create_owner_login(db: Session, client: Client, agency: AgencySettings, email: str, name: str | None = None,
                       welcome: bool = False) -> str:
    """Create the owner's portal login and email it. Returns the temporary password."""
    email = email.strip().lower()
    if db.scalars(select(User).where(User.email == email)).first():
        raise ValueError("That email already has a login.")
    pw = secrets.token_urlsafe(9)
    db.add(User(email=email, name=name or None, role="owner", client_id=client.id, password_hash=hash_password(pw)))
    db.flush()
    s = get_settings()
    number = next((n.number for n in client.numbers if n.number), None)
    extra = ""
    if welcome:
        extra = ("<p>To go live, forward your business line to "
                 f"<b>{esc(number or 'your AI number')}</b> when you're busy or closed. On most phones that's *72 followed by the number. "
                 "You'll get a summary after every call and a report each Monday.</p>")
    messaging.send_email(email, f"Welcome to {agency.brand_name}" if welcome else f"Your {agency.brand_name} dashboard login",
                         f"<p>Your call dashboard for {esc(client.name)}: <a href=\"{s.base_url}/login\">{s.base_url}/login</a></p>"
                         f"<p>Email: {esc(email)}<br>Temporary password: <code>{esc(pw)}</code></p>"
                         f"<p>Change it after your first sign-in.</p>{extra}")
    return pw
