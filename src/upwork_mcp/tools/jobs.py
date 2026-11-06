"""Job search and details tools for Upwork MCP."""

import re
import asyncio
import urllib.parse
from pydantic import BaseModel, Field
from ..browser.client import get_browser
from ..scoring import score_job


class JobSearchParams(BaseModel):
    """Parameters for job search."""
    query: str = Field(description="Search keywords")
    experience_level: str | None = Field(
        default=None,
        description="Experience level: entry, intermediate, or expert"
    )
    job_type: str | None = Field(
        default=None,
        description="Job type: hourly or fixed"
    )
    limit: int = Field(default=10, ge=1, le=50, description="Maximum number of results")


class JobDetailsParams(BaseModel):
    """Parameters for getting job details."""
    job_url: str = Field(description="Full Upwork job URL or job ID")
    my_rate: float | None = Field(default=None, description="Your hourly rate in USD, used for the budget-fit score")


async def get_best_matches(limit: int = 30) -> list[dict]:
    """Fetch Upwork's personalized "Best Matches" feed for the logged-in user.

    This is Upwork's own profile-based matching (`/nx/find-work/best-matches`),
    complementary to keyword search — it surfaces jobs the profile matches that
    a given query may miss. Returns the same shape as `search_jobs`
    (title, url, proposals, posted, budget, job_type, skills, description).

    The feed's tiles are `article[data-test='JobTile']` like search, but some
    child data-test hooks differ, so we fall back to parsing the tile's text.
    """
    browser = get_browser()
    page = await browser.get_page()

    tiles = []
    for _ in range(2):
        await page.goto(
            "https://www.upwork.com/nx/find-work/best-matches",
            wait_until="domcontentloaded",
        )
        for _ in range(22):  # poll ~45s for client-side hydration
            await asyncio.sleep(2)
            # The feed renders tiles as either article/section[data-test='JobTile']
            # or plain children of the job-tile list — query broadly.
            tiles = await page.query_selector_all(
                "article[data-test='JobTile'], section[data-test='JobTile'], "
                "[data-test='job-tile-list'] > section, [data-test='job-tile-list'] > article"
            )
            if tiles:
                break
        if tiles:
            break
        title = (await page.title() or "").lower()
        if "moment" in title:
            await asyncio.sleep(8)
        else:
            break

    _TIER = r"(Less than 5|Fewer than 5|5 to 10|10 to 15|15 to 20|20 to 50|50\+)"
    _POSTED = r"(?:Posted|last week|yesterday|\d+\s+(?:minute|hour|day|week)s?\s+ago)[^\n]*"
    _BUDGET = r"(Hourly:?\s*\$[\d.,\s\-–]+|Fixed[- ]price|Est\.?\s*budget:?\s*\$[\d.,]+|\$[\d,]+\.\d{2})"

    jobs = []
    for tile in tiles[:limit]:
        try:
            link = await tile.query_selector("h2 a, a[href*='/jobs/'], [data-test*='job-tile-title-link']")
            if not link:
                continue
            title = await link.text_content()
            href = await link.get_attribute("href")
            if not title or not href:
                continue
            job = {
                "title": " ".join(title.split()).strip(),
                "url": "https://www.upwork.com" + href.split("?")[0]
                if href.startswith("/") else href.split("?")[0],
                "source": "best_matches",
            }
            text = " ".join(((await tile.text_content()) or "").split())

            prop_el = await tile.query_selector("[data-test='proposals-tier']")
            if prop_el:
                job["proposals"] = (await prop_el.text_content() or "").strip()
            else:
                m = re.search(r"Proposals?:?\s*" + _TIER, text, re.I) or re.search(_TIER, text, re.I)
                if m:
                    job["proposals"] = m.group(1)

            posted_el = await tile.query_selector("[data-test='job-pubilshed-date'], [data-test*='posted']")
            if posted_el:
                job["posted"] = " ".join((await posted_el.text_content() or "").split()).strip()
            else:
                m = re.search(_POSTED, text, re.I)
                if m:
                    job["posted"] = m.group(0).strip()

            budget_el = await tile.query_selector("[data-test='is-fixed-price'], [data-test='is-hourly']")
            if budget_el:
                job["budget"] = " ".join((await budget_el.text_content() or "").split()).strip()
            else:
                m = re.search(_BUDGET, text, re.I)
                if m:
                    job["budget"] = m.group(1).strip()

            skills = [
                (await el.text_content() or "").strip()
                for el in await tile.query_selector_all("[data-test='token']")
            ]
            skills = [s for s in skills if s]
            if skills:
                job["skills"] = skills

            jobs.append(job)
        except Exception:
            continue

    return jobs


async def search_jobs(params: JobSearchParams) -> list[dict]:
    """Search for jobs on Upwork matching the specified criteria.

    Returns a list of job summaries with title, budget, and URL.
    """
    browser = get_browser()
    page = await browser.get_page()

    # Build search URL — use the dedicated job search page (the best-matches
    # feed ignores the query). Job tiles render as article[data-test='JobTile'].
    base_url = "https://www.upwork.com/nx/search/jobs/"
    query_params = {"q": params.query}

    if params.job_type:
        query_params["t"] = "0" if params.job_type.lower() == "hourly" else "1"

    if params.experience_level:
        level_map = {"entry": "1", "intermediate": "2", "expert": "3"}
        level = level_map.get(params.experience_level.lower())
        if level:
            query_params["contractor_tier"] = level

    url = f"{base_url}?{urllib.parse.urlencode(query_params)}"

    # The search page is client-side rendered and sits behind Cloudflare. Tile
    # hydration time varies widely (sometimes >15s). Navigate once, then poll for
    # tiles up to ~45s rather than reloading — a reload just restarts the slow
    # render. Only re-navigate if a Cloudflare "Just a moment..." challenge shows.
    tiles = []
    for nav in range(2):
        await page.goto(url, wait_until="domcontentloaded")

        for _ in range(22):  # poll ~45s
            await asyncio.sleep(2)
            tiles = await page.query_selector_all("article[data-test='JobTile']")
            if tiles:
                break

        if tiles:
            break

        # Still nothing — if it's a Cloudflare interstitial, wait and re-navigate.
        title = (await page.title() or "").lower()
        if "moment" in title:
            await asyncio.sleep(8)
        else:
            break

    jobs = []

    for tile in tiles[:params.limit]:
        try:
            job = {}

            # Title + URL from the title link
            title_link = await tile.query_selector("h2 a, [data-test*='job-tile-title-link']")
            if not title_link:
                continue

            title = await title_link.text_content()
            href = await title_link.get_attribute("href")
            if not title or not href:
                continue

            # Strip query string from href for a clean job URL
            clean_href = href.split("?")[0]
            job["title"] = " ".join(title.split()).strip()
            job["url"] = (
                f"https://www.upwork.com{clean_href}"
                if clean_href.startswith("/")
                else clean_href
            )

            # Description snippet
            desc_el = await tile.query_selector("[data-test*='JobDescription']")
            if desc_el:
                desc = await desc_el.text_content()
                if desc:
                    job["description"] = " ".join(desc.split()).strip()[:300]

            # Budget / job type / experience — JobInfo holds the combined line,
            # e.g. "Fixed price Intermediate Est. budget: $70.00"
            info_el = await tile.query_selector("[data-test='JobInfo']")
            if info_el:
                job["info"] = " ".join((await info_el.text_content() or "").split()).strip()

            type_el = await tile.query_selector("[data-test='job-type-label']")
            if type_el:
                job["job_type"] = (await type_el.text_content() or "").strip()

            budget_el = await tile.query_selector(
                "[data-test='is-fixed-price'], [data-test='is-hourly']"
            )
            if budget_el:
                job["budget"] = " ".join((await budget_el.text_content() or "").split()).strip()

            # Skills/tokens
            skill_els = await tile.query_selector_all("[data-test='token']")
            skills = []
            for el in skill_els:
                text = (await el.text_content() or "").strip()
                if text:
                    skills.append(text)
            if skills:
                job["skills"] = skills

            # Posted date + proposals tier
            posted_el = await tile.query_selector("[data-test='job-pubilshed-date']")
            if posted_el:
                job["posted"] = " ".join((await posted_el.text_content() or "").split()).strip()

            proposals_el = await tile.query_selector("[data-test='proposals-tier']")
            if proposals_el:
                job["proposals"] = (await proposals_el.text_content() or "").strip()

            jobs.append(job)

        except Exception:
            continue

    return jobs


_DETAILS_JS = r"""
() => {
  const txt = s => { const e = document.querySelector(s); return e ? e.innerText.trim() : null; };
  const flat = t => t.split('\n').map(x => x.trim()).filter(Boolean).join(' | ');
  const leaves = [...document.querySelectorAll('span,div,li,strong,p,small')];
  const find = (pred) => { const e = leaves.find(el => el.innerText && pred(el.innerText.trim())); return e ? e.innerText.trim() : null; };
  const title = txt('header h4') || txt('h4') || '';
  const features = [...document.querySelectorAll('ul.features li, .features li')].map(e => flat(e.innerText)).filter(Boolean);
  const skills = [...new Set([...document.querySelectorAll('[data-test=proposals-insights-v2-skill-chip], .air3-token')].map(e => e.innerText.trim()).filter(Boolean))];
  const connectsLine = find(t => /^Send a proposal for:\s*\d+\s*Connects/i.test(t) && t.length < 120);
  const availLine = find(t => /^Available Connects:\s*\d+/i.test(t) && t.length < 60);
  const posted = find(t => /^Posted\b/.test(t) && t.length < 60);
  const proposals = find(t => /^Proposals:?/i.test(t) && t.length < 60);
  const questions = [...document.querySelectorAll('[data-test=Questions] li, .questions li')].map(e => e.innerText.trim()).filter(Boolean);
  return {
    title, description: txt('[data-test=Description]'),
    client_text: txt('[data-test=about-client-container]'),
    activity_text: txt('[data-test=proposals-insights-v2-job-activity-section]'),
    features, skills, connectsLine, availLine, posted, proposals, questions,
    page_title: document.title,
  };
}
"""

_JUNK_TITLES = ("footer navigation", "looking for something", "skip to content", "")


async def get_job_details(params: JobDetailsParams) -> dict:
    """Get detailed information about a specific Upwork job posting.

    Returns description, budget/rate, experience level, duration, skills,
    client stats, connects required, posted age and screening questions.
    Waits for the job description to render (the SPA can show page chrome
    first) and returns ``{"error": "job_details_unavailable"}`` instead of
    junk when the job content never appears.
    """
    browser = get_browser()
    page = await browser.get_page()

    url = params.job_url
    if not url.startswith("http"):
        url = f"https://www.upwork.com/jobs/{url}"

    data: dict = {}
    for attempt in range(2):
        await page.goto(url, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector("[data-test='Description']", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(1.5)
        data = await page.evaluate(_DETAILS_JS)
        if data.get("description") and (data.get("title") or "").strip().lower() not in _JUNK_TITLES:
            break
        await asyncio.sleep(3)

    if not data.get("description"):
        return {
            "url": url,
            "error": "job_details_unavailable",
            "page_title": data.get("page_title"),
            "hint": "Job content did not render (removed/private job, or not logged in).",
        }

    job: dict = {"url": url, "title": data.get("title") or (data.get("page_title") or "").split(" - ")[0]}
    desc = data["description"]
    if desc.startswith("Summary"):
        desc = desc[len("Summary"):].lstrip()
    job["description"] = desc

    features = data.get("features") or []
    for f in features:
        low = f.lower()
        if "$" in f and not job.get("budget") and not re.search(r"spent|hires?\b|avg|member since", low):
            job["budget"] = re.sub(r"\s*\|\s*", " ", f).replace(" - ", "-").strip()
            if "hourly" in low:
                job["job_type"] = "hourly"
            elif "fixed" in low:
                job["job_type"] = "fixed"
        elif low.endswith("| hourly") and not job.get("job_type"):
            job["job_type"] = "hourly"
            job["hours_per_week"] = f.split("|")[0].strip()
        elif "duration" in low or "project length" in low:
            job["project_length"] = f.split("|")[0].strip()
        elif any(x in low for x in ("entry level", "intermediate", "expert")) and not job.get("experience_level"):
            job["experience_level"] = f.split("|")[0].strip()
    if not job.get("job_type") and "fixed" in " ".join(features).lower():
        job["job_type"] = "fixed"

    if data.get("skills"):
        job["skills"] = data["skills"][:15]
    if data.get("posted"):
        job["posted"] = data["posted"].splitlines()[0].strip()
    if data.get("proposals"):
        job["proposals"] = " ".join(data["proposals"].replace("Proposals", "").replace(":", "").split()).strip("()")
    if data.get("questions"):
        job["screening_questions"] = data["questions"]
    if data.get("activity_text"):
        job["bid_range"] = " ".join(data["activity_text"].split())

    m = re.search(r"(\d+)\s*Connects", data.get("connectsLine") or "")
    if m:
        job["connects_required"] = int(m.group(1))
    m = re.search(r"(\d+)", data.get("availLine") or "")
    if m:
        job["connects_available"] = int(m.group(1))

    ct = data.get("client_text") or ""
    if ct:
        client: dict = {"payment_verified": "payment method verified" in ct.lower()}
        for key, pat in (
            ("rating", r"Rating is ([\d.]+) out of 5"),
            ("total_spent", r"(\$[\d.,]+[KMB]?\+?) total spent"),
            ("hire_rate", r"(\d+%) hire rate"),
            ("jobs_posted", r"(\d+) jobs? posted"),
            ("hires", r"(\d+) hires?"),
            ("avg_hourly_paid", r"(\$[\d.,]+) ?/hr avg"),
            ("member_since", r"Member since (.+)"),
        ):
            mm = re.search(pat, ct)
            if mm:
                client[key] = mm.group(1).strip()
        lines = [l.strip() for l in ct.splitlines() if l.strip()]
        for i2, l in enumerate(lines):
            if re.search(r"\d+ of \d+ reviews", l) and i2 + 1 < len(lines):
                client["country"] = lines[i2 + 1]
                break
        job["client"] = client

    job["score_breakdown"] = score_job(job, my_rate=params.my_rate)
    return job
