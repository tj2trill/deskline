"""Test harness: SQLite DB + a fake internet (httpx.MockTransport) standing in for
the website being crawled, OpenAI, Vapi, Stripe, Twilio, Google Places and Cal.com."""
from __future__ import annotations

import json
import os
import re
from urllib.parse import parse_qs

import httpx
import pytest

os.environ["SCHEDULER_ENABLED"] = "false"

SITE_HTML = """<html><head><title>Maple Leaf Plumbing | Hamilton Plumbers</title>
<meta name="description" content="24/7 emergency plumbing in Hamilton. Drains, water heaters, leaks.">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Plumber","name":"Maple Leaf Plumbing",
"telephone":"+1-905-555-0100","address":{"streetAddress":"12 King St","addressLocality":"Hamilton","addressRegion":"ON"},
"openingHoursSpecification":[{"dayOfWeek":["Monday","Tuesday","Wednesday","Thursday","Friday"],"opens":"08:00","closes":"18:00"}]}</script>
</head><body><nav><a href="/services">Services</a><a href="/contact">Contact</a><a href="https://facebook.com/x">FB</a></nav>
<h1>Hamilton's trusted plumbers</h1><p>Family owned since 1998. Call <a href="tel:+19055550100">905-555-0100</a>.</p>
<script src="https://assets.calendly.com/widget.js"></script></body></html>"""
SERVICES_HTML = "<html><head><title>Services</title></head><body><h2>Drain cleaning</h2><p>Camera inspection included.</p><h2>Water heaters</h2></body></html>"
CONTACT_HTML = "<html><head><title>Contact</title></head><body>Email <a href='mailto:office@mapleleafplumbing.test'>office@mapleleafplumbing.test</a></body></html>"


class FakeInternet:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.llm_queue: list[dict] = []  # scripted LLM replies (message dicts)
        self.places: list[dict] = []
        self.counter = 0

    def of(self, host: str, path_re: str = "") -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host and re.search(path_re, r.url.path)]

    def form(self, req: httpx.Request) -> dict:
        return {k: v[0] for k, v in parse_qs(req.content.decode()).items()}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        self.counter += 1
        if host == "redirector.test":  # tries to bounce the crawler into the private network
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        if host == "mapleleafplumbing.test":
            body = {"/": SITE_HTML, "/services": SERVICES_HTML, "/contact": CONTACT_HTML}.get(path.rstrip("/") or "/")
            return httpx.Response(200 if body else 404, text=body or "nope", headers={"content-type": "text/html"})
        if host == "api.openai.com":
            return self._llm(request)
        if host == "api.vapi.ai":
            if request.method == "POST" and path == "/assistant":
                return httpx.Response(201, json={"id": "asst_1"})
            if request.method == "PATCH" and path.startswith("/assistant/"):
                return httpx.Response(200, json={"id": path.split("/")[-1]})
            if request.method == "POST" and path == "/phone-number":
                return httpx.Response(201, json={"id": f"pn_{self.counter}", "number": "+15125550111"})
            if path.startswith("/phone-number/"):
                return httpx.Response(200, json={"id": path.split("/")[-1], "number": "+15125550111"})
            if path == "/call":
                return httpx.Response(201, json={"id": "call_out"})
            return httpx.Response(404, json={"message": "not found"})
        if host == "api.stripe.com":
            ids = {"/v1/billing/meters": "mtr_1", "/v1/products": "prod_1", "/v1/customers": "cus_1"}
            if path in ids:
                return httpx.Response(200, json={"id": ids[path]})
            if path == "/v1/prices":
                return httpx.Response(200, json={"id": f"price_{self.counter}"})
            if path == "/v1/checkout/sessions":
                return httpx.Response(200, json={"id": "cs_1", "url": "https://checkout.stripe.com/c/pay/cs_1"})
            if path == "/v1/billing/meter_events":
                return httpx.Response(200, json={"object": "billing.meter_event"})
            if path == "/v1/billing_portal/sessions":
                return httpx.Response(200, json={"url": "https://billing.stripe.com/p/session_1"})
        if host == "api.twilio.com":
            return httpx.Response(201, json={"sid": f"SM{self.counter}"})
        if host == "places.googleapis.com":
            return httpx.Response(200, json={"places": self.places})
        if host == "api.cal.com":
            if path == "/v2/slots":
                return httpx.Response(200, json={"status": "success", "data": {}})
            return httpx.Response(201, json={"status": "success", "data": {"uid": "cal_uid_1"}})
        if host == "hooks.example.test":
            return httpx.Response(200, json={"status": "Job 42 is scheduled for Friday"})
        return httpx.Response(404, text="unknown host " + host)

    def _llm(self, request):
        body = json.loads(request.content)
        if self.llm_queue:
            msg = self.llm_queue.pop(0)
        else:
            sys = body["messages"][0]["content"]
            if "extract facts" in sys:
                msg = {"content": json.dumps({
                    "business_name": "Maple Leaf Plumbing", "industry": "Plumbing",
                    "summary": "Family-owned Hamilton plumber since 1998 offering 24/7 emergency service.",
                    "services": [{"name": "Drain cleaning", "description": "Includes camera inspection", "price": None},
                                 {"name": "Water heaters", "description": "", "price": None}],
                    "hours": {"mon": [["08:00", "18:00"]], "tue": [["08:00", "18:00"]], "wed": [["08:00", "18:00"]],
                              "thu": [["08:00", "18:00"]], "fri": [["08:00", "18:00"]], "sat": [], "sun": []},
                    "address": "12 King St, Hamilton, ON", "phone": "+1-905-555-0100",
                    "faqs": [{"q": "Do you do emergencies?", "a": "Yes, 24/7."}]})}
            elif body.get("response_format"):
                msg = {"content": json.dumps({"summary": "Caller asked about drains.", "outcome": "info",
                                              "caller_sentiment": "neutral", "unanswered_questions": []})}
            else:
                msg = {"content": "Sure — how can I help?"}
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", **msg}}]})


@pytest.fixture()
def net(tmp_path, monkeypatch):
    env = {
        # TEST_DATABASE_URL=postgresql+psycopg://… runs the same suite against Postgres.
        "DATABASE_URL": os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/test.db",
        "BASE_URL": "https://desk.example.com",
        "SECRET_KEY": "test-secret", "ADMIN_EMAIL": "admin@example.com", "ADMIN_PASSWORD": "pw-123456789",
        "LLM_API_KEY": "sk-test", "VAPI_API_KEY": "vapi-test", "STRIPE_SECRET_KEY": "sk_test_x",
        "STRIPE_WEBHOOK_SECRET": "whsec_test", "TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "tok",
        "TWILIO_FROM_NUMBER": "+15555550000", "GOOGLE_PLACES_API_KEY": "places-key", "EMAIL_PROVIDER": "console",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from app import config, db
    from app.services import http, messaging

    config.reset_settings()
    db.reset_engine()
    if os.environ.get("TEST_DATABASE_URL"):
        from app import models  # noqa: F401

        db.Base.metadata.drop_all(db.engine())
    fake = FakeInternet()
    http.TRANSPORT = httpx.MockTransport(fake)
    messaging.OUTBOX.clear()
    yield fake
    http.TRANSPORT = None
    db.reset_engine()
    config.reset_settings()


@pytest.fixture()
def app_client(net):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(), base_url="https://desk.example.com") as c:
        yield c


@pytest.fixture()
def admin(app_client):
    r = app_client.post("/login", data={"email": "admin@example.com", "password": "pw-123456789"}, follow_redirects=False)
    assert r.status_code == 303
    return app_client


@pytest.fixture()
def db():
    from app.db import SessionLocal

    s = SessionLocal()
    yield s
    s.close()


TOKEN_HEADER = None


def vapi_headers():
    from app.config import get_settings

    return {"X-Deskline-Token": get_settings().webhook_token}
