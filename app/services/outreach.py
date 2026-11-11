"""Cold email sending with every compliance guard in one place. The prospect page
and the Outreach agent both send through here, so neither can skip a check."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from itsdangerous import URLSafeSerializer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AgencySettings, EmailDraft, Prospect, Suppression
from . import messaging, writer


class Blocked(Exception):
    """Sending is not allowed; the message says why (shown to the user / logged by agents)."""


class CapReached(Blocked):
    """Temporary: the daily cap is used up. Try again later."""


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().secret_key, salt="unsubscribe")


def unsubscribe_link(email: str) -> str:
    return f"{get_settings().base_url}/u/{_serializer().dumps(email.lower())}"


def read_unsubscribe_token(token: str) -> str:
    return _serializer().loads(token)


def is_suppressed(db: Session, email: str | None) -> bool:
    return bool(email) and db.scalars(select(Suppression).where(Suppression.email == email.lower())).first() is not None


def sent_last_24h(db: Session) -> int:
    since = datetime.now(timezone.utc) - timedelta(days=1)
    return db.scalar(select(func.count()).select_from(EmailDraft).where(EmailDraft.sent_at >= since)) or 0


def check(db: Session, agency: AgencySettings, prospect: Prospect, draft: EmailDraft) -> None:
    if draft.status != "draft":
        raise Blocked(f"Email {draft.step} was already {draft.status}.")
    if not prospect.email:
        raise Blocked("Add the prospect's email first.")
    if not agency.mailing_address:
        raise Blocked("Add your mailing address in Settings. Anti-spam law (CASL, CAN-SPAM) requires it in every commercial email.")
    if is_suppressed(db, prospect.email):
        raise Blocked("This address unsubscribed. Not sending.")
    if prospect.status in ("replied", "won", "lost"):
        raise Blocked(f"Prospect is marked {prospect.status}; the sequence stops.")
    if sent_last_24h(db) >= agency.daily_email_cap:
        raise CapReached(f"Daily cap of {agency.daily_email_cap} emails reached. It protects your sending domain.")


def send_draft(db: Session, agency: AgencySettings, draft: EmailDraft) -> str:
    prospect = db.get(Prospect, draft.prospect_id)
    check(db, agency, prospect, draft)
    unsub = unsubscribe_link(prospect.email)
    text_footer, html_footer = writer.email_footer(agency, unsub)
    from ..security import text_to_html

    html = text_to_html(draft.body) + html_footer
    from ..config import get_settings

    reply_to = get_settings().inbound_reply_to or agency.support_email
    messaging.send_email(prospect.email, draft.subject, html, draft.body + text_footer, reply_to=reply_to,
                         headers={"List-Unsubscribe": f"<{unsub}>"})
    draft.status, draft.sent_at = "sent", datetime.now(timezone.utc)
    if prospect.status == "new":
        prospect.status = "contacted"
    return f"Sent email {draft.step} to {prospect.email}."


def next_due(db: Session, prospect: Prospect, now: datetime | None = None) -> EmailDraft | None:
    """The next draft in the sequence whose wait has passed, or None."""
    now = now or datetime.now(timezone.utc)
    drafts = db.scalars(select(EmailDraft).where(EmailDraft.prospect_id == prospect.id).order_by(EmailDraft.step)).all()
    first_sent = next((d.sent_at for d in drafts if d.status == "sent" and d.step == min(x.step for x in drafts)), None)
    for d in drafts:
        if d.status != "draft":
            continue
        if d.step == min(x.step for x in drafts):
            return d
        if not first_sent:
            return None
        sent = first_sent if first_sent.tzinfo else first_sent.replace(tzinfo=timezone.utc)
        return d if now >= sent + timedelta(days=d.send_after_days or 0) else None
    return None
