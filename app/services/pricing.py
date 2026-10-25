"""What you charge a client, as graduated minute tiers. Every pricing model
(flat+included+overage, pure per-minute, custom tiers) reduces to tiers, so
the Stripe price and the in-app estimate can never disagree."""
from __future__ import annotations

from ..models import Client


def tiers_for(client: Client) -> list[dict]:
    if client.pricing_model == "per_minute":
        return [{"up_to": None, "cents": float(client.per_minute_cents or 0)}]
    if client.pricing_model == "tiered" and client.tiers:
        tiers = [{"up_to": (int(t["up_to"]) if t.get("up_to") not in (None, "", "inf") else None),
                  "cents": float(t.get("cents") or 0)} for t in client.tiers]
        bounded = sorted([t for t in tiers if t["up_to"] is not None], key=lambda t: t["up_to"])
        open_ended = [t for t in tiers if t["up_to"] is None][:1] or [{"up_to": None, "cents": bounded[-1]["cents"] if bounded else 0}]
        return bounded + open_ended
    tiers = []
    if client.included_minutes:
        tiers.append({"up_to": int(client.included_minutes), "cents": 0.0})
    tiers.append({"up_to": None, "cents": float(client.overage_cents or 0)})
    return tiers


def has_usage_charges(client: Client) -> bool:
    return any(t["cents"] > 0 for t in tiers_for(client))


def usage_cents(tiers: list[dict], minutes: float) -> float:
    total, floor = 0.0, 0
    for t in tiers:
        cap = t["up_to"]
        if minutes <= floor:
            break
        span = (min(minutes, cap) if cap is not None else minutes) - floor
        total += max(0.0, span) * t["cents"]
        if cap is None:
            break
        floor = cap
    return total


def monthly_charge_cents(client: Client, minutes: float) -> float:
    return float(client.monthly_price_cents or 0) + usage_cents(tiers_for(client), minutes)


def describe(client: Client) -> str:
    parts = []
    if client.monthly_price_cents:
        parts.append(f"${client.monthly_price_cents / 100:,.2f}/mo")
    for i, t in enumerate(tiers_for(client)):
        prev = tiers_for(client)[i - 1]["up_to"] if i else 0
        rng = f"{prev:,}–{t['up_to']:,} min" if t["up_to"] is not None else (f"over {prev:,} min" if prev else "every minute")
        parts.append(f"{rng}: " + ("included" if t["cents"] == 0 else f"${t['cents'] / 100:.2f}/min"))
    return "; ".join(parts)
