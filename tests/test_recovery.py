"""Revenue recovery: missed-call text-back, quote follow-up, invoice reminders, attribution."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models import (Booking, Client, Invoice, PhoneNumber, Quote, RecoveryCall, RecoveryEvent, SmsMessage,
                        SmsOptOut, SmsThread)
from tests.conftest import vapi_headers
from tests.test_voice_engine import make_client

REC = "+15195550142"
CUSTOMER = "+15195550177"
TZ = ZoneInfo("America/Toronto")


def sign(path: str, data: dict) -> dict:
    from app.services import twilio

    return {"X-Twilio-Signature": twilio.signature("https://desk.example.com" + path, data, "tok")}


def post_twilio(app_client, path, data):
    return app_client.post(path, data=data, headers=sign(path, data))


@pytest.fixture()
def no_llm(monkeypatch):
    from app import config

    monkeypatch.setenv("LLM_API_KEY", "")
    config.reset_settings()


def setup(admin, db, mode="forward", **fields):
    cid = make_client(admin)
    c = db.get(Client, cid)
    c.recovery_enabled, c.missed_call_mode, c.status = True, mode, "active"
    c.owner_name, c.owner_phone, c.notify_sms = "Dave Smith", "+15195550199", False
    c.business_hours = {d: [["09:00", "17:00"]] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
    c.booking_lead_hours, c.avg_job_value_cents = 0, 60000
    for k, v in fields.items():
        setattr(c, k, v)
    db.add(PhoneNumber(client_id=cid, number=REC, provider="twilio", purpose="recovery", twilio_sid="PN1"))
    db.commit()
    return cid


def texts_to(net, number):
    return [net.form(r)["Body"] for r in net.of("api.twilio.com", "Messages.json") if net.form(r).get("To") == number]


def reply(app_client, body, sid=None, media=None):
    data = {"From": CUSTOMER, "To": REC, "Body": body, "MessageSid": sid or f"SMin{hash(body) & 0xffffff}",
            "NumMedia": str(len(media or []))}
    for i, u in enumerate(media or []):
        data[f"MediaUrl{i}"] = u
    r = post_twilio(app_client, "/webhooks/twilio/sms", data)
    assert r.status_code == 200, r.text
    return r


def missed_call(app_client, sid="CA1", frm=CUSTOMER):
    return post_twilio(app_client, "/webhooks/twilio/voice", {"CallSid": sid, "From": frm, "To": REC, "CallStatus": "ringing"})


# ---------------------------------------------------------------- the wedge: missed call → booked job

def test_unsigned_twilio_webhooks_are_rejected(app_client):
    r = app_client.post("/webhooks/twilio/voice", data={"CallSid": "x", "To": REC},
                        headers={"X-Twilio-Signature": "forged"})
    assert r.status_code == 403
    assert app_client.post("/webhooks/twilio/sms", data={"From": CUSTOMER, "To": REC}).status_code == 403


def test_missed_call_to_booked_job_without_llm(admin, app_client, db, net, no_llm):
    cid = setup(admin, db)
    r = missed_call(app_client)
    assert r.status_code == 200 and "<Hangup/>" in r.text and "sending you a text" in r.text
    opener = texts_to(net, CUSTOMER)
    assert len(opener) == 1 and "Sorry we missed your call" in opener[0] and "STOP" in opener[0]
    assert net.form(net.of("api.twilio.com", "Messages.json")[-1])["From"] == REC  # replies route back here

    reply(app_client, "My panel is buzzing and the lights flicker")
    assert "address" in texts_to(net, CUSTOMER)[-1]
    reply(app_client, "12 Oak St, London")
    assert "How soon" in texts_to(net, CUSTOMER)[-1]
    reply(app_client, "2")
    assert "photo" in texts_to(net, CUSTOMER)[-1]
    reply(app_client, "", media=["https://api.twilio.com/media/ME1"])
    offer = texts_to(net, CUSTOMER)[-1]
    assert "1)" in offer and "3)" in offer and "Reply 1, 2 or 3" in offer
    reply(app_client, "1")
    assert texts_to(net, CUSTOMER)[-1].startswith("You're booked for")

    db.expire_all()
    t = db.scalars(select(SmsThread).where(SmsThread.client_id == cid)).one()
    assert t.status == "booked" and t.data["service"].startswith("My panel") and t.data["address"] == "12 Oak St, London"
    assert t.data["urgency"] == "soon" and t.data["photos"] == ["https://api.twilio.com/media/ME1"]
    b = db.get(Booking, t.booking_id)
    assert b.source == "sms" and b.phone == CUSTOMER and "12 Oak St" in b.notes
    ev = db.scalars(select(RecoveryEvent)).one()
    assert ev.kind == "missed_call_booked" and ev.amount_cents == 60000 and not ev.confirmed and not ev.fee_eligible
    from app.services import messaging, recovery

    assert any("Missed call recovered" in m.get("subject", "") for m in messaging.OUTBOX)
    c = db.get(Client, cid)
    s = recovery.stats(db, c)
    assert (s["missed_calls"], s["texted"], s["engaged"], s["booked"]) == (1, 1, 1, 1)
    assert s["recovered_confirmed"] == 0 and s["recovered_estimated"] == 60000
    # owner confirms the real job value → confirmed + fee-eligible
    c.performance_fee_pct = 5
    recovery.confirm_job_value(db, c, ev.id, 185000)
    db.commit()
    s = recovery.stats(db, c)
    assert s["recovered_confirmed"] == 185000 and s["fee_cents"] == 9250


def test_llm_extracts_several_fields_from_one_text(admin, app_client, db, net):
    import json

    cid = setup(admin, db, ask_photos=False)
    missed_call(app_client)
    net.llm_queue.append({"content": json.dumps({"service": "EV charger install", "address": "88 Elm St, London",
                                                 "name": "Priya", "urgency": "flexible"})})
    reply(app_client, "Hi it's Priya, want an EV charger put in at 88 Elm St, no rush")
    assert "estimate:" in texts_to(net, CUSTOMER)[-1]  # skipped straight to offering times
    db.expire_all()
    t = db.scalars(select(SmsThread).where(SmsThread.client_id == cid)).one()
    assert t.status == "qualified" and t.data["name"] == "Priya" and t.data["address"] == "88 Elm St, London"


def test_redial_gets_one_conversation_not_two_openers(admin, app_client, db, net, no_llm):
    cid = setup(admin, db)
    missed_call(app_client, "CA1")
    missed_call(app_client, "CA2")
    missed_call(app_client, "CA3")
    assert len(texts_to(net, CUSTOMER)) == 1
    db.expire_all()
    t = db.scalars(select(SmsThread).where(SmsThread.client_id == cid)).one()
    assert t.data["calls"] == 3
    assert {rc.thread_id for rc in db.scalars(select(RecoveryCall))} == {t.id}


def test_hidden_caller_id_is_logged_not_texted(admin, app_client, db, net, no_llm):
    setup(admin, db)
    missed_call(app_client, frm="anonymous")
    assert not net.of("api.twilio.com", "Messages.json")
    db.expire_all()
    assert db.scalars(select(RecoveryCall)).one().answered is False


def test_ring_mode_voicemail_pickup_still_counts_as_missed(admin, app_client, db, net, no_llm):
    setup(admin, db, mode="ring", ring_seconds=18)
    r = missed_call(app_client, "CAring1")
    assert "<Dial" in r.text and 'timeout="18"' in r.text and "+15195550199" in r.text
    assert "/webhooks/twilio/screen?p=CAring1" in r.text and f'callerId="{CUSTOMER}"' in r.text
    # Owner picks up and presses 1: answered, no text
    s = post_twilio(app_client, "/webhooks/twilio/screen?p=CAring1", {"CallSid": "CAchild"})
    assert "Press 1" in s.text
    post_twilio(app_client, "/webhooks/twilio/screen-result?p=CAring1", {"CallSid": "CAchild", "Digits": "1"})
    d = post_twilio(app_client, "/webhooks/twilio/dial-status?p=CAring1", {"CallSid": "CAring1", "DialCallStatus": "completed"})
    assert "<Hangup/>" in d.text and not texts_to(net, CUSTOMER)
    # Voicemail answers (Twilio says "completed") but nobody pressed 1 → missed → text
    missed_call(app_client, "CAring2")
    d = post_twilio(app_client, "/webhooks/twilio/dial-status?p=CAring2", {"CallSid": "CAring2", "DialCallStatus": "completed"})
    assert "sending you a text" in d.text and len(texts_to(net, CUSTOMER)) == 1
    db.expire_all()
    calls = {rc.call_sid: rc.answered for rc in db.scalars(select(RecoveryCall))}
    assert calls == {"CAring1": True, "CAring2": False}


def test_stop_opts_out_everywhere(admin, app_client, db, net, no_llm):
    from app.services import recovery, sequences

    cid = setup(admin, db)
    missed_call(app_client)
    reply(app_client, "STOP")
    db.expire_all()
    c = db.get(Client, cid)
    assert db.scalars(select(SmsOptOut)).one().phone == CUSTOMER
    assert db.scalars(select(SmsThread)).one().status == "opted_out"
    n = len(net.of("api.twilio.com", "Messages.json"))
    missed_call(app_client, "CA9")  # new day's call
    q = sequences.create_quote(db, c, amount_cents=100000, phone=CUSTOMER)
    sequences.run_quotes(db, c, q.next_touch_at + timedelta(minutes=1))
    db.commit()
    assert len(net.of("api.twilio.com", "Messages.json")) == n  # nothing else sent
    reply(app_client, "START", sid="SMstart")
    db.expire_all()
    assert not db.scalars(select(SmsOptOut)).all()
    assert recovery.opted_out(db, db.get(Client, cid), CUSTOMER) is False


def test_twilio_21610_marks_opt_out(admin, app_client, db, net, no_llm):
    cid = setup(admin, db)
    net.opted_out.add(CUSTOMER)
    missed_call(app_client)
    db.expire_all()
    assert db.scalars(select(SmsOptOut)).one().client_id == cid
    assert db.scalars(select(SmsMessage)).one().status == "failed"


def test_emergency_goes_straight_to_the_owner(admin, app_client, db, net, no_llm):
    setup(admin, db)
    missed_call(app_client)
    reply(app_client, "Water heater burst, basement is flooding")
    last = texts_to(net, CUSTOMER)[-1]
    assert "urgent" in last and "Dave" in last
    owner = texts_to(net, "+15195550199")
    assert owner and owner[0].startswith("URGENT") and CUSTOMER in owner[0]
    db.expire_all()
    assert db.scalars(select(SmsThread)).one().status == "emergency"


def test_emergency_detection_ignores_negation_and_detectors():
    from app.services.textback import is_emergency

    assert is_emergency("sparks coming out of the outlet")
    assert is_emergency("I smell gas in the kitchen")
    assert not is_emergency("not an emergency, just need a quote")
    assert not is_emergency("need smoke detectors installed")
    assert not is_emergency("fireplace insert quote")


def test_gas_smell_gets_safety_instruction(admin, app_client, db, net, no_llm):
    setup(admin, db)
    missed_call(app_client)
    reply(app_client, "I smell gas near the furnace")
    assert "leave the building" in texts_to(net, CUSTOMER)[-1]


def test_inbound_webhook_retry_is_processed_once(admin, app_client, db, net, no_llm):
    setup(admin, db)
    missed_call(app_client)
    reply(app_client, "Need a new outlet in the garage", sid="SMdup")
    reply(app_client, "Need a new outlet in the garage", sid="SMdup")
    assert len(texts_to(net, CUSTOMER)) == 2  # opener + one answer
    db.expire_all()
    assert len(db.scalars(select(SmsMessage).where(SmsMessage.direction == "in")).all()) == 1


def test_owner_reply_takes_over_and_silences_automation(admin, app_client, db, net, no_llm):
    cid = setup(admin, db)
    missed_call(app_client)
    reply(app_client, "Hot tub wiring")
    t = db.scalars(select(SmsThread)).one()
    r = admin.post(f"/portal/recovery/threads/{t.id}/reply?client_id={cid}", data={"body": "Hi it's Dave, calling you now"},
                   follow_redirects=False)
    assert r.status_code == 303 and texts_to(net, CUSTOMER)[-1] == "Hi it's Dave, calling you now"
    n = len(texts_to(net, CUSTOMER))
    reply(app_client, "ok great")
    assert len(texts_to(net, CUSTOMER)) == n  # no bot reply after a human took over
    db.expire_all()
    assert db.get(SmsThread, t.id).status == "handed_off"


def test_hung_up_on_receptionist_triggers_textback(admin, app_client, db, net, no_llm):
    cid = setup(admin, db)
    db.add(PhoneNumber(client_id=cid, number="+15125550111", provider="vapi", vapi_id="pn_x"))
    db.commit()
    c = db.get(Client, cid)
    msg = {"message": {"type": "end-of-call-report", "call": {"id": "vc1", "assistantId": c.agent.vapi_assistant_id,
           "customer": {"number": CUSTOMER}, "type": "inboundPhoneCall"}, "durationSeconds": 4, "endedReason": "customer-ended-call"}}
    assert app_client.post("/webhooks/vapi", json=msg, headers=vapi_headers()).status_code == 200
    assert "cut off" in texts_to(net, CUSTOMER)[-1]


def test_nudge_then_give_up_on_silent_threads(admin, app_client, db, net, no_llm):
    from app.services import textback

    cid = setup(admin, db)
    missed_call(app_client)
    c = db.get(Client, cid)
    t = db.scalars(select(SmsThread)).one()
    noon = datetime.now(TZ).replace(hour=12, minute=0) + timedelta(days=1)
    t.created_at = t.last_outbound_at = (noon - timedelta(hours=3)).astimezone(timezone.utc)
    db.commit()
    assert textback.nudge_due(db, c, noon.astimezone(timezone.utc)) == 1
    assert textback.nudge_due(db, c, noon.astimezone(timezone.utc)) == 0  # only once
    textback.nudge_due(db, c, (noon + timedelta(days=3)).astimezone(timezone.utc))
    assert t.status == "no_reply"


# ---------------------------------------------------------------- quotes

def _at(day_offset: int, hour: int, base: datetime) -> datetime:
    return (base.astimezone(TZ) + timedelta(days=day_offset)).replace(hour=hour, minute=0).astimezone(timezone.utc)


def test_quote_followups_respect_window_and_stop_on_yes(admin, app_client, db, net, no_llm):
    from app.services import recovery, sequences

    cid = setup(admin, db, default_deposit_cents=50000, deposit_link="https://pay.example.test/dep")
    c = db.get(Client, cid)
    q = sequences.create_quote(db, c, amount_cents=285000, phone=CUSTOMER, customer_name="Jordan Lee",
                               description="electrical panel replacement")
    db.commit()
    start = recovery.aware(q.sequence_start)
    assert recovery.aware(q.next_touch_at) >= start + timedelta(days=1)
    # 11pm on day 1: not sent, pushed to the morning window
    late = _at(1, 23, start)
    q.next_touch_at = late - timedelta(minutes=1)
    assert sequences.run_quotes(db, c, late) == 0
    assert q.next_touch_at.astimezone(TZ).hour == 8 and q.next_touch_at.astimezone(TZ).minute == 30
    morning = q.next_touch_at + timedelta(minutes=1)
    assert sequences.run_quotes(db, c, morning) == 1
    first = texts_to(net, CUSTOMER)[-1]
    assert "Hi Jordan" in first and "$2,850" in first and "STOP" in first
    assert q.followups_sent == 1 and recovery.aware(q.next_touch_at) > morning
    db.commit()
    reply(app_client, "Yes let's go ahead, can you start Tuesday")
    db.expire_all()
    q = db.get(Quote, q.id)
    assert q.status == "deposit_requested" and q.next_touch_at is None
    assert "$500 deposit" in texts_to(net, CUSTOMER)[-1] and "https://pay.example.test/dep" in texts_to(net, CUSTOMER)[-1]
    ev = db.scalars(select(RecoveryEvent)).one()
    assert (ev.kind, ev.amount_cents, ev.confirmed, ev.fee_eligible) == ("quote_recovered", 285000, True, True)
    from app.services import messaging

    assert any("$2,850 quote" in m.get("subject", "") and "accepted" in m.get("subject", "") for m in messaging.OUTBOX)
    # falls through → claim voided
    sequences.set_quote_status(db, db.get(Client, cid), q, "lost")
    db.commit()
    assert db.scalars(select(RecoveryEvent)).one().void


def test_quote_maybe_is_not_an_acceptance(admin, app_client, db, net, no_llm):
    from app.services import sequences

    cid = setup(admin, db)
    c = db.get(Client, cid)
    q = sequences.create_quote(db, c, amount_cents=150000, phone=CUSTOMER, description="deck lighting")
    sequences.run_quotes(db, c, recovery_noon(c, q.next_touch_at))
    db.commit()
    reply(app_client, "Ok thanks, I'll think about it and let you know")
    db.expire_all()
    q = db.get(Quote, q.id)
    assert q.status == "open" and q.next_touch_at is None and q.replied_at  # stopped, owner told, not "won"
    assert not db.scalars(select(RecoveryEvent)).all()


def recovery_noon(c, dt):
    return dt.astimezone(TZ).replace(hour=12).astimezone(timezone.utc) + timedelta(minutes=1)


def test_won_without_any_followup_is_not_claimed(admin, db, no_llm):
    from app.services import sequences

    cid = setup(admin, db)
    c = db.get(Client, cid)
    q = sequences.create_quote(db, c, amount_cents=90000, phone=CUSTOMER)
    sequences.set_quote_status(db, c, q, "accepted")
    db.commit()
    assert q.status == "accepted" and not db.scalars(select(RecoveryEvent)).all()


def test_full_quote_sequence_ends_in_no_response(admin, db, net, no_llm):
    from app.services import sequences

    cid = setup(admin, db)
    c = db.get(Client, cid)
    q = sequences.create_quote(db, c, amount_cents=90000, phone=CUSTOMER, description="pot lights")
    for _ in range(4):
        sequences.run_quotes(db, c, recovery_noon(c, q.next_touch_at))
    sent = texts_to(net, CUSTOMER)
    assert len(sent) == 4 and "last check-in" in sent[-1] and "availability for next week" in sent[2]
    sequences.run_quotes(db, c, q.next_touch_at + timedelta(minutes=1))
    assert q.status == "no_response"


def test_old_imported_quote_is_not_chased(admin, db, no_llm):
    from app.services import sequences

    c = db.get(Client, setup(admin, db))
    q = sequences.create_quote(db, c, amount_cents=90000, phone=CUSTOMER,
                               sent_at=datetime.now(timezone.utc) - timedelta(days=120))
    assert q.status == "stale" and q.next_touch_at is None


# ---------------------------------------------------------------- invoices

def test_invoice_reminders_and_recovery_attribution(admin, app_client, db, net, no_llm):
    from app.services import recovery, sequences

    cid = setup(admin, db, payment_instructions="e-Transfer to pay@sparky.ca")
    c = db.get(Client, cid)
    today = datetime.now(TZ).date()
    inv = sequences.create_invoice(db, c, amount_cents=480000, due=today + timedelta(days=3), phone=CUSTOMER,
                                   customer_name="Sam Patel", number="1043")
    noon = datetime.now(TZ).replace(hour=12, minute=0).astimezone(timezone.utc)
    assert sequences.run_invoices(db, c, noon) == 1
    assert "due" in texts_to(net, CUSTOMER)[-1] and "e-Transfer to pay@sparky.ca" in texts_to(net, CUSTOMER)[-1]
    assert sequences.run_invoices(db, c, noon) == 0  # same day: nothing new
    # skip ahead 10 days (7 days late): only the "7 days past due" reminder goes out, not 0, 3 and 7
    later = noon + timedelta(days=10)
    assert sequences.run_invoices(db, c, later) == 1
    assert "7 days past due" in texts_to(net, CUSTOMER)[-1]
    assert [r["offset"] for r in inv.reminders_sent if r["sent"]] == [-3, 7]
    sequences.mark_invoice_paid(db, c, inv, when=later + timedelta(hours=2))
    db.commit()
    ev = db.scalars(select(RecoveryEvent)).one()
    assert (ev.kind, ev.amount_cents, ev.fee_eligible) == ("invoice_recovered", 480000, True)
    assert recovery.stats(db, c)["invoices_recovered_cents"] == 480000


def test_invoice_paid_on_time_is_not_fee_eligible(admin, db, net, no_llm):
    from app.services import recovery, sequences

    c = db.get(Client, setup(admin, db, performance_fee_pct=5))
    today = datetime.now(TZ).date()
    inv = sequences.create_invoice(db, c, amount_cents=100000, due=today, phone=CUSTOMER)
    noon = datetime.now(TZ).replace(hour=12).astimezone(timezone.utc)
    sequences.run_invoices(db, c, noon)
    sequences.mark_invoice_paid(db, c, inv, when=noon + timedelta(hours=1))
    db.commit()
    ev = db.scalars(select(RecoveryEvent)).one()
    assert ev.kind == "invoice_on_time" and not ev.fee_eligible
    s = recovery.stats(db, c)
    assert s["fee_cents"] == 0 and s["recovered_confirmed"] == 0 and s["invoices_on_time_cents"] == 100000


def test_invoice_escalates_to_owner_and_stops_texting(admin, db, net, no_llm):
    from app.services import messaging, sequences

    c = db.get(Client, setup(admin, db))
    inv = sequences.create_invoice(db, c, amount_cents=250000, due=datetime.now(TZ).date() - timedelta(days=30),
                                   phone=CUSTOMER, number="88")
    sequences.run_invoices(db, c, datetime.now(TZ).replace(hour=12).astimezone(timezone.utc))
    assert inv.escalated_at and not texts_to(net, CUSTOMER)
    assert any("Invoice 88 is 30 days overdue" in m.get("subject", "") for m in messaging.OUTBOX)


@pytest.mark.parametrize("text,status,paused", [
    ("I sent the e-transfer this morning", "open", True),
    ("Haven't paid yet, will pay next friday", "open", True),
    ("This invoice is wrong, you charged for 2 visits", "disputed", True),
])
def test_invoice_replies(admin, app_client, db, net, no_llm, text, status, paused):
    from app.services import sequences

    c = db.get(Client, setup(admin, db))
    inv = sequences.create_invoice(db, c, amount_cents=100000, due=datetime.now(TZ).date() + timedelta(days=3), phone=CUSTOMER)
    sequences.run_invoices(db, c, datetime.now(TZ).replace(hour=12).astimezone(timezone.utc))
    db.commit()
    reply(app_client, text)
    db.expire_all()
    inv = db.get(Invoice, inv.id)
    assert inv.status == status and inv.paused is paused
    if "e-transfer" in text:
        assert inv.customer_says_paid_at is not None


# ---------------------------------------------------------------- CSV import, screens, setup

def test_csv_import_quotes_and_invoices(admin, db, no_llm):
    cid = setup(admin, db)
    today = datetime.now(TZ).date()
    quotes = ("Estimate #,Customer,Phone,Job,Total,Date\n"
              f"E-1,Jordan Lee,519-555-0101,Panel upgrade,\"$2,850.00\",{today.isoformat()}\n"
              f"E-2,Ana,(519) 555-0102,Pot lights,950,{today.strftime('%m/%d/%Y')}\n"
              "E-3,Bad Row,5195550103,Thing,,2026-01-01\n")
    r = admin.post(f"/portal/recovery/quotes/import?client_id={cid}", files={"file": ("q.csv", quotes, "text/csv")},
                   follow_redirects=False)
    assert r.status_code == 303
    rows = db.scalars(select(Quote).where(Quote.client_id == cid).order_by(Quote.id)).all()
    assert [(q.ref, q.amount_cents, q.phone) for q in rows] == [("E-1", 285000, "+15195550101"), ("E-2", 95000, "+15195550102")]
    admin.post(f"/portal/recovery/quotes/import?client_id={cid}", files={"file": ("q.csv", quotes, "text/csv")})
    assert len(db.scalars(select(Quote).where(Quote.client_id == cid)).all()) == 2  # re-import doesn't duplicate
    invoices = ("Invoice No,Client Name,Mobile,Balance Due,Due Date\n"
                f"1043,Sam,5195550104,4800,{(today + timedelta(days=5)).isoformat()}\n"
                "1044,Lee,5195550105,100,someday\n")
    admin.post(f"/portal/recovery/invoices/import?client_id={cid}", files={"file": ("i.csv", invoices, "text/csv")})
    inv = db.scalars(select(Invoice).where(Invoice.client_id == cid)).all()
    assert [(i.number, i.amount_cents) for i in inv] == [("1043", 480000)]


def test_pages_render(admin, app_client, db, net, no_llm):
    cid = setup(admin, db)
    missed_call(app_client)
    reply(app_client, "Generator hookup")
    from app.services import sequences

    c = db.get(Client, cid)
    sequences.create_quote(db, c, amount_cents=285000, phone=CUSTOMER, description="panel")
    sequences.create_invoice(db, c, amount_cents=480000, due=datetime.now(TZ).date() - timedelta(days=2), phone=CUSTOMER)
    db.commit()
    r = admin.get(f"/portal/recovery?client_id={cid}")
    assert r.status_code == 200 and "Revenue recovery" in r.text and "$2,850" in r.text and "Overdue" in r.text
    t = db.scalars(select(SmsThread)).first()
    r = admin.get(f"/portal/recovery/threads/{t.id}")
    assert r.status_code == 200 and "Generator hookup" in r.text
    r = admin.get(f"/clients/{cid}?tab=recovery")
    assert r.status_code == 200 and "Recovery number" in r.text and "(519) 555-0142" in r.text


def test_buy_recovery_number_sets_webhooks(admin, db, net):
    cid = make_client(admin)
    r = admin.post(f"/clients/{cid}/recovery/number/buy", data={"area_code": "519", "country": "CA"}, follow_redirects=False)
    assert r.status_code == 303
    search = net.of("api.twilio.com", "AvailablePhoneNumbers/CA/Local.json")[0]
    assert search.url.params["AreaCode"] == "519" and search.url.params["SmsEnabled"] == "true"
    buy = net.form([r for r in net.of("api.twilio.com", "IncomingPhoneNumbers.json") if r.method == "POST"][0])
    assert buy["SmsUrl"] == "https://desk.example.com/webhooks/twilio/sms"
    assert buy["VoiceUrl"] == "https://desk.example.com/webhooks/twilio/voice"
    db.expire_all()
    n = db.scalars(select(PhoneNumber).where(PhoneNumber.client_id == cid)).one()
    assert (n.number, n.purpose, n.twilio_sid) == (REC, "recovery", "PN_new")
    c = db.get(Client, cid)
    assert c.numbers == [] and c.recovery_numbers[0].number == REC  # receptionist screens don't see it


def test_simulator_runs_without_twilio(admin, db, monkeypatch, no_llm):
    from app import config
    from app.services import messaging

    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
    config.reset_settings()
    cid = setup(admin, db)
    admin.post(f"/clients/{cid}/recovery/simulate/call", data={"phone": "519 555 0177"})
    admin.post(f"/clients/{cid}/recovery/simulate/text", data={"phone": "519 555 0177", "body": "Need a quote for a hot tub hookup"})
    bodies = [m["body"] for m in messaging.OUTBOX if m["channel"] == "sms"]
    assert "Sorry we missed your call" in bodies[0] and "address" in bodies[1]
