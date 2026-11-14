"""Turn a lead's 'whys' into money: an estimate of what unanswered calls cost them, and a call brief
(what to open with, what to say, what to upsell) written into the prospect's Notes.

Every number here is a labelled starting estimate computed in code from what Google shows about
the business (reviews, hours, niche). The brief tells you to confirm them on the call, and the
Proposal form is pre-filled with them so the ROI you send matches what you said."""
from __future__ import annotations

import math
import re

from ..models import Prospect
from . import writer

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Rough first-job ticket by niche (CAD). A starting point only; the brief asks you to confirm it.
JOB_VALUE = [
    ("water damage", 1500), ("restoration", 1500), ("solar", 5000), ("paving", 2500), ("concrete", 2000),
    ("fence", 1500), ("lawyer", 1500), ("attorney", 1500), ("law", 1500), ("moving", 900), ("movers", 900),
    ("roof", 800), ("orthodont", 500), ("septic", 500), ("hvac", 450), ("heating", 450), ("air condition", 450),
    ("auto repair", 400), ("mechanic", 400), ("body shop", 400), ("landscap", 400), ("pool", 400), ("tree", 400),
    ("plumb", 350), ("electric", 300), ("garage door", 300), ("contractor", 300), ("remodel", 300),
    ("pest", 250), ("dent", 250), ("vet", 200), ("clean", 200), ("appliance repair", 200), ("locksmith", 150),
    ("tow", 150), ("med spa", 250), ("spa", 120), ("physio", 100), ("chiropract", 90), ("salon", 80),
]
DEFAULT_JOB_VALUE = 300
CLOSE_RATE = 0.3
# Inbound calls a week, using Google review count as the demand signal.
CALLS_PER_WEEK = [(300, 60), (100, 40), (25, 20), (0, 10)]
BASE_MISSED = 0.20      # callers who hit voicemail while staff are busy or on a job
WEEKEND_MISSED = 0.10   # extra share when closed on a weekend day
EVENING_MISSED = 0.05   # extra share when the latest close is 6:30pm or earlier
PHONE_TROUBLE_MISSED = 0.10  # extra share when their own reviews complain about reaching them by phone
EARLY_CLOSE = 18.5

_DAY_LINE = re.compile(r"^\s*([A-Za-z]+day)\s*:\s*(.+)$")
_RANGE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([AP]M)?\s*[-–—]\s*(\d{1,2})(?::(\d{2}))?\s*([AP]M)", re.I)


def _clock(h: str, m: str | None, ap: str) -> float:
    hour = int(h) % 12 + (12 if ap.upper() == "PM" else 0)
    return hour + int(m or 0) / 60


def parse_hours(hours: list[str]) -> dict[str, str | list[tuple[float, float]]]:
    """Google weekdayDescriptions -> {day: "closed" | "24h" | [(open, close), ...]} in decimal hours.
    A close past midnight is reported above 24."""
    out: dict[str, str | list[tuple[float, float]]] = {}
    for line in hours or []:
        text = re.sub(r"[   ]", " ", line)
        m = _DAY_LINE.match(text)
        if not m:
            continue
        day, rest = m.group(1).capitalize(), m.group(2).strip()
        if rest.lower().startswith("closed"):
            out[day] = "closed"
        elif "24 hours" in rest.lower():
            out[day] = "24h"
        else:
            spans = []
            for h1, m1, ap1, h2, m2, ap2 in _RANGE.findall(rest):
                end = _clock(h2, m2, ap2)
                start = _clock(h1, m1, ap1 or ap2)
                if not ap1 and start > end:  # "1:00 - 5:00 PM" style, start shares the end's meridiem unless that's impossible
                    start = _clock(h1, m1, "AM")
                if end <= start:
                    end += 24
                spans.append((start, end))
            if spans:
                out[day] = spans
    return out


def _fmt_clock(t: float) -> str:
    t %= 24
    h, m = int(t), round((t - int(t)) * 60)
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _closing_times(parsed: dict) -> list[str]:
    """'6:30 PM Tue-Fri', '5:00 PM Sat': latest close per open day, grouped over consecutive days."""
    groups: list[tuple[float, list[str]]] = []
    for day in DAYS:
        v = parsed.get(day)
        if not isinstance(v, list):
            continue
        close = max(e for _, e in v)
        if groups and groups[-1][0] == close and DAYS.index(groups[-1][1][-1]) == DAYS.index(day) - 1:
            groups[-1][1].append(day)
        else:
            groups.append((close, [day]))
    return [f"{_fmt_clock(c)} {d[0][:3]}" + (f"-{d[-1][:3]}" if len(d) > 1 else "") for c, d in groups]


def job_value(p: Prospect) -> tuple[int, str]:
    """(average job value, where it came from). Google's own price range wins over the niche table."""
    price = ((p.signals or {}).get("places") or {}).get("price")
    if price and (price.get("start") or price.get("end")):
        lo, hi = price.get("start"), price.get("end")
        value = round((lo + hi) / 2) if lo and hi else (lo or hi)
        span = f"${lo}-${hi}" if lo and hi else (f"${lo}+" if lo else f"up to ${hi}")
        return value, f"their Google price range {span}"
    text = f"{p.niche or ''} {' '.join(p.types or [])} {p.name}".lower()
    return next((v for k, v in JOB_VALUE if k in text), DEFAULT_JOB_VALUE), "niche estimate"


# Things customers write when they couldn't reach the business by phone.
PHONE_TROUBLE = re.compile(
    r"[^.!?\n]*\b(no one (answer|pick)|nobody (answer|pick)|(never|didn'?t|don'?t|doesn'?t|won'?t) (answer|pick up|call(ed)? (me )?back|return)"
    r"|couldn'?t (get through|reach|get a hold|get ahold)|hard to (reach|get a hold|get ahold|book)|voice ?mail|went to voicemail"
    r"|no answer|unanswered|call(ed)? (several|many|multiple) times|phone (is )?(always )?(busy|off))\w*[^.!?\n]*[.!?]?", re.I)
_NAME_AFTER = re.compile(r"\b(?:with|by|ask for|thanks?|thank you|to|see|recommend)\s+([A-Z][a-z]{2,})\b")
_NAME_BEFORE = re.compile(r"\b([A-Z][a-z]{2,})\s+(?:did|does|was|is|took|gave|made|always|really|has|cut|styled|fixed|"
                          r"came|showed|explained|knows|went|listened|answered|helped|got)\b")
NOT_NAMES = {"The", "This", "They", "She", "Her", "His", "And", "But", "Very", "Highly", "Great", "Best", "Everyone",
             "Everything", "Staff", "Service", "Canada", "Google", "Thanks", "Thank", "Would", "Will", "What", "When",
             "Our", "Their", "That", "Then", "Also", "Definitely", "Overall", "Absolutely", "Amazing", "Super", "Really",
             "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Today", "Yesterday", "First"}


def review_evidence(p: Prospect) -> dict:
    """From Google reviews: quotes about not reaching them by phone, and names customers mention (likely staff)."""
    reviews = ((p.signals or {}).get("places") or {}).get("reviews") or []
    business = set(re.findall(r"[A-Za-z]+", p.name or ""))
    quotes, counts = [], {}
    for r in reviews:
        text = r.get("text") or ""
        m = PHONE_TROUBLE.search(text)
        if m:
            quotes.append(" ".join(m.group(0).split())[:200])
        # "Bibi took care of me" is strong on its own; "came to Oshawa" is not, so it needs a second mention.
        for n, weight in [(n, 2) for n in set(_NAME_BEFORE.findall(text))] + [(n, 1) for n in set(_NAME_AFTER.findall(text))]:
            if n not in NOT_NAMES and n not in business:
                counts[n] = counts.get(n, 0) + weight
    names = sorted((n for n in counts if counts[n] >= 2), key=lambda n: -counts[n])[:3]
    return {"reviews": len(reviews), "phone_quotes": quotes, "names": names}


def estimate(p: Prospect, monthly_price_cents: int = 0) -> dict:
    parsed = parse_hours(p.hours or [])
    closed = [d for d in DAYS if parsed.get(d) == "closed"]
    closes = [max(e for _, e in v) for v in parsed.values() if isinstance(v, list)]
    early = bool(closes) and max(closes) <= EARLY_CLOSE and "24h" not in parsed.values()
    rc = p.review_count or 0
    calls = next(c for floor, c in CALLS_PER_WEEK if rc >= floor)
    share = BASE_MISSED + (WEEKEND_MISSED if {"Saturday", "Sunday"} & set(closed) else 0) + (EVENING_MISSED if early else 0)
    if review_evidence(p)["phone_quotes"]:
        share += PHONE_TROUBLE_MISSED
    share = round(min(share, 0.5), 2)
    missed = max(1, round(calls * share))
    value, value_source = job_value(p)
    r = writer.roi({"missed_calls_per_week": missed, "avg_job_value": value, "close_rate": CLOSE_RATE},
                   monthly_price_cents)
    return {**r, "missed_calls_per_week": missed, "avg_job_value": value, "value_source": value_source,
            "calls_per_week": calls,
            "missed_share": share, "closed_days": closed, "closes_early": early,
            "closing_times": _closing_times(parsed)}


def _money(x: float) -> str:
    return f"${x:,.0f}"


def brief(p: Prospect, monthly_price_cents: int) -> str:
    e = estimate(p, monthly_price_cents)
    lines = ["CALL BRIEF (auto-filled from the lead finder; once you edit it, it's yours)",
             f"Est. {e['missed_calls_per_week']} missed calls/week -> about {_money(e['recovered_per_month'])}/month in lost jobs",
             f"  ({e['calls_per_week']} calls/week from {p.review_count or 0} reviews, {round(e['missed_share'] * 100)}% missed, "
             f"{_money(e['avg_job_value'])} average job from {e['value_source']}, {round(CLOSE_RATE * 100)}% close rate. "
             "Calls and missed share are estimates: confirm on the call.)"]
    price = monthly_price_cents / 100
    if price:
        jobs = max(1, math.ceil(price / e["avg_job_value"]))
        lines.append(f"Plan: {_money(price)}/month pays for itself with {jobs} recovered job{'s' if jobs > 1 else ''} a month"
                     + (f" ({e['roi_multiple']}x return at these numbers)." if e["roi_multiple"] else "."))
    if e["closed_days"]:
        opener = f"When you're closed {_join(e['closed_days'])}, where do those calls go?"
    elif e["closes_early"] and e["closing_times"]:
        opener = "After you close for the day, who picks up the phone?"
    else:
        opener = "When you're busy with a customer, where do new callers end up?"
    ev = review_evidence(p)
    if ev["phone_quotes"]:
        opener = "A few of your customers mention trouble getting through on the phone. Can I show you a fix?"
    if ev["names"]:
        lines += ["", f"Ask for: {_join(ev['names'])} (named in their Google reviews)"]
    lines += ["", f"Open with: \"{opener}\"", "", "Why they'll buy:"]
    lines += [f"- A customer wrote: \"{q}\"" for q in ev["phone_quotes"][:2]]
    if e["closed_days"]:
        lines.append(f"- Closed {_join(e['closed_days'])}: those callers reach voicemail and call the next business")
    if e["closes_early"] and e["closing_times"]:
        lines.append(f"- Closes {', '.join(e['closing_times'])}: evening calls go unanswered")
    if p.review_count:
        lines.append(f"- {p.review_count} reviews" + (f" at {p.rating}" if p.rating else "")
                     + (": busy enough to be losing calls, big enough to pay" if p.review_count >= 25 else ""))
    skip = ("Closed on weekends", "Closes by", "No website", "Only a ", f"{p.review_count} reviews")
    lines += [f"- {r}" for r in (p.score_reasons or []) if not r.startswith(skip)]
    if not p.website:
        social = (p.signals or {}).get("social")
        lines += ["", (f"Upsell: Only a {social['name']} page ({social['url']}), no website. " if social else "Upsell: No website. ")
                  + "Generate the demo site below and offer it with the setup fee."]
    lines += ["", "Ask: \"Roughly how many calls go to voicemail in a week?\" Put their answer in the Proposal.",
              "Close: \"What email should I send the demo to?\" Save it in Email above, or type it when you tap Called."]
    return "\n".join(lines)


def fill_blanks(p: Prospect, monthly_price_cents: int) -> bool:
    """Write the call brief into Notes when Notes is empty or still the last auto brief. Returns True if changed."""
    if not p.phone:
        return False
    sig = p.signals or {}
    if p.notes and p.notes != sig.get("auto_notes"):
        return False  # you've written your own notes
    text = brief(p, monthly_price_cents)
    if p.notes == text:
        return False
    p.notes = text
    p.signals = {**sig, "auto_notes": text}
    return True
