"""Executors for agent actions. Each one reuses the same guarded service the
dashboard uses, so an approved action can't do anything a person couldn't."""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AgencySettings, AgentAction, Client, EmailDraft, KnowledgeGap
from .base import utcnow

EXECUTORS: dict[str, tuple[str, Callable[[Session, dict], str]]] = {}


class Deferred(Exception):
    """A temporary condition (e.g. today's email cap). The action stays approved and is retried."""


def executor(kind: str, label: str):
    def wrap(fn):
        EXECUTORS[kind] = (label, fn)
        return fn
    return wrap


def label_of(kind: str) -> str:
    return EXECUTORS.get(kind, (kind.replace("_", " "), None))[0]


def _agency(db: Session) -> AgencySettings:
    return db.scalars(select(AgencySettings)).first()


def _client(db: Session, payload: dict) -> Client:
    c = db.get(Client, int(payload["client_id"]))
    if not c:
        raise ValueError("The client no longer exists.")
    return c


def execute(db: Session, action: AgentAction, decided_by: str) -> AgentAction:
    """Run an action once. Failures are recorded on the action, never raised."""
    if action.status not in ("pending", "approved", "failed"):
        return action
    entry = EXECUTORS.get(action.kind)
    action.decided_by, action.decided_at = decided_by, utcnow()
    if not entry:
        action.status, action.result = "failed", f"No executor for {action.kind}"
        db.commit()
        return action
    action_id = action.id
    try:
        result = entry[1](db, dict(action.payload or {}))
        action.status, action.result = "done", (result or "Done")[:1000]
        db.commit()
    except Deferred as e:
        db.rollback()
        action = db.get(AgentAction, action_id)
        action.status, action.result = "approved", f"Waiting: {e}"[:1000]
        action.decided_by, action.decided_at = decided_by, utcnow()
        db.commit()
    except Exception as e:  # includes outreach.Blocked, ProviderError, NotConfigured
        db.rollback()
        action = db.get(AgentAction, action_id)
        action.status, action.result = "failed", str(e)[:1000]
        action.decided_by, action.decided_at = decided_by, utcnow()
        db.commit()
    return action


# ------------------------------------------------------------------ executors

@executor("send_email_draft", "Send an outreach email")
def _send_email(db: Session, p: dict) -> str:
    from ..services import outreach

    draft = db.get(EmailDraft, int(p["draft_id"]))
    if not draft:
        raise ValueError("The draft was deleted.")
    try:
        return outreach.send_draft(db, _agency(db), draft)
    except outreach.CapReached as e:
        raise Deferred(str(e)) from e


@executor("buy_number", "Get a phone number")
def _buy_number(db: Session, p: dict) -> str:
    from ..services import provisioning

    c = _client(db, p)
    if c.numbers:
        return f"{c.name} already has a number."
    pn = provisioning.provision_number(db, c, p.get("area_code"))
    return f"{pn.number or 'Number'} now answers for {c.name}."


@executor("send_checkout", "Email a checkout link")
def _send_checkout(db: Session, p: dict) -> str:
    from ..services import provisioning

    c = _client(db, p)
    if c.stripe_status in ("active", "trialing"):
        return f"{c.name} is already subscribed."
    provisioning.send_checkout(db, c, _agency(db), email=True)
    return f"Checkout link emailed to {c.owner_email}."


@executor("send_checkout_reminder", "Remind about an unpaid checkout")
def _checkout_reminder(db: Session, p: dict) -> str:
    from ..services import messaging

    c = _client(db, p)
    if c.stripe_status in ("active", "trialing"):
        return f"{c.name} subscribed in the meantime. No reminder sent."
    if not (c.checkout_url and c.owner_email):
        raise ValueError("No checkout link or owner email.")
    a = _agency(db)
    messaging.send_email(c.owner_email, f"Your receptionist for {c.name} is waiting",
                         f"<p>Hi {c.owner_name or 'there'},</p><p>Your AI receptionist is set up and ready to answer "
                         f"for {c.name}. Finish signing up here: <a href=\"{c.checkout_url}\">start your subscription</a>.</p>"
                         f"<p>Questions? Just reply to this email.</p><p>{a.sender_name or a.brand_name}</p>")
    return f"Reminder sent to {c.owner_email}."


@executor("onboard_owner", "Create the owner's login and send a welcome")
def _onboard(db: Session, p: dict) -> str:
    from ..services import provisioning

    c = _client(db, p)
    if not c.owner_email:
        raise ValueError("No owner email on file.")
    provisioning.create_owner_login(db, c, _agency(db), c.owner_email, c.owner_name, welcome=True)
    if c.status == "demo":
        c.status = "active"
    return f"Welcome and login sent to {c.owner_email}."


@executor("publish_answer", "Teach the receptionist an answer")
def _publish_answer(db: Session, p: dict) -> str:
    from ..jobs import sync_agent
    from ..services import agent_builder

    gap = db.get(KnowledgeGap, int(p["gap_id"]))
    if not gap or gap.status != "open":
        return "That question was already handled."
    client = db.get(Client, gap.client_id)
    gap.answer, gap.status = p["answer"], "answered"
    client.agent.learned_faqs = [*(client.agent.learned_faqs or []), {"q": gap.question, "a": p["answer"]}]
    agent_builder.refresh_prompt(client, client.agent)
    status = sync_agent(db, client, client.agent)
    return f"Answer added for {client.name}. {status}"


@executor("add_instruction", "Add an instruction to a receptionist")
def _add_instruction(db: Session, p: dict) -> str:
    from ..jobs import sync_agent
    from ..services import agent_builder

    c = _client(db, p)
    text = p["text"].strip()
    current = c.agent.custom_instructions or ""
    if text in current:
        return "That instruction is already there."
    c.agent.custom_instructions = (current + "\n" + text).strip()
    agent_builder.refresh_prompt(c, c.agent)
    return f"Instruction added for {c.name}. " + sync_agent(db, c, c.agent)


@executor("email_owner", "Email a business owner")
def _email_owner(db: Session, p: dict) -> str:
    from ..services import messaging

    c = _client(db, p)
    if not c.owner_email:
        raise ValueError("No owner email on file.")
    body = p["body"]
    messaging.send_email(c.owner_email, p["subject"], "<p>" + body.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>", body)
    return f"Emailed {c.owner_email}."


@executor("pause_client", "Pause a client")
def _pause(db: Session, p: dict) -> str:
    c = _client(db, p)
    if c.stripe_status in ("active", "trialing"):
        return f"{c.name} paid in the meantime. Not paused."
    c.status = "paused"
    return f"{c.name} paused. Calls now forward to {c.transfer_number or 'a short message'}."


@executor("apply_setting", "Change a setting")
def _apply_setting(db: Session, payload: dict) -> str:
    """A learned change to another agent's settings. Same effect as editing it on the agent's page."""
    from .registry import REGISTRY
    from .runner import ensure_configs
    from ..services.learning import target_key

    agent = payload.get("agent")
    if agent not in REGISTRY:
        raise ValueError(f"Unknown agent {agent}")
    cfg = ensure_configs(db)[agent]
    values = dict(cfg.settings or {})
    knobs = {k.name: k for k in REGISTRY[agent].knobs}
    if payload.get("op") == "retire_target":
        line = str(payload["value"])
        targets = values.get("targets", knobs["targets"].default) or []
        values["targets"] = [t for t in targets if target_key(t) != target_key(line)]
        values["retired"] = list(dict.fromkeys([*(values.get("retired") or []), line]))
        cfg.settings = values
        return f"{REGISTRY[agent].title} stopped searching {line}. It's listed under retired targets if you want it back."
    name = payload.get("setting")
    if name not in knobs:
        raise ValueError(f"{REGISTRY[agent].title} has no setting {name}")
    values[name] = payload.get("value")
    cfg.settings = values
    return f"{REGISTRY[agent].title}: {knobs[name].label} is now {payload.get('value')}."


@executor("set_mode", "Change how an agent works")
def _set_mode(db: Session, payload: dict) -> str:
    from .base import MODES
    from .registry import REGISTRY
    from .runner import ensure_configs

    agent, mode = payload.get("agent"), payload.get("mode")
    if agent not in REGISTRY or mode not in MODES:
        raise ValueError("Unknown agent or mode")
    ensure_configs(db)[agent].mode = mode
    return f"{REGISTRY[agent].title} is now on {MODES[mode]}."
