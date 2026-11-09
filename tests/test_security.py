"""Security regressions: every test here pins a hole that used to be open."""
from __future__ import annotations

import pytest

from tests.conftest import vapi_headers


def test_cross_site_posts_are_refused(admin):
    r = admin.post("/settings/team", data={"email": "evil@x.test"}, headers={"Origin": "https://attacker.test"},
                   follow_redirects=False)
    assert r.status_code == 403
    r = admin.post("/logout", headers={"Sec-Fetch-Site": "cross-site"}, follow_redirects=False)
    assert r.status_code == 403
    # same origin (either the Host header or BASE_URL) still works
    r = admin.post("/settings/test", data={"channel": "email", "to": "a@b.test"},
                   headers={"Origin": "https://desk.example.com"}, follow_redirects=False)
    assert r.status_code == 303


def test_webhooks_and_widget_are_not_blocked_by_the_csrf_guard(app_client):
    r = app_client.post("/webhooks/vapi", json={"message": {"type": "noop"}},
                        headers={**vapi_headers(), "Origin": "https://api.vapi.ai"})
    assert r.status_code == 200
    assert app_client.post("/webhooks/vapi", json={}, headers={"Origin": "https://x.test"}).status_code == 401


def test_security_headers(admin, app_client):
    h = admin.get("/").headers
    assert "frame-ancestors 'none'" in h["content-security-policy"] and "script-src 'self'" in h["content-security-policy"]
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert h["cache-control"] == "no-store"
    assert "unsafe-inline" not in h["content-security-policy"].split("script-src")[1].split(";")[0]


@pytest.mark.parametrize("target", ["/\\evil.test", "//evil.test", "https://evil.test", "/\tevil", "javascript:alert(1)"])
def test_login_never_redirects_off_site(app_client, target):
    r = app_client.post("/login", data={"email": "admin@example.com", "password": "pw-123456789", "next": target},
                        follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_login_is_throttled(app_client):
    from app.routers import auth

    auth.FAILS_PER_ACCOUNT._hits.clear()
    auth.FAILS_PER_IP._hits.clear()
    for _ in range(8):
        assert app_client.post("/login", data={"email": "admin@example.com", "password": "nope"}).status_code == 401
    r = app_client.post("/login", data={"email": "admin@example.com", "password": "pw-123456789"}, follow_redirects=False)
    assert r.status_code == 429 and "retry-after" in r.headers
    auth.FAILS_PER_ACCOUNT._hits.clear()
    auth.FAILS_PER_IP._hits.clear()


def test_password_change_signs_out_other_sessions(net):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app, base_url="https://desk.example.com") as a, TestClient(app, base_url="https://desk.example.com") as b:
        for c in (a, b):
            assert c.post("/login", data={"email": "admin@example.com", "password": "pw-123456789"},
                          follow_redirects=False).status_code == 303
        assert b.get("/", follow_redirects=False).status_code == 200
        a.post("/account/password", data={"current": "pw-123456789", "new": "a-much-longer-password"})
        assert a.get("/", follow_redirects=False).status_code == 200  # the browser that changed it stays in
        assert b.get("/", follow_redirects=False).status_code == 303  # a stolen/old cookie is dead


def test_placeholder_secrets_refuse_to_boot_on_a_public_url(net, monkeypatch):
    from app import config
    from app.main import check_deploy_secrets

    monkeypatch.setenv("SECRET_KEY", "replace-with-64-random-chars")
    config.reset_settings()
    with pytest.raises(RuntimeError):
        check_deploy_secrets(config.get_settings())
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")
    config.reset_settings()
    check_deploy_secrets(config.get_settings())  # local development is allowed


def test_javascript_urls_never_reach_an_href(admin, db):
    r = admin.post("/leads/import", data={"csv_text": "name,website\nEvil Co,javascript:alert(1)\n=cmd|x,https://ok.test\n"},
                   follow_redirects=False)
    sid = r.headers["location"].rsplit("/", 1)[1]
    from app.models import Prospect

    rows = db.query(Prospect).all()
    assert all("javascript" not in (p.website or "") for p in rows)
    # the export cannot start a cell with a formula character
    csv_text = admin.get(f"/leads/{sid}/export.csv").text
    assert "\n=cmd" not in csv_text and "'=cmd|x" in csv_text
    # and a value that was already in the database is neutralised when rendered
    p = rows[0]
    p.website = "javascript:alert(1)"
    db.commit()
    assert 'href="javascript:' not in admin.get(f"/prospects/{p.id}").text


def test_brand_colour_cannot_inject_css(admin, db):
    admin.post("/settings", data={"brand_name": "X", "primary_color": "red;}body{display:none"})
    from app.models import AgencySettings

    assert db.query(AgencySettings).first().primary_color.startswith("#")


def test_caller_supplied_text_is_escaped_in_owner_emails(admin, db):
    from app.models import Client
    from app.services import messaging, workflows

    admin.post("/clients", data={"name": "Acme", "owner_email": "o@acme.test"})
    c = db.query(Client).first()
    admin.post("/workflows", data={"name": "n", "trigger": "lead_captured", "action_type": "email",
                                   "action_text": "Lead: {caller_name} {caller_name.__class__}"})
    sent = []
    orig = messaging.send_email
    messaging.send_email = lambda to, subject, html, text=None, **kw: sent.append(html) or "x"
    try:
        workflows.run(db, "lead_captured", c, {"caller_name": "<img src=x onerror=alert(1)>"})
    finally:
        messaging.send_email = orig
    assert sent and "<img" not in sent[0] and "&lt;img" in sent[0]
    assert "class 'str'" not in sent[0]  # no attribute walking through format strings


def test_ssrf_guard_rejects_non_public_addresses():
    from app.services import crawler, http

    saved, http.TRANSPORT = http.TRANSPORT, None
    try:
        for url in ("http://127.0.0.1/", "http://[::ffff:10.0.0.5]/", "http://100.64.0.1/", "http://169.254.169.254/",
                    "ftp://example.com/", "file:///etc/passwd"):
            with pytest.raises(ValueError):
                crawler.assert_public_host(url)
    finally:
        http.TRANSPORT = saved


def test_workflow_webhook_must_be_https(admin, db):
    from app.models import Workflow

    admin.post("/workflows", data={"name": "n", "trigger": "call_ended", "action_type": "webhook", "action_to": "http://10.0.0.1/x"})
    assert db.query(Workflow).count() == 0


def test_agent_redirects_stay_on_site(admin):
    r = admin.post("/agents/actions/999/approve", data={"back": "/\\evil.test"}, follow_redirects=False)
    assert r.status_code in (303, 404)
    assert "evil" not in r.headers.get("location", "")


def test_chat_endpoint_rejects_junk(admin, db):
    from app.models import Client

    admin.post("/clients", data={"name": "Acme"})
    key = db.query(Client).first().widget_key
    assert admin.post(f"/api/chat/{key}", content=b"[1,2]", headers={"content-type": "application/json"}).status_code == 400
    assert admin.post(f"/api/chat/{key}", content=b"x" * 20000).status_code == 413


def test_owner_cannot_reach_agency_pages(admin, db, net):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import Client, User
    from app.security import hash_password

    admin.post("/clients", data={"name": "Acme"})
    admin.post("/clients", data={"name": "Other"})
    acme, other = db.query(Client).order_by(Client.id).all()
    db.add(User(email="own@acme.test", role="owner", client_id=acme.id, password_hash=hash_password("owner-pass-1234")))
    db.commit()
    with TestClient(create_app(), base_url="https://desk.example.com") as o:
        o.post("/login", data={"email": "own@acme.test", "password": "owner-pass-1234"})
        for path in ("/", "/clients", f"/clients/{other.id}", "/settings", "/billing", "/search.json?q=ac", "/leads", "/agents"):
            assert o.get(path, follow_redirects=False).status_code == 403, path
        assert o.get(f"/portal?client_id={other.id}").status_code == 200  # ignored: still their own business
        assert "Other" not in o.get(f"/portal?client_id={other.id}").text
        assert o.post(f"/clients/{other.id}/delete", data={"confirm": "Other"}, follow_redirects=False).status_code == 403
