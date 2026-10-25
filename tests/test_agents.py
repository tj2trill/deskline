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
