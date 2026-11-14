import hashlib
import hmac
import json
import time

from sqlalchemy import select

from app.models import ProspectSearch, Call, Client, ChatSession, Contact, EmailDraft, Prospect, Site, User
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
    {"id": "p4", "displayName": {"text": "Bayfront Drain Pros"}, "nationalPhoneNumber": "(905) 555-0144",
     "rating": 4.5, "userRatingCount": 140, "businessStatus": "OPERATIONAL", "types": ["plumber"],
     "formattedAddress": "9 King St, Hamilton",
     "regularOpeningHours": {"weekdayDescriptions": ["Monday: 8:00 AM – 5:00 PM", "Saturday: Closed", "Sunday: Closed"]}},
]


def test_lead_search_finds_businesses_without_a_website(admin, db, net):
    net.places = PLACES
    r = admin.post("/leads/search", data={"niche": "plumber", "location": "Hamilton, ON"}, follow_redirects=False)
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    ps = db.scalars(select(Prospect).where(Prospect.search_id == sid).order_by(Prospect.score.desc())).all()
    # has a website (p1) and closed (p3) are skipped
    assert [p.name for p in ps] == ["Bayfront Drain Pros", "No Phone Co"]
    assert all(p.website is None for p in ps)
    top = ps[0]
    assert top.score > 50 and any("No website" in r for r in top.score_reasons) and any("weekend" in r for r in top.score_reasons)
    assert ps[1].score == 0
    sent = json.loads(net.of("places.googleapis.com")[0].content)
    assert sent["textQuery"] == "plumber in Hamilton, ON"
    assert "places.websiteUri" in net.of("places.googleapis.com")[0].headers["X-Goog-FieldMask"]
    csv = admin.get(f"/leads/{sid}/export.csv").text
    assert csv.splitlines()[0].startswith("name,score") and "Bayfront Drain Pros" in csv
    # re-running the same search doesn't duplicate
    admin.post("/leads/search", data={"niche": "plumber", "location": "Hamilton, ON"})
    assert len(db.scalars(select(Prospect).where(Prospect.place_id == "p4")).all()) == 1
    assert db.scalars(select(Prospect).where(Prospect.place_id == "p1")).first() is None


def test_lead_search_canada_wide(admin, db, net):
    from app.services import leads

    net.places = PLACES
    r = admin.post("/leads/search", data={"niche": "plumber", "location": "canada"}, follow_redirects=False)
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    db.expire_all()
    s = db.get(ProspectSearch, sid)
    assert s.location == "Canada"
    queries = [json.loads(q.content)["textQuery"] for q in net.of("places.googleapis.com")]
    assert queries == [f"plumber in {c}" for c in leads.CANADA_CITIES]
    for city in ("Toronto, ON", "Vancouver, BC", "Halifax, NS", "St. John's, NL", "Iqaluit, NU", "Whitehorse, YT"):
        assert city in leads.CANADA_CITIES
    # every city returned the same places: each business is stored once, websites skipped
    ps = db.scalars(select(Prospect).where(Prospect.search_id == sid)).all()
    assert sorted(p.name for p in ps) == ["Bayfront Drain Pros", "No Phone Co"]
    assert s.result_count == 2
    assert s.cities_total == s.cities_done == len(leads.CANADA_CITIES)
    page = admin.get(f"/leads/{sid}").text
    assert "Bayfront Drain Pros" in page and 'http-equiv="refresh"' not in page


def test_canada_wide_search_shows_progress_while_running(admin, db):
    s = ProspectSearch(niche="plumber", location="Canada", cities_total=58, cities_done=12)
    db.add(s)
    db.commit()
    page = admin.get(f"/leads/{s.id}").text
    assert "12 of 58 cities done" in page and 'http-equiv="refresh"' in page


def _search(admin, location, niche="plumber"):
    r = admin.post("/leads/search", data={"niche": niche, "location": location}, follow_redirects=False)
    return int(r.headers["location"].rsplit("/", 1)[1])


def test_search_widens_when_every_result_is_used(admin, db, net):
    from app.services import leads

    net.places = [PLACES[3]]  # one business with a phone and no website
    sid = _search(admin, "Hamilton, ON")
    db.expire_all()
    assert db.get(ProspectSearch, sid).location == "Hamilton, ON"

    # results still unused: searching again stays in the city
    sid = _search(admin, "hamilton, on")
    db.expire_all()
    assert db.get(ProspectSearch, sid).location == "Hamilton, ON"

    # every result used: next search widens to the province
    for p in db.scalars(select(Prospect)).all():
        p.status = "contacted"
    db.add(Prospect(name="No Phone Co", niche="plumber", status="new"))  # can't be called, doesn't block widening
    db.commit()
    net.requests.clear()
    sid = _search(admin, "Hamilton, ON")
    db.expire_all()
    s = db.get(ProspectSearch, sid)
    ontario = [c for c in leads.CANADA_CITIES if c.endswith(", ON")]
    assert s.location == "Ontario" and s.cities_total == len(ontario) == s.cities_done
    queries = [json.loads(q.content)["textQuery"] for q in net.of("places.googleapis.com")]
    assert queries == [f"plumber in {c}" for c in ontario]

    # used again: widens to Canada, and stays there
    for p in db.scalars(select(Prospect)).all():
        p.status = "lost"
    db.commit()
    sid = _search(admin, "Hamilton, ON")
    db.expire_all()
    assert db.get(ProspectSearch, sid).location == "Canada"
    sid = _search(admin, "Hamilton, ON")
    db.expire_all()
    assert db.get(ProspectSearch, sid).location == "Canada"

    # a different niche starts in the city again
    sid = _search(admin, "Hamilton, ON", niche="roofer")
    db.expire_all()
    assert db.get(ProspectSearch, sid).location == "Hamilton, ON"


def test_enrich_imported_prospect_with_website(admin, db, net):
    p = Prospect(name="Rapid Rooter Plumbing", niche="plumber", phone="(905) 555-0101",
                 website="https://mapleleafplumbing.test", review_count=212)
    db.add(p)
    db.commit()
    admin.post(f"/prospects/{p.id}/enrich")
    db.expire_all()
    p = db.get(Prospect, p.id)
    assert p.email == "office@mapleleafplumbing.test" and "Calendly" in p.signals["booking_tools"]


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


def test_quebec_is_left_out_of_prospecting(admin, db, net):
    from app.services import leads

    assert not [c for c in leads.CANADA_CITIES if c.endswith(", QC")] and "QC" not in leads.PROVINCES
    net.places = PLACES + [
        {"id": "pq", "displayName": {"text": "Plomberie Rue Hamilton"}, "nationalPhoneNumber": "(514) 555-0101",
         "formattedAddress": "12 Rue Hamilton, Montréal, Québec H2X 1A1, Canada", "businessStatus": "OPERATIONAL"}]
    r = admin.post("/leads/search", data={"niche": "plumber", "location": "Hamilton, ON"}, follow_redirects=False)
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    names = [p.name for p in db.scalars(select(Prospect).where(Prospect.search_id == sid))]
    assert "Plomberie Rue Hamilton" not in names and "Bayfront Drain Pros" in names


def test_deleting_a_client_takes_its_public_site_down(admin, db, app_client):
    from app.models import Client, Site

    c = Client(name="Gone Plumbing", slug="gone-plumbing")
    db.add(c)
    db.flush()
    db.add(Site(client_id=c.id, slug="gone-plumbing", content={"headline": "x"}))
    db.commit()
    assert app_client.get("/s/gone-plumbing").status_code == 200
    r = admin.post(f"/clients/{c.id}/delete", data={"confirm": "Gone Plumbing"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.query(Site).filter_by(slug="gone-plumbing").count() == 0
    assert app_client.get("/s/gone-plumbing").status_code == 404
