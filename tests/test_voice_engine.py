import json

from sqlalchemy import select

from app.models import Agent, Booking, Call, Client, Contact, KnowledgeGap, PhoneNumber
from tests.conftest import vapi_headers


def make_client(admin, **extra):
    data = {"website": "mapleleafplumbing.test", "owner_email": "owner@maple.test", "owner_phone": "905-555-0199",
            "timezone": "America/Toronto", **extra}
    r = admin.post("/clients", data=data, follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers["location"].split("/")[2].split("?")[0])


def test_website_to_agent_with_llm(admin, net, db):
    cid = make_client(admin)
    c = db.get(Client, cid)
    a = c.agent
    assert a.build_status == "ready", a.build_error
    assert c.name == "Maple Leaf Plumbing"
    assert c.business_hours["mon"] == [["08:00", "18:00"]] and c.business_hours["sat"] == []
    assert "Drain cleaning" in a.knowledge and "Do you do emergencies?" in a.knowledge
    assert '{{"now" | date:' in a.system_prompt and "America/Toronto" in a.system_prompt
    assert a.vapi_assistant_id == "asst_1" and a.sync_status == "ok"
    payload = json.loads(net.of("api.vapi.ai", "^/assistant$")[0].content)
    names = [t.get("function", {}).get("name") or t["type"] for t in payload["model"]["tools"]]
    assert names[:5] == ["check_availability", "book_appointment", "take_message", "capture_lead", "send_text"]
    assert "endCall" in names and "transferCall" not in names  # no transfer number yet
    assert payload["server"]["headers"]["X-Deskline-Token"] == vapi_headers()["X-Deskline-Token"]
    assert payload["transcriber"] == {"provider": "deepgram", "model": "nova-3", "language": "en"}
    assert "This call may be recorded" in payload["firstMessage"]
    # crawler followed same-site links only
    paths = {r.url.path for r in net.of("mapleleafplumbing.test")}
    assert {"/", "/services", "/contact"} <= paths


def test_heuristic_build_without_llm(admin, net, db, monkeypatch):
    from app import config

    monkeypatch.setenv("LLM_API_KEY", "")
    config.reset_settings()
    cid = make_client(admin)
    c = db.get(Client, cid)
    assert c.agent.build_status == "ready"
    assert c.name == "Maple Leaf Plumbing"  # from JSON-LD
    assert c.business_hours["fri"] == [["08:00", "18:00"]]
    assert c.business_phone == "+1-905-555-0100"
    assert not net.of("api.openai.com")


def test_transfer_number_adds_transfer_tool_and_fallback(admin, net, db):
    cid = make_client(admin)
    admin.post(f"/clients/{cid}/numbers/buy", data={"area_code": "512"})
    r = admin.post(f"/clients/{cid}/info", data={"name": "Maple Leaf Plumbing", "transfer_number": "905 555 0123",
                                                 "timezone": "America/Toronto", "status": "active"})
    assert r.status_code in (200, 303)
    patch = [r for r in net.of("api.vapi.ai", "^/assistant/") if r.method == "PATCH"][-1]
    tools = json.loads(patch.content)["model"]["tools"]
    transfer = [t for t in tools if t["type"] == "transferCall"][0]
    assert transfer["destinations"][0]["number"] == "+19055550123"
    num_patch = [r for r in net.of("api.vapi.ai", "^/phone-number/") if r.method == "PATCH"][-1]
    body = json.loads(num_patch.content)
    assert body["fallbackDestination"]["number"] == "+19055550123" and "assistantId" not in body
    buy = json.loads([r for r in net.of("api.vapi.ai", "^/phone-number$")][0].content)
    assert buy["numberDesiredAreaCode"] == "512" and "assistantId" not in buy  # routed via assistant-request


def _setup_live(admin, db, **client_fields):
    cid = make_client(admin)
    admin.post(f"/clients/{cid}/numbers/buy", data={"area_code": ""})
    c = db.get(Client, cid)
    for k, v in client_fields.items():
        setattr(c, k, v)
    c.business_hours = {d: [["09:00", "17:00"]] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
    c.booking_lead_hours = 0
    db.commit()
    return c


def test_assistant_request_routing(app_client, admin, db):
    c = _setup_live(admin, db, status="active")
    pn = c.numbers[0].vapi_id
    msg = {"message": {"type": "assistant-request", "call": {"id": "call_A", "phoneNumberId": pn, "customer": {"number": "+14165550000"}}}}
    assert app_client.post("/webhooks/vapi", json=msg).status_code == 401
    r = app_client.post("/webhooks/vapi", json=msg, headers=vapi_headers())
    assert r.json() == {"assistantId": "asst_1"}
    # unpaid client with a transfer number → forwarded, never dropped
    c.status, c.transfer_number = "paused", "+19055550123"
    db.commit()
    r = app_client.post("/webhooks/vapi", json=msg, headers=vapi_headers())
    assert r.json()["destination"]["number"] == "+19055550123"
    # minute cap with forward action
    c.status, c.monthly_minute_cap, c.cap_action = "active", 5, "forward"
    db.add(Call(client_id=c.id, vapi_call_id="old", billable_minutes=6))
    db.commit()
    r = app_client.post("/webhooks/vapi", json=msg, headers=vapi_headers())
    assert "destination" in r.json()


def _tool_msg(call_id, name, args, assistant="asst_1"):
    return {"message": {"type": "tool-calls", "call": {"id": call_id, "assistantId": assistant, "customer": {"number": "+14165550000"}},
                        "toolCallList": [{"id": "tc1", "type": "function", "function": {"name": name, "arguments": args}}],
                        "toolWithToolCallList": []}}


def test_booking_flow_over_phone(app_client, admin, db, net):
    c = _setup_live(admin, db)
    r = app_client.post("/webhooks/vapi", json=_tool_msg("call_B", "check_availability", {"date": "tomorrow"}), headers=vapi_headers())
    res = r.json()["results"][0]
    assert res["toolCallId"] == "tc1" and "result" in res
    avail = json.loads(res["result"])
    assert avail["available"], avail
    slot = avail["available"][0]["start"]
    r = app_client.post("/webhooks/vapi", json=_tool_msg("call_B", "book_appointment",
                        {"start": slot, "name": "Jane Doe", "service": "Leak"}), headers=vapi_headers())
    assert r.json()["results"][0]["result"].startswith("Booked for")
    b = db.scalars(select(Booking).where(Booking.client_id == c.id)).one()
    assert b.phone == "+14165550000" and b.vapi_call_id == "call_B"  # caller ID used when not spoken
    sms = [net.form(r) for r in net.of("api.twilio.com")]
    assert any("You're booked" in s["Body"] and s["To"] == "+14165550000" for s in sms)
    # double-booking the same slot is refused with a speakable reason
    r = app_client.post("/webhooks/vapi", json=_tool_msg("call_C", "book_appointment",
                        {"start": slot, "name": "Bob", "phone": "4165551111"}), headers=vapi_headers())
    assert "no longer available" in r.json()["results"][0]["error"]


def test_take_message_and_custom_function(app_client, admin, db, net):
    c = _setup_live(admin, db)
    r = app_client.post("/webhooks/vapi", json=_tool_msg("call_D", "take_message",
                        {"name": "Ann", "phone": "4165552222", "message": "Basement flooding", "urgency": "urgent"}),
                        headers=vapi_headers())
    assert "right away" in r.json()["results"][0]["result"]
    ct = db.scalars(select(Contact).where(Contact.client_id == c.id)).one()
    assert ct.kind == "callback" and ct.urgency == "urgent"
    admin.post(f"/clients/{c.id}/functions", data={"name": "job_status", "description": "Look up job", "method": "POST",
               "url": "https://hooks.example.test/job", "parameters": '{"phone": {"type": "string"}}'})
    r = app_client.post("/webhooks/vapi", json=_tool_msg("call_D", "x_job_status", {"phone": "4165552222"}), headers=vapi_headers())
    assert "Job 42" in r.json()["results"][0]["result"]


def end_report(call_id, seconds=125, structured=None, cost=0.31):
    return {"message": {"type": "end-of-call-report", "endedReason": "customer-ended-call",
                        "call": {"id": call_id, "assistantId": "asst_1", "customer": {"number": "+14165550000"}},
                        "startedAt": "2026-10-20T14:00:00Z", "endedAt": f"2026-10-20T14:0{seconds // 60}:{seconds % 60:02d}Z",
                        "cost": cost,
                        "artifact": {"transcript": "AI: Hi\nUser: Do you fix sump pumps?", "recordingUrl": "https://storage.vapi.ai/r.wav"},
                        "analysis": {"summary": "Jane booked a leak repair.", "structuredData": structured or {}}}}


def test_end_of_call_quality_loop_and_idempotency(app_client, admin, db, net):
    from app.services import messaging

    c = _setup_live(admin, db, status="active", stripe_customer_id="cus_1", stripe_status="active",
                    stripe_price_ids={"metered": "price_m", "signature": "x"})
    structured = {"caller_name": "Jane", "outcome": "info", "caller_sentiment": "negative",
                  "unanswered_questions": ["Do you fix sump pumps?"], "assistant_mistake": True,
                  "assistant_mistake_reason": "Quoted a price not on the site"}
    for _ in range(2):  # Vapi retries: second delivery must be a no-op
        r = app_client.post("/webhooks/vapi", json=end_report("call_E", 125, structured), headers=vapi_headers())
        assert r.status_code == 200
    call = db.scalars(select(Call).where(Call.vapi_call_id == "call_E")).one()
    assert call.billable_minutes == 3 and call.cost_cents == 31.0
    assert call.flagged and "price" in call.flag_reason and call.sentiment == "negative"
    gap = db.scalars(select(KnowledgeGap).where(KnowledgeGap.client_id == c.id)).one()
    assert gap.question == "Do you fix sump pumps?"
    meter = net.of("api.stripe.com", "meter_events")
    assert len(meter) == 1
    f = net.form(meter[0])
    assert f["payload[value]"] == "3" and f["identifier"] == "call_call_E"
    emails = [m for m in messaging.OUTBOX if m["channel"] == "email" and m["to"] == "owner@maple.test"]
    assert len(emails) == 1 and "Jane" in emails[0]["subject"]
    # teach the gap → it lands in the live prompt
    admin.post(f"/clients/{c.id}/gaps/{gap.id}", data={"answer": "Yes, we repair and replace sump pumps.", "action": "answer"})
    db.expire_all()
    agent = db.get(Agent, c.agent.id)
    assert "sump pumps" in agent.system_prompt and agent.learned_faqs[0]["a"].startswith("Yes")
    patch = [r for r in net.of("api.vapi.ai", "^/assistant/") if r.method == "PATCH"][-1]
    assert "sump pumps" in json.loads(patch.content)["model"]["messages"][0]["content"]


def test_own_analysis_when_provider_returns_none(app_client, admin, db, net):
    _setup_live(admin, db)
    msg = end_report("call_F")
    msg["message"]["analysis"] = {}
    app_client.post("/webhooks/vapi", json=msg, headers=vapi_headers())
    call = db.scalars(select(Call).where(Call.vapi_call_id == "call_F")).one()
    assert call.summary == "Caller asked about drains." and call.sentiment == "neutral"


def test_margin_and_cap_alerts(app_client, admin, db):
    from app.models import EventLog

    c = _setup_live(admin, db, status="active", monthly_price_cents=100, included_minutes=500, overage_cents=0,
                    monthly_minute_cap=2, cap_action="alert")
    app_client.post("/webhooks/vapi", json=end_report("call_G", 125, cost=5.0), headers=vapi_headers())
    kinds = [e.message for e in db.scalars(select(EventLog).where(EventLog.kind == "alert"))]
    assert any(k.startswith("cap:") for k in kinds) and any(k.startswith("margin:") for k in kinds)


def test_workflow_runs_on_condition(app_client, admin, db, net):
    c = _setup_live(admin, db)
    admin.post("/workflows", data={"name": "urgent", "trigger": "call_ended", "client_id": str(c.id),
               "cond_field": "outcome", "cond_op": "equals", "cond_value": "booked",
               "action_type": "sms", "action_to": "{owner_phone}", "action_text": "Booked: {caller_name}"})
    before = len(net.of("api.twilio.com"))
    app_client.post("/webhooks/vapi", json=end_report("call_H", 60, {"outcome": "info"}), headers=vapi_headers())
    assert len(net.of("api.twilio.com")) == before
    app_client.post("/webhooks/vapi", json=end_report("call_I", 60, {"outcome": "booked", "caller_name": "Zed"}), headers=vapi_headers())
    bodies = [net.form(r)["Body"] for r in net.of("api.twilio.com")]
    assert "Booked: Zed" in bodies


def test_unknown_assistant_is_logged_not_crashed(app_client, net):
    r = app_client.post("/webhooks/vapi", json=end_report("call_X"), headers=vapi_headers())
    assert r.status_code == 200
