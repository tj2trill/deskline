"""The lead's 'whys' become numbers and a call brief: what they're losing, what to open with, what to upsell."""
from app.models import Prospect
from app.services import leads, opportunity

HOURS = ["Monday: Closed", "Tuesday: 10:00 AM – 6:30 PM", "Wednesday: 10:00 AM – 6:30 PM",
         "Thursday: 10:00 AM – 6:30 PM", "Friday: 10:00 AM – 6:30 PM",
         "Saturday: 10:00 AM – 5:00 PM", "Sunday: Closed"]


def _lead(**kw):
    base = dict(name="Bayfront Drain Pros", niche="plumber", phone="905-555-0123", rating=4.8, review_count=523,
                hours=HOURS, enriched=True, signals={})
    base.update(kw)
    p = Prospect(**base)
    leads.score(p)
    return p


def test_parse_hours_handles_closed_split_and_24h():
    h = opportunity.parse_hours(HOURS + ["Monday: Open 24 hours"])
    assert h["Sunday"] == "closed" and h["Monday"] == "24h"
    assert h["Tuesday"] == [(10.0, 18.5)]
    split = opportunity.parse_hours(["Friday: 8:00 AM – 12:00 PM, 1:00 – 5:00 PM",
                                     "Saturday: 6:00 PM – 2:00 AM"])
    assert split["Friday"] == [(8.0, 12.0), (13.0, 17.0)]
    assert split["Saturday"] == [(18.0, 26.0)]  # past midnight


def test_estimate_uses_the_whys():
    est = opportunity.estimate(_lead())
    assert est["closed_days"] == ["Monday", "Sunday"]
    assert est["calls_per_week"] == 60 and est["missed_share"] == 0.35
    assert est["missed_calls_per_week"] == 21 and est["avg_job_value"] == 350
    assert est["recovered_per_month"] == round(21 * 4.33 * 0.3 * 350, 2)
    quiet = opportunity.estimate(_lead(review_count=3, hours=["Monday: Open 24 hours"], niche="bakery", name="Crumbs"))
    assert quiet["missed_share"] == 0.2 and quiet["avg_job_value"] == opportunity.DEFAULT_JOB_VALUE


def test_brief_fills_notes_and_keeps_your_edits():
    p = _lead()
    assert opportunity.fill_blanks(p, 29700) is True
    assert "21 missed calls/week" in p.notes and "$9,548/month" in p.notes
    assert "closed Monday and Sunday" in p.notes and "6:30 PM" in p.notes
    assert "No website" in p.notes and "$297/month" in p.notes
    assert all(ord(c) < 128 for c in p.notes)
    # unchanged auto brief is refreshed when the lead changes
    p.review_count = 40
    leads.score(p)
    assert opportunity.fill_blanks(p, 29700) is True and "40 reviews" in p.notes
    # once you edit it, it is yours
    p.notes = p.notes + "\nOwner is Dave."
    assert opportunity.fill_blanks(p, 29700) is False and p.notes.endswith("Owner is Dave.")
    # cleared notes refill
    p.notes = None
    assert opportunity.fill_blanks(p, 29700) is True and p.notes


def test_no_phone_lead_gets_no_brief():
    p = _lead(phone=None)
    assert opportunity.fill_blanks(p, 29700) is False and not p.notes


def test_prospect_page_shows_brief_and_prefilled_proposal(admin, app_client, db):
    p = _lead()
    db.add(p)
    db.commit()
    html = app_client.get(f"/prospects/{p.id}").text
    assert "21 missed calls/week" in html
    assert 'name="missed_calls_per_week" type="number" value="21"' in html
    assert 'name="avg_job_value" type="number" value="350"' in html
    assert "No website to find one on" in html
    db.refresh(p)
    assert p.notes and p.signals.get("auto_notes") == p.notes


DETAILS = {
    "priceRange": {"startPrice": {"currencyCode": "CAD", "units": "50"}, "endPrice": {"currencyCode": "CAD", "units": "100"}},
    "reviews": [
        {"rating": 5, "relativePublishTimeDescription": "a month ago",
         "text": {"text": "Tony did an amazing job with my hair! Came all the way to Oshawa for it."}},
        {"rating": 5, "text": {"text": "Bibi took care of me. Great salon."}},
        {"rating": 3, "text": {"text": "Great cut from Tony. I called three times and nobody answered the phone, so I just walked in."}},
    ],
}


def test_real_google_data_drives_the_brief(admin, app_client, db, net):
    net.place_details = DETAILS
    p = _lead(name="Hairworx", niche="hair salon", place_id="ChIJhairworx")
    db.add(p)
    db.commit()
    html = app_client.get(f"/prospects/{p.id}").text
    assert len(net.of("places.googleapis.com", "/v1/places/ChIJhairworx")) == 1
    db.refresh(p)
    assert p.signals["places"]["price"] == {"start": 50, "end": 100, "currency": "CAD"}
    est = opportunity.estimate(p)
    assert est["avg_job_value"] == 75 and est["value_source"] == "their Google price range $50-$100"
    assert est["missed_share"] == 0.45  # 35% + their own review about not getting through
    assert "Ask for: Tony and Bibi (named in their Google reviews)" in p.notes
    assert "Oshawa" not in opportunity.review_evidence(p)["names"]
    assert "nobody answered the phone" in p.notes
    assert 'name="avg_job_value" type="number" value="75"' in html
    app_client.get(f"/prospects/{p.id}")  # fetched once, not on every view
    assert len(net.of("places.googleapis.com", "/v1/places/")) == 1


def test_facebook_only_business_is_still_a_lead(admin, db, net):
    from sqlalchemy import select

    from app.models import Prospect as P

    net.places = [
        {"id": "fb1", "displayName": {"text": "Shear Joy Salon"}, "nationalPhoneNumber": "(905) 555-0199",
         "websiteUri": "https://www.facebook.com/shearjoysalon", "userRatingCount": 80, "businessStatus": "OPERATIONAL",
         "types": ["hair_salon"], "formattedAddress": "3 Simcoe St, Oshawa, ON"},
        {"id": "real1", "displayName": {"text": "Has A Site"}, "nationalPhoneNumber": "(905) 555-0100",
         "websiteUri": "https://hasasite.test", "businessStatus": "OPERATIONAL", "formattedAddress": "Oshawa, ON"},
    ]
    admin.post("/leads/search", data={"niche": "hair salon", "location": "Oshawa, ON"})
    ps = db.scalars(select(P).where(P.place_id.in_(["fb1", "real1"]))).all()
    assert [p.place_id for p in ps] == ["fb1"]
    p = ps[0]
    assert p.website is None and p.signals["social"] == {"url": "https://www.facebook.com/shearjoysalon", "name": "Facebook"}
    assert "Only a Facebook page, no website: a website is an easy add-on sale" in p.score_reasons
    html = admin.get(f"/prospects/{p.id}").text
    assert 'href="https://www.facebook.com/shearjoysalon"' in html and "Check their Facebook page" in html
    db.refresh(p)
    assert "Only a Facebook page (https://www.facebook.com/shearjoysalon)" in p.notes


def test_social_site_detection():
    assert leads.social_site("https://m.facebook.com/x") == "Facebook"
    assert leads.social_site("http://instagram.com/x") == "Instagram"
    assert leads.social_site("https://notfacebook.com") is None
    assert leads.social_site("https://hairworx.ca") is None and leads.social_site(None) is None


def test_email_typed_next_to_called_is_saved(admin, db):
    from app.models import AgentAction

    p = _lead(name="Called Co", status="demo")
    db.add(p)
    db.commit()
    bad = AgentAction(agent_key="outreach", kind="call_prospect", title="Call", payload={"prospect_id": p.id}, status="pending")
    good = AgentAction(agent_key="outreach", kind="call_prospect", title="Call", payload={"prospect_id": p.id}, status="pending")
    db.add_all([bad, good])
    db.commit()
    admin.post(f"/agents/actions/{bad.id}/approve", data={"email": "not-an-email", "back": "/agents"})
    db.refresh(bad)
    assert bad.status == "pending"
    admin.post(f"/agents/actions/{good.id}/approve", data={"email": " Owner@CalledCo.ca ", "back": "/agents"})
    db.refresh(good)
    db.refresh(p)
    assert good.status == "done" and p.email == "owner@calledco.ca" and p.status == "contacted"
    assert "saved owner@calledco.ca" in good.result
