from __future__ import annotations

import hmac

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import Client, User


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode()[:72], hashed.encode())
    except ValueError:
        return False


class LoginRequired(Exception):
    pass


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user:
        raise LoginRequired()
    request.state.user = user
    return user


def agency_user(user: User = Depends(current_user)) -> User:
    if not user.is_agency:
        raise HTTPException(403, "Agency access only")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admin only")
    return user


def client_for_user(db: Session, user: User, client_id: int) -> Client:
    """Authorisation boundary: owners only ever see their own business."""
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(404, "Client not found")
    if not user.is_agency and user.client_id != client.id:
        raise HTTPException(404, "Client not found")
    return client


def valid_webhook_token(value: str | None) -> bool:
    expected = get_settings().webhook_token
    return bool(value) and hmac.compare_digest(value, expected)
