from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..security import valid_webhook_token
from ..services import billing, calls, eventlog, replies, vapi

router = APIRouter(prefix="/webhooks")
log = logging.getLogger("deskline.webhooks")


@router.post("/vapi")
async def vapi_webhook(request: Request, db: Session = Depends(get_db)):
    if not valid_webhook_token(request.headers.get("x-deskline-token")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    msg = body.get("message") or body
    mtype = msg.get("type")
    try:
        if mtype == "assistant-request":
            out = calls.route_inbound(db, msg)
            db.commit()
            return out
        if mtype == "tool-calls":
            return _tool_calls(db, msg)
        if mtype == "end-of-call-report":
            calls.process_end_of_call(db, msg)
            db.commit()
            return {}
        if mtype == "status-update":
            calls.upsert_call(db, vapi.call_facts(msg))
            db.commit()
            return {}
        return {}
    except Exception as e:
        db.rollback()
        log.exception("vapi webhook %s failed", mtype)
        eventlog.record(db, "vapi", f"{mtype} handler crashed: {e}", level="error")
        db.commit()
        if mtype == "tool-calls":  # the caller is waiting: let the agent recover gracefully
            return {"results": [{"toolCallId": t["id"], "name": t["name"],
                                 "error": "System error. Take a message instead."} for t in vapi.tool_calls(msg)]}
        if mtype == "assistant-request":
            return {"error": "Sorry, we're having trouble right now. Please call back in a few minutes."}
        return JSONResponse({"error": "internal"}, status_code=500)


def _tool_calls(db: Session, msg: dict) -> dict:
    facts = vapi.call_facts(msg)
    client, _ = calls.resolve_client(db, facts)
    results = []
    for tc in vapi.tool_calls(msg):
        if not client:
            results.append({"toolCallId": tc["id"], "name": tc["name"], "error": "Unknown business."})
            continue
        ok, text = calls.run_tool(db, client, tc["name"], tc["arguments"], vapi_call_id=facts.get("vapi_call_id"),
                                  caller_number=facts.get("from_number"))
        results.append({"toolCallId": tc["id"], "name": tc["name"], ("result" if ok else "error"): text})
    if facts.get("vapi_call_id"):
        calls.upsert_call(db, facts)
    db.commit()
    return {"results": results}


@router.post("/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    try:
        event = billing.verify_signature(payload, request.headers.get("stripe-signature", ""),
                                         get_settings().stripe_webhook_secret)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    result = billing.handle_event(db, event)
    db.commit()
    return {"received": True, "result": result}


@router.post("/inbound-email/{token}")
async def inbound_email(token: str, request: Request, db: Session = Depends(get_db)):
    """Replies to outreach, forwarded by your inbound-mail provider. The token is in Settings."""
    if not valid_webhook_token(token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    ctype = request.headers.get("content-type", "")
    if "json" in ctype:
        data = await request.json()
    else:
        form = await request.form()
        data = {k: v for k, v in form.items() if isinstance(v, str)}
    fields = replies.parse_provider_payload(data if isinstance(data, dict) else {})
    if not fields["from_email"]:
        return JSONResponse({"error": "no sender"}, status_code=400)
    rec = replies.handle(db, **fields)
    db.commit()
    return {"ok": True, "intent": rec.intent, "confidence": rec.confidence, "prospect_id": rec.prospect_id, "applied": rec.applied}
