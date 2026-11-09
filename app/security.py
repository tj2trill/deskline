from __future__ import annotations

import hashlib
import hmac
import html
import ipaddress
import re
import threading
import time
from collections import deque
from urllib.parse import urlparse

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


def password_version(user: User) -> str:
    """Short fingerprint of the current password hash, stored in the session.
    Changing the password changes it, which signs out every other session of that user."""
    key = get_settings().secret_key.encode()
    return hmac.new(key, f"pv:{user.id}:{user.password_hash}".encode(), hashlib.sha256).hexdigest()[:16]


def start_session(request: Request, user: User) -> None:
    request.session.clear()  # never reuse a pre-login session id (fixation)
    request.session["uid"] = user.id
    request.session["pv"] = password_version(user)


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user or not hmac.compare_digest(str(request.session.get("pv") or ""), password_version(user)):
        request.session.pop("uid", None)
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


# ------------------------------------------------------------------ redirects, URLs, colours

def safe_redirect_path(target: str | None, default: str = "/", prefix: str = "/") -> str:
    """Only same-site absolute paths. Browsers treat "/\\host" and "//host" as another origin,
    and control characters can split the Location header."""
    t = target or ""
    if (not t.startswith("/") or t.startswith("//") or "\\" in t or any(ord(ch) < 32 or ord(ch) == 127 for ch in t)
            or not t.startswith(prefix)):
        return default
    return t


def safe_url(value: str | None, schemes: tuple[str, ...] = ("http", "https")) -> str:
    """A URL that is safe to put in href/src, else "". Blocks javascript:, data:, vbscript: and friends."""
    v = (value or "").strip()
    if not v or any(ord(ch) < 32 for ch in v):
        return ""
    if v.startswith("/") and not v.startswith("//") and "\\" not in v:
        return v
    scheme = urlparse(v).scheme.lower()
    return v if scheme in schemes else ""


_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def safe_color(value: str | None, default: str = "#4f46e5") -> str:
    """Colours are interpolated into <style>; accept hex only so nothing else can be smuggled into CSS."""
    v = (value or "").strip()
    return v if _HEX_COLOR.match(v) else default


def esc(value) -> str:
    """HTML-escape a value for hand-built email bodies."""
    return html.escape("" if value is None else str(value), quote=True)


def text_to_html(text: str | None) -> str:
    """Plain text → safe HTML paragraphs (escape first, then turn newlines into markup)."""
    return "<p>" + esc(text).replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_global and not ip.is_multicast


# ------------------------------------------------------------------ throttling

class Throttle:
    """Sliding-window counter kept in process memory (the app runs one worker; see Dockerfile).
    Bounded: stale keys are pruned so an attacker cannot grow it without limit."""

    def __init__(self, limit: int, window: float, max_keys: int = 20000):
        self.limit, self.window, self.max_keys = limit, window, max_keys
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _fresh(self, key: str, now: float) -> deque:
        q = self._hits.get(key)
        if q is None:
            if len(self._hits) >= self.max_keys:
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]:
                    del self._hits[k]
                if len(self._hits) >= self.max_keys:  # still full of live keys: drop the oldest half
                    for k in sorted(self._hits, key=lambda k: self._hits[k][-1])[: self.max_keys // 2]:
                        del self._hits[k]
            q = self._hits[key] = deque()
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._fresh(key, time.monotonic())) >= self.limit

    def hit(self, key: str) -> bool:
        """Record one event; False when the key is over its limit (the event is not recorded)."""
        with self._lock:
            now = time.monotonic()
            q = self._fresh(key, now)
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def retry_after(self, key: str) -> int:
        with self._lock:
            q = self._hits.get(key)
            return max(1, int(self.window - (time.monotonic() - q[0]))) if q else 1


def client_ip(request: Request) -> str:
    """The peer address. Behind the reverse proxy uvicorn's --proxy-headers already resolves it;
    the raw X-Forwarded-For header is never read here because any client can set it."""
    return request.client.host if request.client else "?"
