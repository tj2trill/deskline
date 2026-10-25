"""Vapi integration: assistants, phone numbers, outbound test calls, and
payload parsing for server messages."""
from __future__ import annotations

from datetime import datetime

from ..config import get_settings
from ..models import Agent, Client, CustomFunction
from . import http

FN = "function"


def configured() -> bool:
    return bool(get_settings().vapi_api_key)


def _client():
    s = get_settings()
    if not s.vapi_api_key:
        raise http.NotConfigured("Set VAPI_API_KEY to sync agents and phone numbers with Vapi.")
    return http.client(timeout=30, base_url=s.vapi_base_url, headers={"Authorization": f"Bearer {s.vapi_api_key}"})


def webhook_server() -> dict:
    s = get_settings()
    return {"url": s.base_url.rstrip("/") + "/webhooks/vapi", "timeoutSeconds": 20,
            "headers": {"X-Deskline-Token": s.webhook_token}}


def _fn(name: str, description: str, properties: dict, required: list[str], speak: str | None = None) -> dict:
    tool = {
        "type": FN,
        "function": {"name": name, "description": description,
                     "parameters": {"type": "object", "properties": properties, "required": required}},
        "server": webhook_server(),
    }
    if speak:
        tool["messages"] = [{"type": "request-start", "content": speak}]
    return tool


def build_tools(client: Client, functions: list[CustomFunction]) -> list[dict]:
    tools: list[dict] = []
    if client.booking_mode != "none":
        tools.append(_fn(
            "check_availability",
            "Get open appointment times for a day. Call this before offering times. The result also states today's date.",
            {"date": {"type": "string", "description": "Day requested: YYYY-MM-DD, or words like 'today', 'tomorrow', 'next tuesday'."},
             "service": {"type": "string", "description": "What the appointment is for, if known."}},
            ["date"], speak="Let me check the schedule.",
        ))
        tools.append(_fn(
            "book_appointment",
            "Book a confirmed appointment slot that check_availability returned.",
            {"start": {"type": "string", "description": "Exact slot start as returned by check_availability (YYYY-MM-DDTHH:MM, business local time)."},
             "name": {"type": "string"}, "phone": {"type": "string", "description": "Caller's best phone number"},
             "email": {"type": "string", "description": "Optional email"},
             "service": {"type": "string"}, "notes": {"type": "string", "description": "Address, problem description, anything the team needs"}},
            ["start", "name", "phone"], speak="Booking that for you now.",
        ))
    tools.append(_fn(
        "take_message",
        "Record a message or callback request for the team.",
        {"name": {"type": "string"}, "phone": {"type": "string"},
         "message": {"type": "string", "description": "What they need, in one or two sentences"},
         "urgency": {"type": "string", "enum": ["low", "normal", "urgent"]}},
        ["name", "phone", "message"],
    ))
    tools.append(_fn(
        "capture_lead",
        "Save a new potential customer's details.",
        {"name": {"type": "string"}, "phone": {"type": "string"}, "email": {"type": "string"},
         "interest": {"type": "string", "description": "Service they're interested in"}, "notes": {"type": "string"}},
        ["name", "phone"],
    ))
    tools.append(_fn(
        "send_text",
        "Text the caller useful info (booking link, address, website) after they agree.",
        {"phone": {"type": "string"},
         "kind": {"type": "string", "enum": ["booking_link", "address", "website", "custom"]},
         "custom_text": {"type": "string", "description": "Only for kind=custom; under 300 characters"}},
        ["phone", "kind"],
    ))
    for f in functions:
        if not f.enabled:
            continue
        tools.append(_fn(f"x_{f.name}"[:64], f.description, (f.parameters or {}).get("properties", {}),
                         (f.parameters or {}).get("required", []), speak=f.speak_while_running))
    if client.transfer_number:
        tools.append({
            "type": "transferCall",
            "destinations": [{
                "type": "number", "number": client.transfer_number,
                "message": "Sure, connecting you now. One moment.",
                "description": "Transfer to a staff member when the caller asks for a human, has an emergency, or needs something you can't handle.",
            }],
        })
    tools.append({"type": "endCall"})
    return tools


STRUCTURED_SCHEMA = {
    "type": "object",
    "properties": {
        "caller_name": {"type": "string"},
        "caller_intent": {"type": "string", "description": "Why they called, a few words"},
        "outcome": {"type": "string", "enum": ["booked", "transferred", "message", "lead", "info", "spam", "missed"]},
        "urgent": {"type": "boolean"},
        "follow_up_needed": {"type": "boolean"},
        "caller_sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
        "unanswered_questions": {"type": "array", "items": {"type": "string"},
                                 "description": "Questions the caller asked that the assistant could not answer from its information"},
        "assistant_mistake": {"type": "boolean", "description": "True if the assistant gave wrong info, looped, or frustrated the caller"},
        "assistant_mistake_reason": {"type": "string"},
    },
}


def assistant_payload(client: Client, agent: Agent, functions: list[CustomFunction]) -> dict:
    s = get_settings()
    lang = agent.language or "en"
    model = {
        "provider": s.vapi_model_provider,
        "model": agent.llm_model or s.vapi_model,
        "messages": [{"role": "system", "content": agent.system_prompt or ""}],
        "tools": build_tools(client, functions),
        "temperature": 0.4,
    }
    if s.vapi_model_provider == "custom-llm":
        model["url"] = s.vapi_custom_llm_url
    return {
        "name": client.name[:40],
        "firstMessage": agent.first_message or f"Thanks for calling {client.name}. How can I help?",
        "model": model,
        "voice": {"provider": agent.voice_provider or s.vapi_voice_provider, "voiceId": agent.voice_id or s.vapi_voice_id},
        "transcriber": {"provider": "deepgram", "model": s.vapi_transcriber_model, "language": lang},
        "server": webhook_server(),
        "serverMessages": ["end-of-call-report", "tool-calls", "status-update"],
        "artifactPlan": {"recordingEnabled": True},
        "analysisPlan": {
            "summaryPlan": {"enabled": True},
            "structuredDataPlan": {"enabled": True, "schema": STRUCTURED_SCHEMA},
        },
        "endCallPhrases": ["goodbye", "have a great day", "bye now"],
        "maxDurationSeconds": s.vapi_max_call_seconds,
        "metadata": {"deskline_client_id": str(client.id), "deskline_agent_id": str(agent.id)},
    }


def upsert_assistant(client: Client, agent: Agent, functions: list[CustomFunction]) -> str:
    payload = assistant_payload(client, agent, functions)
    with _client() as c:
        if agent.vapi_assistant_id:
            r = c.patch(f"/assistant/{agent.vapi_assistant_id}", json=payload)
            if r.status_code == 404:  # deleted on Vapi's side: recreate instead of failing forever
                r = c.post("/assistant", json=payload)
        else:
            r = c.post("/assistant", json=payload)
        http.check("Vapi", r)
        return r.json()["id"]


def ping() -> None:
    """Cheap authenticated call used by the ops monitor."""
    with _client() as c:
        http.check("Vapi", c.get("/assistant", params={"limit": 1}))


def delete_assistant(assistant_id: str) -> None:
    with _client() as c:
        r = c.delete(f"/assistant/{assistant_id}")
        if r.status_code not in (200, 204, 404):
            http.check("Vapi", r)


# Numbers carry NO assistantId. Vapi then sends `assistant-request` to our server on
# every inbound call, so routing (pause on non-payment, minute caps) is decided live.
# If our server is unreachable Vapi uses fallbackDestination → the owner's phone.

def fallback_destination(client: Client) -> dict | None:
    # Only the explicit transfer number: the main business line may itself forward
    # to the AI number, and forwarding back to it would loop.
    if not client.transfer_number:
        return None
    return {"type": "number", "number": client.transfer_number,
            "message": "Please hold while I connect you."}


def _number_body(client: Client) -> dict:
    body = {"name": client.name[:40], "server": webhook_server()}
    fb = fallback_destination(client)
    if fb:
        body["fallbackDestination"] = fb
    return body


def buy_number(client: Client, area_code: str | None) -> dict:
    body = {"provider": "vapi", **_number_body(client)}
    if area_code:
        body["numberDesiredAreaCode"] = area_code
    with _client() as c:
        return http.check("Vapi", c.post("/phone-number", json=body)).json()


def import_twilio_number(client: Client, number: str) -> dict:
    s = get_settings()
    if not (s.twilio_account_sid and s.twilio_auth_token):
        raise http.NotConfigured("Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN to import Twilio numbers.")
    body = {"provider": "twilio", "number": number, "twilioAccountSid": s.twilio_account_sid,
            "twilioAuthToken": s.twilio_auth_token, **_number_body(client)}
    with _client() as c:
        return http.check("Vapi", c.post("/phone-number", json=body)).json()


def update_number(client: Client, vapi_number_id: str, provider: str) -> None:
    """Re-push server URL + fallback (e.g. after the transfer number changes)."""
    body = {"provider": provider, **_number_body(client)}
    with _client() as c:
        http.check("Vapi", c.patch(f"/phone-number/{vapi_number_id}", json=body))


def release_number(vapi_number_id: str) -> None:
    with _client() as c:
        r = c.delete(f"/phone-number/{vapi_number_id}")
        if r.status_code not in (200, 204, 404):
            http.check("Vapi", r)


def get_number(vapi_number_id: str) -> dict:
    with _client() as c:
        return http.check("Vapi", c.get(f"/phone-number/{vapi_number_id}")).json()


def outbound_test_call(assistant_id: str, phone_number_id: str, to: str) -> dict:
    with _client() as c:
        return http.check("Vapi", c.post("/call", json={
            "assistantId": assistant_id, "phoneNumberId": phone_number_id, "customer": {"number": to}})).json()


# ---------- server message parsing (tolerant: fields move between versions) ----------

def dig(d: dict | None, *path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
        if cur is None:
            return default
    return cur


def parse_dt(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def call_facts(msg: dict) -> dict:
    """Normalise an end-of-call-report / status-update into flat facts."""
    call = msg.get("call") or {}
    artifact = msg.get("artifact") or call.get("artifact") or {}
    analysis = msg.get("analysis") or call.get("analysis") or {}
    started = parse_dt(msg.get("startedAt") or call.get("startedAt"))
    ended = parse_dt(msg.get("endedAt") or call.get("endedAt"))
    duration = msg.get("durationSeconds")
    if duration is None and started and ended:
        duration = max(0.0, (ended - started).total_seconds())
    recording = artifact.get("recordingUrl") or dig(artifact, "recording", "mono", "combinedUrl") \
        or dig(artifact, "recording", "url") or msg.get("recordingUrl")
    cost = msg.get("cost", call.get("cost"))
    return {
        "vapi_call_id": call.get("id") or msg.get("callId"),
        "assistant_id": call.get("assistantId") or dig(msg, "assistant", "id"),
        "metadata": dig(msg, "assistant", "metadata") or {},
        "phone_number_id": call.get("phoneNumberId") or dig(msg, "phoneNumber", "id"),
        "call_type": call.get("type"),
        "from_number": dig(call, "customer", "number") or dig(msg, "customer", "number"),
        "to_number": dig(msg, "phoneNumber", "number") or dig(call, "phoneNumber", "number"),
        "started_at": started,
        "ended_at": ended,
        "duration_seconds": float(duration or 0),
        "ended_reason": msg.get("endedReason") or call.get("endedReason"),
        "summary": analysis.get("summary") or msg.get("summary"),
        "structured": analysis.get("structuredData") or {},
        "transcript": artifact.get("transcript") or msg.get("transcript"),
        "recording_url": recording,
        "cost_cents": round(float(cost) * 100, 3) if cost is not None else None,
        "status": msg.get("status") or call.get("status"),
    }


def tool_calls(msg: dict) -> list[dict]:
    """[{id, name, arguments(dict)}] from a tool-calls message."""
    import json

    out = []
    for tc in msg.get("toolCallList") or [t.get("toolCall", {}) for t in msg.get("toolWithToolCallList") or []]:
        fn = tc.get("function") or {}
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                args = {}
        out.append({"id": tc.get("id"), "name": fn.get("name"), "arguments": args})
    return out
