"""Profile and connects tools for Upwork MCP."""

import asyncio
import re

from ..browser.client import get_browser


async def _wait_rendered(page, needle: str | None = None, tries: int = 12) -> str:
    """Poll until the page has meaningful rendered text (SPA hydration)."""
    text = ""
    for _ in range(tries):
        await asyncio.sleep(2)
        text = await page.evaluate("() => document.body.innerText")
        if text and len(text) > 150 and "can't find this page" not in text:
            if needle is None or needle in text:
                break
    return text or ""


async def _resolve_profile_url(page) -> str | None:
    """Find the logged-in freelancer's own profile URL (/freelancers/~<id>)."""
    await page.goto("https://www.upwork.com/nx/find-work/best-matches", wait_until="domcontentloaded")
    await _wait_rendered(page)
    href = await page.evaluate(
        """() => {
            const a = document.querySelector("a[href*='/freelancers/~']");
            return a ? a.getAttribute('href') : null;
        }"""
    )
    if not href:
        # Fallback: the custom profile URL is printed on the settings page
        await page.goto("https://www.upwork.com/freelancers/settings/profile", wait_until="domcontentloaded")
        body = await _wait_rendered(page)
        m = re.search(r"/freelancers/~[0-9a-f]+", body)
        href = m.group(0) if m else None
    if not href:
        return None
    href = href.split("?")[0]
    return href if href.startswith("http") else f"https://www.upwork.com{href}"


async def get_my_profile() -> dict:
    """Get your Upwork freelancer profile information.

    Returns profile data including name, title, hourly rate, availability,
    location, overview, and skill tags.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    profile_url = await _resolve_profile_url(page)
    if not profile_url:
        return {"error": "Could not locate profile URL while logged in."}

    profile: dict = {"profile_url": profile_url}

    await page.goto(profile_url, wait_until="domcontentloaded")
    text = await _wait_rendered(page, needle="/hr")

    # Name / location come from microdata; fall back to the <title> tag.
    micro = await page.evaluate(
        """() => {
            const g = p => document.querySelector(`[itemprop='${p}']`)?.textContent?.trim() || null;
            return { name: g('name'), locality: g('locality'), country: g('country-name') };
        }"""
    )

    # <title> format: "{name} - {title} - Upwork Freelancer from {location}"
    title_tag = await page.title()
    tm = re.match(r"^(.*?) - (.*?) - Upwork Freelancer from (.*?)$", title_tag or "")

    profile["name"] = micro.get("name") or (tm.group(1).strip() if tm else None)

    if micro.get("locality") and micro.get("country"):
        profile["location"] = f"{micro['locality']}, {micro['country']}"
    elif tm:
        profile["location"] = tm.group(3).strip()

    if tm:
        profile["title"] = tm.group(2).strip()

    # Hourly rate, e.g. "$43.00/hr"
    rate = re.search(r"\$\d+(?:\.\d+)?\s*/\s*hr", text)
    if rate:
        profile["hourly_rate"] = rate.group(0).replace(" ", "")

    # Availability badge
    if "Available now" in text:
        profile["availability"] = "Available now"
    elif "Offline" in text:
        profile["availability"] = "Offline"

    # Job Success Score (absent for newer freelancers)
    jss = re.search(r"(\d+)%\s*Job Success", text)
    if jss:
        profile["job_success_score"] = f"{jss.group(1)}%"

    # Overview / bio — the longest line-clamped block on the profile
    overview = await page.evaluate(
        """() => {
            let best = '';
            document.querySelectorAll("[class*='line-clamp']").forEach(e => {
                const t = (e.textContent || '').replace(/\\s+/g, ' ').trim();
                if (t.length > best.length) best = t;
            });
            return best;
        }"""
    )
    if overview and len(overview) > 40:
        profile["overview"] = overview.strip()

    # Skills
    skills = await page.evaluate(
        """() => [...new Set(
            [...document.querySelectorAll('.skill-name')]
                .map(e => (e.textContent || '').replace(/\\s+/g, ' ').trim())
                .filter(Boolean)
        )]"""
    )
    profile["skills"] = skills

    # Connects balance (separate page)
    await asyncio.sleep(3)
    profile["connects"] = await get_connects_balance()

    return profile


async def get_connects_balance() -> dict:
    """Get current Upwork Connects balance.

    Returns the number of available connects.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    connects: dict = {}

    # The Connects History page shows the balance as e.g. "110 Connects".
    await page.goto("https://www.upwork.com/nx/plans/connects/history", wait_until="domcontentloaded")
    await _wait_rendered(page, needle="Connects")

    balance = await page.evaluate(
        """() => {
            const els = [...document.querySelectorAll('h1,h2,h3,strong,span,div')];
            for (const e of els) {
                const t = (e.textContent || '').replace(/\\s+/g, ' ').trim();
                if (/^[\\d,]+\\s+Connects$/i.test(t) && e.querySelectorAll('*').length <= 1) {
                    return t;
                }
            }
            return null;
        }"""
    )

    # Fallback: the Connects Hub shows the number in a .title-large element.
    if not balance:
        await asyncio.sleep(2)
        await page.goto("https://www.upwork.com/nx/plans/connects/", wait_until="domcontentloaded")
        await _wait_rendered(page)
        balance = await page.evaluate(
            """() => {
                const e = document.querySelector('.title-large');
                return e ? (e.textContent || '').trim() : null;
            }"""
        )

    if balance:
        numbers = re.findall(r"\d[\d,]*", balance)
        if numbers:
            connects["available"] = int(numbers[0].replace(",", ""))

    return connects


async def get_profile_stats() -> dict:
    """Get profile statistics including earnings and work history.

    Returns stats like job success score, total earnings, hours worked, and
    jobs completed. Newer freelancers may have few or none of these.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    profile_url = await _resolve_profile_url(page)
    if not profile_url:
        return {"error": "Could not locate profile URL while logged in."}

    await page.goto(profile_url, wait_until="domcontentloaded")
    text = await _wait_rendered(page, needle="/hr")

    stats: dict = {}

    jss = re.search(r"(\d+)%\s*Job Success", text)
    if jss:
        stats["job_success_score"] = f"{jss.group(1)}%"

    earnings = re.search(r"\$[\d.,]+[KMB]?\+?\s*(?:total earnings|earned|Total earned)", text, re.I)
    if earnings:
        stats["total_earnings"] = earnings.group(0).strip()

    hours = re.search(r"([\d,]+)\s*(?:total hours|hours worked)", text, re.I)
    if hours:
        stats["total_hours"] = hours.group(1).strip()

    jobs = re.search(r"([\d,]+)\s*(?:total jobs|jobs? completed|completed jobs)", text, re.I)
    if jobs:
        stats["jobs_completed"] = jobs.group(1).strip()

    return stats
