"""Sales + marketing writing tools. Numbers (ROI, prices) are computed in code,
never by the model, and prompts forbid invented stats or testimonials."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..models import AgencySettings, Prospect
from . import crawler, llm

NO_FABRICATION = ("Do not invent statistics, testimonials, awards, client names or guarantees. "
                  "Only use facts provided. Plain, confident, specific language; no hype words.")


def roi(inputs: dict, monthly_price_cents: int) -> dict:
    missed = float(inputs.get("missed_calls_per_week") or 10)
    value = float(inputs.get("avg_job_value") or 300)
    close = float(inputs.get("close_rate") or 0.3)
    recovered = missed * 4.33 * close * value
    price = monthly_price_cents / 100 if monthly_price_cents else 0
    return {"missed_calls_per_week": missed, "avg_job_value": value, "close_rate": close,
            "recovered_per_month": round(recovered, 2), "price_per_month": price,
            "roi_multiple": round(recovered / price, 1) if price else None}


def proposal(agency: AgencySettings, business: str, niche: str, notes: str, inputs: dict, pricing_text: str,
             monthly_price_cents: int, setup_fee_cents: int) -> dict:
    r = roi(inputs, monthly_price_cents)
    content = {
        "headline": f"Never miss another call at {business}",
        "intro": f"{agency.brand_name} will answer every call to {business} — nights, weekends and busy hours — "
                 f"book appointments straight into your calendar, and text you a summary of each call.",
        "problem": f"When you're on a job or after hours, callers who reach voicemail usually call the next {niche or 'business'} on the list.",
        "solution": "An AI receptionist trained on your services, hours and policies answers in seconds, "
                    "handles common questions, books jobs, takes detailed messages and transfers urgent calls to you.",
        "next_steps": ["Approve this proposal", "15-minute setup call to confirm services and hours",
                       "Forward your missed calls to your new number", "Go live the same week"],
    }
    if llm.available():
        try:
            gen = llm.complete_json(
                "You write short, specific sales proposals for an agency selling AI phone receptionists to local "
                "businesses. " + NO_FABRICATION +
                ' Return {"headline","intro","problem","solution","next_steps":[str]}. Each text field max 70 words.',
                f"Agency: {agency.brand_name}\nBusiness: {business}\nNiche: {niche}\nWhat we know: {notes}\n"
                f"Their estimated missed calls/week: {r['missed_calls_per_week']}")
            content.update({k: v for k, v in gen.items() if k in content and v})
        except Exception:
            pass
    content["features"] = ["Answers 24/7 in a natural voice", "Books appointments into your calendar",
                           "Texts callers confirmations and links", "Transfers urgent calls to your cell",
                           "Call summaries, recordings and transcripts", "Captures every lead's name and number"]
    content["pricing"] = {"text": pricing_text, "monthly": monthly_price_cents / 100, "setup": setup_fee_cents / 100}
    content["roi"] = r
    return content


# Outreach angles the learning loop tests against each other. Each is a different opening
# argument; follow-ups stay the same so the comparison is about the angle.
ANGLES = {
    "missed_calls": "Open with missed calls: callers who reach voicemail usually call the next business instead.",
    "demo_first": "Open with the demo line: invite them to call a receptionist that is already trained on their business.",
    "after_hours": "Open with evenings and weekends: calls outside business hours still get answered and booked.",
    "busy_owner": "Open with the owner's time: no answering the phone mid-job; every call is summarised by text.",
}
ANGLE_LABELS = {"missed_calls": "Missed calls", "demo_first": "Call the demo", "after_hours": "After hours", "busy_owner": "Owner's time"}


def angles_for(demo_number: str | None) -> list[str]:
    return [a for a in ANGLES if a != "demo_first" or demo_number]


def email_sequence(agency: AgencySettings, p: Prospect, demo_number: str | None = None, angle: str = "missed_calls") -> list[dict]:
    sender = agency.sender_name or agency.brand_name
    reasons = "; ".join(p.score_reasons or [])
    niche = p.niche or "business"
    demo = f"You can hear it working by calling {demo_number}. It's set up with {p.name}'s info." if demo_number else ""
    if angle not in ANGLES or (angle == "demo_first" and not demo_number):
        angle = "missed_calls"
    openers = {
        "missed_calls": (f"Calls to {p.name} that go to voicemail",
                         f"When you're busy or closed, where do calls go? Most callers who hit voicemail try the next {niche} instead."),
        "demo_first": (f"I set up a receptionist for {p.name}",
                       f"I trained an AI receptionist on {p.name}'s website. Call {demo_number} and ask it what your customers usually ask."),
        "after_hours": (f"{p.name} after 5pm",
                        f"A lot of {niche} calls come in evenings and weekends, when nobody can pick up. Those callers rarely leave a message."),
        "busy_owner": (f"Answering the phone mid-job",
                       f"If you're the one answering {p.name}'s phone between jobs, that's time you're not billing for."),
    }
    subject, opener = openers[angle]
    fallback = [
        {"step": 1, "days": 0, "subject": subject,
         "body": f"Hi {p.name} team,\n\n{opener}\n\nWe set up AI receptionists that answer every call, book appointments "
                 f"and text you a summary. {demo if angle != 'demo_first' else ''}\n\nWorth a 10-minute look?\n\n{sender}"},
        {"step": 2, "days": 3, "subject": f"Re: {subject}",
         "body": "Quick follow-up. Happy to set up a free demo line trained on your website so you can hear how it "
                 f"handles your typical calls before deciding anything.\n\n{sender}"},
        {"step": 3, "days": 7, "subject": "Should I close the loop?",
         "body": "I'll stop reaching out after this one. If missed calls ever become a problem, reply and I'll "
                 f"send the demo over.\n\n{sender}"},
    ]
    if not llm.available():
        return fallback
    try:
        data = llm.complete_json(
            "Write a 3-email cold outreach sequence from an agency to a local business about an AI phone "
            "receptionist. Under 110 words each, plain text, one clear ask, no links except a phone number if given. "
            f"Email 1 must follow this angle: {ANGLES[angle]} Emails 2 and 3 are short, polite follow-ups. "
            + NO_FABRICATION + ' Return {"emails":[{"step":1,"days":0,"subject":str,"body":str}, ...]}.',
            f"Sender: {sender} at {agency.brand_name}\nBusiness: {p.name}\nNiche: {p.niche}\nCity/address: {p.address}\n"
            f"Rating: {p.rating} from {p.review_count} reviews\nObservations: {reasons}\nDemo line: {demo or 'none'}",
            temperature=0.6)
        emails = data.get("emails") or []
        clean = [{"step": int(e.get("step", i + 1)), "days": int(e.get("days", [0, 3, 7][min(i, 2)])),
                  "subject": str(e["subject"])[:200], "body": str(e["body"])} for i, e in enumerate(emails[:4]) if e.get("subject")]
        return clean or fallback
    except Exception:
        return fallback


def email_footer(agency: AgencySettings, unsubscribe_url: str) -> tuple[str, str]:
    addr = agency.mailing_address or ""
    text = f"\n\n--\n{agency.brand_name}{', ' + addr if addr else ''}\nDon't want these emails? {unsubscribe_url}"
    html = (f'<p style="color:#888;font-size:12px;margin-top:24px">{agency.brand_name}{", " + addr if addr else ""}<br>'
            f'<a href="{unsubscribe_url}">Unsubscribe</a></p>')
    return text, html


# ---------------- marketing tools for clients ----------------

TOOLS = {
    "review_reply": {"title": "Review responder", "fields": ["business", "rating", "review"],
                     "desc": "Professional replies to Google/Yelp reviews."},
    "social_calendar": {"title": "Social media calendar", "fields": ["business", "industry", "weeks", "platforms"],
                        "desc": "A dated content plan with post copy."},
    "winback": {"title": "Customer win-back", "fields": ["business", "segment", "offer"],
                "desc": "SMS + email to re-engage past customers."},
    "seo_audit": {"title": "Website SEO audit", "fields": ["url"],
                  "desc": "On-page checks plus prioritized fixes."},
    "call_script": {"title": "Cold-call script", "fields": ["business", "niche", "notes"],
                    "desc": "A short script for pitching a receptionist by phone."},
}


def run_tool(name: str, f: dict) -> str:
    if name == "seo_audit":
        return seo_audit(f.get("url", ""))
    prompts = {
        "review_reply": (f"Write a reply from {f.get('business')} to this {f.get('rating')}-star review. Thank them, "
                         "address specifics, no excuses, invite offline contact for complaints. Under 90 words.\n\n"
                         f"Review: {f.get('review')}"),
        "social_calendar": (f"Create a {f.get('weeks') or 4}-week social calendar for {f.get('business')}, a "
                            f"{f.get('industry')} business, on {f.get('platforms') or 'Facebook and Instagram'}. "
                            "2 posts/week. For each: week, day, platform, post copy, image idea. Use a table."),
        "winback": (f"Write a win-back SMS (under 160 chars) and a short email for {f.get('business')} targeting "
                    f"{f.get('segment') or 'customers who have not booked in 6 months'} with offer: "
                    f"{f.get('offer') or 'none — just a check-in'}. Include opt-out text in the SMS."),
        "call_script": (f"Write a 60-second cold-call script for pitching an AI phone receptionist to "
                        f"{f.get('business') or 'a local business'} ({f.get('niche')}). Notes: {f.get('notes')}. "
                        "Include an opener, one question about missed calls, 3 objection handlers, and a close for a demo."),
    }
    if name not in prompts:
        raise ValueError("Unknown tool")
    if not llm.available():
        return "Connect an LLM (LLM_API_KEY or a local LLM_BASE_URL) to use this tool."
    return llm.complete("You are a sharp small-business marketer. " + NO_FABRICATION, prompts[name], temperature=0.6)


def seo_audit(url: str) -> str:
    url = crawler.normalize_url(url)
    final, html = crawler.fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    desc_tag = soup.find("meta", attrs={"name": "description"})
    desc = desc_tag.get("content", "").strip() if desc_tag else ""
    h1s = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    imgs = soup.find_all("img")
    no_alt = [i for i in imgs if not (i.get("alt") or "").strip()]
    has_ld = bool(soup.select("script[type='application/ld+json']"))
    has_viewport = bool(soup.find("meta", attrs={"name": "viewport"}))
    tel = bool(soup.select("a[href^='tel:']"))
    words = len(re.findall(r"\w+", soup.get_text(" ")))
    checks = [
        ("Title tag", bool(title) and 15 <= len(title) <= 65, f"'{title}' ({len(title)} chars; aim 30–60)"),
        ("Meta description", bool(desc) and 70 <= len(desc) <= 165, f"{len(desc)} chars; aim 120–160"),
        ("Exactly one H1", len(h1s) == 1, f"found {len(h1s)}: {h1s[:3]}"),
        ("Image alt text", not no_alt, f"{len(no_alt)} of {len(imgs)} images missing alt"),
        ("LocalBusiness structured data", has_ld, "JSON-LD present" if has_ld else "no JSON-LD found"),
        ("Mobile viewport", has_viewport, ""),
        ("Click-to-call link", tel, "tel: link present" if tel else "no tel: link — mobile visitors can't tap to call"),
        ("Content depth", words >= 300, f"~{words} words on the page"),
        ("HTTPS", final.startswith("https://"), final),
    ]
    report = [f"SEO audit for {final}", ""]
    for name, ok, note in checks:
        report.append(f"{'PASS' if ok else 'FIX '} · {name}" + (f" — {note}" if note else ""))
    if llm.available():
        try:
            fails = [f"{n}: {note}" for n, ok, note in checks if not ok]
            text = soup.get_text(" ")[:4000]
            recs = llm.complete("You are a local-SEO consultant. Give the 5 highest-impact fixes as a numbered list, "
                                "each one line, specific to this page. " + NO_FABRICATION,
                                f"Failed checks: {fails}\nTitle: {title}\nDescription: {desc}\nPage text: {text}",
                                temperature=0.3)
            report += ["", "Recommended fixes:", recs]
        except Exception:
            pass
    return "\n".join(report)
