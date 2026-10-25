"""Website → clean text pages + structured hints (phones, emails, hours, JSON-LD).
Built-in crawler by default; Firecrawl is used per-page when FIRECRAWL_API_KEY is set."""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..config import get_settings
from . import http

PRIORITY = ("service", "about", "contact", "faq", "pricing", "price", "hours", "book", "appointment",
            "location", "menu", "team", "insurance", "areas", "emergency", "warranty", "policy")
SKIP_EXT = re.compile(r"\.(jpg|jpeg|png|gif|webp|svg|pdf|zip|mp4|mp3|css|js|ico|xml)(\?|$)", re.I)
PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\b[2-9]\d{2}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CHAT_SIGNS = {"intercom": "Intercom", "drift.com": "Drift", "tidio": "Tidio", "podium": "Podium",
              "birdeye": "Birdeye", "livechatinc": "LiveChat", "tawk.to": "Tawk", "crisp.chat": "Crisp",
              "zendesk": "Zendesk", "hubspot": "HubSpot chat", "smith.ai": "Smith.ai", "ruby.com": "Ruby"}
BOOKING_SIGNS = {"calendly": "Calendly", "cal.com": "Cal.com", "acuityscheduling": "Acuity",
                 "squareup.com/appointments": "Square Appointments", "housecallpro": "Housecall Pro",
                 "servicetitan": "ServiceTitan", "jobber": "Jobber", "zocdoc": "Zocdoc", "vagaro": "Vagaro",
                 "booksy": "Booksy", "mindbody": "Mindbody", "opentable": "OpenTable", "resy": "Resy"}


@dataclass
class Page:
    url: str
    title: str
    text: str


@dataclass
class CrawlResult:
    root: str
    pages: list[Page] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    jsonld: list[dict] = field(default_factory=list)
    meta_description: str = ""
    signals: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def combined_text(self, limit: int = 30000) -> str:
        out, total = [], 0
        for p in self.pages:
            chunk = f"## {p.title or p.url}\nURL: {p.url}\n{p.text}\n"
            if total + len(chunk) > limit:
                chunk = chunk[: max(0, limit - total)]
            out.append(chunk)
            total += len(chunk)
            if total >= limit:
                break
        return "\n".join(out)


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def assert_public_host(url: str) -> None:
    """Refuse to fetch internal addresses (SSRF guard: this server may sit next to Postgres)."""
    host = urlparse(url).hostname or ""
    if not host:
        raise ValueError("URL has no host")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        if http.TRANSPORT is not None:  # tests use a mock transport, no DNS
            return
        try:
            addresses = [ipaddress.ip_address(i[4][0]) for i in socket.getaddrinfo(host, None)]
        except socket.gaierror as e:
            raise ValueError(f"Cannot resolve {host}") from e
    for ip in addresses:
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError(f"{host} resolves to a non-public address")


def _clean_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form"]):
        tag.decompose()
    for tag in soup.select("nav, footer [class*=cookie], [aria-hidden=true]"):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if len(ln) > 1]
    # drop exact duplicate lines (menus repeated across pages)
    seen, out = set(), []
    for ln in lines:
        if ln not in seen:
            seen.add(ln)
            out.append(ln)
    return "\n".join(out)


def fetch(url: str, max_redirects: int = 5) -> tuple[str, str]:
    """GET a user-supplied URL, validating every redirect hop against private addresses."""
    with http.client(timeout=15, follow_redirects=False) as c:
        for _ in range(max_redirects + 1):
            assert_public_host(url)
            r = c.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
            if r.is_redirect and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            http.check("Website", r)
            return str(r.url), r.text
    raise ValueError("Too many redirects")


def _firecrawl(url: str) -> str | None:
    key = get_settings().firecrawl_api_key
    if not key:
        return None
    try:
        with http.client(timeout=45, headers={"Authorization": f"Bearer {key}"}) as c:
            r = c.post("https://api.firecrawl.dev/v2/scrape",
                       json={"url": url, "formats": ["markdown"], "onlyMainContent": True})
            if r.status_code >= 400:
                return None
            data = r.json().get("data") or {}
            return data.get("markdown")
    except Exception:
        return None  # built-in extraction is the fallback


def _harvest(result: CrawlResult, html: str, soup: BeautifulSoup) -> None:
    low = html.lower()
    for needle, name in CHAT_SIGNS.items():
        if needle in low:
            result.signals.setdefault("chat_widgets", [])
            if name not in result.signals["chat_widgets"]:
                result.signals["chat_widgets"].append(name)
    for needle, name in BOOKING_SIGNS.items():
        if needle in low:
            result.signals.setdefault("booking_tools", [])
            if name not in result.signals["booking_tools"]:
                result.signals["booking_tools"].append(name)
    for a in soup.select("a[href^='tel:']"):
        num = a["href"][4:].strip()
        if num and num not in result.phones:
            result.phones.append(num)
    for a in soup.select("a[href^='mailto:']"):
        em = a["href"][7:].split("?")[0].strip()
        if em and em not in result.emails:
            result.emails.append(em)
    for script in soup.select("script[type='application/ld+json']"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items:
            if isinstance(it, dict) and it.get("@type"):
                result.jsonld.append(it)


def crawl(url: str, max_pages: int | None = None) -> CrawlResult:
    url = normalize_url(url)
    max_pages = max_pages or get_settings().crawl_max_pages
    result = CrawlResult(root=url)
    try:
        final_url, html = fetch(url)
    except Exception as e:  # root must load; surface the real reason
        raise ValueError(f"Could not load {url}: {e}") from e
    root_host = urlparse(final_url).netloc.replace("www.", "")
    queue: list[tuple[int, str, str | None]] = [(0, final_url, html)]
    seen = {final_url.rstrip("/")}
    while queue and len(result.pages) < max_pages:
        queue.sort(key=lambda t: t[0])
        _, page_url, page_html = queue.pop(0)
        if page_html is None:
            try:
                page_url, page_html = fetch(page_url)
            except Exception as e:
                result.errors.append(f"{page_url}: {e}")
                continue
        soup = BeautifulSoup(page_html, "html.parser")
        _harvest(result, page_html, soup)
        if not result.meta_description:
            md = soup.find("meta", attrs={"name": "description"})
            if md and md.get("content"):
                result.meta_description = md["content"].strip()
        title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
        links = []
        for a in soup.find_all("a", href=True):
            href = urljoin(page_url, a["href"].split("#")[0])
            p = urlparse(href)
            if p.scheme not in ("http", "https") or p.netloc.replace("www.", "") != root_host:
                continue
            if SKIP_EXT.search(p.path):
                continue
            key = href.rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            label = (a.get_text(" ") + " " + p.path).lower()
            prio = 1 if any(w in label for w in PRIORITY) else 5
            links.append((prio + p.path.count("/"), href, None))
        queue.extend(links)
        text = _firecrawl(page_url) or _clean_text(soup)
        result.pages.append(Page(url=page_url, title=title, text=text[:12000]))
    all_text = "\n".join(p.text for p in result.pages)
    for m in PHONE_RE.findall(all_text):
        if m not in result.phones and len(result.phones) < 6:
            result.phones.append(m)
    for m in EMAIL_RE.findall(all_text):
        if m not in result.emails and not m.lower().endswith((".png", ".jpg")) and len(result.emails) < 6:
            result.emails.append(m)
    return result
