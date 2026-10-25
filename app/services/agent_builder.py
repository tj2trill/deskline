"""Paste a website → business profile → receptionist prompt + knowledge.

Two paths, same output shape:
  * LLM extraction (best): structured JSON profile from crawled pages.
  * Heuristic extraction (no key): JSON-LD, meta tags, tel/mailto, page text.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models import Agent, Client
from . import crawler, llm

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_NAMES = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
             "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}

PROFILE_SCHEMA_HINT = """{
  "business_name": str, "industry": str, "summary": str (2-3 sentences),
  "services": [{"name": str, "description": str, "price": str|null}],
  "hours": {"mon": [["09:00","17:00"]], ..., "sun": []}  (24h, empty list = closed, null if unknown),
  "address": str|null, "service_area": str|null, "phone": str|null, "email": str|null,
  "faqs": [{"q": str, "a": str}]  (only facts stated on the site),
  "booking_notes": str|null, "emergency_policy": str|null, "payment_options": str|null,
  "differentiators": [str], "languages": [str]
}"""


def extract_profile_llm(crawl: crawler.CrawlResult) -> dict:
    system = (
        "You extract facts about a local business from its website for a phone receptionist. "
        "Use ONLY information present in the pages. Never invent prices, hours, guarantees or credentials; "
        "use null or empty lists when unknown. Schema:\n" + PROFILE_SCHEMA_HINT
    )
    user = (
        f"Website: {crawl.root}\nMeta description: {crawl.meta_description}\n"
        f"Phones found: {crawl.phones}\nEmails found: {crawl.emails}\n"
        f"Structured data: {str(crawl.jsonld)[:3000]}\n\nPAGES:\n{crawl.combined_text(24000)}"
    )
    data = llm.complete_json(system, user, temperature=0.1)
    return normalize_profile(data)


def _jsonld_business(crawl: crawler.CrawlResult) -> dict:
    for item in crawl.jsonld:
        t = item.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(x and x not in ("WebSite", "WebPage", "BreadcrumbList", "Organization", "ImageObject") for x in types) or "Organization" in types:
            return item
    return {}


def _parse_ld_hours(biz: dict) -> dict | None:
    hours = {d: [] for d in DAYS}
    found = False
    for spec in biz.get("openingHoursSpecification") or []:
        if not isinstance(spec, dict):
            continue
        days = spec.get("dayOfWeek") or []
        days = days if isinstance(days, list) else [days]
        for d in days:
            key = str(d).split("/")[-1][:3].lower()
            if key in hours and spec.get("opens") and spec.get("closes"):
                hours[key].append([spec["opens"][:5], spec["closes"][:5]])
                found = True
    return hours if found else None


def extract_profile_heuristic(crawl: crawler.CrawlResult) -> dict:
    biz = _jsonld_business(crawl)
    addr = biz.get("address")
    if isinstance(addr, dict):
        addr = ", ".join(str(addr.get(k)) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode") if addr.get(k))
    first = crawl.pages[0] if crawl.pages else None
    name = biz.get("name") or (first.title.split("|")[0].split(" - ")[0].strip() if first and first.title else "")
    # services: short headings-looking lines on service pages
    services = []
    for p in crawl.pages:
        if "service" in p.url.lower():
            for ln in p.text.splitlines():
                if 3 < len(ln) < 60 and not ln.endswith(".") and len(services) < 12:
                    services.append({"name": ln, "description": "", "price": None})
    return normalize_profile({
        "business_name": name,
        "industry": (biz.get("@type") if isinstance(biz.get("@type"), str) else None),
        "summary": crawl.meta_description or (first.text[:300] if first else ""),
        "services": services,
        "hours": _parse_ld_hours(biz),
        "address": addr if isinstance(addr, str) else None,
        "phone": biz.get("telephone") or (crawl.phones[0] if crawl.phones else None),
        "email": biz.get("email") or (crawl.emails[0] if crawl.emails else None),
        "faqs": [],
    })


def normalize_profile(p: dict) -> dict:
    p = dict(p or {})
    p.setdefault("business_name", "")
    p["services"] = [s if isinstance(s, dict) else {"name": str(s)} for s in (p.get("services") or [])][:25]
    p["faqs"] = [f for f in (p.get("faqs") or []) if isinstance(f, dict) and f.get("q") and f.get("a")][:25]
    hours = p.get("hours")
    if isinstance(hours, dict):
        clean = {}
        for d in DAYS:
            spans = hours.get(d) or []
            clean[d] = [[str(a)[:5], str(b)[:5]] for a, b in (s for s in spans if isinstance(s, (list, tuple)) and len(s) == 2)]
        p["hours"] = clean
    else:
        p["hours"] = None
    for k in ("differentiators", "languages"):
        p[k] = [str(x) for x in (p.get(k) or [])][:10]
    return p


def format_hours(hours: dict) -> str:
    lines = []
    for d in DAYS:
        spans = hours.get(d) or []
        lines.append(f"- {DAY_NAMES[d]}: " + (", ".join(f"{a}–{b}" for a, b in spans) if spans else "Closed"))
    return "\n".join(lines)


def build_knowledge(profile: dict, crawl: crawler.CrawlResult | None, limit: int = 14000) -> str:
    parts = []
    if profile.get("summary"):
        parts.append(f"About: {profile['summary']}")
    if profile.get("services"):
        parts.append("Services:\n" + "\n".join(
            f"- {s.get('name')}" + (f": {s['description']}" if s.get("description") else "") + (f" (price: {s['price']})" if s.get("price") else "")
            for s in profile["services"]))
    for key, label in (("service_area", "Service area"), ("payment_options", "Payment options"),
                       ("booking_notes", "Booking notes"), ("emergency_policy", "Emergencies")):
        if profile.get(key):
            parts.append(f"{label}: {profile[key]}")
    if profile.get("differentiators"):
        parts.append("Why customers choose us: " + "; ".join(profile["differentiators"]))
    if profile.get("faqs"):
        parts.append("FAQs:\n" + "\n".join(f"Q: {f['q']}\nA: {f['a']}" for f in profile["faqs"]))
    text = "\n\n".join(parts)
    if crawl and len(text) < limit:
        text += "\n\nWEBSITE EXCERPTS (reference only):\n" + crawl.combined_text(limit - len(text))
    return text[:limit]


def compose_prompt(client: Client, agent: Agent) -> str:
    p = agent.profile or {}
    tz = client.timezone or "UTC"
    now_var = '{{"now" | date: "%A, %B %d, %Y, %I:%M %p", "' + tz + '"}}'
    lines = [
        f"You are {agent.name}, the receptionist answering the phone for {client.name}"
        + (f", a {client.industry or p.get('industry')}" if (client.industry or p.get("industry")) else "")
        + (f" in {client.address}" if client.address else "") + ".",
        "You are speaking on a live phone call. Keep replies short (1–2 sentences), warm and natural. "
        "Ask one question at a time. Never read out URLs, lists or markdown. Spell out times like 'two thirty pm'.",
        f"Current local date and time: {now_var}. Caller's number: {{{{customer.number}}}}.",
        "",
        "# Your goals, in order",
        "1. Find out why they are calling.",
        "2. Answer questions using ONLY the business information below. If you don't know, say you'll have the team follow up — never guess prices, availability, or policies.",
    ]
    if client.booking_mode != "none":
        lines.append("3. If they want an appointment or estimate: get their name and best phone number, call check_availability for the day they want, offer up to three times, then call book_appointment. Confirm the final date and time back to them.")
    else:
        lines.append("3. If they want an appointment: take their name, number and preferred times with take_message and tell them the team will call to confirm.")
    lines.append("4. For anything you can't resolve, use take_message (urgency 'urgent' for emergencies) and tell them when to expect a callback.")
    if client.transfer_number:
        lines.append("5. If the caller insists on a human, has an emergency, or is an existing customer with an active job, transfer them with transferCall.")
    lines.append("Always capture name + phone for new customers with capture_lead before the call ends, if they haven't booked.")
    if client.booking_link:
        lines.append("You can text the caller the booking link with send_text if they prefer to book online.")
    lines += [
        "If the caller is a spam or sales call, politely end the call.",
        "",
        "# Business information",
        f"Business name: {client.name}",
    ]
    if client.business_phone:
        lines.append(f"Main phone: {client.business_phone}")
    if client.address:
        lines.append(f"Address: {client.address}")
    if client.service_area or p.get("service_area"):
        lines.append(f"Service area: {client.service_area or p.get('service_area')}")
    lines.append("Hours (" + tz + "):\n" + format_hours(client.business_hours or {}))
    if agent.learned_faqs:
        lines += ["", "# Answers confirmed by the business (these override anything else)"]
        lines += [f"Q: {f['q']}\nA: {f['a']}" for f in agent.learned_faqs if f.get("q") and f.get("a")]
    if agent.knowledge:
        lines += ["", agent.knowledge]
    if agent.custom_instructions:
        lines += ["", "# Extra instructions from the business (follow these)", agent.custom_instructions]
    return "\n".join(lines)


def default_first_message(client: Client, agent: Agent) -> str:
    msg = f"Thanks for calling {client.name}, this is {agent.name}."
    if client.recording_disclosure:
        msg += " This call may be recorded."
    return msg + " How can I help you today?"


def domain_of(url: str | None) -> str:
    return re.sub(r"^https?://(www\.)?", "", (url or "").strip().lower()).split("/")[0]


def is_placeholder_name(client: Client) -> bool:
    """True when the name was auto-filled from the domain (so the site's real name should replace it)."""
    name = (client.name or "").strip().lower()
    return not name or name == domain_of(client.website) or name == (client.website or "").lower()


def build_agent(db: Session, client: Client, agent: Agent, website: str | None = None) -> None:
    """Crawl → profile → knowledge → prompt. Raises with a readable reason on failure."""
    url = website or client.website
    crawl = None
    profile: dict = {}
    if url:
        crawl = crawler.crawl(url)
        if llm.available():
            try:
                profile = extract_profile_llm(crawl)
            except Exception as e:  # fall back but keep the cause visible
                profile = extract_profile_heuristic(crawl)
                profile["_llm_error"] = str(e)[:300]
        else:
            profile = extract_profile_heuristic(crawl)
    agent.profile = profile
    # Fill blanks on the client from what the site says; never overwrite agency edits.
    if profile.get("business_name") and is_placeholder_name(client):
        client.name = profile["business_name"][:160]
    if not client.industry and profile.get("industry"):
        client.industry = str(profile["industry"])[:120]
    if not client.address and profile.get("address"):
        client.address = str(profile["address"])[:300]
    if not client.business_phone and profile.get("phone"):
        client.business_phone = str(profile["phone"])[:40]
    if not client.service_area and profile.get("service_area"):
        client.service_area = str(profile["service_area"])[:300]
    if profile.get("hours") and any(profile["hours"].values()):
        client.business_hours = profile["hours"]
    agent.knowledge = build_knowledge(profile, crawl)
    if not agent.first_message:
        agent.first_message = default_first_message(client, agent)
    if agent.prompt_mode != "manual":
        agent.system_prompt = compose_prompt(client, agent)
    agent.build_status = "ready"
    agent.build_error = None
    agent.built_at = datetime.now(timezone.utc)


def refresh_prompt(client: Client, agent: Agent) -> None:
    if agent.prompt_mode != "manual":
        agent.system_prompt = compose_prompt(client, agent)


def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")
