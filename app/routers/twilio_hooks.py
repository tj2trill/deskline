"""Twilio webhooks for recovery numbers: calls (missed-call detection) and texts (the conversation).

Both answer Twilio immediately and do the slow part (sending texts, LLM extraction) after the response,
because Twilio gives a webhook 15 seconds and an LLM call alone can take longer than that.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db, session_scope
from ..models import Client, RecoveryCall
from ..services import eventlog, messaging, recovery, twilio

router = APIRouter(prefix="/webhooks/twilio")
log = logging.getLogger("deskline.twilio")


def xml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


async def _form(request: Request) -> dict | None:
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    pq = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if not twilio.valid_request(pq, form, request.headers.get("x-twilio-signature")):
        return None
    return form


def _forbidden() -> Response:
    return Response(status_code=403, content="invalid signature")


def _text_back(client_id: int, caller: str | None, call_sid: str) -> None:
    with session_scope() as db:
        client = db.get(Client, client_id)
        if client:
            recovery.start_missed_call(db, client, caller, origin="missed", call_sid=call_sid)


def _missed_twiml(client: Client) -> str:
    return twilio.twiml(twilio.say(f"Sorry we missed your call at {client.name}. We're sending you a text right now "
                                   "so we can help you faster. Talk soon."), twilio.hangup())


@router.post("/voice")
async def voice(request: Request, bg: BackgroundTasks, db: Session = Depends(get_db)):
    form = await _form(request)
    if form is None:
        return _forbidden()
    client = recovery.client_for_number(db, form.get("To"))
    call_sid = form.get("CallSid") or ""
    if not client:
        return xml(twilio.twiml(twilio.say("Sorry, this number isn't in service."), twilio.hangup()))
    owner_line = messaging.normalize_phone(client.transfer_number or client.owner_phone)
    if not client.recovery_enabled or client.status in ("paused", "churned"):
        # Never a dead line: if recovery is off, a ring-mode number still reaches the business.
        if owner_line and client.missed_call_mode == "ring":
            return xml(twilio.twiml(f"<Dial>{owner_line}</Dial>"))
        return xml(twilio.twiml(twilio.say(f"Sorry, {client.name} can't take your call right now."), twilio.hangup()))
    rc = db.scalars(select(RecoveryCall).where(RecoveryCall.call_sid == call_sid)).first()
    if not rc:
        rc = RecoveryCall(client_id=client.id, call_sid=call_sid, from_number=form.get("From"), to_number=form.get("To"),
                          mode=client.missed_call_mode)
        db.add(rc)
    if client.missed_call_mode == "ring" and owner_line:
        db.commit()
        caller = messaging.normalize_phone(form.get("From"))
        return xml(twilio.twiml(twilio.dial_screened(
            owner_line, timeout=client.ring_seconds or 20,
            action=twilio.public_url(f"/webhooks/twilio/dial-status?p={call_sid}"),
            screen_url=twilio.public_url(f"/webhooks/twilio/screen?p={call_sid}"), caller_id=caller)))
    # forward mode: the carrier only sends us calls the owner didn't pick up
    rc.answered, rc.dial_status = False, "forwarded"
    db.commit()
    bg.add_task(_text_back, client.id, form.get("From"), call_sid)
    return xml(_missed_twiml(client))


@router.post("/screen")
async def screen(request: Request, db: Session = Depends(get_db)):
    """Played to the owner when they pick up: a person presses 1, voicemail can't."""
    if await _form(request) is None:
        return _forbidden()
    rc = db.scalars(select(RecoveryCall).where(RecoveryCall.call_sid == request.query_params.get("p", ""))).first()
    name = db.get(Client, rc.client_id).name if rc else "your business"
    action = twilio.public_url(f"/webhooks/twilio/screen-result?p={request.query_params.get('p', '')}")
    return xml(twilio.twiml(twilio.gather_digit(action, f"Call for {name}. Press 1 to answer."), twilio.hangup()))


@router.post("/screen-result")
async def screen_result(request: Request, db: Session = Depends(get_db)):
    form = await _form(request)
    if form is None:
        return _forbidden()
    rc = db.scalars(select(RecoveryCall).where(RecoveryCall.call_sid == request.query_params.get("p", ""))).first()
    if rc and form.get("Digits") == "1":
        rc.screened_ok = True
        db.commit()
        return xml(twilio.twiml())  # empty response: the call bridges
    return xml(twilio.twiml(twilio.hangup()))


@router.post("/dial-status")
async def dial_status(request: Request, bg: BackgroundTasks, db: Session = Depends(get_db)):
    form = await _form(request)
    if form is None:
        return _forbidden()
    sid = request.query_params.get("p") or form.get("CallSid") or ""
    rc = db.scalars(select(RecoveryCall).where(RecoveryCall.call_sid == sid)).first()
    if not rc:
        return xml(twilio.twiml(twilio.hangup()))
    client = db.get(Client, rc.client_id)
    rc.dial_status = form.get("DialCallStatus")
    # Answered means a person pressed 1, not Twilio's "completed" (which a voicemail pickup also produces).
    rc.answered = bool(rc.screened_ok)
    db.commit()
    if rc.answered:
        return xml(twilio.twiml(twilio.hangup()))
    bg.add_task(_text_back, client.id, rc.from_number, rc.call_sid)
    return xml(_missed_twiml(client))


def _inbound(client_id: int, frm: str, body: str, media: list[str], sid: str | None) -> None:
    with session_scope() as db:
        client = db.get(Client, client_id)
        if client:
            recovery.handle_inbound(db, client, frm, body, media, sid)


@router.post("/sms")
async def sms(request: Request, bg: BackgroundTasks, db: Session = Depends(get_db)):
    form = await _form(request)
    if form is None:
        return _forbidden()
    client = recovery.client_for_number(db, form.get("To"))
    if not client:
        eventlog.record(db, "twilio", f"Text to unknown number {form.get('To')}", level="warn")
        db.commit()
        return xml(twilio.twiml())
    try:
        n = int(form.get("NumMedia") or 0)
    except ValueError:
        n = 0
    media = [form[f"MediaUrl{i}"] for i in range(min(n, 10)) if form.get(f"MediaUrl{i}")]
    bg.add_task(_inbound, client.id, form.get("From") or "", form.get("Body") or "", media, form.get("MessageSid"))
    return xml(twilio.twiml())
