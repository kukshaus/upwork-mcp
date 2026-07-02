"""Job search and details tools for Upwork MCP."""

import re
import asyncio
import urllib.parse
from pydantic import BaseModel, Field
from ..browser.client import get_browser


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


async def get_job_details(params: JobDetailsParams) -> dict:
    """Get detailed information about a specific Upwork job posting.

    Returns comprehensive job details including description, client history,
    skills required, and application requirements.
    """
    browser = get_browser()
    page = await browser.get_page()

    # Normalize URL
    url = params.job_url
    if not url.startswith("http"):
        url = f"https://www.upwork.com/jobs/{url}"

    await page.goto(url, wait_until="networkidle")
    await asyncio.sleep(3)

    job = {"url": url}

    # Title
    title_el = await page.query_selector("h1, h2")
    if title_el:
        job["title"] = (await title_el.text_content() or "").strip()

    # Full description
    desc_el = await page.query_selector("[data-test='description'], .description, article p")
    if desc_el:
        job["description"] = (await desc_el.text_content() or "").strip()

    # Get all text blocks to find budget, experience, etc.
    all_text = await page.query_selector_all("p, span, div")
    for el in all_text:
        text = await el.text_content()
        if not text:
            continue
        text = text.strip()

        # Budget
        if "$" in text and len(text) < 50 and not job.get("budget"):
            job["budget"] = text

        # Experience level
        if any(x in text.lower() for x in ["entry level", "intermediate", "expert"]):
            if not job.get("experience_level"):
                job["experience_level"] = text

        # Project length
        if any(x in text.lower() for x in ["less than", "1 to 3", "3 to 6", "more than"]):
            if "month" in text.lower() and not job.get("project_length"):
                job["project_length"] = text

    # Skills
    skill_els = await page.query_selector_all("[class*='skill'], [class*='token'], button")
    skills = []
    for el in skill_els[:15]:
        text = await el.text_content()
        if text and 2 < len(text.strip()) < 30:
            skills.append(text.strip())
    if skills:
        job["skills"] = list(set(skills))[:10]

    # Client info
    client = {}
    client_section = await page.query_selector("[data-test='client-info'], [class*='client']")
    if client_section:
        client_text = await client_section.text_content()
        if client_text:
            # Look for location, rating, etc.
            if "Payment" in client_text and "verified" in client_text.lower():
                client["payment_verified"] = True
            # Extract spending info
            spent_match = re.search(r"\$[\d,]+[KMB]?\+?\s*(spent|total)", client_text, re.I)
            if spent_match:
                client["total_spent"] = spent_match.group(0)

    if client:
        job["client"] = client

    # Connects required
    connects_els = await page.query_selector_all("span, div")
    for el in connects_els:
        text = await el.text_content()
        if text and "connect" in text.lower():
            numbers = re.findall(r"\d+", text)
            if numbers:
                job["connects_required"] = int(numbers[0])
                break

    return job
