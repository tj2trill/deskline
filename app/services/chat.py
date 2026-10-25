"""Website chat widget brain: same knowledge + tools as the phone agent."""
from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from ..models import ChatSession, Client
from . import calls, llm, scheduling

LIQUID_NOW = re.compile(r'\{\{\s*"now"\s*\|[^}]*\}\}')
PHONE_IN_TEXT = re.compile(r"(\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})")


def _tool_specs(client: Client) -> list[dict]:
    from . import vapi

    specs = []
    for t in vapi.build_tools(client, []):
        if t.get("type") == "function" and t["function"]["name"] != "send_text":
            specs.append({"type": "function", "function": t["function"]})
    return specs


def system_prompt(client: Client) -> str:
    agent = client.agent
    base = (agent.system_prompt if agent and agent.system_prompt else f"You help visitors of {client.name}.")
    now = scheduling.now_local(client)
    base = LIQUID_NOW.sub(now.strftime("%A, %B %d, %Y, %I:%M %p"), base).replace("{{customer.number}}", "unknown")
    return ("You are chatting with a visitor in the website chat box (not a phone call). Replies may use short "
            "sentences and simple line breaks, no markdown headings. Ask for a phone number before booking.\n\n" + base)


def reply(db: Session, client: Client, session: ChatSession, text: str) -> str:
    history = list(session.messages or [])
    history.append({"role": "user", "content": text[:2000]})
    if not llm.available():
        answer = _fallback(db, client, session, text)
        history.append({"role": "assistant", "content": answer})
        session.messages = history[-30:]
        return answer
    msgs = [{"role": "system", "content": system_prompt(client)}] + [m for m in history[-20:] if m.get("role") != "system"]
    tools = _tool_specs(client)
    answer = ""
    for _ in range(4):
        out = llm.chat(msgs, tools=tools or None, temperature=0.3, max_tokens=400)
        calls_ = out.get("tool_calls") or []
        if not calls_:
            answer = (out.get("content") or "").strip()
            break
        msgs.append({"role": "assistant", "content": out.get("content") or "", "tool_calls": calls_})
        for tc in calls_:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            _, result = calls.run_tool(db, client, fn.get("name"), args, source="chat")
            msgs.append({"role": "tool", "tool_call_id": tc.get("id"), "content": result})
    if not answer:
        answer = "Thanks! Someone from our team will follow up shortly."
    history.append({"role": "assistant", "content": answer})
    session.messages = history[-30:]
    return answer


def _fallback(db: Session, client: Client, session: ChatSession, text: str) -> str:
    m = PHONE_IN_TEXT.search(text)
    if m:
        calls.run_tool(db, client, "capture_lead", {"name": "Website visitor", "phone": m.group(1), "notes": text[:500]},
                       source="chat")
        return "Thanks — we've got your number and someone will reach out soon."
    phone = (client.numbers[0].number if client.numbers else None) or client.business_phone
    return (f"Thanks for reaching out to {client.name}! Leave your name and phone number here and we'll call you back"
            + (f", or call us any time at {phone}." if phone else "."))


def origin_allowed(client: Client, origin: str | None) -> bool:
    allowed = [o.strip().rstrip("/") for o in (client.widget_allowed_origins or "").split(",") if o.strip()]
    if not allowed:
        return True
    return bool(origin) and origin.rstrip("/") in allowed
