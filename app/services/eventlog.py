import logging

from sqlalchemy.orm import Session

from ..models import EventLog

log = logging.getLogger("deskline")


def record(db: Session, kind: str, message: str, client_id: int | None = None, level: str = "info", **data) -> None:
    """Persist an operational event (webhook receipts, provider errors) so failures
    are traceable from the dashboard instead of only in container logs."""
    getattr(log, "warning" if level == "warn" else level, log.info)("[%s] %s", kind, message)
    db.add(EventLog(kind=kind, message=message[:4000], client_id=client_id, level=level, data=_jsonable(data)))


def _jsonable(d):
    out = {}
    for k, v in d.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, dict)):
            out[k] = v
        else:
            out[k] = str(v)
    return out
