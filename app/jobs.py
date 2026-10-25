"""Work that runs after the HTTP response (crawl + LLM can take a minute)."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import session_scope
from .models import Agent, Client, CustomFunction, PhoneNumber
from .services import agent_builder, eventlog, http, vapi

log = logging.getLogger("deskline.jobs")


def sync_agent(db: Session, client: Client, agent: Agent) -> str:
    """Push the agent to Vapi. Returns a status line for the UI."""
    if not vapi.configured():
        agent.sync_status, agent.sync_error = "skipped", "VAPI_API_KEY not set — saved locally only."
        return agent.sync_error
    functions = db.scalars(select(CustomFunction).where(CustomFunction.client_id == client.id)).all()
    try:
        agent.vapi_assistant_id = vapi.upsert_assistant(client, agent, functions)
        agent.sync_status, agent.sync_error = "ok", None
    except (http.ProviderError, http.NotConfigured) as e:
        agent.sync_status, agent.sync_error = "error", str(e)
        eventlog.record(db, "vapi", f"Assistant sync failed: {e}", client_id=client.id, level="warn")
        return f"Vapi sync failed: {e}"
    # Numbers route via assistant-request, so only their server URL + fallback need refreshing.
    for n in db.scalars(select(PhoneNumber).where(PhoneNumber.client_id == client.id, PhoneNumber.vapi_id.is_not(None))):
        try:
            vapi.update_number(client, n.vapi_id, n.provider)
        except http.ProviderError as e:
            eventlog.record(db, "vapi", f"Number {n.number} update failed: {e}", client_id=client.id, level="warn")
    return "Synced to Vapi."


def build_and_sync(client_id: int, website: str | None = None) -> None:
    with session_scope() as db:
        client = db.get(Client, client_id)
        if not client:
            return
        agent = client.agent
        agent.build_status, agent.build_error = "building", None
        db.commit()
        try:
            agent_builder.build_agent(db, client, agent, website)
        except Exception as e:
            agent.build_status, agent.build_error = "error", str(e)[:1000]
            # Still produce a usable prompt from whatever the agency typed in.
            agent_builder.refresh_prompt(client, agent)
            agent.first_message = agent.first_message or agent_builder.default_first_message(client, agent)
            eventlog.record(db, "builder", f"Build failed: {e}", client_id=client.id, level="warn")
        sync_agent(db, client, agent)
