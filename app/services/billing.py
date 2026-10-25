"""Stripe billing for YOUR clients (the businesses). Plain REST so there's no
SDK-version drift. Usage is reported per call to a Billing Meter; Stripe owns
invoicing, retries, receipts and the card portal."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AgencySettings, Call, Client, Payment
from . import eventlog, http, pricing

API = "https://api.stripe.com/v1"


def configured() -> bool:
    return bool(get_settings().stripe_secret_key)


def _flatten(data, prefix="") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            out += _flatten(v, f"{prefix}[{k}]" if prefix else str(k))
    elif isinstance(data, (list, tuple)):
        for i, v in enumerate(data):
            out += _flatten(v, f"{prefix}[{i}]")
    elif data is None:
        pass
    elif isinstance(data, bool):
        out.append((prefix, "true" if data else "false"))
    else:
        out.append((prefix, str(data)))
    return out


def _req(method: str, path: str, data: dict | None = None, idempotency_key: str | None = None) -> dict:
    s = get_settings()
    if not s.stripe_secret_key:
        raise http.NotConfigured("Set STRIPE_SECRET_KEY to bill clients.")
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    pairs = _flatten(data or {})
    with http.client(timeout=30, auth=(s.stripe_secret_key, ""), headers=headers) as c:
        if method == "GET":
            r = c.get(API + path, params=pairs)
        else:  # Stripe wants bracketed form keys (a[b][0]=v); encode the pairs ourselves
            r = c.request(method, API + path, content=urlencode(pairs),
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
        return http.check("Stripe", r).json()


def ping() -> None:
    _req("GET", "/customers", {"limit": 1})


def ensure_meter(db: Session, agency: AgencySettings) -> str:
    if agency.stripe_meter_id:
        return agency.stripe_meter_id
    try:
        meter = _req("POST", "/billing/meters", {
            "display_name": "Receptionist minutes", "event_name": agency.stripe_meter_event_name,
            "default_aggregation": {"formula": "sum"},
            "customer_mapping": {"type": "by_id", "event_payload_key": "stripe_customer_id"},
            "value_settings": {"event_payload_key": "value"},
        })
    except http.ProviderError as e:
        if "exist" not in e.detail.lower():
            raise
        meters = _req("GET", "/billing/meters", {"limit": 100}).get("data", [])
        meter = next((m for m in meters if m.get("event_name") == agency.stripe_meter_event_name), None)
        if not meter:
            raise
    agency.stripe_meter_id = meter["id"]
    return meter["id"]


def _pricing_signature(client: Client) -> str:
    raw = json.dumps([client.monthly_price_cents, pricing.tiers_for(client), client.setup_fee_cents,
                      get_settings().currency], sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def ensure_prices(db: Session, client: Client, agency: AgencySettings) -> dict:
    """Prices are immutable in Stripe: create a new set whenever the pricing changes."""
    cur = get_settings().currency
    sig = _pricing_signature(client)
    ids = dict(client.stripe_price_ids or {})
    if ids.get("signature") == sig:
        return ids
    if not client.stripe_product_id:
        product = _req("POST", "/products", {"name": f"AI Receptionist — {client.name}",
                                             "metadata": {"deskline_client_id": client.id}})
        client.stripe_product_id = product["id"]
    ids = {"signature": sig}
    if client.monthly_price_cents:
        ids["flat"] = _req("POST", "/prices", {"currency": cur, "unit_amount": client.monthly_price_cents,
                                               "recurring": {"interval": "month"}, "product": client.stripe_product_id,
                                               "nickname": "Monthly plan"})["id"]
    if pricing.has_usage_charges(client):
        meter_id = ensure_meter(db, agency)
        tiers = pricing.tiers_for(client)
        body = {"currency": cur, "product": client.stripe_product_id, "nickname": "Call minutes",
                "recurring": {"interval": "month", "usage_type": "metered", "meter": meter_id}}
        if len(tiers) == 1:
            body.update({"billing_scheme": "per_unit", "unit_amount_decimal": f"{tiers[0]['cents']:.4f}"})
        else:
            body.update({"billing_scheme": "tiered", "tiers_mode": "graduated",
                         "tiers": [{"up_to": t["up_to"] if t["up_to"] is not None else "inf",
                                    "unit_amount_decimal": f"{t['cents']:.4f}"} for t in tiers]})
        ids["metered"] = _req("POST", "/prices", body)["id"]
    if client.setup_fee_cents:
        ids["setup"] = _req("POST", "/prices", {"currency": cur, "unit_amount": client.setup_fee_cents,
                                                "product": client.stripe_product_id, "nickname": "Setup fee"})["id"]
    client.stripe_price_ids = ids
    return ids


def ensure_customer(client: Client) -> str:
    if client.stripe_customer_id:
        return client.stripe_customer_id
    cust = _req("POST", "/customers", {"name": client.name, "email": client.owner_email or None,
                                       "phone": client.owner_phone or None,
                                       "metadata": {"deskline_client_id": client.id}})
    client.stripe_customer_id = cust["id"]
    return cust["id"]


def create_checkout(db: Session, client: Client, agency: AgencySettings) -> str:
    if not (client.monthly_price_cents or pricing.has_usage_charges(client)):
        raise ValueError("Set a monthly price or per-minute rate before creating a checkout link.")
    ids = ensure_prices(db, client, agency)
    customer = ensure_customer(client)
    s = get_settings()
    items = []
    if ids.get("flat"):
        items.append({"price": ids["flat"], "quantity": 1})
    if ids.get("metered"):
        items.append({"price": ids["metered"]})
    if ids.get("setup"):
        items.append({"price": ids["setup"], "quantity": 1})
    body = {"mode": "subscription", "customer": customer, "line_items": items,
            "success_url": f"{s.base_url}/billing/thanks?c={client.slug}",
            "cancel_url": f"{s.base_url}/billing/thanks?c={client.slug}&cancelled=1",
            "metadata": {"deskline_client_id": client.id},
            "subscription_data": {"metadata": {"deskline_client_id": client.id}}}
    if client.trial_days:
        body["subscription_data"]["trial_period_days"] = client.trial_days
    session = _req("POST", "/checkout/sessions", body)
    client.checkout_url = session["url"]
    return session["url"]


def portal_url(client: Client) -> str:
    if not client.stripe_customer_id:
        raise ValueError("This client has no Stripe customer yet.")
    s = get_settings()
    return _req("POST", "/billing_portal/sessions", {"customer": client.stripe_customer_id,
                                                     "return_url": f"{s.base_url}/portal"})["url"]


def report_usage(db: Session, client: Client, call: Call, agency: AgencySettings) -> bool:
    """Send this call's billable minutes to the Stripe meter, once (identifier = call id)."""
    if call.usage_reported or call.billable_minutes <= 0:
        return False
    if not (configured() and client.stripe_customer_id and client.stripe_status in ("active", "trialing", "past_due")):
        return False
    if not (client.stripe_price_ids or {}).get("metered"):
        return False
    ts = int((call.ended_at or datetime.now(timezone.utc)).timestamp())
    _req("POST", "/billing/meter_events", {
        "event_name": agency.stripe_meter_event_name, "identifier": f"call_{call.vapi_call_id}",
        "timestamp": ts, "payload": {"stripe_customer_id": client.stripe_customer_id, "value": call.billable_minutes},
    })
    call.usage_reported = True
    return True


# ---------------- webhooks ----------------

def verify_signature(payload: bytes, header: str, secret: str, tolerance: int = 300) -> dict:
    if not secret:
        raise ValueError("STRIPE_WEBHOOK_SECRET is not set")
    parts = dict(p.split("=", 1) for p in (header or "").split(",") if "=" in p)
    sigs = [v for k, v in (p.split("=", 1) for p in (header or "").split(",") if "=" in p) if k == "v1"]
    ts = parts.get("t")
    if not ts or not sigs:
        raise ValueError("Malformed Stripe-Signature header")
    if abs(time.time() - int(ts)) > tolerance:
        raise ValueError("Stripe signature timestamp outside tolerance")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        raise ValueError("Stripe signature mismatch")
    return json.loads(payload)


def _client_from(db: Session, obj: dict) -> Client | None:
    cid = (obj.get("metadata") or {}).get("deskline_client_id")
    if cid and str(cid).isdigit():
        c = db.get(Client, int(cid))
        if c:
            return c
    sub = obj.get("subscription") if isinstance(obj.get("subscription"), str) else None
    cust = obj.get("customer") if isinstance(obj.get("customer"), str) else None
    q = db.query(Client)
    if obj.get("object") == "subscription":
        c = q.filter(Client.stripe_subscription_id == obj.get("id")).first()
        if c:
            return c
    if sub:
        c = q.filter(Client.stripe_subscription_id == sub).first()
        if c:
            return c
    if cust:
        return q.filter(Client.stripe_customer_id == cust).first()
    return None


def handle_event(db: Session, event: dict) -> str:
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    client = _client_from(db, obj)
    if not client:
        eventlog.record(db, "stripe", f"{etype}: no matching client", level="warn", event_id=event.get("id"))
        return "ignored"
    if etype == "checkout.session.completed":
        client.stripe_customer_id = obj.get("customer") or client.stripe_customer_id
        client.stripe_subscription_id = obj.get("subscription") or client.stripe_subscription_id
        client.stripe_status = client.stripe_status or "active"
        if client.status == "demo":
            client.status = "active"
    elif etype.startswith("customer.subscription."):
        client.stripe_subscription_id = obj.get("id")
        client.stripe_status = "canceled" if etype.endswith("deleted") else obj.get("status")
        if client.stripe_status == "past_due":
            client.past_due_since = client.past_due_since or datetime.now(timezone.utc)
        elif client.stripe_status in ("active", "trialing"):
            client.past_due_since = None
        if client.stripe_status in ("active", "trialing") and client.status == "demo":
            client.status = "active"
        if client.stripe_status in ("canceled", "unpaid"):
            client.status = "paused"
    elif etype in ("invoice.paid", "invoice.payment_succeeded"):
        client.past_due_since = None
        if obj.get("id") and not db.query(Payment).filter_by(stripe_invoice_id=obj["id"]).first():
            db.add(Payment(client_id=client.id, stripe_invoice_id=obj["id"], amount_cents=int(obj.get("amount_paid") or 0),
                           currency=obj.get("currency") or "usd", status="paid"))
    elif etype == "invoice.payment_failed":
        client.stripe_status = "past_due"
        client.past_due_since = client.past_due_since or datetime.now(timezone.utc)
        eventlog.record(db, "billing", f"Payment failed for {client.name}", client_id=client.id, level="warn")
    eventlog.record(db, "stripe", etype, client_id=client.id)
    return "ok"
