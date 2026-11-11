from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import SessionLocal, init_db
from .models import AgencySettings, User
from .security import LoginRequired, hash_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


PLACEHOLDER_SECRETS = {"dev-secret-change-me", "replace-with-64-random-chars", "change-me", "secret", ""}
PLACEHOLDER_PASSWORDS = {"change-me-now", "pick-a-long-password", "password", "admin", ""}
log = logging.getLogger("deskline")


def is_local(base_url: str) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(base_url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "testserver") or host.endswith(".localhost")


def check_deploy_secrets(s) -> None:
    """Refuse to serve a public URL with the documented placeholder secrets: SECRET_KEY signs the
    session cookie and derives the webhook token, so a known value means anyone can sign in as admin."""
    if is_local(s.base_url):
        if s.secret_key in PLACEHOLDER_SECRETS:
            log.warning("SECRET_KEY is a placeholder. Fine on localhost, never on a public URL.")
        return
    if s.secret_key.strip() in PLACEHOLDER_SECRETS:
        raise RuntimeError("SECRET_KEY is still the placeholder value. Generate one with: "
                           "python -c \"import secrets;print(secrets.token_urlsafe(48))\"")
    if not s.secure_cookies and s.base_url.startswith("https://"):
        log.warning("BASE_URL is https but SECURE_COOKIES is false: the session cookie can travel over plain http.")


def bootstrap() -> None:
    """Create tables, the agency settings row and the first admin (from env)."""
    s = get_settings()
    check_deploy_secrets(s)
    init_db()
    with SessionLocal() as db:
        if not db.scalars(select(AgencySettings)).first():
            db.add(AgencySettings(brand_name=s.app_name))
        if not db.scalars(select(User)).first():
            if not is_local(s.base_url) and s.admin_password.strip() in PLACEHOLDER_PASSWORDS:
                raise RuntimeError("ADMIN_PASSWORD is still the placeholder value. Set a long, unique password "
                                   "before the first start on a public URL.")
            db.add(User(email=s.admin_email.lower(), name="Admin", role="admin", password_hash=hash_password(s.admin_password)))
        db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    from . import scheduler

    bootstrap()
    task = asyncio.create_task(scheduler.run_forever()) if scheduler.enabled() else None
    yield
    if task:
        task.cancel()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title=s.app_name, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=s.secret_key, same_site="lax",
                       https_only=s.secure_cookies, max_age=60 * 60 * 24 * 14)

    from pathlib import Path
    from urllib.parse import urlparse

    from fastapi.staticfiles import StaticFiles

    from .routers import agents_routes, auth, clients, growth, portal, public, settings_routes, webhooks

    for r in (auth.router, clients.router, growth.router, agents_routes.router, settings_routes.router, portal.router,
              public.router, webhooks.router):
        app.include_router(r)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    own_host = (urlparse(s.base_url).hostname or "").lower()
    cache: dict = {"at": 0.0, "map": {}}

    def site_for_host(host: str) -> str | None:
        import time

        from .models import Site

        if time.monotonic() - cache["at"] > 60:
            with SessionLocal() as db:
                cache["map"] = {d.lower(): slug for d, slug in db.execute(
                    select(Site.domain, Site.slug).where(Site.domain.is_not(None), Site.published.is_(True))).all()}
            cache["at"] = time.monotonic()
        return cache["map"].get(host) or cache["map"].get(host.removeprefix("www."))

    @app.middleware("http")
    async def custom_domains(request: Request, call_next):
        """Serve a client's generated site on its own domain (DNS → this server)."""
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if request.url.path == "/" and host and host not in (own_host, "localhost", "127.0.0.1", "testserver"):
            slug = site_for_host(host)
            if slug:
                request.scope["path"] = f"/s/{slug}"
        return await call_next(request)

    base_origin = "{0.scheme}://{0.netloc}".format(urlparse(s.base_url)).lower()
    # Endpoints called by other servers or by embedded widgets on customer sites. They carry their own
    # authentication (shared token, Stripe signature, per-client origin allow-list), not the session cookie.
    csrf_exempt = ("/webhooks/", "/api/chat/")
    app_csp = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
               "media-src 'self' https:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
               "frame-ancestors 'none'; form-action 'self' https://checkout.stripe.com https://billing.stripe.com")
    site_csp = (f"default-src 'self' {base_origin}; script-src 'self' {base_origin}; style-src 'unsafe-inline'; "
                f"img-src 'self' data: https:; connect-src 'self' {base_origin}; object-src 'none'; base-uri 'none'; "
                "form-action 'self'")

    @app.middleware("http")
    async def browser_guards(request: Request, call_next):
        """Cross-site request forgery guard + response hardening headers.

        CSRF: the session cookie is SameSite=Lax, which stops cross-site form posts in current browsers,
        but not requests from sibling subdomains or older clients. Browsers attach Origin (and
        Sec-Fetch-Site) to every state-changing request, so any that names a foreign origin is refused
        before it reaches a handler. Requests with neither header come from non-browser clients, which
        cannot carry a victim's cookie."""
        path = request.url.path
        if request.method not in ("GET", "HEAD", "OPTIONS") and not path.startswith(csrf_exempt):
            origin = (request.headers.get("origin") or "").lower()
            host = (request.headers.get("host") or "").lower()
            same = True
            if origin:
                same = origin == base_origin or (origin != "null" and urlparse(origin).netloc == host)
            elif request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
                same = False
            if not same:
                from fastapi.responses import JSONResponse

                return JSONResponse({"detail": "Cross-site request blocked. Reload the page and try again."}, status_code=403)
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        if s.secure_cookies:
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        path = request.scope.get("path", path)  # custom-domain requests are rewritten to /s/<slug> further in
        public_site = path.startswith("/s/")
        if path.startswith(("/widget/", "/api/chat/", "/static/", "/webhooks/")):
            pass  # scripts, JSON and assets: no document to protect
        elif public_site:
            h.setdefault("Content-Security-Policy", site_csp)
        else:
            h.setdefault("Content-Security-Policy", app_csp)
            h.setdefault("X-Frame-Options", "DENY")
            if "text/html" in h.get("content-type", ""):
                h.setdefault("Cache-Control", "no-store")  # signed-in pages must not sit in shared caches
        return response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/api/") or request.headers.get("HX-Request"):
            from fastapi.responses import JSONResponse

            return JSONResponse({"error": "login required"}, status_code=401)
        from urllib.parse import quote

        return RedirectResponse(f"/login?next={quote(request.url.path, safe='/')}", status_code=303)

    from starlette.exceptions import HTTPException as StarletteHTTPException

    ERROR_COPY = {
        403: ("You don't have access to this page", "This part of the dashboard is only for the agency team or an admin. "
                                                    "If you think that's a mistake, ask whoever set up your account."),
        404: ("We can't find that page", "The link may be old, or the item was deleted."),
        405: ("That link can't be opened directly", "Go back and use the button on the page instead."),
    }

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        """People get a page with a way back; scripts and API callers keep getting JSON."""
        from fastapi.responses import JSONResponse

        wants_html = "text/html" in (request.headers.get("accept") or "")
        soft_post = request.method != "GET" and request.headers.get("x-requested-with") == "deskline"  # shown as a toast
        path = request.url.path
        if not wants_html or soft_post or path.startswith(("/api/", "/webhooks/", "/static/", "/widget/")) or exc.status_code >= 500:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))
        from .templating import render

        uid = request.session.get("uid") if "session" in request.scope else None
        with SessionLocal() as db:
            user = db.get(User, uid) if uid else None
            home = "/login" if not user else ("/" if user.is_agency else "/portal")
        title, body = ERROR_COPY.get(exc.status_code, ("Something went wrong", str(exc.detail)))
        if exc.status_code == 400 and exc.detail:
            title, body = "That didn't work", str(exc.detail)
        return render(request, "error.html", status_code=exc.status_code, code=exc.status_code, title=title, body=body,
                      home=home, signed_in=bool(user))

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


app = create_app()
