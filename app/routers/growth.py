"""Finding and closing clients: lead finder, outreach, proposals, demo sites, AI tools."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import Client, EmailDraft, Proposal, Prospect, ProspectSearch, Site, Suppression
from ..security import agency_user
from ..services import http, leads, pricing, sitegen, writer
from ..templating import flash, render
from .settings_routes import agency_settings

router = APIRouter(dependencies=[Depends(agency_user)])


def _prospect(db: Session, pid: int) -> Prospect:
    p = db.get(Prospect, pid)
    if not p:
        raise HTTPException(404)
    return p


@router.get("/leads", response_class=HTMLResponse)
def leads_home(request: Request, db: Session = Depends(get_db)):
    searches = db.scalars(select(ProspectSearch).order_by(ProspectSearch.created_at.desc()).limit(30)).all()
    top = db.scalars(select(Prospect).where(Prospect.status.in_(["new", "contacted", "replied", "demo"]))
                     .order_by(Prospect.score.desc()).limit(25)).all()
    return render(request, "leads/index.html", searches=searches, top=top, places_ready=leads.configured())


@router.post("/leads/search")
def leads_search(request: Request, niche: str = Form(...), location: str = Form(...), db: Session = Depends(get_db)):
    try:
        s = leads.run_search(db, niche.strip(), location.strip())
        db.commit()
    except (http.ProviderError, http.NotConfigured) as e:
        db.rollback()
        flash(request, str(e), "error")
        return RedirectResponse("/leads", status_code=303)
    flash(request, f"Found {s.result_count} new businesses.")
    return RedirectResponse(f"/leads/{s.id}", status_code=303)


@router.post("/leads/import")
async def leads_import(request: Request, file: UploadFile | None = File(None), csv_text: str = Form(""),
                       niche: str = Form("imported"), db: Session = Depends(get_db)):
    text = csv_text
    if file and file.filename:
        text = (await file.read()).decode("utf-8-sig", errors="replace")
    if not text.strip():
        flash(request, "Paste CSV text or choose a file (columns: name, phone, website, email, address).", "error")
        return RedirectResponse("/leads", status_code=303)
    s = leads.import_csv(db, text, niche or "imported")
    db.commit()
    flash(request, f"Imported {s.result_count} leads.")
    return RedirectResponse(f"/leads/{s.id}", status_code=303)


@router.get("/leads/{search_id}", response_class=HTMLResponse)
def leads_results(request: Request, search_id: int, db: Session = Depends(get_db)):
    s = db.get(ProspectSearch, search_id)
    if not s:
        raise HTTPException(404)
    rows = db.scalars(select(Prospect).where(Prospect.search_id == s.id).order_by(Prospect.score.desc())).all()
    return render(request, "leads/results.html", search=s, prospects=rows)


@router.get("/leads/{search_id}/export.csv")
def leads_export(search_id: int, db: Session = Depends(get_db)):
    rows = db.scalars(select(Prospect).where(Prospect.search_id == search_id).order_by(Prospect.score.desc())).all()
    return PlainTextResponse(leads.to_csv(rows), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="leads-{search_id}.csv"'})


@router.post("/leads/{search_id}/enrich")
def leads_enrich_all(request: Request, search_id: int, limit: int = Form(10), db: Session = Depends(get_db)):
    rows = db.scalars(select(Prospect).where(Prospect.search_id == search_id, Prospect.enriched.is_(False))
                      .order_by(Prospect.score.desc()).limit(min(limit, 25))).all()
    done = 0
    for p in rows:
        try:
            leads.enrich(p)
            done += 1
        except Exception as e:
            p.enriched = True
            p.signals = {**(p.signals or {}), "error": str(e)[:200]}
    db.commit()
    flash(request, f"Checked {done} websites for emails, chat widgets and online booking.")
    return RedirectResponse(f"/leads/{search_id}", status_code=303)


@router.get("/prospects/{pid}", response_class=HTMLResponse)
def prospect_detail(request: Request, pid: int, db: Session = Depends(get_db)):
    p = _prospect(db, pid)
    emails = db.scalars(select(EmailDraft).where(EmailDraft.prospect_id == p.id).order_by(EmailDraft.step)).all()
    proposals = db.scalars(select(Proposal).where(Proposal.prospect_id == p.id).order_by(Proposal.created_at.desc())).all()
    site = db.scalars(select(Site).where(Site.prospect_id == p.id)).first()
    client = db.get(Client, p.client_id) if p.client_id else None
    suppressed = bool(p.email and db.scalars(select(Suppression).where(Suppression.email == p.email.lower())).first())
    return render(request, "leads/prospect.html", p=p, emails=emails, proposals=proposals, site=site, client=client,
                  suppressed=suppressed, agency=agency_settings(db))


@router.post("/prospects/{pid}/update")
def prospect_update(pid: int, status: str = Form("new"), email: str = Form(""), notes: str = Form(""),
                    db: Session = Depends(get_db)):
    p = _prospect(db, pid)
    p.status = status if status in ("new", "contacted", "replied", "demo", "won", "lost") else p.status
    if p.status in ("replied", "won") and not p.replied_at:
        from datetime import datetime, timezone

        p.replied_at = datetime.now(timezone.utc)
    p.email, p.notes = email.strip() or None, notes or None
    db.commit()
    return RedirectResponse(f"/prospects/{pid}", status_code=303)


@router.post("/prospects/{pid}/status")
def prospect_status(request: Request, pid: int, bg: BackgroundTasks, status: str = Form(...), db: Session = Depends(get_db)):
    """Change only the pipeline stage. Marking a deal won wakes the Closer so the checkout link is ready sooner."""
    p = _prospect(db, pid)
    if status not in ("new", "contacted", "replied", "demo", "won", "lost"):
        raise HTTPException(400, "Unknown status")
    p.status = status
    if status in ("replied", "won") and not p.replied_at:
        from datetime import datetime, timezone

        p.replied_at = datetime.now(timezone.utc)
    db.commit()
    message = f"{p.name} marked {status}."
    if status == "won":
        from ..agents import runner
        from ..models import AgentConfig

        cfg = db.scalars(select(AgentConfig).where(AgentConfig.key == "closer")).first()
        if cfg and cfg.mode != "off":
            bg.add_task(runner.run_agent, "closer", "manual")
            message += " The Closer is preparing their checkout link."
        else:
            message += " Turn the Closer on to send their checkout link."
    if "application/json" in (request.headers.get("accept") or ""):
        return {"ok": True, "message": message}
    flash(request, message)
    return RedirectResponse(f"/prospects/{pid}", status_code=303)


@router.post("/prospects/{pid}/enrich")
def prospect_enrich(request: Request, pid: int, db: Session = Depends(get_db)):
    p = _prospect(db, pid)
    try:
        leads.enrich(p)
        flash(request, "Website checked.")
    except Exception as e:
        flash(request, f"Couldn't read their site: {e}", "error")
    db.commit()
    return RedirectResponse(f"/prospects/{pid}", status_code=303)


@router.post("/prospects/{pid}/emails/generate")
def emails_generate(request: Request, pid: int, db: Session = Depends(get_db)):
    p = _prospect(db, pid)
    client = db.get(Client, p.client_id) if p.client_id else None
    demo = client.numbers[0].number if client and client.numbers else None
    for d in db.scalars(select(EmailDraft).where(EmailDraft.prospect_id == p.id, EmailDraft.status == "draft")):
        db.delete(d)
    from ..services import learning

    angle = learning.AngleModel(db).choose(p.niche, demo)
    p.angle, p.contacted_with_demo = angle, bool(demo)
    for e in writer.email_sequence(agency_settings(db), p, demo, angle=angle):
        db.add(EmailDraft(prospect_id=p.id, step=e["step"], send_after_days=e["days"], subject=e["subject"], body=e["body"]))
    db.commit()
    flash(request, "Draft sequence ready — review before sending.")
    return RedirectResponse(f"/prospects/{pid}", status_code=303)


@router.post("/prospects/{pid}/emails/{eid}/save")
def email_save(pid: int, eid: int, subject: str = Form(...), body: str = Form(...), db: Session = Depends(get_db)):
    d = db.get(EmailDraft, eid)
    if not d or d.prospect_id != pid:
        raise HTTPException(404)
    d.subject, d.body = subject, body
    db.commit()
    return RedirectResponse(f"/prospects/{pid}", status_code=303)


@router.post("/prospects/{pid}/emails/{eid}/send")
def email_send(request: Request, pid: int, eid: int, db: Session = Depends(get_db)):
    from ..services import outreach

    p = _prospect(db, pid)
    d = db.get(EmailDraft, eid)
    if not d or d.prospect_id != pid or d.status != "draft":
        raise HTTPException(404)
    try:
        msg = outreach.send_draft(db, agency_settings(db), d)
        db.commit()
        flash(request, msg)
    except outreach.Blocked as e:
        flash(request, str(e), "error")
    except Exception as e:
        db.rollback()
        flash(request, f"Send failed: {e}", "error")
    return RedirectResponse(f"/prospects/{p.id}", status_code=303)


def _make_proposal(db: Session, *, prospect: Prospect | None, client: Client | None, form: dict) -> Proposal:
    a = agency_settings(db)
    name = client.name if client else prospect.name
    niche = (client.industry if client else prospect.niche) or ""
    monthly = round(float(form.get("monthly_price") or (client.monthly_price_cents / 100 if client else a.default_monthly_price_cents / 100)) * 100)
    setup = round(float(form.get("setup_fee") or 0) * 100)
    probe = Client(name=name, pricing_model="flat", monthly_price_cents=monthly,
                   included_minutes=int(form.get("included_minutes") or a.default_included_minutes),
                   overage_cents=a.default_overage_cents)
    if client:
        probe = client
    inputs = {k: form.get(k) for k in ("missed_calls_per_week", "avg_job_value", "close_rate")}
    notes = form.get("notes") or (("; ".join(prospect.score_reasons or [])) if prospect else (client.notes or ""))
    content = writer.proposal(a, name, niche, notes, inputs, pricing.describe(probe), monthly, setup)
    prop = Proposal(prospect_id=prospect.id if prospect else None, client_id=client.id if client else None,
                    title=f"AI Receptionist for {name}", inputs=inputs, content=content)
    db.add(prop)
    db.commit()
    return prop


@router.post("/prospects/{pid}/proposal")
async def prospect_proposal(request: Request, pid: int, db: Session = Depends(get_db)):
    prop = _make_proposal(db, prospect=_prospect(db, pid), client=None, form=dict(await request.form()))
    return RedirectResponse(f"/proposals/{prop.id}", status_code=303)


@router.post("/clients/{client_id}/proposal")
async def client_proposal(request: Request, client_id: int, db: Session = Depends(get_db)):
    c = db.get(Client, client_id)
    if not c:
        raise HTTPException(404)
    prop = _make_proposal(db, prospect=None, client=c, form=dict(await request.form()))
    return RedirectResponse(f"/proposals/{prop.id}", status_code=303)


@router.get("/proposals/{prop_id}", response_class=HTMLResponse)
def proposal_view(request: Request, prop_id: int, db: Session = Depends(get_db)):
    prop = db.get(Proposal, prop_id)
    if not prop:
        raise HTTPException(404)
    share = f"{get_settings().base_url}/p/{prop.token}"
    return render(request, "proposal.html", prop=prop, share=share, agency=agency_settings(db), internal=True)


@router.post("/prospects/{pid}/site")
def prospect_site(request: Request, pid: int, db: Session = Depends(get_db)):
    site = sitegen.for_prospect(db, _prospect(db, pid))
    db.commit()
    flash(request, "Demo site generated.")
    return RedirectResponse(f"/sites/{site.id}", status_code=303)


@router.post("/clients/{client_id}/site")
def client_site(request: Request, client_id: int, db: Session = Depends(get_db)):
    c = db.get(Client, client_id)
    if not c:
        raise HTTPException(404)
    site = sitegen.for_client(db, c)
    db.commit()
    flash(request, "Website generated.")
    return RedirectResponse(f"/sites/{site.id}", status_code=303)


@router.get("/sites/{site_id}", response_class=HTMLResponse)
def site_edit(request: Request, site_id: int, db: Session = Depends(get_db)):
    site = db.get(Site, site_id)
    if not site:
        raise HTTPException(404)
    return render(request, "site_edit.html", site=site, url=f"{get_settings().base_url}/s/{site.slug}")


@router.post("/sites/{site_id}")
def site_save(request: Request, site_id: int, headline: str = Form(""), subheadline: str = Form(""),
              about: str = Form(""), services: str = Form(""), why: str = Form(""), reviews: str = Form(""),
              phone: str = Form(""), booking_link: str = Form(""), theme_color: str = Form("#2563eb"),
              domain: str = Form(""), published: str = Form(""), db: Session = Depends(get_db)):
    site = db.get(Site, site_id)
    if not site:
        raise HTTPException(404)
    c = dict(site.content or {})
    c.update(headline=headline, subheadline=subheadline, about=about, phone=phone or None, booking_link=booking_link or None)
    c["services"] = [{"name": ln.split("|")[0].strip(), "description": (ln.split("|", 1)[1].strip() if "|" in ln else "")}
                     for ln in services.splitlines() if ln.strip()]
    c["why"] = [ln.strip() for ln in why.splitlines() if ln.strip()]
    # Real reviews only — paste them from Google with the reviewer's first name.
    c["reviews"] = [{"author": ln.split("|")[0].strip(), "text": ln.split("|", 1)[1].strip()}
                    for ln in reviews.splitlines() if "|" in ln]
    site.content, site.theme_color = c, theme_color[:20]
    site.domain = domain.strip().lower() or None
    site.published = bool(published)
    db.commit()
    flash(request, "Site saved.")
    return RedirectResponse(f"/sites/{site_id}", status_code=303)


@router.get("/tools", response_class=HTMLResponse)
def tools_home(request: Request, t: str = ""):
    return render(request, "tools.html", tools=writer.TOOLS, result=None,
                  active=t if t in writer.TOOLS else None, values={})


@router.post("/tools/{name}", response_class=HTMLResponse)
async def tools_run(request: Request, name: str):
    if name not in writer.TOOLS:
        raise HTTPException(404)
    values = {k: v for k, v in (await request.form()).items()}
    try:
        result = writer.run_tool(name, values)
    except Exception as e:
        result = f"Error: {e}"
    return render(request, "tools.html", tools=writer.TOOLS, result=result, active=name, values=values)
