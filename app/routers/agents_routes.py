"""The agent team: modes, settings, run history, and the approvals inbox."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import actions as agent_actions
from ..agents import office, runner
from ..agents.base import MODES
from ..agents.registry import REGISTRY, TEAM
from ..db import get_db
from ..models import AgencySettings, AgentAction, AgentConfig, AgentRun, User
from ..security import agency_user
from ..templating import flash, render

router = APIRouter(prefix="/agents", dependencies=[Depends(agency_user)])


def every(minutes: int) -> str:
    if minutes % 1440 == 0:
        return "daily" if minutes == 1440 else f"every {minutes // 1440} days"
    if minutes % 60 == 0:
        return "hourly" if minutes == 60 else f"every {minutes // 60} hours"
    return f"every {minutes} min"


def wants_json(request: Request) -> bool:
    return "application/json" in (request.headers.get("accept") or "")


def _configs(db: Session) -> dict[str, AgentConfig]:
    cfgs = runner.ensure_configs(db)
    db.commit()
    return cfgs


@router.get("", response_class=HTMLResponse)
def team(request: Request, db: Session = Depends(get_db)):
    cfgs = _configs(db)
    pending = db.scalars(select(AgentAction).where(AgentAction.status == "pending").order_by(AgentAction.created_at)).all()
    waiting = db.scalars(select(AgentAction).where(AgentAction.status == "approved").order_by(AgentAction.created_at)).all()
    recent = db.scalars(select(AgentAction).where(AgentAction.status.in_(["done", "failed", "rejected"]))
                        .order_by(AgentAction.decided_at.desc()).limit(25)).all()
    agency = db.scalars(select(AgencySettings)).first()
    return render(request, "agents/index.html", team=TEAM, cfgs=cfgs, pending=pending, waiting=waiting, recent=recent,
                  agency=agency, modes=MODES, every=every, label_of=agent_actions.label_of, registry=REGISTRY)


@router.get("/learning", response_class=HTMLResponse)
def learning_view(request: Request, db: Session = Depends(get_db)):
    from ..config import get_settings
    from ..models import Lesson
    from ..services import learning

    cfg = _configs(db)["strategist"]
    st = {**{k.name: k.default for k in REGISTRY["strategist"].knobs}, **(cfg.settings or {})}
    report = learning.evaluate(db, min_n=max(5, int(st["min_sample"])), sure=min(0.99, max(0.6, int(st["confidence"]) / 100)))
    lessons = db.scalars(select(Lesson).order_by(Lesson.changed_at.desc())).all()
    return render(request, "agents/learning.html", r=report, lessons=lessons, cfg=cfg,
                  reply_capture=bool(get_settings().inbound_reply_to))


@router.get("/office", response_class=HTMLResponse)
def office_view(request: Request, db: Session = Depends(get_db)):
    return render(request, "agents/office.html", snap=office.snapshot(db))


@router.get("/office.json")
def office_json(db: Session = Depends(get_db)):
    return JSONResponse(office.snapshot(db), headers={"Cache-Control": "no-store"})


@router.get("/{key}", response_class=HTMLResponse)
def agent_detail(request: Request, key: str, db: Session = Depends(get_db)):
    agent = REGISTRY.get(key)
    if not agent:
        raise HTTPException(404)
    cfg = _configs(db)[key]
    runs = db.scalars(select(AgentRun).where(AgentRun.agent_key == key).order_by(AgentRun.started_at.desc()).limit(30)).all()
    acts = db.scalars(select(AgentAction).where(AgentAction.agent_key == key).order_by(AgentAction.created_at.desc()).limit(30)).all()
    values = {k.name: (cfg.settings or {}).get(k.name, k.default) for k in agent.knobs}
    return render(request, "agents/detail.html", agent=agent, cfg=cfg, runs=runs, acts=acts, values=values, modes=MODES,
                  every=every, label_of=agent_actions.label_of)


@router.get("/{key}/status", response_class=HTMLResponse)
def agent_status(request: Request, key: str, db: Session = Depends(get_db)):
    cfg = _configs(db).get(key)
    if not cfg:
        raise HTTPException(404)
    return render(request, "agents/_status.html", cfg=cfg)


@router.post("/switch")
def switch(request: Request, db: Session = Depends(get_db)):
    agency = db.scalars(select(AgencySettings)).first()
    agency.agents_enabled = not agency.agents_enabled
    db.commit()
    if wants_json(request):
        return {"ok": True, "enabled": agency.agents_enabled,
                "message": "Agents resumed." if agency.agents_enabled else "All agents paused."}
    flash(request, "Agents resumed." if agency.agents_enabled else "All agents paused. Nothing runs until you resume.")
    return RedirectResponse("/agents", status_code=303)


@router.post("/{key}/mode")
def set_mode(request: Request, key: str, mode: str = Form(...), back: str = Form("/agents"), db: Session = Depends(get_db)):
    if key not in REGISTRY or mode not in MODES:
        raise HTTPException(400)
    cfg = _configs(db)[key]
    was_off = cfg.mode == "off"
    cfg.mode = mode
    if was_off and mode != "off":
        cfg.next_run_at = None  # run on the next tick rather than waiting a full interval
    db.commit()
    if wants_json(request):
        return {"ok": True, "mode": mode, "message": f"{REGISTRY[key].title}: {MODES[mode]}."}
    flash(request, f"{REGISTRY[key].title}: {MODES[mode]}.")
    return RedirectResponse(back if back.startswith("/agents") else "/agents", status_code=303)


@router.post("/{key}/settings")
async def save_settings(request: Request, key: str, db: Session = Depends(get_db)):
    agent = REGISTRY.get(key)
    if not agent:
        raise HTTPException(404)
    form = await request.form()
    cfg = _configs(db)[key]
    values = dict(cfg.settings or {})
    for k in agent.knobs:
        raw = form.get(k.name)
        if k.type == "bool":
            values[k.name] = raw is not None
        elif k.type == "int":
            try:
                values[k.name] = max(0, int(raw))
            except (TypeError, ValueError):
                flash(request, f"{k.label} must be a whole number.", "error")
                return RedirectResponse(f"/agents/{key}", status_code=303)
        elif k.type == "lines":
            values[k.name] = [ln.strip() for ln in str(raw or "").splitlines() if ln.strip()]
        else:
            values[k.name] = str(raw or "").strip()
    try:
        cfg.interval_minutes = max(5, int(form.get("interval_minutes") or cfg.interval_minutes))
    except ValueError:
        pass
    cfg.settings = values
    db.commit()
    flash(request, f"{agent.title} settings saved.")
    return RedirectResponse(f"/agents/{key}", status_code=303)


@router.post("/{key}/run")
def run_now(request: Request, key: str, bg: BackgroundTasks):
    if key not in REGISTRY:
        raise HTTPException(404)
    bg.add_task(runner.run_agent, key, "manual")
    if wants_json(request):
        return {"ok": True, "message": f"{REGISTRY[key].title} is on it."}
    flash(request, f"{REGISTRY[key].title} is running. Results appear below in a moment.")
    return RedirectResponse(f"/agents/{key}", status_code=303)


def _who(request: Request, db: Session) -> str:
    user = db.get(User, request.session.get("uid"))
    return user.email if user else "agency"


@router.post("/actions/{action_id}/{decision}")
def decide(request: Request, action_id: int, decision: str, back: str = Form("/agents"), db: Session = Depends(get_db)):
    if decision not in ("approve", "reject", "retry"):
        raise HTTPException(400)
    a = runner.decide(db, action_id, approve=decision != "reject", who=_who(request, db))
    if not a:
        raise HTTPException(404)
    if a.status == "done":
        msg, kind = a.result or "Done.", "ok"
    elif a.status == "failed":
        msg, kind = f"Couldn't do it: {a.result}", "error"
    elif a.status == "approved":
        msg, kind = a.result or "Approved. It will run shortly.", "ok"
    else:
        msg, kind = "Rejected. The agent won't propose it again.", "ok"
    if wants_json(request):
        return {"ok": kind == "ok", "status": a.status, "message": msg, "agent": a.agent_key, "kind": a.kind}
    flash(request, msg, kind)
    return RedirectResponse(back if back.startswith("/") and not back.startswith("//") else "/agents", status_code=303)


@router.post("/actions/approve-all")
def approve_all(request: Request, agent: str = Form(""), db: Session = Depends(get_db)):
    q = select(AgentAction).where(AgentAction.status == "pending")
    if agent:
        q = q.where(AgentAction.agent_key == agent)
    who = _who(request, db)
    done = failed = 0
    for a in db.scalars(q.order_by(AgentAction.created_at)).all():
        a = runner.decide(db, a.id, approve=True, who=who)
        done += a.status in ("done", "approved")
        failed += a.status == "failed"
    flash(request, f"Approved {done}." + (f" {failed} couldn't be done; see Activity." if failed else ""),
          "error" if failed and not done else "ok")
    return RedirectResponse("/agents", status_code=303)
