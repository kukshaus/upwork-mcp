"""Deterministic opportunity scoring for an Upwork job.

Adapted from the four-pillar ``score_opportunity`` model in
AbbottDevelopments/upwork-mcp-server (Apache-2.0): client quality, budget fit,
competition and skill match, 25 points each, 100 total.

This module scores the three pillars that can be computed from scraped job
data. The fourth pillar, skill fit, needs judgment about the job description,
so the caller (an LLM) supplies it as a 0-100 ``fit_score`` and
:func:`opportunity_score` scales it to 25 points.
"""

from __future__ import annotations

import re
from datetime import datetime

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _money(text: str | None) -> float | None:
    """'$10K+' -> 10000, '$1.2M' -> 1200000, '$500' -> 500."""
    m = re.search(r"\$\s*([\d.,]+)\s*([KMB])?", text or "", re.I)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    return value * {"K": 1e3, "M": 1e6, "B": 1e9}.get((m.group(2) or "").upper(), 1)


def _int(text: str | None) -> int | None:
    m = re.search(r"\d+", str(text or ""))
    return int(m.group(0)) if m else None


def _months_since(member_since: str | None, now: datetime) -> int | None:
    """'Mar 2, 2020' or 'Mar 2020' -> whole months until now."""
    m = re.search(r"([A-Za-z]{3})[a-z]*\.?\s+(?:\d{1,2},\s*)?(\d{4})", member_since or "")
    if not m or m.group(1).lower() not in _MONTHS:
        return None
    return (now.year - int(m.group(2))) * 12 + now.month - _MONTHS[m.group(1).lower()]


def _posted_days(posted: str | None) -> float | None:
    """'Posted 32 minutes ago' -> 0.02, 'Posted 3 days ago' -> 3, 'yesterday' -> 1."""
    t = (posted or "").lower()
    if "yesterday" in t:
        return 1.0
    m = re.search(r"(\d+)\s*(minute|hour|day|week|month)", t)
    if not m:
        return None
    n = int(m.group(1))
    return n * {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30}[m.group(2)]


def _proposal_floor(proposals: str | None) -> int | None:
    """Lower bound of an Upwork proposals tier: 'Less than 5' -> 0, '10 to 15' -> 10, '50+' -> 50."""
    t = (proposals or "").lower()
    if "less than" in t:
        return 0
    return _int(t)


def score_client(client: dict | None, now: datetime | None = None) -> tuple[int, list[str]]:
    """0-25: payment verified, hires, total spent, rating, account age (5 each)."""
    if not client:
        return 0, ["red: no client data"]
    now = now or datetime.now()
    flags: list[str] = []
    score = 0
    if client.get("payment_verified"):
        score += 5
    else:
        flags.append("red: payment not verified")

    hires = _int(client.get("hires"))
    if not hires:
        flags.append("red: 0 hires")
    else:
        score += 2 if hires <= 3 else 4 if hires <= 10 else 5

    spent = _money(client.get("total_spent")) or 0
    score += 0 if spent < 100 else 2 if spent < 1_000 else 4 if spent < 10_000 else 5

    try:
        rating = float(client.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0.0
    score += min(int(round(rating)), 5)
    if 0 < rating < 4:
        flags.append(f"yellow: client rating {rating}")

    months = _months_since(client.get("member_since"), now)
    if months is not None:
        score += 1 if months < 6 else 3 if months < 24 else 5
        if months < 6:
            flags.append("yellow: new client account")
    return min(score, 25), flags


def score_budget(job: dict, my_rate: float | None) -> tuple[int, list[str]]:
    """0-25: hourly max rate vs my rate, or fixed budget in hours of my rate. 15 = neutral."""
    if not my_rate:
        return 15, ["yellow: no rate given, neutral budget score"]
    budget = job.get("budget") or ""
    amounts = [_money(a) for a in re.findall(r"\$\s*[\d.,]+\s*[KMB]?", budget, re.I)]
    amounts = [a for a in amounts if a]
    if not amounts:
        return 15, ["yellow: no budget specified"]
    top = max(amounts)
    if (job.get("job_type") == "hourly") or "hourly" in budget.lower():
        ratio = top / my_rate
        flags = [f"red: max ${top:.0f}/hr is under half your rate"] if ratio < 0.5 else []
        return round(min(ratio, 1) * 25), flags
    hours = top / my_rate
    if hours >= 10:
        return 25, []
    if hours >= 5:
        return 20, []
    return 10, [f"yellow: fixed ${top:.0f} is under 5h of your rate"]


def score_competition(job: dict) -> tuple[int, list[str]]:
    """0-25: proposals tier (15), posting age (5), invites sent (5)."""
    flags: list[str] = []
    score = 0
    floor = _proposal_floor(job.get("proposals"))
    if floor is None:
        score += 8
    elif floor < 5:
        score += 15
        flags.append("green: under 5 proposals")
    else:
        score += 12 if floor < 10 else 8 if floor < 20 else 4 if floor < 50 else 0

    days = _posted_days(job.get("posted"))
    if days is None:
        score += 2
    else:
        score += 5 if days < 1 else 4 if days < 3 else 3 if days < 7 else 2 if days < 14 else 1
        if days < 1 / 12:
            flags.append("green: posted under 2h ago")

    m = re.search(r"invites sent:?\s*(\d+)", job.get("bid_range") or "", re.I)
    invites = int(m.group(1)) if m else None
    score += 3 if invites is None else 5 if invites == 0 else 3 if invites <= 3 else 1
    return min(score, 25), flags


def score_job(job: dict, my_rate: float | None = None, now: datetime | None = None) -> dict:
    """Score the three data pillars of a job (from get_job_details). Max 75."""
    c, cf = score_client(job.get("client"), now)
    b, bf = score_budget(job, my_rate)
    k, kf = score_competition(job)
    return {
        "client_quality": c,
        "budget_fit": b,
        "competition": k,
        "subtotal_75": c + b + k,
        "flags": cf + bf + kf,
        "how_to_total": "opportunity_score = subtotal_75 + round(fit_score / 4)",
    }


def opportunity_score(breakdown: dict, fit_score: int) -> int:
    """Combine the data pillars with a 0-100 skill-fit judgment into a 0-100 score."""
    return int(breakdown["subtotal_75"]) + round(max(0, min(fit_score, 100)) / 4)
