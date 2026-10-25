import hashlib
import hmac
import json
import time

from sqlalchemy import select

from app.models import Call, Client, ChatSession, Contact, EmailDraft, Prospect, Site, User
from tests.test_voice_engine import _setup_live, make_client


# ---------------- pricing + Stripe ----------------

def test_graduated_pricing_math():
    from app.services import pricing

    c = Client(name="x", pricing_model="flat", monthly_price_cents=29700, included_minutes=500, overage_cents=25)
    assert pricing.usage_cents(pricing.tiers_for(c), 400) == 0
    assert pricing.usage_cents(pricing.tiers_for(c), 620) == 120 * 25
    assert pricing.monthly_charge_cents(c, 620) == 29700 + 3000
    t = Client(name="t", pricing_model="tiered", monthly_price_cents=0,
               tiers=[{"up_to": None, "cents": 15}, {"up_to": 100, "cents": 30}, {"up_to": 1000, "cents": 20}])
    assert pricing.usage_cents(pricing.tiers_for(t), 1100) == 100 * 30 + 900 * 20 + 100 * 15
    pm = Client(name="p", pricing_model="per_minute", per_minute_cents=40, monthly_price_cents=0)
    assert pricing.monthly_charge_cents(pm, 10) == 400


def _sign(payload: bytes, secret="whsec_test"):
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def test_checkout_creates_metered_tiered_price_and_webhook_activates(admin, app_client, db, net):
    cid = make_client(admin)
    admin.post(f"/clients/{cid}/billing", data={"pricing_model": "flat", "monthly_price": "297", "included_minutes": "500",
               "overage": "0.25", "setup_fee": "199", "trial_days": "7"})
    r = admin.post(f"/clients/{cid}/billing/checkout", data={"send": "1"}, follow_redirects=False)
    assert r.status_code == 303
    prices = [net.form(r) for r in net.of("api.stripe.com", "^/v1/prices$")]
    metered = [p for p in prices if p.get("recurring[usage_type]") == "metered"][0]
    assert metered["recurring[meter]"] == "mtr_1" and metered["tiers_mode"] == "graduated"
    assert metered["tiers[0][up_to]"] == "500" and metered["tiers[0][unit_amount_decimal]"] == "0.0000"
    assert metered["tiers[1][up_to]"] == "inf" and metered["tiers[1][unit_amount_decimal]"] == "25.0000"
    flat = [p for p in prices if p.get("recurring[interval]") == "month" and "recurring[usage_type]" not in p][0]
    assert flat["unit_amount"] == "29700"
    session = net.form(net.of("api.stripe.com", "checkout/sessions")[0])
    assert session["mode"] == "subscription" and session["subscription_data[trial_period_days]"] == "7"
    assert "line_items[1][quantity]" not in session  # metered items must not carry a quantity
    c = db.get(Client, cid)
    assert c.checkout_url.startswith("https://checkout.stripe.com")
    # signed webhook activates the client
    event = {"id": "evt_1", "type": "checkout.session.completed",
             "data": {"object": {"customer": "cus_1", "subscription": "sub_1", "metadata": {"deskline_client_id": str(cid)}}}}
    body = json.dumps(event).encode()
    assert app_client.post("/webhooks/stripe", content=body, headers={"stripe-signature": "t=1,v1=bad"}).status_code == 400
    r = app_client.post("/webhooks/stripe", content=body, headers={"stripe-signature": _sign(body)})
    assert r.json()["result"] == "ok"
    db.expire_all()
    c = db.get(Client, cid)
    assert c.stripe_subscription_id == "sub_1" and c.status == "active"
    # repricing creates new immutable prices; unchanged pricing reuses them
    n_prices = len(net.of("api.stripe.com", "^/v1/prices$"))
    admin.post(f"/clients/{cid}/billing/checkout", data={})
    assert len(net.of("api.stripe.com", "^/v1/prices$")) == n_prices


# ---------------- lead finder + outreach ----------------

PLACES = [
    {"id": "p1", "displayName": {"text": "Rapid Rooter Plumbing"}, "nationalPhoneNumber": "(905) 555-0101",
     "websiteUri": "https://mapleleafplumbing.test", "rating": 4.7, "userRatingCount": 212, "businessStatus": "OPERATIONAL",
     "types": ["plumber"], "formattedAddress": "1 Main St, Hamilton",
     "regularOpeningHours": {"weekdayDescriptions": ["Monday: 8:00 AM – 5:00 PM", "Saturday: Closed", "Sunday: Closed"]}},
    {"id": "p2", "displayName": {"text": "No Phone Co"}, "businessStatus": "OPERATIONAL", "types": ["store"]},
    {"id": "p3", "displayName": {"text": "Gone Plumbing"}, "businessStatus": "CLOSED_PERMANENTLY"},
]


def test_lead_search_scoring_enrich_export(admin, db, net):
    net.places = PLACES
    r = admin.post("/leads/search", data={"niche": "plumber", "location": "Hamilton, ON"}, follow_redirects=False)
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    ps = db.scalars(select(Prospect).where(Prospect.search_id == sid).order_by(Prospect.score.desc())).all()
    assert [p.name for p in ps] == ["Rapid Rooter Plumbing", "No Phone Co"]  # closed business skipped
    top = ps[0]
    assert top.score > 50 and any("weekend" in r for r in top.score_reasons) and any("212 reviews" in r for r in top.score_reasons)
    assert ps[1].score == 0
    sent = json.loads(net.of("places.googleapis.com")[0].content)
    assert sent["textQuery"] == "plumber in Hamilton, ON"
    assert "places.websiteUri" in net.of("places.googleapis.com")[0].headers["X-Goog-FieldMask"]
    admin.post(f"/prospects/{top.id}/enrich")
    db.expire_all()
    top = db.get(Prospect, top.id)
    assert top.email == "office@mapleleafplumbing.test" and "Calendly" in top.signals["booking_tools"]
    csv = admin.get(f"/leads/{sid}/export.csv").text
    assert csv.splitlines()[0].startswith("name,score") and "Rapid Rooter" in csv
    # re-running the same search doesn't duplicate
    admin.post("/leads/search", data={"niche": "plumber", "location": "Hamilton, ON"})
    assert db.scalar(select(Prospect).where(Prospect.place_id == "p1").with_only_columns(Prospect.id).limit(5)) is not None
    assert len(db.scalars(select(Prospect).where(Prospect.place_id == "p1")).all()) == 1


def test_cold_email_compliance(admin, app_client, db, net):
    from app.services import messaging

    p = Prospect(name="Rapid Rooter", niche="plumber", email="owner@rapid.test", phone="9055550101")
    db.add(p)
    db.commit()
    admin.post(f"/prospects/{p.id}/emails/generate")
    drafts = db.scalars(select(EmailDraft).where(EmailDraft.prospect_id == p.id)).all()
    assert len(drafts) >= 1
    d = drafts[0]
    r = admin.post(f"/prospects/{p.id}/emails/{d.id}/send", follow_redirects=True)
    assert "mailing address" in r.text  # blocked until address set
    admin.post("/settings", data={"brand_name": "Northline AI", "mailing_address": "1 Bay St, Toronto ON", "primary_color": "#123456"})
    admin.post(f"/prospects/{p.id}/emails/{d.id}/send")
    mail = [m for m in messaging.OUTBOX if m["to"] == "owner@rapid.test"][-1]
    assert "1 Bay St, Toronto ON" in mail["body"] and "/u/" in mail["body"]
    unsub = mail["body"].split("Don't want these emails? ")[1].strip()
    path = "/" + unsub.split("/", 3)[3]
    assert app_client.post(path).status_code == 200
    d2 = drafts[1]
    r = admin.post(f"/prospects/{p.id}/emails/{d2.id}/send", follow_redirects=True)
    assert "unsubscribed" in r.text


def test_proposal_roi_is_computed_not_generated(admin, app_client, db):
    p = Prospect(name="Rapid Rooter", niche="plumber")
    db.add(p)
    db.commit()
    r = admin.post(f"/prospects/{p.id}/proposal", data={"missed_calls_per_week": "8", "avg_job_value": "400",
                                                       "close_rate": "0.25", "monthly_price": "300"}, follow_redirects=False)
    page = admin.get(r.headers["location"])
    assert "$3,464" in page.text  # 8 × 4.33 × 0.25 × 400
    assert "11.5×" in page.text
    from app.models import Proposal

    prop = db.scalars(select(Proposal)).one()
    assert app_client.get(f"/p/{prop.token}").status_code == 200


def test_site_generation_public_and_custom_domain(admin, app_client, db):
    cid = make_client(admin)
    r = admin.post(f"/clients/{cid}/site", follow_redirects=False)
    site = db.scalars(select(Site).where(Site.client_id == cid)).one()
    pub = app_client.get(f"/s/{site.slug}")
    assert pub.status_code == 200 and "/widget/" in pub.text
    admin.post(f"/sites/{site.id}", data={"headline": "Maple Leaf Plumbing", "subheadline": "x", "about": "y",
               "services": "Drains | Fast", "reviews": "Sam | Great work", "theme_color": "#ff0000",
               "domain": "www.mapleleaf.test", "published": "1"})
    r = app_client.get("/", headers={"host": "www.mapleleaf.test"})
    assert r.status_code == 200 and "Great work" in r.text


# ---------------- chat widget ----------------

def test_chat_widget_books_lead_via_tool_call(app_client, admin, db, net):
    c = _setup_live(admin, db)
    js = app_client.get(f"/widget/{c.widget_key}.js")
    assert js.status_code == 200 and "dl-panel" in js.text
    net.llm_queue = [
        {"content": None, "tool_calls": [{"id": "t1", "type": "function", "function": {
            "name": "capture_lead", "arguments": json.dumps({"name": "Pat", "phone": "4165553333", "interest": "water heater"})}}]},
        {"content": "Thanks Pat — someone will call you shortly."},
    ]
    r = app_client.post(f"/api/chat/{c.widget_key}", json={"visitor_id": "v1", "message": "Need a water heater, I'm Pat 416-555-3333"},
                        headers={"origin": "https://mapleleafplumbing.test"})
    assert r.json()["reply"].startswith("Thanks Pat") and r.headers["access-control-allow-origin"] == "https://mapleleafplumbing.test"
    lead = db.scalars(select(Contact).where(Contact.client_id == c.id, Contact.source == "chat")).one()
    assert lead.name == "Pat" and lead.kind == "lead"
    sys = json.loads(net.of("api.openai.com")[-2].content)["messages"][0]["content"]
    assert '{{"now"' not in sys and "website chat box" in sys  # Vapi template vars rendered for chat
    assert db.scalars(select(ChatSession)).one().messages[-1]["role"] == "assistant"
    c.widget_allowed_origins = "https://mapleleafplumbing.test"
    db.commit()
    bad = app_client.post(f"/api/chat/{c.widget_key}", json={"message": "hi"}, headers={"origin": "https://evil.test"})
    assert bad.status_code == 403


# ---------------- portal isolation ----------------

def test_owner_portal_is_isolated(app_client, admin, db):
    a = _setup_live(admin, db)
    b_id = make_client(admin, website="", name="Other Biz")
    db.add_all([Call(client_id=a.id, vapi_call_id="ca", duration_seconds=40, summary="A's call"),
                Call(client_id=b_id, vapi_call_id="cb", duration_seconds=40, summary="B secret")])
    db.commit()
    admin.post(f"/clients/{a.id}/owner-login", data={"email": "owner@a.test"})
    pw = [u for u in db.scalars(select(User).where(User.email == "owner@a.test"))][0]
    from app.security import hash_password

    pw.password_hash = hash_password("owner-pass-123")
    db.commit()
    app_client.post("/logout")
    r = app_client.post("/login", data={"email": "owner@a.test", "password": "owner-pass-123", "next": "/clients"},
                        follow_redirects=False)
    assert r.headers["location"] == "/portal"
    home = app_client.get("/portal")
    assert home.status_code == 200
    assert "A&#39;s call" in home.text or "A's call" in home.text
    cb = db.scalars(select(Call).where(Call.vapi_call_id == "cb")).one()
    assert app_client.get(f"/portal/calls/{cb.id}").status_code == 404
    assert app_client.get("/clients").status_code == 403
    assert app_client.get(f"/portal?client_id={b_id}").text.count("B secret") == 0


def test_weekly_report(admin, db):
    from app.services import messaging, reports

    c = _setup_live(admin, db, status="active")
    db.add(Call(client_id=c.id, vapi_call_id="w1", duration_seconds=90, billable_minutes=2))
    db.commit()
    assert reports.send_weekly(db, c, force=True)
    mail = [m for m in messaging.OUTBOX if m["to"] == "owner@maple.test"][-1]
    assert "1 calls answered" in mail["subject"]
    assert not reports.send_weekly(db, c)  # not again within 6 days
