from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..security import verify_password
from ..templating import render

router = APIRouter()


def _safe_next(nxt: str | None, user: User) -> str:
    default = "/" if user.is_agency else "/portal"
    if not nxt or not nxt.startswith("/") or nxt.startswith("//"):
        return default
    if not user.is_agency and not nxt.startswith("/portal"):
        return "/portal"
    return nxt


@router.get("/login")
def login_page(request: Request, next: str = ""):
    return render(request, "login.html", next=next)


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form(""),
          db: Session = Depends(get_db)):
    user = db.scalars(select(User).where(User.email == email.strip().lower())).first()
    if not user or not verify_password(password, user.password_hash):
        return render(request, "login.html", next=next, email=email, error="That email and password don't match. Check both and try again.", status_code=401)
    request.session.clear()
    request.session["uid"] = user.id
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    return RedirectResponse(_safe_next(next, user), status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
