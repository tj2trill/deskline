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


def bootstrap() -> None:
    """Create tables, the agency settings row and the first admin (from env)."""
    init_db()
    s = get_settings()
    with SessionLocal() as db:
        if not db.scalars(select(AgencySettings)).first():
            db.add(AgencySettings(brand_name=s.app_name))
        if not db.scalars(select(User)).first():
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

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/api/") or request.headers.get("HX-Request"):
            from fastapi.responses import JSONResponse

            return JSONResponse({"error": "login required"}, status_code=401)
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


app = create_app()
