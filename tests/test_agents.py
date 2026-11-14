"""Agent framework guardrails: kill switch, approvals, dedupe, failure isolation, email cap deferral."""


def _setup(db, **agency):
    from app.agents import runner
    from app.main import bootstrap
    from app.models import AgencySettings

    bootstrap()
    a = db.query(AgencySettings).first()
    for k, v in agency.items():
        setattr(a, k, v)
    cfgs = runner.ensure_configs(db)
    db.commit()
    return cfgs


def test_paused_team_runs_nothing(net, db):
    from app.agents import runner
    from app.models import AgentRun

    _setup(db, agents_enabled=False)
    assert runner.tick() == []
    assert db.query(AgentRun).count() == 0


def test_rejected_action_is_never_proposed_again(net, db):
    from app.agents import runner
    from app.models import AgentAction, Prospect

    cfgs = _setup(db, agents_enabled=True, mailing_address="1 Main St")
    cfgs["outreach"].settings = {"require_demo": False, "window_start": "00:00", "window_end": "23:59", "weekdays_only": False}
    db.add(Prospect(name="A", niche="plumber", email="a@a.ca", score=90))
    db.commit()
    runner.run_agent("outreach", "manual")
    a = db.query(AgentAction).one()
    assert a.status == "pending"
    runner.decide(db, a.id, approve=False, who="me")
    runner.run_agent("outreach", "manual")
    db.expire_all()
    assert db.query(AgentAction).count() == 1


def test_email_cap_defers_instead_of_failing(net, db):
    from app.agents import runner
    from app.models import AgentAction, EmailDraft, Prospect

    cfgs = _setup(db, agents_enabled=True, mailing_address="1 Main St", daily_email_cap=0)
    cfgs["outreach"].settings = {"require_demo": False, "window_start": "00:00", "window_end": "23:59", "weekdays_only": False}
    p = Prospect(name="A", niche="plumber", email="a@a.ca", score=90)
    db.add(p)
    db.flush()
    d = EmailDraft(prospect_id=p.id, step=1, subject="s", body="b")
    db.add(d)
    db.flush()
    db.add(AgentAction(agent_key="outreach", kind="send_email_draft", title="Email 1 to A", payload={"draft_id": d.id},
                       dedupe_key=f"email:{d.id}", status="pending"))
    db.commit()
    a = runner.decide(db, db.query(AgentAction).one().id, approve=True, who="me")
    assert (a.status, a.result[:7]) == ("approved", "Waiting"), a.result


def test_one_agent_crashing_does_not_stop_the_others(net, db, monkeypatch):
    from app.agents import runner
    from app.agents.registry import REGISTRY

    _setup(db, agents_enabled=True)
    monkeypatch.setattr(type(REGISTRY["account_manager"]), "run", lambda self, ctx: 1 / 0)
    ran = runner.tick()
    assert "ops_monitor" in ran
    from app.models import AgentConfig

    db.expire_all()
    am = db.query(AgentConfig).filter_by(key="account_manager").one()
    assert am.last_status == "error" and am.running_since is None


def test_no_website_prospect_gets_a_listing_demo_then_lands_on_the_call_list(net, db):
    """A business found with no website and no email: Demo builder builds from its Google listing, Outreach puts it
    on the call list (even on Autopilot, since a person makes the call), and "Called" marks it contacted."""
    from app.agents import runner
    from app.models import AgentAction, Client, Prospect, Site

    cfgs = _setup(db, agents_enabled=True)  # no mailing address: emails are paused, calls are not
    cfgs["outreach"].mode = "auto"
    db.add(Prospect(name="Bayfront Drain Pros", niche="plumber", phone="+19055550123", address="1 Bay St, Hamilton, ON",
                    hours=["Monday: 8:00 AM - 5:00 PM"], rating=4.8, review_count=31, score=90))
    db.commit()

    run = runner.run_agent("demo_builder", "manual")
    assert run.status == "ok", run.summary
    db.expire_all()
    p = db.query(Prospect).one()
    c = db.get(Client, p.client_id)
    assert p.status == "demo" and c.website is None and c.address == "1 Bay St, Hamilton, ON"
    assert "Monday: 8:00 AM - 5:00 PM" in c.agent.knowledge and "4.8" in c.agent.knowledge
    site = db.query(Site).filter_by(client_id=c.id).one()

    run = runner.run_agent("outreach", "manual")
    assert "1 added to your call list" in run.summary, run.summary
    a = db.query(AgentAction).filter_by(kind="call_prospect").one()
    assert a.status == "pending" and "+19055550123" in a.title and f"/s/{site.slug}" in a.detail
    runner.run_agent("outreach", "manual")
    assert db.query(AgentAction).filter_by(kind="call_prospect").count() == 1  # listed once

    a = runner.decide(db, a.id, approve=True, who="me")
    db.expire_all()
    assert a.status == "done" and db.query(Prospect).one().status == "contacted"


def test_email_from_a_call_gets_the_demo_as_promised_not_a_cold_pitch(net, db):
    """Called + an email typed in: Outreach sends 'the demo, as promised' with the demo site, even with require_demo
    on and no demo number, and the learning loop doesn't count it as a cold email."""
    from app.agents import runner
    from app.models import AgentAction, EmailDraft, Prospect, Site
    from app.services import learning

    cfgs = _setup(db, agents_enabled=True, mailing_address="1 Main St")
    cfgs["outreach"].settings = {"require_demo": True, "window_start": "00:00", "window_end": "23:59", "weekdays_only": False}
    db.add(Prospect(name="Bayfront Drain Pros", niche="plumber", phone="+19055550123", address="1 Bay St, Hamilton, ON",
                    hours=["Monday: 8:00 AM - 5:00 PM"], rating=4.8, review_count=31, score=90))
    db.commit()
    runner.run_agent("demo_builder", "manual")
    runner.run_agent("outreach", "manual")
    db.expire_all()
    a = db.query(AgentAction).filter_by(kind="call_prospect").one()
    assert "What email should I send the link to?" in a.detail
    a.payload = {**a.payload, "email": "owner@bayfront.test"}
    db.commit()
    runner.decide(db, a.id, approve=True, who="me")

    run = runner.run_agent("outreach", "manual")
    db.expire_all()
    p = db.query(Prospect).one()
    site = db.query(Site).filter_by(client_id=p.client_id).one()
    drafts = db.query(EmailDraft).filter_by(prospect_id=p.id).order_by(EmailDraft.step).all()
    assert [d.step for d in drafts] == [1, 2, 3]
    assert drafts[0].subject == "The demo for Bayfront Drain Pros, as promised"
    assert "Thanks for taking my call" in drafts[0].body and f"/s/{site.slug}" in drafts[0].body
    assert p.angle is None and any("sending the demo they asked for on the call" in str(x) for x in (run.log or []))
    assert db.query(AgentAction).filter_by(kind="send_email_draft").count() == 1
    fact = next(f for f in learning.facts(db) if f.id == p.id)
    assert fact.has_email and not fact.contacted
