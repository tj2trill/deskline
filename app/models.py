from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_token(n: int = 18) -> str:
    return secrets.token_urlsafe(n)


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60] or "site"


DEFAULT_HOURS = {
    "mon": [["09:00", "17:00"]],
    "tue": [["09:00", "17:00"]],
    "wed": [["09:00", "17:00"]],
    "thu": [["09:00", "17:00"]],
    "fri": [["09:00", "17:00"]],
    "sat": [],
    "sun": [],
}


class AgencySettings(Base):
    __tablename__ = "agency_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_name: Mapped[str] = mapped_column(String(120), default="Deskline")
    timezone: Mapped[str] = mapped_column(String(60), default="America/Toronto")  # dashboard display
    logo_url: Mapped[str | None] = mapped_column(String(500))
    primary_color: Mapped[str] = mapped_column(String(20), default="#2563eb")
    website: Mapped[str | None] = mapped_column(String(300))
    support_email: Mapped[str | None] = mapped_column(String(200))
    support_phone: Mapped[str | None] = mapped_column(String(40))
    mailing_address: Mapped[str | None] = mapped_column(String(300))  # required in cold-email footers
    sender_name: Mapped[str | None] = mapped_column(String(120))
    # Defaults applied to new clients
    default_monthly_price_cents: Mapped[int] = mapped_column(Integer, default=29700)
    default_included_minutes: Mapped[int] = mapped_column(Integer, default=500)
    default_overage_cents: Mapped[int] = mapped_column(Integer, default=25)
    default_setup_fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    default_trial_days: Mapped[int] = mapped_column(Integer, default=0)
    stripe_meter_id: Mapped[str | None] = mapped_column(String(80))
    stripe_meter_event_name: Mapped[str] = mapped_column(String(80), default="deskline_minutes")
    daily_email_cap: Mapped[int] = mapped_column(Integer, default=40)
    alert_email: Mapped[str | None] = mapped_column(String(200))  # where margin / failure alerts go
    margin_alert_pct: Mapped[int] = mapped_column(Integer, default=70)  # alert when cost ≥ this % of revenue
    agents_enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # master switch for the agent team
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="staff")  # admin | staff | owner
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_agency(self) -> bool:
        return self.role in ("admin", "staff")


class Client(Base):
    """A business you sell the receptionist to."""

    __tablename__ = "clients"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="demo")  # demo | active | paused | churned
    website: Mapped[str | None] = mapped_column(String(300))
    industry: Mapped[str | None] = mapped_column(String(120))
    business_phone: Mapped[str | None] = mapped_column(String(40))
    transfer_number: Mapped[str | None] = mapped_column(String(40))
    owner_name: Mapped[str | None] = mapped_column(String(120))
    owner_email: Mapped[str | None] = mapped_column(String(200))
    owner_phone: Mapped[str | None] = mapped_column(String(40))
    timezone: Mapped[str] = mapped_column(String(60), default="America/Toronto")
    address: Mapped[str | None] = mapped_column(String(300))
    service_area: Mapped[str | None] = mapped_column(String(300))
    business_hours: Mapped[dict] = mapped_column(JSON, default=lambda: dict(DEFAULT_HOURS))

    # Booking
    booking_mode: Mapped[str] = mapped_column(String(20), default="internal")  # internal | calcom | none
    slot_minutes: Mapped[int] = mapped_column(Integer, default=60)
    booking_lead_hours: Mapped[int] = mapped_column(Integer, default=2)
    booking_horizon_days: Mapped[int] = mapped_column(Integer, default=21)
    calcom_api_key: Mapped[str | None] = mapped_column(String(200))
    calcom_event_type_id: Mapped[str | None] = mapped_column(String(40))
    booking_link: Mapped[str | None] = mapped_column(String(300))

    # Notifications / behaviour
    notify_email: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_sms: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_min_seconds: Mapped[int] = mapped_column(Integer, default=10)
    sms_followup: Mapped[bool] = mapped_column(Boolean, default=True)
    recording_disclosure: Mapped[bool] = mapped_column(Boolean, default=True)
    owner_can_edit: Mapped[bool] = mapped_column(Boolean, default=True)

    # Website chat widget
    widget_key: Mapped[str] = mapped_column(String(40), unique=True, default=lambda: new_token(12))
    widget_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    widget_allowed_origins: Mapped[str | None] = mapped_column(String(500))  # comma list; blank = any

    # Pricing you charge this client
    pricing_model: Mapped[str] = mapped_column(String(20), default="flat")  # flat | per_minute | tiered
    monthly_price_cents: Mapped[int] = mapped_column(Integer, default=29700)
    included_minutes: Mapped[int] = mapped_column(Integer, default=500)
    overage_cents: Mapped[int] = mapped_column(Integer, default=25)
    per_minute_cents: Mapped[int] = mapped_column(Integer, default=0)
    tiers: Mapped[list] = mapped_column(JSON, default=list)  # [{"up_to": 500, "cents": 0}, {"up_to": None, "cents": 20}]
    setup_fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    trial_days: Mapped[int] = mapped_column(Integer, default=0)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(80))
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(80))
    stripe_status: Mapped[str | None] = mapped_column(String(40))
    stripe_product_id: Mapped[str | None] = mapped_column(String(80))
    stripe_price_ids: Mapped[dict] = mapped_column(JSON, default=dict)
    checkout_url: Mapped[str | None] = mapped_column(String(1000))

    # Cost guardrail: when monthly minutes hit the cap, calls forward to the business line
    # (cap_action="forward") or keep answering while alerting you (cap_action="alert").
    monthly_minute_cap: Mapped[int] = mapped_column(Integer, default=0)  # 0 = no cap
    cap_action: Mapped[str] = mapped_column(String(10), default="alert")
    cap_tripped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    past_due_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Weekly value report to the owner (retention)
    report_weekly: Mapped[bool] = mapped_column(Boolean, default=True)
    avg_job_value_cents: Mapped[int] = mapped_column(Integer, default=25000)
    last_report_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Revenue recovery (missed-call text-back, quote follow-up, invoice reminders). See services/recovery.py.
    recovery_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # forward = the owner's carrier forwards unanswered calls to the recovery number (every call that lands is missed)
    # ring    = the recovery number IS the business line; it rings the owner first and screens for voicemail
    missed_call_mode: Mapped[str] = mapped_column(String(10), default="forward")
    ring_seconds: Mapped[int] = mapped_column(Integer, default=20)
    textback_opener: Mapped[str | None] = mapped_column(Text)  # blank = built-in opener
    ask_photos: Mapped[bool] = mapped_column(Boolean, default=True)
    quote_followup_days: Mapped[list] = mapped_column(JSON, default=lambda: [1, 3, 7, 14])
    quote_max_age_days: Mapped[int] = mapped_column(Integer, default=60)  # older imported quotes aren't contacted
    invoice_reminder_days: Mapped[list] = mapped_column(JSON, default=lambda: [-3, 0, 3, 7, 14])
    invoice_escalate_days: Mapped[int] = mapped_column(Integer, default=21)  # hand to the owner, stop automated texts
    default_deposit_cents: Mapped[int] = mapped_column(Integer, default=0)
    deposit_link: Mapped[str | None] = mapped_column(String(500))
    payment_instructions: Mapped[str | None] = mapped_column(String(300))  # e.g. "e-Transfer to pay@maple.ca"
    send_window_start: Mapped[str] = mapped_column(String(5), default="08:30")  # follow-ups/reminders only
    send_window_end: Mapped[str] = mapped_column(String(5), default="19:30")
    performance_fee_pct: Mapped[float] = mapped_column(Float, default=0)

    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    agents: Mapped[list[Agent]] = relationship(back_populates="client", cascade="all, delete-orphan")
    # Receptionist (Vapi) numbers only: every existing screen treats client.numbers as "the AI line".
    numbers: Mapped[list[PhoneNumber]] = relationship(
        primaryjoin="and_(Client.id == PhoneNumber.client_id, PhoneNumber.purpose != 'recovery')",
        back_populates="client", cascade="all, delete-orphan")
    # Recovery (Twilio) numbers; rows are added/removed directly, the DB cascade cleans up with the client.
    recovery_numbers: Mapped[list[PhoneNumber]] = relationship(
        primaryjoin="and_(Client.id == PhoneNumber.client_id, PhoneNumber.purpose == 'recovery')", viewonly=True)

    @property
    def agent(self) -> Agent | None:
        return self.agents[0] if self.agents else None


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(60), default="Ava")
    language: Mapped[str] = mapped_column(String(12), default="en")
    voice_provider: Mapped[str | None] = mapped_column(String(40))
    voice_id: Mapped[str | None] = mapped_column(String(120))
    llm_model: Mapped[str | None] = mapped_column(String(80))
    first_message: Mapped[str | None] = mapped_column(Text)
    prompt_mode: Mapped[str] = mapped_column(String(10), default="auto")  # auto | manual
    system_prompt: Mapped[str | None] = mapped_column(Text)
    custom_instructions: Mapped[str | None] = mapped_column(Text)
    knowledge: Mapped[str | None] = mapped_column(Text)
    learned_faqs: Mapped[list] = mapped_column(JSON, default=list)  # answered knowledge gaps; survive rebuilds
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    vapi_assistant_id: Mapped[str | None] = mapped_column(String(80), index=True)
    sync_status: Mapped[str] = mapped_column(String(20), default="never")  # never | ok | error | skipped
    sync_error: Mapped[str | None] = mapped_column(Text)
    build_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | building | ready | error
    build_error: Mapped[str | None] = mapped_column(Text)
    built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    client: Mapped[Client] = relationship(back_populates="agents")


class PhoneNumber(Base):
    __tablename__ = "phone_numbers"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    number: Mapped[str | None] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(20), default="vapi")  # vapi | twilio
    vapi_id: Mapped[str | None] = mapped_column(String(80), index=True)
    label: Mapped[str | None] = mapped_column(String(80))
    purpose: Mapped[str] = mapped_column(String(12), default="receptionist")  # receptionist | recovery
    twilio_sid: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    client: Mapped[Client] = relationship(back_populates="numbers")


class Call(Base):
    __tablename__ = "calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    vapi_call_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    call_type: Mapped[str | None] = mapped_column(String(40))
    from_number: Mapped[str | None] = mapped_column(String(40))
    to_number: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str | None] = mapped_column(String(30))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float] = mapped_column(Float, default=0)
    billable_minutes: Mapped[int] = mapped_column(Integer, default=0)
    ended_reason: Mapped[str | None] = mapped_column(String(120))
    summary: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    recording_url: Mapped[str | None] = mapped_column(String(1000))
    structured: Mapped[dict] = mapped_column(JSON, default=dict)
    outcome: Mapped[str | None] = mapped_column(String(30))  # booked | transferred | message | lead | info | spam | missed
    cost_cents: Mapped[float | None] = mapped_column(Float)  # provider-reported
    usage_reported: Mapped[bool] = mapped_column(Boolean, default=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)  # side effects ran (webhooks get retried)
    sentiment: Mapped[str | None] = mapped_column(String(12))  # positive | neutral | negative
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    flag_reason: Mapped[str | None] = mapped_column(String(300))
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class KnowledgeGap(Base):
    """A question callers asked that the agent couldn't answer. Answer it once → agent learns."""

    __tablename__ = "knowledge_gaps"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id", ondelete="SET NULL"))
    question: Mapped[str] = mapped_column(String(500))
    times_asked: Mapped[int] = mapped_column(Integer, default=1)
    answer: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | answered | ignored
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Contact(Base):
    """Leads, messages and callback requests captured by the phone agent or chat widget."""

    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id", ondelete="SET NULL"))
    vapi_call_id: Mapped[str | None] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="lead")  # lead | message | callback
    name: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(200))
    details: Mapped[str | None] = mapped_column(Text)
    urgency: Mapped[str] = mapped_column(String(10), default="normal")
    source: Mapped[str] = mapped_column(String(10), default="call")  # call | chat | site
    status: Mapped[str] = mapped_column(String(12), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    vapi_call_id: Mapped[str | None] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(10), default="call")
    name: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(200))
    service: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str] = mapped_column(String(12), default="internal")
    external_id: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(12), default="confirmed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CustomFunction(Base):
    """A tool the phone agent can call that hits any HTTPS endpoint (CRM, Zapier, n8n...)."""

    __tablename__ = "custom_functions"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)  # JSON schema
    url: Mapped[str] = mapped_column(String(500))
    method: Mapped[str] = mapped_column(String(8), default="POST")
    headers: Mapped[dict] = mapped_column(JSON, default=dict)
    speak_while_running: Mapped[str | None] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Workflow(Base):
    """If <trigger> and <conditions> then <actions>. client_id NULL = applies to every client."""

    __tablename__ = "workflows"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    trigger: Mapped[str] = mapped_column(String(30))
    conditions: Mapped[list] = mapped_column(JSON, default=list)
    actions: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    visitor_id: Mapped[str] = mapped_column(String(64), index=True)
    messages: Mapped[list] = mapped_column(JSON, default=list)
    page_url: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProspectSearch(Base):
    __tablename__ = "prospect_searches"
    id: Mapped[int] = mapped_column(primary_key=True)
    niche: Mapped[str] = mapped_column(String(120))
    location: Mapped[str] = mapped_column(String(160))
    source: Mapped[str] = mapped_column(String(20), default="google_places")
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Prospect(Base):
    __tablename__ = "prospects"
    id: Mapped[int] = mapped_column(primary_key=True)
    search_id: Mapped[int | None] = mapped_column(ForeignKey("prospect_searches.id", ondelete="SET NULL"), index=True)
    place_id: Mapped[str | None] = mapped_column(String(200), index=True)
    name: Mapped[str] = mapped_column(String(200))
    niche: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(40))
    website: Mapped[str | None] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(String(300))
    rating: Mapped[float | None] = mapped_column(Float)
    review_count: Mapped[int | None] = mapped_column(Integer)
    maps_url: Mapped[str | None] = mapped_column(String(500))
    types: Mapped[list] = mapped_column(JSON, default=list)
    hours: Mapped[list] = mapped_column(JSON, default=list)  # weekdayDescriptions
    signals: Mapped[dict] = mapped_column(JSON, default=dict)
    score: Mapped[int] = mapped_column(Integer, default=0)
    score_reasons: Mapped[list] = mapped_column(JSON, default=list)
    enriched: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(12), default="new")  # new contacted replied demo won lost
    notes: Mapped[str | None] = mapped_column(Text)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="SET NULL"))
    # learning: which outreach angle this prospect got, whether the first email carried a live demo line,
    # and when they replied (set by the inbound reply webhook or when you mark them replied)
    angle: Mapped[str | None] = mapped_column(String(30))
    contacted_with_demo: Mapped[bool | None] = mapped_column(Boolean)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Proposal(Base):
    __tablename__ = "proposals"
    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(40), unique=True, default=lambda: new_token(16))
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"))
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmailDraft(Base):
    __tablename__ = "email_drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    step: Mapped[int] = mapped_column(Integer, default=1)
    send_after_days: Mapped[int] = mapped_column(Integer, default=0)
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="draft")  # draft | sent | skipped
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Suppression(Base):
    __tablename__ = "suppressions"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    reason: Mapped[str] = mapped_column(String(40), default="unsubscribe")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Site(Base):
    __tablename__ = "sites"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"))
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    domain: Mapped[str | None] = mapped_column(String(200), unique=True)
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    theme_color: Mapped[str] = mapped_column(String(20), default="#2563eb")
    published: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="SET NULL"), index=True)
    stripe_invoice_id: Mapped[str] = mapped_column(String(80), unique=True)
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="usd")
    status: Mapped[str] = mapped_column(String(20), default="paid")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class EventLog(Base):
    __tablename__ = "event_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int | None] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    level: Mapped[str] = mapped_column(String(8), default="info")
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AgentConfig(Base):
    """One row per agent: how autonomous it is, its knobs, and its schedule state."""

    __tablename__ = "agent_configs"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    mode: Mapped[str] = mapped_column(String(10), default="approve")  # off | approve | auto
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    running_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(12))
    last_summary: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[dict] = mapped_column(JSON, default=dict)  # agent-private memory (e.g. last health check)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_key: Mapped[str] = mapped_column(String(40), index=True)
    trigger: Mapped[str] = mapped_column(String(12), default="schedule")  # schedule | manual
    status: Mapped[str] = mapped_column(String(12), default="running")  # running | ok | error | skipped
    summary: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    log: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentAction(Base):
    """Something an agent wants to do in the outside world. In 'Ask me first' mode it waits
    here for approval; in Autopilot it's executed immediately and recorded."""

    __tablename__ = "agent_actions"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_key: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(300))
    detail: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending approved rejected done failed
    result: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InboundEmail(Base):
    """A reply that arrived through the inbound-email webhook, with its typed classification."""

    __tablename__ = "inbound_emails"
    id: Mapped[int] = mapped_column(primary_key=True)
    from_email: Mapped[str] = mapped_column(String(200), index=True)
    subject: Mapped[str | None] = mapped_column(String(300))
    text: Mapped[str | None] = mapped_column(Text)
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="SET NULL"), index=True)
    intent: Mapped[str | None] = mapped_column(String(20))  # interested | question | not_now | not_interested | unsubscribe | auto_reply | unclear
    confidence: Mapped[float | None] = mapped_column(Float)
    method: Mapped[str | None] = mapped_column(String(10))  # llm | rules
    applied: Mapped[str | None] = mapped_column(String(200))  # what was done because of it
    message_id: Mapped[str | None] = mapped_column(String(300), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Lesson(Base):
    """Something the system has learned from its own results. Re-evaluated from raw facts on every
    Strategist run; `verdict` changes are what the brief and the office announce."""

    __tablename__ = "lessons"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(160), unique=True)  # e.g. "target:plumber|hamilton, on"
    area: Mapped[str] = mapped_column(String(20))  # target | angle | demo | score | timing | client | niche | agent
    subject: Mapped[str] = mapped_column(String(200))
    verdict: Mapped[str] = mapped_column(String(12))  # works | fails | early | earns | loses
    statement: Mapped[str] = mapped_column(String(400))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ====================================================================== revenue recovery
#
# The three leaks between the phone call and the payment: missed calls, quotes nobody follows up,
# invoices paid late. Every customer-facing text lives in sms_messages; every dollar the system
# claims credit for lives in recovery_events (one row per thing, so nothing is double-counted).


class RecoveryCall(Base):
    """A call that reached a recovery number (Twilio). In 'ring' mode answered calls are recorded too,
    so the funnel can start at total calls rather than at missed calls."""

    __tablename__ = "recovery_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    call_sid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    from_number: Mapped[str | None] = mapped_column(String(40))
    to_number: Mapped[str | None] = mapped_column(String(40))
    mode: Mapped[str] = mapped_column(String(10), default="forward")  # forward | ring | vapi
    answered: Mapped[bool] = mapped_column(Boolean, default=False)
    screened_ok: Mapped[bool] = mapped_column(Boolean, default=False)  # owner pressed 1 (a human, not voicemail)
    dial_status: Mapped[str | None] = mapped_column(String(20))
    thread_id: Mapped[int | None] = mapped_column(ForeignKey("sms_threads.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class SmsThread(Base):
    """One conversation with one customer about one thing (a missed call, a quote, an invoice)."""

    __tablename__ = "sms_threads"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    phone: Mapped[str] = mapped_column(String(40), index=True)
    kind: Mapped[str] = mapped_column(String(12), default="missed_call")  # missed_call | quote | invoice
    # missed_call: texted → engaged → qualified → booked | emergency | handed_off | no_reply | closed | opted_out
    status: Mapped[str] = mapped_column(String(12), default="texted")
    step: Mapped[str | None] = mapped_column(String(20))  # what we asked last (slot being filled)
    data: Mapped[dict] = mapped_column(JSON, default=dict)  # service, address, name, urgency, photos, offered…
    quote_id: Mapped[int | None] = mapped_column(ForeignKey("quotes.id", ondelete="SET NULL"))
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"))
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id", ondelete="SET NULL"))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    nudged: Mapped[bool] = mapped_column(Boolean, default=False)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SmsMessage(Base):
    __tablename__ = "sms_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    thread_id: Mapped[int | None] = mapped_column(ForeignKey("sms_threads.id", ondelete="CASCADE"), index=True)
    direction: Mapped[str] = mapped_column(String(3))  # in | out
    body: Mapped[str] = mapped_column(Text, default="")
    media: Mapped[list] = mapped_column(JSON, default=list)
    sid: Mapped[str | None] = mapped_column(String(64), unique=True)  # Twilio MessageSid: makes webhook retries idempotent
    status: Mapped[str] = mapped_column(String(12), default="sent")  # sent | console | failed | received
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class SmsOptOut(Base):
    __tablename__ = "sms_optouts"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    phone: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Quote(Base):
    __tablename__ = "quotes"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    ref: Mapped[str | None] = mapped_column(String(60))  # the contractor's own quote number
    customer_name: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(300), default="your project")
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sequence_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # open → (replied) → accepted → deposit_requested → deposit_paid → scheduled | declined | no_response | stale | lost
    status: Mapped[str] = mapped_column(String(18), default="open", index=True)
    followups_sent: Mapped[int] = mapped_column(Integer, default=0)
    next_touch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_touch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reply: Mapped[str | None] = mapped_column(Text)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deposit_cents: Mapped[int] = mapped_column(Integer, default=0)
    deposit_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deposit_paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused: Mapped[bool] = mapped_column(Boolean, default=False)  # owner is handling it personally
    source: Mapped[str] = mapped_column(String(10), default="manual")  # manual | csv
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    number: Mapped[str | None] = mapped_column(String(60))
    customer_name: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(200))
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # midnight local of the due day, stored UTC
    payment_link: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)  # open | paid | void | disputed
    reminders_sent: Mapped[list] = mapped_column(JSON, default=list)  # offsets (days vs due) already handled
    last_touch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    customer_says_paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reply: Mapped[str | None] = mapped_column(Text)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_cents: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(10), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class RecoveryEvent(Base):
    """The ledger of money the system claims credit for. `confirmed` amounts come from the contractor's own
    numbers (quote total, invoice paid, job value they entered); unconfirmed ones are estimates
    (avg job value) and never count toward a performance fee."""

    __tablename__ = "recovery_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24))  # missed_call_booked | quote_recovered | invoice_recovered | invoice_on_time
    ref: Mapped[str] = mapped_column(String(40), unique=True)  # e.g. "thread:12" — one claim per thing
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    fee_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    void: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
