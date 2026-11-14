"""Lead finder: Google Places (New) Text Search → scored prospects.

Score = how likely a business loses money to unanswered calls. Every point
comes with a human-readable reason so you can sanity-check it."""
from __future__ import annotations

import csv
import io
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Prospect, ProspectSearch
from . import crawler, eventlog, http

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.nationalPhoneNumber",
    "places.internationalPhoneNumber", "places.websiteUri", "places.rating", "places.userRatingCount",
    "places.regularOpeningHours", "places.businessStatus", "places.types", "places.googleMapsUri", "nextPageToken",
])
# Call-driven niches where a missed call is a lost job.
HIGH_VALUE = ["plumb", "hvac", "heating", "air condition", "roof", "electric", "locksmith", "garage door", "pest",
              "dent", "orthodont", "chiropract", "vet", "law", "attorney", "lawyer", "auto repair", "mechanic",
              "tow", "body shop", "landscap", "tree", "clean", "restoration", "water damage", "moving", "movers",
              "salon", "spa", "med spa", "clinic", "physio", "insurance", "real estate", "contractor", "remodel",
              "pool", "solar", "appliance repair", "septic", "paving", "concrete", "fence"]


# "Canada" as the location searches each of these in turn: the 50 largest cities
# plus every provincial and territorial capital. Google returns at most 60
# businesses per query, so one national query would miss almost everyone.
CANADA_CITIES = [
    "Toronto, ON", "Montreal, QC", "Calgary, AB", "Ottawa, ON", "Edmonton, AB", "Winnipeg, MB", "Mississauga, ON",
    "Vancouver, BC", "Brampton, ON", "Hamilton, ON", "Surrey, BC", "Quebec City, QC", "Halifax, NS", "Laval, QC",
    "London, ON", "Markham, ON", "Vaughan, ON", "Gatineau, QC", "Saskatoon, SK", "Kitchener, ON", "Longueuil, QC",
    "Burnaby, BC", "Windsor, ON", "Regina, SK", "Oakville, ON", "Richmond, BC", "Richmond Hill, ON",
    "Burlington, ON", "Oshawa, ON", "Sherbrooke, QC", "Sudbury, ON", "Abbotsford, BC", "Levis, QC",
    "Coquitlam, BC", "Barrie, ON", "Saguenay, QC", "Kelowna, BC", "Guelph, ON", "Trois-Rivieres, QC",
    "Whitby, ON", "Cambridge, ON", "St. Catharines, ON", "Kingston, ON", "Waterloo, ON", "Thunder Bay, ON",
    "Red Deer, AB", "Lethbridge, AB", "Nanaimo, BC", "Kamloops, BC", "Victoria, BC", "St. John's, NL",
    "Moncton, NB", "Saint John, NB", "Fredericton, NB", "Charlottetown, PE", "Whitehorse, YT",
    "Yellowknife, NT", "Iqaluit, NU",
]
# What the Prospector searches when you haven't given it targets: call-driven trades
# where a missed call is a lost job, in every city above.
AUTO_NICHES = [
    "plumber", "electrician", "HVAC contractor", "roofer", "locksmith", "garage door repair", "pest control",
    "dentist", "chiropractor", "physiotherapist", "veterinarian", "auto repair", "towing service", "landscaper",
    "cleaning service", "water damage restoration", "moving company", "hair salon", "appliance repair",
    "paving contractor",
]


def auto_targets() -> list[str]:
    return [f"{n} | {c}" for n in AUTO_NICHES for c in CANADA_CITIES]


_CANADA_WIDE = {"canada", "all of canada", "canada wide", "canada-wide"}


PROVINCES = {"ON": "Ontario", "QC": "Quebec", "BC": "British Columbia", "AB": "Alberta", "MB": "Manitoba",
             "SK": "Saskatchewan", "NS": "Nova Scotia", "NB": "New Brunswick", "NL": "Newfoundland and Labrador",
             "PE": "Prince Edward Island", "YT": "Yukon", "NT": "Northwest Territories", "NU": "Nunavut"}


def is_canada_wide(location: str) -> bool:
    return location.strip().lower() in _CANADA_WIDE


def canonical(location: str) -> str:
    """"hamilton, on" -> "Hamilton, ON" when it is one of the known cities."""
    loc = location.strip()
    if is_canada_wide(loc):
        return "Canada"
    return next((c for c in CANADA_CITIES if c.lower() == loc.lower()), loc)


def areas(base: str) -> list[tuple[str, list[str]]]:
    """The widening steps for a typed location: (label, cities to search)."""
    if base == "Canada":
        return [("Canada", CANADA_CITIES)]
    out = [(base, [base])]
    code = base.rsplit(",", 1)[-1].strip().upper() if "," in base else ""
    if code in PROVINCES:
        prov = [c for c in CANADA_CITIES if c.endswith(f", {code}")]
        cities = prov if base in prov else [base] + prov
        if len(cities) > 1:
            out.append((PROVINCES[code], cities))
    out.append(("Canada", CANADA_CITIES if base in CANADA_CITIES else [base] + CANADA_CITIES))
    return out


def _unused_left(db: Session, niche: str, base: str) -> int:
    """Callable prospects still marked new from earlier searches for this niche and place."""
    ids = [s.id for s in db.scalars(select(ProspectSearch).where(ProspectSearch.niche.ilike(niche)))
           if (s.base_location or s.location).lower() == base.lower()]
    if not ids:
        return 0
    return len(db.scalars(select(Prospect.id).where(Prospect.search_id.in_(ids), Prospect.status == "new",
                                                    Prospect.phone.is_not(None))).all())


def configured() -> bool:
    return bool(get_settings().google_places_api_key)


def search_places(query: str, max_results: int = 60) -> list[dict]:
    key = get_settings().google_places_api_key
    if not key:
        raise http.NotConfigured("Set GOOGLE_PLACES_API_KEY (Places API (New) enabled) or import a CSV of leads.")
    out, token = [], None
    with http.client(timeout=20, headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELD_MASK}) as c:
        while len(out) < max_results:
            body = {"textQuery": query, "pageSize": 20}
            if token:
                body["pageToken"] = token
            data = http.check("Google Places", c.post(PLACES_URL, json=body)).json()
            out += data.get("places", [])
            token = data.get("nextPageToken")
            if not token:
                break
    return out[:max_results]


def _closes_early_or_weekends(hours: list[str]) -> list[str]:
    reasons = []
    text = " ".join(hours).lower()
    if "sunday: closed" in text or "saturday: closed" in text:
        reasons.append("Closed on weekends, so weekend calls go unanswered")
    ends = re.findall(r"[–-]\s*(\d{1,2}):?(\d{2})?\s*(am|pm)", text)
    pm_ends = [int(h) for h, _, ap in ends if ap == "pm"]
    if pm_ends and max(pm_ends) <= 6:
        reasons.append("Closes by 6pm, so evening calls go unanswered")
    return reasons


def score(p: Prospect) -> None:
    pts, why = 0, []
    if not p.phone:
        p.score, p.score_reasons = 0, ["No public phone number"]
        return
    niche_text = f"{p.niche or ''} {' '.join(p.types or [])} {p.name}".lower()
    if any(k in niche_text for k in HIGH_VALUE):
        pts += 25
        why.append("Call-driven business: a missed call is usually a lost job")
    rc = p.review_count or 0
    if rc >= 100:
        pts += 20
        why.append(f"{rc} reviews, so call volume is high")
    elif rc >= 25:
        pts += 12
        why.append(f"{rc} reviews, so demand is steady")
    elif rc < 5:
        pts -= 5
        why.append("Very few reviews; may be too small to pay")
    for r in _closes_early_or_weekends(p.hours or []):
        pts += 12
        why.append(r)
    if not p.website:
        pts += 10
        why.append("No website, so a website is an easy add-on sale")
    sig = p.signals or {}
    if p.enriched:
        if sig.get("chat_widgets"):
            pts -= 10
            why.append("Already uses " + ", ".join(sig["chat_widgets"]))
        if not sig.get("booking_tools"):
            pts += 10
            why.append("No online booking, so the phone is the only way in")
        else:
            why.append("Has online booking (" + ", ".join(sig["booking_tools"]) + ")")
    if p.rating and p.rating < 4.0:
        pts += 5
        why.append(f"Rated {p.rating}: faster answers would help their reviews")
    p.score = max(0, min(100, pts + 20))
    p.score_reasons = why


def start_search(db: Session, niche: str, location: str) -> tuple[ProspectSearch, str | None]:
    """Create the search. If every callable business from the last search for this
    niche and place has been used, widen one step (city -> province -> Canada).
    Returns the search and, when it widened, the area it widened from."""
    base = canonical(location)
    steps = areas(base)
    prev = next((s for s in db.scalars(select(ProspectSearch).where(ProspectSearch.niche.ilike(niche))
                                       .order_by(ProspectSearch.id.desc()))
                 if (s.base_location or s.location).lower() == base.lower()), None)
    tier, widened_from = 0, None
    if prev is not None:
        tier = min(prev.tier, len(steps) - 1)
        if tier < len(steps) - 1 and _unused_left(db, niche, base) == 0:
            widened_from, tier = steps[tier][0], tier + 1
    label, cities = steps[tier]
    search = ProspectSearch(niche=niche, location=label, base_location=base, tier=tier,
                            cities_total=len(cities), cities_done=0)
    db.add(search)
    db.flush()
    return search, widened_from


def fill_search(db: Session, search: ProspectSearch, commit_each_city: bool = False) -> list[str]:
    """Search each city for the niche and store businesses with no website.
    Returns the cities whose search failed (Canada-wide only; a single-city
    failure raises). Raises if every city failed."""
    if not configured():
        raise http.NotConfigured("Set GOOGLE_PLACES_API_KEY (Places API (New) enabled) or import a CSV of leads.")
    cities = areas(search.base_location or search.location)[search.tier][1]
    seen = {pid for pid in db.scalars(select(Prospect.place_id).where(Prospect.place_id.is_not(None)))}
    failed: list[str] = []
    for city in cities:
        try:
            places = search_places(f"{search.niche} in {city}")
        except http.ProviderError as e:
            if len(cities) == 1:
                raise
            failed.append(city)
            eventlog.record(db, "leads", f"Search for {search.niche} in {city} failed: {e}", level="warn")
            search.cities_done += 1
            if commit_each_city:
                db.commit()
            continue
        for pl in places:
            if pl.get("businessStatus") not in (None, "OPERATIONAL"):
                continue
            if pl.get("id") in seen:
                continue
            if pl.get("websiteUri"):  # prospecting targets businesses with no website
                continue
            p = Prospect(search_id=search.id, place_id=pl.get("id"), niche=search.niche,
                         name=(pl.get("displayName") or {}).get("text") or "Unknown",
                         phone=pl.get("nationalPhoneNumber") or pl.get("internationalPhoneNumber"),
                         address=pl.get("formattedAddress"),
                         rating=pl.get("rating"), review_count=pl.get("userRatingCount"),
                         maps_url=pl.get("googleMapsUri"), types=pl.get("types") or [],
                         hours=(pl.get("regularOpeningHours") or {}).get("weekdayDescriptions") or [])
            score(p)
            db.add(p)
            seen.add(p.place_id)
            search.result_count += 1
        search.cities_done += 1
        if commit_each_city:
            db.commit()
    if len(failed) == len(cities):
        raise http.ProviderError(f"Google Places search failed for every city ({len(cities)}); see the event log.")
    return failed


def run_search(db: Session, niche: str, location: str) -> ProspectSearch:
    search, _ = start_search(db, niche, location)
    fill_search(db, search)
    return search


def enrich(p: Prospect) -> None:
    """Visit the prospect's site: find a contact email, chat widgets, online booking."""
    if not p.website:
        p.enriched = True
        score(p)
        return
    result = crawler.crawl(p.website, max_pages=3)
    p.signals = {**(p.signals or {}), **result.signals}
    emails = [e for e in result.emails if not e.lower().startswith(("noreply", "no-reply"))]
    if emails and not p.email:
        p.email = emails[0]
    p.enriched = True
    score(p)


def import_csv(db: Session, text: str, niche: str = "imported") -> ProspectSearch:
    search = ProspectSearch(niche=niche, location="CSV import", source="csv")
    db.add(search)
    db.flush()
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        low = {k.lower().strip(): (v or "").strip() for k, v in row.items() if k}
        name = low.get("name") or low.get("business") or low.get("company")
        if not name:
            continue
        p = Prospect(search_id=search.id, name=name[:200], niche=low.get("niche") or niche,
                     phone=low.get("phone") or None, website=low.get("website") or low.get("site") or None,
                     email=low.get("email") or None, address=low.get("address") or None,
                     rating=float(low["rating"]) if low.get("rating", "").replace(".", "", 1).isdigit() else None,
                     review_count=int(low["reviews"]) if low.get("reviews", "").isdigit() else None)
        score(p)
        db.add(p)
        search.result_count += 1
    return search


def to_csv(prospects: list[Prospect]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "score", "phone", "email", "website", "address", "rating", "reviews", "status", "reasons", "maps_url"])
    for p in prospects:
        w.writerow([p.name, p.score, p.phone, p.email, p.website, p.address, p.rating, p.review_count, p.status,
                    "; ".join(p.score_reasons or []), p.maps_url])
    return buf.getvalue()
