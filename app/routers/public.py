"""Unauthenticated surfaces: chat widget, hosted sites, shared proposals, unsubscribe."""
from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from itsdangerous import BadSignature
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import ChatSession, Client, Proposal, Site, Suppression
from ..services import chat, outreach
from ..templating import render

router = APIRouter()
_RATE: dict[str, deque] = defaultdict(deque)


def _rate_ok(key: str, limit: int = 20, window: float = 60.0) -> bool:
    now = time.monotonic()
    q = _RATE[key]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def _cors(client: Client, request: Request) -> dict:
    origin = request.headers.get("origin")
    if not chat.origin_allowed(client, origin):
        return {}
    return {"Access-Control-Allow-Origin": origin or "*", "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type", "Vary": "Origin"}


def _widget_client(db: Session, key: str) -> Client:
    c = db.scalars(select(Client).where(Client.widget_key == key)).first()
    if not c or not c.widget_enabled or c.status in ("paused", "churned"):
        raise HTTPException(404)
    return c


@router.options("/api/chat/{key}")
def chat_preflight(key: str, request: Request, db: Session = Depends(get_db)):
    return Response(status_code=204, headers=_cors(_widget_client(db, key), request))


@router.post("/api/chat/{key}")
async def chat_message(key: str, request: Request, db: Session = Depends(get_db)):
    client = _widget_client(db, key)
    headers = _cors(client, request)
    if not headers:
        return JSONResponse({"error": "origin not allowed"}, status_code=403)
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    if not _rate_ok(f"{key}:{ip}"):
        return JSONResponse({"reply": "You're sending messages quickly — give me a moment."}, status_code=429, headers=headers)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "bad json"}, status_code=400, headers=headers)
    visitor = str(body.get("visitor_id") or "anon")[:64]
    text = str(body.get("message") or "").strip()[:2000]
    if not text:
        return JSONResponse({"error": "empty"}, status_code=400, headers=headers)
    session = db.scalars(select(ChatSession).where(ChatSession.client_id == client.id,
                                                   ChatSession.visitor_id == visitor)).first()
    if not session:
        session = ChatSession(client_id=client.id, visitor_id=visitor, page_url=str(body.get("page_url") or "")[:500])
        db.add(session)
        db.flush()
    try:
        answer = chat.reply(db, client, session, text)
    except Exception:
        db.rollback()
        phone = (client.numbers[0].number if client.numbers else None) or client.business_phone
        answer = "Sorry, I'm having trouble right now." + (f" Please call {phone}." if phone else "")
        return JSONResponse({"reply": answer}, headers=headers)
    session.updated_at = datetime.now(timezone.utc)
    db.commit()
    return JSONResponse({"reply": answer}, headers=headers)


@router.get("/widget/{key}.js")
def widget_js(key: str, db: Session = Depends(get_db)):
    client = _widget_client(db, key)
    from ..models import AgencySettings

    agency = db.scalars(select(AgencySettings)).first()
    color = agency.primary_color if agency else "#2563eb"
    cfg = {"api": f"{get_settings().base_url}/api/chat/{key}", "name": client.name, "color": color,
           "greeting": f"Hi! Questions about {client.name}? I can help or book you in."}
    js = WIDGET_JS.replace("__CONFIG__", json.dumps(cfg))
    return Response(js, media_type="application/javascript", headers={"Cache-Control": "public, max-age=300"})


@router.get("/s/{slug}", response_class=HTMLResponse)
def public_site(request: Request, slug: str, db: Session = Depends(get_db)):
    site = db.scalars(select(Site).where(Site.slug == slug)).first()
    if not site or not site.published:
        raise HTTPException(404)
    client = db.get(Client, site.client_id) if site.client_id else None
    widget = f"{get_settings().base_url}/widget/{client.widget_key}.js" if client and client.widget_enabled else None
    return render(request, "site_public.html", site=site, c=site.content or {}, widget=widget)


@router.get("/p/{token}", response_class=HTMLResponse)
def public_proposal(request: Request, token: str, db: Session = Depends(get_db)):
    prop = db.scalars(select(Proposal).where(Proposal.token == token)).first()
    if not prop:
        raise HTTPException(404)
    if not prop.viewed_at:
        prop.viewed_at = datetime.now(timezone.utc)
        db.commit()
    from ..models import AgencySettings

    return render(request, "proposal.html", prop=prop, agency=db.scalars(select(AgencySettings)).first(),
                  internal=False, share=None)


@router.get("/u/{token}", response_class=HTMLResponse)
def unsubscribe_page(request: Request, token: str):
    try:
        email = outreach.read_unsubscribe_token(token)
    except BadSignature:
        raise HTTPException(404)
    return render(request, "unsubscribe.html", email=email, token=token, done=False)


@router.post("/u/{token}", response_class=HTMLResponse)
def unsubscribe(request: Request, token: str, db: Session = Depends(get_db)):
    try:
        email = outreach.read_unsubscribe_token(token)
    except BadSignature:
        raise HTTPException(404)
    if not db.scalars(select(Suppression).where(Suppression.email == email)).first():
        db.add(Suppression(email=email))
        db.commit()
    return render(request, "unsubscribe.html", email=email, token=token, done=True)


@router.get("/billing/thanks", response_class=HTMLResponse)
def billing_thanks(request: Request, c: str = "", cancelled: str = ""):
    return render(request, "thanks.html", cancelled=bool(cancelled))


WIDGET_JS = r"""(function(){
var C=__CONFIG__;if(window.__desklineLoaded)return;window.__desklineLoaded=1;
var vid;try{vid=localStorage.getItem('dl_vid');if(!vid){vid=Math.random().toString(36).slice(2)+Date.now().toString(36);localStorage.setItem('dl_vid',vid);}}catch(e){vid='v'+Math.random().toString(36).slice(2);}
var css='.dl-btn{position:fixed;right:20px;bottom:20px;width:56px;height:56px;border-radius:50%;border:0;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.25);z-index:2147483000;color:#fff;font-size:24px}'+
'.dl-panel{position:fixed;right:20px;bottom:88px;width:340px;max-width:calc(100vw - 32px);height:460px;max-height:calc(100vh - 120px);background:#fff;border-radius:14px;box-shadow:0 10px 40px rgba(0,0,0,.25);display:none;flex-direction:column;overflow:hidden;z-index:2147483000;font:14px/1.4 system-ui,sans-serif;color:#111}'+
'.dl-head{padding:12px 14px;color:#fff;font-weight:600}.dl-msgs{flex:1;overflow:auto;padding:12px;background:#f7f7f8}'+
'.dl-m{margin:6px 0;padding:8px 11px;border-radius:12px;max-width:85%;white-space:pre-wrap;word-wrap:break-word}.dl-u{background:#e7e9ee;margin-left:auto}.dl-a{background:#fff;border:1px solid #e5e5e5}'+
'.dl-form{display:flex;border-top:1px solid #eee}.dl-form input{flex:1;border:0;padding:12px;font:inherit;outline:none}.dl-form button{border:0;background:none;padding:0 14px;font-weight:600;cursor:pointer}';
var st=document.createElement('style');st.textContent=css;document.head.appendChild(st);
var btn=document.createElement('button');btn.className='dl-btn';btn.style.background=C.color;btn.setAttribute('aria-label','Chat with '+C.name);btn.textContent='✉';
var p=document.createElement('div');p.className='dl-panel';p.setAttribute('role','dialog');
p.innerHTML='<div class="dl-head"></div><div class="dl-msgs" aria-live="polite"></div><form class="dl-form"><input aria-label="Message" placeholder="Type a message…" maxlength="2000"><button type="submit">Send</button></form>';
p.querySelector('.dl-head').style.background=C.color;p.querySelector('.dl-head').textContent=C.name;
var msgs=p.querySelector('.dl-msgs'),form=p.querySelector('form'),input=p.querySelector('input'),busy=false;
function add(t,who){var d=document.createElement('div');d.className='dl-m '+(who==='u'?'dl-u':'dl-a');d.textContent=t;msgs.appendChild(d);msgs.scrollTop=msgs.scrollHeight;return d;}
add(C.greeting,'a');
btn.onclick=function(){var open=p.style.display==='flex';p.style.display=open?'none':'flex';if(!open)input.focus();};
form.onsubmit=function(e){e.preventDefault();var t=input.value.trim();if(!t||busy)return;input.value='';add(t,'u');busy=true;var w=add('…','a');
fetch(C.api,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({visitor_id:vid,message:t,page_url:location.href})})
.then(function(r){return r.json();}).then(function(j){w.textContent=j.reply||'Sorry, something went wrong.';})
.catch(function(){w.textContent='Connection problem — please try again.';}).finally(function(){busy=false;});};
document.body.appendChild(btn);document.body.appendChild(p);
})();"""
