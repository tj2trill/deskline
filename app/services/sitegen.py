"""One-page website generator for clients (or prospects) without a decent site."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Client, Prospect, Site, slugify
from . import agent_builder, llm
from .writer import NO_FABRICATION


def unique_slug(db: Session, base: str) -> str:
    slug, i = slugify(base), 2
    while db.scalars(select(Site).where(Site.slug == slug)).first():
        slug = f"{slugify(base)}-{i}"
        i += 1
    return slug


def generate_content(name: str, niche: str | None, facts: str) -> dict:
    base = {
        "headline": f"{name}",
        "subheadline": f"Trusted {niche or 'local'} services. Call or book online any time — we answer 24/7.",
        "about": f"{name} provides {niche or 'professional'} services to the local community.",
        "services": [], "why": ["Fast response", "Clear pricing", "Local and reliable"],
        "faqs": [], "cta_text": "Call now — we answer 24/7", "reviews": [],
    }
    if not llm.available():
        return base
    try:
        data = llm.complete_json(
            "Write copy for a one-page local business website. " + NO_FABRICATION +
            ' Return {"headline":str(max 9 words),"subheadline":str(max 25 words),"about":str(max 80 words),'
            '"services":[{"name":str,"description":str(max 25 words)}](3-6),"why":[str](3-4, max 8 words each),'
            '"faqs":[{"q":str,"a":str}](0-5, only if facts support them),"cta_text":str(max 6 words)}',
            f"Business: {name}\nNiche: {niche}\nKnown facts:\n{facts[:8000]}", temperature=0.5)
        base.update({k: v for k, v in data.items() if k in base and v})
    except Exception:
        pass
    return base


def for_client(db: Session, client: Client) -> Site:
    agent = client.agent
    facts = (agent.knowledge if agent else "") or ""
    site = db.scalars(select(Site).where(Site.client_id == client.id)).first()
    content = generate_content(client.name, client.industry, facts)
    content.update({"phone": (client.numbers[0].number if client.numbers else None) or client.business_phone,
                    "address": client.address, "email": client.owner_email,
                    "hours": agent_builder.format_hours(client.business_hours or {}).replace("- ", "").split("\n"),
                    "booking_link": client.booking_link, "service_area": client.service_area})
    if site:
        site.content = {**content, "reviews": (site.content or {}).get("reviews", [])}
    else:
        site = Site(client_id=client.id, slug=unique_slug(db, client.name), content=content)
        db.add(site)
    db.flush()
    return site


def for_prospect(db: Session, p: Prospect) -> Site:
    facts = f"Address: {p.address}\nRating: {p.rating} ({p.review_count} reviews)\nHours: {p.hours}"
    content = generate_content(p.name, p.niche, facts)
    content.update({"phone": p.phone, "address": p.address, "hours": p.hours or [], "email": p.email})
    site = db.scalars(select(Site).where(Site.prospect_id == p.id)).first()
    if site:
        site.content = content
    else:
        site = Site(prospect_id=p.id, slug=unique_slug(db, p.name), content=content)
        db.add(site)
    db.flush()
    return site
