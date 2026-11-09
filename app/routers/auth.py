from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..security import Throttle, client_ip, hash_password, safe_redirect_path, start_session, verify_password
from ..templating import render

router = APIRouter()


# Password guessing: 8 failures per address+email, 30 per address, in 10 minutes. bcrypt is slow on purpose,
# so without a cap the login form is also a cheap way to pin the CPU.
FAILS_PER_ACCOUNT = Throttle(limit=8, window=600)
FAILS_PER_IP = Throttle(limit=30, window=600)
_DUMMY_HASH = hash_password("deskline-timing-equaliser")


def _safe_next(nxt: str | None, user: User) -> str:
    if user.is_agency:
        return safe_redirect_path(nxt, "/")
    return safe_redirect_path(nxt, "/portal", prefix="/portal")


@router.get("/login")
def login_page(request: Request, next: str = ""):
    return render(request, "login.html", next=next)


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form(""),
          db: Session = Depends(get_db)):
    email_key = email.strip().lower()[:200]
    ip = client_ip(request)
    acct_key = f"{ip}|{email_key}"
    if FAILS_PER_ACCOUNT.blocked(acct_key) or FAILS_PER_IP.blocked(ip):
        wait = max(1, FAILS_PER_ACCOUNT.retry_after(acct_key) // 60)
        resp = render(request, "login.html", next=next, email=email, status_code=429,
                      error=f"Too many attempts. Wait about {wait} minute{'s' if wait != 1 else ''} and try again.")
        resp.headers["Retry-After"] = str(wait * 60)
        return resp
    user = db.scalars(select(User).where(User.email == email_key)).first()
    # Always run one bcrypt comparison so response time does not reveal which emails have accounts.
    ok = verify_password(password, user.password_hash if user else _DUMMY_HASH)
    if not user or not ok:
        FAILS_PER_ACCOUNT.hit(acct_key)
        FAILS_PER_IP.hit(ip)
        return render(request, "login.html", next=next, email=email, error="That email and password don't match. Check both and try again.", status_code=401)
    FAILS_PER_ACCOUNT.clear(acct_key)
    start_session(request, user)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    return RedirectResponse(_safe_next(next, user), status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
