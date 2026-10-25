"""The agents' learning loop, reply capture, the Strategist, and the Office feed."""
import json
import random
from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime.now(timezone.utc)


def seed_outreach(db, plan, angle_pos=None):
    """plan: {(niche, location): (contacted, positive, paying)}. Returns prospects by target."""
    from app.models import Client, EmailDraft, Prospect, ProspectSearch

    out = {}
    for (niche, loc), (n, pos, paying) in plan.items():
        s = ProspectSearch(niche=niche, location=loc, result_count=n)
        db.add(s)
        db.flush()
        ps = []
        for i in range(n):
            p = Prospect(search_id=s.id, name=f"{niche} {loc} {i}", niche=niche, email=f"o{i}@{niche}-{loc[:3].lower()}{i}.ca",
                         score=70, status="contacted", angle="missed_calls", contacted_with_demo=i % 2 == 0)
            db.add(p)
            db.flush()
            db.add(EmailDraft(prospect_id=p.id, step=1, subject="s", body="b", status="sent", sent_at=NOW - timedelta(days=5)))
            if i < pos:
                p.status = "replied"
            if i < paying:
                c = Client(name=p.name, slug=f"c-{p.id}", status="active", industry=niche, stripe_status="active",
                           monthly_price_cents=29700)
                db.add(c)
                db.flush()
                p.client_id, p.status = c.id, "won"
            ps.append(p)
        out[(niche, loc)] = ps
    db.commit()
    return out


# ------------------------------------------------------------------ statistics

def test_wilson_interval_is_honest_at_small_samples():
    from app.services.learning import wilson

    lo, hi = wilson(1, 2)
    assert lo < 0.2 and hi > 0.8           # one reply out of two proves almost nothing
    lo, hi = wilson(50, 500)
    assert 0.075 < lo < 0.1 < hi < 0.13


def test_judge_needs_data_and_confidence():
    from app.services.learning import judge

    assert judge(3, 5, 10, 200, 0.05)[0] == "early"           # 60% looks great, but n=5
    assert judge(12, 40, 8, 200, 0.05)[0] == "works"          # 30% vs 4% with n=40
    assert judge(0, 40, 20, 200, 0.08)[0] == "fails"          # 0% vs 10%
    assert judge(4, 40, 20, 200, 0.1)[0] in ("early", "average")  # same as the rest


def test_more_arms_raise_the_bar():
    """Five equal targets with noise must not produce a 'winner' by chance."""
    from app.services.learning import Arm, judge_all

    rng = random.Random(1)
    arms = {}
    for k in range(5):
        pos = sum(rng.random() < 0.06 for _ in range(40))
        arms[str(k)] = Arm(str(k), str(k), n=40, pos=pos)
    judge_all(arms, 0.06)
    assert all(a.verdict != "works" for a in arms.values())


# ------------------------------------------------------------------ evaluation + proposals

def test_evaluate_finds_working_and_dead_targets_and_money(net, db):
    from app.main import bootstrap
    from app.services import learning

    bootstrap()
    seed_outreach(db, {("plumber", "Hamilton, ON"): (40, 10, 2), ("roofer", "Toronto, ON"): (40, 0, 0),
                       ("dentist", "Ottawa, ON"): (6, 1, 0)})
    r = learning.evaluate(db)
    by = {l["key"]: l for l in r["lessons"]}
    assert by["target:plumber | hamilton, on"]["verdict"] == "works"
    assert by["target:roofer | toronto, on"]["verdict"] == "fails"
    assert by["target:dentist | ottawa, on"]["verdict"] == "early"
    assert "Plumber | Hamilton, ON" in by["target:plumber | hamilton, on"]["statement"]
    assert r["funnel"]["paying"] == 2 and r["funnel"]["mrr"] == 2 * 29700
    assert any(l["area"] == "client" and l["verdict"] == "earns" for l in r["lessons"])


def test_unprofitable_client_is_flagged(net, db):
    from app.main import bootstrap
    from app.models import Call, Client
    from app.services import learning

    bootstrap()
    c = Client(name="Chatty Spa", slug="chatty", status="active", industry="spa", stripe_status="active",
               monthly_price_cents=1000, tiers=[])
    db.add(c)
    db.flush()
    for i in range(30):
        db.add(Call(client_id=c.id, vapi_call_id=f"x{i}", duration_seconds=600, billable_minutes=10, cost_cents=150))
    db.commit()
    les = [l for l in learning.evaluate(db)["lessons"] if l["key"] == f"client:{c.id}"]
    assert les and les[0]["verdict"] == "loses" and "loses" in les[0]["statement"]


def test_strategist_proposes_retiring_dead_target_and_approval_applies_it(net, db):
    from app.agents import runner
    from app.main import bootstrap
    from app.models import AgentAction, AgentConfig, EventLog, Lesson

    bootstrap()
    seed_outreach(db, {("plumber", "Hamilton, ON"): (40, 10, 1), ("roofer", "Toronto, ON"): (40, 0, 0)})
    cfgs = runner.ensure_configs(db)
    cfgs["prospector"].settings = {"targets": ["plumber | Hamilton, ON", "Roofer | Toronto, ON"]}
    db.commit()

    run = runner.run_agent("strategist", "manual")
    assert run.status == "ok", run.summary
    db.expire_all()
    a = db.query(AgentAction).filter_by(kind="apply_setting").one()
    assert a.status == "pending" and "Roofer | Toronto, ON" in a.title
    assert db.query(Lesson).filter_by(verdict="fails").count() >= 1
    assert db.query(EventLog).filter_by(kind="learning").count() >= 2

    announced = db.query(EventLog).filter_by(kind="learning").count()
    runner.decide(db, a.id, approve=True, who="admin@example.com")
    db.expire_all()
    st = db.query(AgentConfig).filter_by(key="prospector").one().settings
    assert st["targets"] == ["plumber | Hamilton, ON"] and st["retired"] == ["Roofer | Toronto, ON"]

    # running again doesn't re-propose or re-announce
    runner.run_agent("strategist", "manual")
    db.expire_all()
    assert db.query(AgentAction).filter_by(kind="apply_setting").count() == 1
    assert db.query(EventLog).filter_by(kind="learning").count() == announced


def test_autopilot_is_proposed_only_after_a_clean_record(net, db):
    from app.agents import runner
    from app.main import bootstrap
    from app.models import AgentAction
    from app.services import learning

    bootstrap()
    runner.ensure_configs(db)
    for i in range(16):
        db.add(AgentAction(agent_key="outreach", kind="send_email_draft", title=f"e{i}", payload={}, dedupe_key=f"d{i}",
                           status="done", decided_by="admin@example.com"))
    db.add(AgentAction(agent_key="billing_guardian", kind="pause_client", title="p", payload={}, dedupe_key="p1",
                       status="done", decided_by="admin@example.com"))
    db.commit()
    r = learning.evaluate(db)
    props = learning.proposals(db, r, {"propose_autopilot": True})
    assert [p["payload"] for p in props if p["kind"] == "set_mode"] == [{"agent": "outreach", "mode": "auto"}]


# ------------------------------------------------------------------ policies

def test_rank_targets_prefers_what_pays_but_still_explores(net, db):
    from app.main import bootstrap
    from app.services import learning

    bootstrap()
    seed_outreach(db, {("plumber", "Hamilton, ON"): (40, 10, 2), ("roofer", "Toronto, ON"): (40, 0, 0)})
    targets = ["roofer | Toronto, ON", "plumber | Hamilton, ON", "hvac | Burlington, ON"]
    rng = random.Random(5)
    firsts = [learning.rank_targets(db, targets, rng)[0] for _ in range(200)]
    assert firsts.count("plumber | Hamilton, ON") > 120
    assert firsts.count("roofer | Toronto, ON") < 5
    assert firsts.count("hvac | Burlington, ON") > 0      # never searched: still gets tried


def test_angle_model_shifts_to_the_angle_that_gets_replies(net, db):
    from app.main import bootstrap
    from app.models import Prospect
    from app.services import learning

    bootstrap()
    ps = seed_outreach(db, {("plumber", "Hamilton, ON"): (120, 0, 0)})[("plumber", "Hamilton, ON")]
    for i, p in enumerate(ps):
        p.angle = ["missed_calls", "after_hours", "busy_owner"][i % 3]
        if p.angle == "after_hours" and i % 2 == 0:
            p.status = "replied"
    db.commit()
    model = learning.AngleModel(db, random.Random(3))
    picks = [model.choose("plumber", None) for _ in range(300)]
    assert picks.count("after_hours") > 240
    assert "demo_first" not in picks                     # no demo line, so that angle can't be used
    assert db.query(Prospect).count() == 120


def test_outreach_records_angle_and_demo_flag(net, db):
    from app.agents import runner
    from app.main import bootstrap
    from app.models import AgencySettings, EmailDraft, Prospect

    bootstrap()
    a = db.query(AgencySettings).first()
    a.mailing_address, a.agents_enabled = "1 Main St, Hamilton", True
    p = Prospect(name="Rapid Rooter", niche="plumber", email="hi@rapid.ca", score=80, status="new")
    db.add(p)
    cfgs = runner.ensure_configs(db)
    cfgs["outreach"].settings = {"require_demo": False, "window_start": "00:00", "window_end": "23:59", "weekdays_only": False}
    db.commit()
    run = runner.run_agent("outreach", "manual")
    assert run.status == "ok", run.log
    db.expire_all()
    p = db.query(Prospect).one()
    assert p.angle in ("missed_calls", "after_hours", "busy_owner") and p.contacted_with_demo is False
    assert db.query(EmailDraft).filter_by(prospect_id=p.id).count() >= 2


# ------------------------------------------------------------------ reply capture

def _reply(client, payload, token=None, **kw):
    from app.config import get_settings

    return client.post(f"/webhooks/inbound-email/{token or get_settings().webhook_token}", **({"json": payload} | kw))


def test_inbound_reply_requires_token(app_client):
    assert _reply(app_client, {"from": "a@b.ca", "text": "hi"}, token="nope").status_code == 401


def test_unsubscribe_reply_suppresses_and_stops_sequence(app_client, db):
    from app.models import EmailDraft, Prospect, Suppression

    p = Prospect(name="Steeltown Drains", email="owner@steeltown.ca", status="contacted")
    db.add(p)
    db.flush()
    db.add(EmailDraft(prospect_id=p.id, step=2, subject="Re", body="b", status="draft"))
    db.commit()
    r = _reply(app_client, {"From": "Owner <OWNER@steeltown.ca>", "Subject": "Re: Calls",
                            "TextBody": "Please remove me from your list.\n\nOn Mon, Oct 5, 2026 Deskline wrote:\n> Hi",
                            "MessageID": "m-1"})
    assert r.status_code == 200 and r.json()["intent"] == "unsubscribe"
    db.expire_all()
    assert db.query(Suppression).filter_by(email="owner@steeltown.ca").count() == 1
    assert db.query(EmailDraft).filter_by(prospect_id=p.id).one().status == "skipped"
    assert db.query(Prospect).get(p.id).status == "lost"
    # same message again (provider retry) is not processed twice
    _reply(app_client, {"From": "owner@steeltown.ca", "TextBody": "Please remove me", "MessageID": "m-1"})
    from app.models import InboundEmail

    assert db.query(InboundEmail).count() == 1


def test_interested_reply_uses_typed_llm_decision(app_client, db, net):
    from app.models import Prospect
    from app.services import messaging

    p = Prospect(name="Rapid Rooter", email="info@rapidrooter.ca", website="https://rapidrooter.ca", status="contacted")
    db.add(p)
    db.commit()
    net.llm_queue.append({"content": json.dumps({"intent": "interested", "confidence": 0.93, "reason": "wants a call"})})
    # the owner answers from another address at the same business domain (form-encoded, Mailgun style)
    r = _reply(app_client, None, data={"sender": "mike@rapidrooter.ca", "subject": "Re: Calls", "stripped-text": "Sure, call me Tuesday"})
    body = r.json()
    assert body["intent"] == "interested" and body["confidence"] == 0.93 and body["prospect_id"] == p.id
    db.expire_all()
    p = db.query(Prospect).get(p.id)
    assert p.status == "replied" and p.replied_at is not None
    assert any("replied" in m["subject"] for m in messaging.OUTBOX)  # you're told right away


def test_low_confidence_reply_is_left_for_you(app_client, db, net):
    from app.models import Prospect

    p = Prospect(name="Bayview", email="hello@bayview.ca", status="contacted")
    db.add(p)
    db.commit()
    net.llm_queue.append({"content": json.dumps({"intent": "interested", "confidence": 0.5})})
    _reply(app_client, {"from": "hello@bayview.ca", "text": "hmm maybe, what is this exactly"})
    db.expire_all()
    assert db.query(Prospect).get(p.id).status == "contacted"   # not acted on, but the sequence stopped
    assert db.query(Prospect).get(p.id).replied_at is not None


def test_rules_fallback_without_llm(monkeypatch):
    from app.services import llm, replies

    monkeypatch.setattr(llm, "available", lambda: False)
    assert replies.classify("Not interested, thanks")[0] == "not_interested"
    assert replies.classify("I'm out of the office until Monday")[0] == "auto_reply"
    assert replies.classify("How much does it cost?")[0] == "question"
    assert replies.classify("ok")[0] == "unclear"


# ------------------------------------------------------------------ office

def test_office_snapshot_is_built_from_real_state(admin, db):
    from app.agents import runner
    from app.models import AgencySettings, AgentAction, Prospect

    a = db.query(AgencySettings).first()
    a.agents_enabled = True
    runner.ensure_configs(db)
    db.add(Prospect(name="Steeltown Drains", email="x@s.ca", status="replied", score=80))
    db.add(AgentAction(agent_key="closer", kind="send_checkout", title="Email Maple their checkout link", payload={},
                       dedupe_key="checkout:1", status="pending"))
    db.commit()
    snap = admin.get("/agents/office.json").json()
    closer = next(x for x in snap["agents"] if x["key"] == "closer")
    assert closer["status"] == "waiting" and closer["pending"][0]["title"] == "Email Maple their checkout link"
    # money closest to cash comes first
    assert snap["next_step"]["agent"] == "closer"
    assert snap["replied"][0]["name"] == "Steeltown Drains"
    assert any(c["from"] == "outreach" and "Steeltown Drains" in c["lines"][0][1] for c in snap["conversations"])
    assert {x["key"] for x in snap["agents"]} >= {"strategist", "prospector"}


def test_office_actions_answer_in_json(admin, db):
    from app.agents import runner
    from app.models import AgentAction, AgentConfig, Prospect

    runner.ensure_configs(db)
    p = Prospect(name="Rapid", email="r@r.ca", status="replied")
    act = AgentAction(agent_key="outreach", kind="send_email_draft", title="Email 1", payload={"draft_id": 999},
                      dedupe_key="email:999", status="pending")
    db.add_all([p, act])
    db.commit()
    h = {"Accept": "application/json"}
    r = admin.post(f"/agents/actions/{act.id}/reject", data={"back": "/agents/office"}, headers=h)
    assert r.json()["status"] == "rejected"
    r = admin.post("/agents/account_manager/mode", data={"mode": "off"}, headers=h)
    assert r.json()["ok"] and db.query(AgentConfig).filter_by(key="account_manager").one().mode == "off"
    r = admin.post(f"/prospects/{p.id}/status", data={"status": "won"}, headers=h)
    assert "marked won" in r.json()["message"]
    db.expire_all()
    assert db.query(Prospect).get(p.id).replied_at is not None
    assert admin.get("/agents/office").status_code == 200
    assert admin.get("/agents/learning").status_code == 200


def test_upgrade_fills_defaults_for_new_flag_columns(net):
    """An upgraded database must not read a new boolean flag as NULL (= off)."""
    from sqlalchemy import text

    from app import db
    from app.models import AgencySettings

    db.init_db()
    eng = db.engine()
    with db.SessionLocal() as s:
        s.add(AgencySettings(brand_name="X"))
        s.commit()
    with eng.begin() as conn:
        conn.execute(text('ALTER TABLE agency_settings DROP COLUMN agents_enabled'))
    db.add_missing_columns()
    with db.SessionLocal() as s:
        assert s.query(AgencySettings).first().agents_enabled is True
