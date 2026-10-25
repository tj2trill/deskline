"""The learning loop: measure what works and what makes money, then steer the agents toward it.

Everything is recomputed from raw facts on every run (emails actually sent, replies captured,
demo-line calls, wins, Stripe payments, provider costs). Nothing depends on agents grading their
own work, and nothing drifts, because there are no running counters to get out of sync.

Method, in plain terms:
- Each choice (a search target, an email angle, sending with a demo line...) is an "arm".
- Its positive-reply rate gets a Beta posterior that starts from the team-wide rate, worth
  PRIOR_N prospects, so a lucky first reply doesn't make a winner.
- An arm is called working / not working only when it has MIN_N contacted prospects AND the
  posterior says it beats (or trails) the other arms with probability >= SURE. Otherwise it's
  "too early" or "about average". Monte Carlo uses a fixed seed so verdicts don't flicker.
- Choices the agents make (which target to search next, which angle to write) use Thompson
  sampling on the same posteriors: mostly the best-looking arm, sometimes an unproven one.
- Money comes last and outranks everything: revenue actually collected, monthly subscription
  value of won clients, and per-client margin after provider costs.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (AgencySettings, AgentAction, AgentConfig, Call, Client, EmailDraft, EventLog, InboundEmail, Lesson,
                      Payment, Prospect, ProspectSearch, Suppression)
from . import pricing
from .writer import ANGLE_LABELS, ANGLES, angles_for

MIN_N = 20
SURE = 0.90
PRIOR_N = 10
DEFAULT_RATE = 0.03          # used only before there is any data at all
DEMO_CALL_SECONDS = 20       # a call to a demo line this long counts as real interest
SCORE_BUCKETS = [(0, 49), (50, 64), (65, 79), (80, 100)]
HOUR_BUCKETS = [(0, 10, "Before 11am"), (11, 13, "11am to 2pm"), (14, 23, "After 2pm")]
NO_AUTOPILOT = {"billing_guardian"}   # pausing clients stays a human decision


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def norm(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def target_key(line: str) -> str:
    niche, _, location = line.partition("|")
    return f"{norm(niche)} | {norm(location)}"


def pct(x: float) -> str:
    return f"{x * 100:.0f}%" if x >= 0.1 or x == 0 else f"{x * 100:.1f}%"


def usd(cents: float) -> str:
    return f"${abs(cents) / 100:,.0f}"


# ------------------------------------------------------------------ statistics

def wilson(s: int, n: int, z: float = 1.645) -> tuple[float, float]:
    """90% interval for a rate. Honest at small n, unlike s/n ± something."""
    if n == 0:
        return 0.0, 1.0
    p = s / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - m) / d), min(1.0, (c + m) / d)


def _prior(rate: float) -> tuple[float, float]:
    r = min(max(rate, 0.005), 0.95)
    return r * PRIOR_N, (1 - r) * PRIOR_N


def prob_better(s1: int, n1: int, s2: int, n2: int, base: float, draws: int = 4000, seed: int = 7) -> float:
    """P(rate of arm 1 > rate of arm 2), both with the shared prior. Fixed seed: stable verdicts."""
    rng = random.Random(seed)
    a0, b0 = _prior(base)
    wins = sum(rng.betavariate(a0 + s1, b0 + n1 - s1) > rng.betavariate(a0 + s2, b0 + n2 - s2) for _ in range(draws))
    return wins / draws


def judge(s: int, n: int, rest_s: int, rest_n: int, base: float, min_n: int = MIN_N, sure: float = SURE) -> tuple[str, float | None]:
    if n < min_n:
        return "early", None
    if rest_n < min_n:
        if n >= 3 * min_n and s == 0:
            return "fails", 0.0
        return "early", None
    p = prob_better(s, n, rest_s, rest_n, base)
    if p >= sure:
        return "works", p
    if p <= 1 - sure:
        return "fails", p
    return ("average" if n >= 3 * min_n else "early"), p


# ------------------------------------------------------------------ facts

@dataclass
class Fact:
    id: int
    name: str
    target: str | None
    niche: str
    angle: str | None
    demo: bool | None
    score: int
    has_email: bool
    contacted: bool
    sent_at: datetime | None
    positive: bool
    engaged: bool
    won: bool
    paying: bool
    revenue: int
    mrr: int
    unsub: bool


def facts(db: Session) -> list[Fact]:
    first_sent = {pid: _aware(at) for pid, at in db.execute(
        select(EmailDraft.prospect_id, func.min(EmailDraft.sent_at)).where(EmailDraft.status == "sent")
        .group_by(EmailDraft.prospect_id)).all()}
    suppressed = {e.lower() for (e,) in db.execute(select(Suppression.email))}
    searches = {s.id: s for s in db.scalars(select(ProspectSearch))}
    clients = {c.id: c for c in db.scalars(select(Client))}
    revenue = {cid: int(v or 0) for cid, v in db.execute(
        select(Payment.client_id, func.sum(Payment.amount_cents)).where(Payment.status == "paid").group_by(Payment.client_id)).all()}
    positive_reply = {pid for (pid,) in db.execute(select(InboundEmail.prospect_id).where(
        InboundEmail.intent.in_(["interested", "question"]), InboundEmail.prospect_id.is_not(None)))}
    prospects = db.scalars(select(Prospect)).all()
    demo_clients = [p.client_id for p in prospects if p.client_id]
    last_demo_call = {}
    if demo_clients:
        last_demo_call = {cid: _aware(at) for cid, at in db.execute(
            select(Call.client_id, func.max(Call.created_at)).where(Call.client_id.in_(demo_clients),
                                                                    Call.duration_seconds >= DEMO_CALL_SECONDS)
            .group_by(Call.client_id)).all()}
    out = []
    for p in prospects:
        s = searches.get(p.search_id)
        c = clients.get(p.client_id)
        paying = bool(c and c.stripe_status in ("active", "trialing"))
        sent = first_sent.get(p.id)
        positive = paying or p.status in ("replied", "won") or p.id in positive_reply
        called = bool(sent and c and last_demo_call.get(c.id) and last_demo_call[c.id] > sent)
        out.append(Fact(
            id=p.id, name=p.name, target=f"{norm(s.niche)} | {norm(s.location)}" if s else None,
            niche=norm(p.niche or (s.niche if s else "")) or "unknown", angle=p.angle, demo=p.contacted_with_demo,
            score=p.score or 0, has_email=bool(p.email), contacted=sent is not None, sent_at=sent,
            positive=positive, engaged=positive or called, won=p.status == "won" or paying, paying=paying,
            revenue=revenue.get(p.client_id, 0) if p.client_id else 0,
            mrr=(c.monthly_price_cents or 0) if paying else 0, unsub=bool(p.email and p.email.lower() in suppressed)))
    return out


@dataclass
class Arm:
    key: str
    label: str
    leads: int = 0
    with_email: int = 0
    n: int = 0          # contacted
    pos: int = 0        # positive replies (or better)
    engaged: int = 0    # positive or called the demo line
    won: int = 0
    paying: int = 0
    revenue: int = 0
    mrr: int = 0
    unsub: int = 0
    searches: int = 0
    verdict: str = "early"
    p_better: float | None = None
    extra: dict = field(default_factory=dict)

    @property
    def rate(self) -> float:
        return self.pos / self.n if self.n else 0.0

    def row(self) -> dict:
        lo, hi = wilson(self.pos, self.n)
        return {"key": self.key, "label": self.label, "leads": self.leads, "n": self.n, "pos": self.pos, "rate": self.rate,
                "lo": lo, "hi": hi, "engaged": self.engaged, "won": self.won, "paying": self.paying, "revenue": self.revenue,
                "mrr": self.mrr, "unsub": self.unsub, "verdict": self.verdict, "p": self.p_better, **self.extra}


def group(fs: list[Fact], key, label=None, *, leads_too: bool = False) -> dict[str, Arm]:
    arms: dict[str, Arm] = {}
    for f in fs:
        k = key(f)
        if k is None:
            continue
        a = arms.setdefault(k, Arm(k, label(k) if label else k))
        if leads_too:
            a.leads += 1
            a.with_email += f.has_email
        if not f.contacted:
            continue
        a.n += 1
        a.pos += f.positive
        a.engaged += f.engaged
        a.won += f.won
        a.paying += f.paying
        a.revenue += f.revenue
        a.mrr += f.mrr
        a.unsub += f.unsub
    return arms


def base_rate(fs: list[Fact]) -> float:
    n = sum(f.contacted for f in fs)
    s = sum(f.positive for f in fs if f.contacted)
    return (s + DEFAULT_RATE * PRIOR_N) / (n + PRIOR_N)


def judge_all(arms: dict[str, Arm], base: float, min_n: int = MIN_N, sure: float = SURE) -> None:
    """Judge every arm against the rest. With more arms, more chances of a fluke winner, so the
    allowed error is split across the arms being judged (Bonferroni)."""
    tot_s, tot_n = sum(a.pos for a in arms.values()), sum(a.n for a in arms.values())
    k = max(1, sum(1 for a in arms.values() if a.n >= min_n))
    strict = 1 - (1 - sure) / k
    for a in arms.values():
        a.verdict, a.p_better = judge(a.pos, a.n, tot_s - a.pos, tot_n - a.n, base, min_n, strict)


def _rest(arms: dict[str, Arm], key: str) -> float:
    s = sum(a.pos for k, a in arms.items() if k != key)
    n = sum(a.n for k, a in arms.items() if k != key)
    return s / n if n else 0.0


# ------------------------------------------------------------------ policies the agents use

class AngleModel:
    """Thompson sampling over email angles, per niche, shrunk toward each angle's overall rate."""

    def __init__(self, db: Session, rng: random.Random | None = None):
        self.rng = rng or random.Random()
        every = facts(db)
        fs = [f for f in every if f.contacted and f.angle]
        self.base = base_rate(every)
        self.glob = group(fs, lambda f: f.angle)
        self.by_niche = defaultdict(dict)
        for f in fs:
            a = self.by_niche[f.niche].setdefault(f.angle, [0, 0])
            a[0] += f.positive
            a[1] += 1

    def choose(self, niche: str | None, demo_number: str | None) -> str:
        best, best_v = "missed_calls", -1.0
        for angle in angles_for(demo_number):
            g = self.glob.get(angle)
            a0, b0 = _prior(self.base)
            ga, gb = a0 + (g.pos if g else 0), b0 + ((g.n - g.pos) if g else 0)
            mean = ga / (ga + gb)
            s, n = self.by_niche.get(norm(niche), {}).get(angle, [0, 0])
            a, b = mean * PRIOR_N + s, (1 - mean) * PRIOR_N + n - s
            v = self.rng.betavariate(a, b)
            if v > best_v:
                best, best_v = angle, v
        return best


def rank_targets(db: Session, targets: list[str], rng: random.Random | None = None) -> list[str]:
    """Order search targets by sampled expected value per search:
    leads per search × share with an email × positive-reply rate × niche money multiplier."""
    rng = rng or random.Random()
    fs = facts(db)
    base = base_rate(fs)
    arms = group(fs, lambda f: f.target, leads_too=True)
    searches = defaultdict(int)
    for s in db.scalars(select(ProspectSearch)):
        searches[f"{norm(s.niche)} | {norm(s.location)}"] += 1
    tot_leads = sum(a.leads for a in arms.values())
    tot_searches = sum(searches.values())
    global_lps = tot_leads / tot_searches if tot_searches else 20.0
    paying = [f for f in fs if f.paying]
    avg_mrr = (sum(f.mrr for f in paying) / len(paying)) if paying else 0
    niche_mrr = defaultdict(list)
    for f in paying:
        niche_mrr[f.niche].append(f.mrr)
    scored = []
    for line in targets:
        k = target_key(line)
        a = arms.get(k) or Arm(k, line)
        a0, b0 = _prior(base)
        p = rng.betavariate(a0 + a.pos, b0 + a.n - a.pos)
        lps = (a.leads + global_lps * 2) / (searches[k] + 2)
        reach = (a.with_email + 1) / (a.leads + 2)
        niche = norm(line.partition("|")[0])
        mult = (sum(niche_mrr[niche]) / len(niche_mrr[niche]) / avg_mrr) if avg_mrr and len(niche_mrr[niche]) >= 2 else 1.0
        scored.append((p * lps * reach * mult, line))
    scored.sort(key=lambda x: -x[0])
    return [line for _, line in scored]


# ------------------------------------------------------------------ evaluation

def _local(dt: datetime, tz: str) -> datetime:
    try:
        return dt.astimezone(ZoneInfo(tz or "UTC"))
    except Exception:
        return dt


def client_margins(db: Session, days: int = 30) -> list[dict]:
    from .calls import estimated_cost_cents

    since = utcnow() - timedelta(days=days)
    out = []
    for c in db.scalars(select(Client).where(Client.status == "active")).all():
        calls = db.scalars(select(Call).where(Call.client_id == c.id, Call.created_at >= since)).all()
        minutes = sum(x.billable_minutes or 0 for x in calls)
        revenue = pricing.monthly_charge_cents(c, minutes)
        cost = sum(estimated_cost_cents(x) for x in calls)
        out.append({"id": c.id, "name": c.name, "industry": norm(c.industry) or "unknown", "revenue": round(revenue),
                    "cost": round(cost), "margin": round(revenue - cost), "minutes": minutes, "calls": len(calls),
                    "paying": c.stripe_status in ("active", "trialing"), "canceled": c.stripe_status in ("canceled", "unpaid")})
    return out


def evaluate(db: Session, min_n: int = MIN_N, sure: float = SURE) -> dict:
    """The full picture: what works, what doesn't, what makes money, what loses it."""
    agency = db.scalars(select(AgencySettings)).first()
    tz = agency.timezone if agency else "UTC"
    fs = facts(db)
    contacted = [f for f in fs if f.contacted]
    base = base_rate(fs)
    lessons: list[dict] = []

    def lesson(key, area, subject, verdict, statement, **evidence):
        lessons.append({"key": key, "area": area, "subject": subject, "verdict": verdict, "statement": statement, "evidence": evidence})

    # -- targets (labelled the way you wrote them)
    shown = {}
    for s in db.scalars(select(ProspectSearch).order_by(ProspectSearch.created_at)):
        shown.setdefault(f"{norm(s.niche)} | {norm(s.location)}", f"{s.niche.strip()[:1].upper()}{s.niche.strip()[1:]} | {s.location.strip()}")
    targets = group(fs, lambda f: f.target, lambda k: shown.get(k, k), leads_too=True)
    for s in db.scalars(select(ProspectSearch)):
        k = f"{norm(s.niche)} | {norm(s.location)}"
        if k in targets:
            targets[k].searches += 1
    judge_all(targets, base, min_n, sure)
    for a in targets.values():
        rest = _rest(targets, a.key)
        money = f" {a.paying} paying, {usd(a.mrr)}/mo." if a.paying else ""
        if a.verdict == "works":
            st = f"{a.label} works: {a.pos} positive replies from {a.n} emails ({pct(a.rate)}), vs {pct(rest)} for other targets.{money}"
        elif a.verdict == "fails":
            st = f"{a.label} isn't working: {a.pos} positive replies from {a.n} emails ({pct(a.rate)}), vs {pct(rest)} elsewhere."
        elif a.verdict == "average":
            st = f"{a.label} is about average: {pct(a.rate)} positive replies from {a.n} emails.{money}"
        else:
            st = f"{a.label}: too early to tell ({a.n} emailed of {a.leads} found)."
        lesson(f"target:{a.key}", "target", a.label, a.verdict, st, n=a.n, pos=a.pos, rate=a.rate, rest=rest, leads=a.leads,
               paying=a.paying, mrr=a.mrr, p=a.p_better)

    # -- email angles
    angles = group(contacted, lambda f: f.angle, lambda k: ANGLE_LABELS.get(k, k))
    judge_all(angles, base, min_n, sure)
    for a in angles.values():
        rest = _rest(angles, a.key)
        if a.verdict in ("works", "fails"):
            word = "gets more" if a.verdict == "works" else "gets fewer"
            st = f'The "{a.label}" email {word} positive replies: {pct(a.rate)} from {a.n} sent, vs {pct(rest)} for the other angles.'
        else:
            st = f'The "{a.label}" email: {pct(a.rate)} positive replies from {a.n} sent' + (" (no clear difference yet)." if a.n >= min_n else " (too early).")
        lesson(f"angle:{a.key}", "angle", a.label, a.verdict, st, n=a.n, pos=a.pos, rate=a.rate, rest=rest, unsub=a.unsub, p=a.p_better)

    # -- demo line in the first email
    demo = group([f for f in contacted if f.demo is not None], lambda f: "with" if f.demo else "without")
    w, wo = demo.get("with"), demo.get("without")
    demo_verdict = None
    if w and wo:
        v, p = judge(w.pos, w.n, wo.pos, wo.n, base, min_n, sure)
        demo_verdict = v
        if v == "works":
            st = f"A live demo line helps: {pct(w.rate)} positive replies with one ({w.n} emails) vs {pct(wo.rate)} without ({wo.n})."
        elif v == "fails":
            st = f"Demo lines aren't helping: {pct(w.rate)} with one ({w.n}) vs {pct(wo.rate)} without ({wo.n}). Each costs a number."
        else:
            st = f"Demo line effect: {pct(w.rate)} with ({w.n}) vs {pct(wo.rate)} without ({wo.n}). Not clear yet."
        lesson("demo:line", "demo", "Demo line in email", v, st, with_n=w.n, with_pos=w.pos, without_n=wo.n, without_pos=wo.pos, p=p)

    # -- lead score: does our scoring predict replies?
    def bucket(f):
        for lo, hi in SCORE_BUCKETS:
            if lo <= f.score <= hi:
                return f"{lo}-{hi}"
        return None
    scores = group(contacted, bucket, lambda k: f"Score {k}")
    judge_all(scores, base, min_n, sure)
    for a in scores.values():
        if a.verdict in ("works", "fails"):
            st = f"Leads scored {a.key} reply {pct(a.rate)} of the time ({a.n} emailed) vs {pct(_rest(scores, a.key))} for other scores."
            lesson(f"score:{a.key}", "score", a.label, a.verdict, st, n=a.n, pos=a.pos, rate=a.rate)

    # -- send time
    def hour_bucket(f):
        h = _local(f.sent_at, tz).hour
        return next(lbl for lo, hi, lbl in HOUR_BUCKETS if lo <= h <= hi)
    timing = group([f for f in contacted if f.sent_at], hour_bucket)
    judge_all(timing, base, min_n, sure)
    for a in timing.values():
        if a.verdict in ("works", "fails"):
            st = f"Emails sent {a.key.lower()} get {pct(a.rate)} positive replies ({a.n} sent) vs {pct(_rest(timing, a.key))} at other times."
            lesson(f"timing:{a.key}", "timing", a.key, a.verdict, st, n=a.n, pos=a.pos, rate=a.rate)

    # -- whole funnel sanity check
    total_n, total_pos = len(contacted), sum(f.positive for f in contacted)
    if total_n >= 5 * min_n and total_pos == 0:
        lesson("global:no-replies", "global", "All outreach", "fails",
               f"No positive replies from {total_n} emails across every target. Check deliverability (SPF, DKIM, DMARC) "
               "and the offer before sending more.", n=total_n)

    # -- money: which clients earn and which lose
    margins = client_margins(db)
    for m in margins:
        if m["margin"] < 0:
            lesson(f"client:{m['id']}", "client", m["name"], "loses",
                   f"{m['name']} loses {usd(-m['margin'])} a month: {usd(m['cost'])} of call costs against {usd(m['revenue'])} billed. "
                   "Raise the price, add per-minute pricing, or set a minute cap.", **m)
        elif m["paying"]:
            lesson(f"client:{m['id']}", "client", m["name"], "earns",
                   f"{m['name']} earns {usd(m['margin'])} a month after {usd(m['cost'])} of call costs.", **m)

    # -- money by niche
    by_niche: dict[str, list[dict]] = defaultdict(list)
    for m in margins:
        by_niche[m["industry"]].append(m)
    niche_rows = []
    for niche, ms in by_niche.items():
        rev, cost = sum(x["revenue"] for x in ms), sum(x["cost"] for x in ms)
        paying = sum(x["paying"] for x in ms)
        row = {"niche": niche, "clients": len(ms), "paying": paying, "revenue": rev, "cost": cost, "margin": rev - cost,
               "margin_pct": (rev - cost) / rev if rev else 0.0, "canceled": sum(x["canceled"] for x in ms)}
        niche_rows.append(row)
        if paying >= 2:
            verdict = "earns" if row["margin"] > 0 else "loses"
            st = (f"{niche.title()} clients {'earn' if verdict == 'earns' else 'lose'} {usd(abs(row['margin']))} a month across "
                  f"{paying} paying ({row['margin_pct'] * 100:.0f}% margin).")
            lesson(f"niche:{niche}", "niche", niche.title(), verdict, st, **row)
    niche_rows.sort(key=lambda r: -r["margin"])
    if len([r for r in niche_rows if r["paying"] >= 2]) >= 2:
        best = niche_rows[0]
        lesson("niche:best", "niche", "Best niche", "earns",
               f"{best['niche'].title()} is your most profitable niche: {usd(best['margin'])} a month from {best['paying']} paying clients.",
               **best)

    # -- your decisions on agent proposals
    since = utcnow() - timedelta(days=60)
    decisions = defaultdict(lambda: {"approved": 0, "rejected": 0, "failed": 0})
    for key, status, who in db.execute(select(AgentAction.agent_key, AgentAction.status, AgentAction.decided_by).where(
            AgentAction.created_at >= since, AgentAction.status.in_(["done", "failed", "rejected"]))):
        if who and who.startswith("autopilot"):
            continue
        d = decisions[key]
        d["rejected" if status == "rejected" else "approved"] += 1
        d["failed"] += status == "failed"
    from ..agents.registry import REGISTRY

    for key, d in decisions.items():
        n = d["approved"] + d["rejected"]
        if key not in REGISTRY or n < 10:
            continue
        title = REGISTRY[key].title
        if d["rejected"] / n <= 0.03:
            lesson(f"agent:{key}", "agent", title, "works",
                   f"You approved {d['approved']} of {n} {title} proposals in 60 days. It has earned Autopilot.", **d)
        elif d["rejected"] / n >= 0.5:
            lesson(f"agent:{key}", "agent", title, "fails",
                   f"You rejected {d['rejected']} of {n} {title} proposals in 60 days. Its settings probably don't match what you want.", **d)

    funnel = {"leads": len(fs), "contacted": total_n, "positive": total_pos, "engaged": sum(f.engaged for f in contacted),
              "won": sum(f.won for f in fs), "paying": sum(f.paying for f in fs), "revenue": sum(f.revenue for f in fs),
              "mrr": sum(f.mrr for f in fs), "unsub": sum(f.unsub for f in contacted), "base": base}
    return {
        "funnel": funnel, "lessons": lessons, "decisions": dict(decisions), "demo_verdict": demo_verdict,
        "targets": sorted((a.row() | {"searches": a.searches} for a in targets.values()), key=lambda r: (-r["mrr"], -r["pos"], -r["n"])),
        "angles": sorted((a.row() for a in angles.values()), key=lambda r: -r["rate"]),
        "demo": [a.row() for a in demo.values()], "scores": sorted((a.row() for a in scores.values()), key=lambda r: r["key"]),
        "timing": [a.row() for a in timing.values()], "clients": sorted(margins, key=lambda m: m["margin"]), "niches": niche_rows,
        "min_n": min_n, "sure": sure,
    }


# ------------------------------------------------------------------ proposals (closing the loop)

def proposals(db: Session, report: dict, settings: dict) -> list[dict]:
    """Setting changes the evidence supports. Each is executed only through an approval
    (or immediately when the Strategist is on Autopilot)."""
    cfgs = {c.key: c for c in db.scalars(select(AgentConfig))}
    from ..agents.registry import REGISTRY

    def current(agent, name):
        knob = next((k for k in REGISTRY[agent].knobs if k.name == name), None)
        cfg = cfgs.get(agent)
        return (cfg.settings or {}).get(name, knob.default if knob else None) if cfg else (knob.default if knob else None)

    out = []
    by_key = {l["key"]: l for l in report["lessons"]}
    # retire targets that clearly don't work
    if settings.get("retire_targets", True):
        for line in current("prospector", "targets") or []:
            les = by_key.get(f"target:{target_key(line)}")
            if les and les["verdict"] == "fails":
                out.append({"kind": "apply_setting", "dedupe": f"learn:retire:{target_key(line)}",
                            "title": f"Stop searching {line.strip()}", "detail": les["statement"],
                            "payload": {"agent": "prospector", "op": "retire_target", "value": line}})
    # raise the outreach score cutoff when low-scored leads don't reply
    min_score = int(current("outreach", "min_score") or 0)
    for lo, hi in SCORE_BUCKETS:
        les = by_key.get(f"score:{lo}-{hi}")
        if les and les["verdict"] == "fails" and lo <= min_score <= hi and hi + 1 <= 90:
            out.append({"kind": "apply_setting", "dedupe": f"learn:outreach:min_score:{hi + 1}",
                        "title": f"Only email leads scored {hi + 1} or higher", "detail": les["statement"],
                        "payload": {"agent": "outreach", "op": "set", "setting": "min_score", "value": hi + 1}})
    # wait for demo lines when they clearly lift replies
    if report["demo_verdict"] == "works" and not current("outreach", "require_demo"):
        out.append({"kind": "apply_setting", "dedupe": "learn:outreach:require_demo:true",
                    "title": "Always wait for the demo line before emailing", "detail": by_key["demo:line"]["statement"],
                    "payload": {"agent": "outreach", "op": "set", "setting": "require_demo", "value": True}})
    # Autopilot for agents you always approve
    if settings.get("propose_autopilot", True):
        for key, d in report["decisions"].items():
            n = d["approved"] + d["rejected"]
            cfg = cfgs.get(key)
            if (key in REGISTRY and key not in NO_AUTOPILOT and cfg and cfg.mode == "approve" and n >= 15
                    and d["rejected"] / n <= 0.03 and d["failed"] / n <= 0.1):
                out.append({"kind": "set_mode", "dedupe": f"learn:autopilot:{key}:{n // 15}",
                            "title": f"Put {REGISTRY[key].title} on Autopilot",
                            "detail": f"You approved {d['approved']} of {n} of its proposals in 60 days and rejected {d['rejected']}. "
                                      "On Autopilot it acts right away, inside the same limits.",
                            "payload": {"agent": key, "mode": "auto"}})
    return out


def sync_lessons(db: Session, lessons: list[dict]) -> list[Lesson]:
    """Store current lessons; return the ones whose verdict is new or changed."""
    now = utcnow()
    existing = {l.key: l for l in db.scalars(select(Lesson))}
    changed = []
    seen = set()
    for d in lessons:
        seen.add(d["key"])
        cur = existing.get(d["key"])
        if not cur:
            cur = Lesson(key=d["key"], area=d["area"], subject=d["subject"][:200], verdict=d["verdict"],
                         statement=d["statement"][:400], evidence=_jsonable(d["evidence"]), first_seen=now, changed_at=now, updated_at=now)
            db.add(cur)
            if d["verdict"] != "early":
                changed.append(cur)
            continue
        if cur.verdict != d["verdict"]:
            cur.changed_at = now
            if d["verdict"] != "early":
                changed.append(cur)
        cur.verdict, cur.statement, cur.subject = d["verdict"], d["statement"][:400], d["subject"][:200]
        cur.evidence, cur.updated_at = _jsonable(d["evidence"]), now
    for key, cur in existing.items():
        if key not in seen:
            db.delete(cur)   # derived data: the fact it came from is gone
    for l in changed:
        db.add(EventLog(kind="learning", level="info", message=l.statement[:1000], data={"key": l.key, "verdict": l.verdict}))
    return changed


def _jsonable(d: dict) -> dict:
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, float):
            out[k] = round(v, 4)
        elif isinstance(v, (int, str, bool)) or v is None:
            out[k] = v
        else:
            out[k] = str(v)
    return out


def top_lessons(db: Session, limit: int = 6) -> list[dict]:
    order = {"loses": 0, "fails": 1, "works": 2, "earns": 3}
    rows = db.scalars(select(Lesson).where(Lesson.verdict.in_(list(order)))).all()
    rows.sort(key=lambda l: (order[l.verdict], -(l.changed_at.timestamp() if l.changed_at else 0)))
    return [{"key": l.key, "area": l.area, "subject": l.subject, "verdict": l.verdict, "statement": l.statement,
             "changed_at": _aware(l.changed_at).isoformat() if l.changed_at else None} for l in rows[:limit]]


__all__ = ["ANGLES", "AngleModel", "evaluate", "facts", "proposals", "rank_targets", "sync_lessons", "top_lessons", "wilson"]
