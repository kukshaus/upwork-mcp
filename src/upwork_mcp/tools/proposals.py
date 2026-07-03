"""Proposal tools for Upwork MCP."""

import asyncio
import re

from pydantic import BaseModel, Field
from ..browser.client import get_browser
from .. import tracker


class ProposalsParams(BaseModel):
    """Parameters for getting proposals."""
    status: str = Field(
        default="active",
        description="Filter by status: active, submitted, archived, or all"
    )
    limit: int = Field(default=20, ge=1, le=50, description="Maximum number of results")


class SubmitProposalParams(BaseModel):
    """Parameters for submitting a proposal."""
    job_url: str = Field(description="Full Upwork job URL")
    cover_letter: str = Field(description="Cover letter content")
    rate: float | None = Field(default=None, description="Proposed hourly rate (for hourly jobs)")
    bid: float | None = Field(default=None, description="Bid amount (for fixed-price jobs)")
    answers: list[str] | None = Field(default=None, description="Answers to screening questions")
    posted_age: str | None = Field(
        default=None,
        description="Job age at apply time, e.g. 'Posted 32 minutes ago' — recorded "
        "for time-to-apply analytics (applying <60min after posting is the single "
        "highest-leverage timing factor for view rates)",
    )
    estimated_duration: str = Field(
        default="1 to 3 months",
        description="Project duration for fixed-price jobs: 'Less than 1 month', "
        "'1 to 3 months', '3 to 6 months', or 'More than 6 months'",
    )


async def get_proposals(params: ProposalsParams) -> list[dict]:
    """Get your submitted proposals on Upwork.

    Returns a list of proposals with job title, status, bid amount, and dates.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    # Navigate to proposals page
    status_path = {
        "active": "active",
        "submitted": "submitted",
        "archived": "archived",
        "all": ""
    }.get(params.status.lower(), "active")

    url = f"https://www.upwork.com/nx/proposals/{'?status=' + status_path if status_path else ''}"
    await page.goto(url, wait_until="networkidle")

    proposals = []

    # Wait for proposals to load
    try:
        await page.wait_for_selector('[data-test="proposal-tile"], .proposal-row', timeout=10000)
    except Exception:
        # No proposals or different structure
        pass

    # Extract proposal cards
    proposal_els = await page.query_selector_all('[data-test="proposal-tile"], .proposal-row, article')

    for el in proposal_els[:params.limit]:
        try:
            proposal = await _extract_proposal(el)
            if proposal:
                proposals.append(proposal)
        except Exception:
            continue

    return proposals


async def _extract_proposal(el) -> dict | None:
    """Extract proposal data from element."""
    proposal = {}

    # Job title
    title_el = await el.query_selector('[data-test="job-title"], .job-title, a h3, h4')
    if title_el:
        proposal["job_title"] = (await title_el.text_content() or "").strip()
        href = await title_el.get_attribute("href")
        if href:
            proposal["job_url"] = href if href.startswith("http") else f"https://www.upwork.com{href}"

    if not proposal.get("job_title"):
        return None

    # Status
    status_el = await el.query_selector('[data-test="proposal-status"], .status-badge, .proposal-status')
    if status_el:
        proposal["status"] = (await status_el.text_content() or "").strip()

    # Bid/rate
    bid_el = await el.query_selector('[data-test="bid-amount"], .bid, .rate')
    if bid_el:
        proposal["bid"] = (await bid_el.text_content() or "").strip()

    # Submitted date
    date_el = await el.query_selector('[data-test="submitted-date"], .date, time')
    if date_el:
        proposal["submitted"] = (await date_el.text_content() or "").strip()

    # Client viewed
    viewed_el = await el.query_selector('[data-test="client-viewed"], .viewed')
    proposal["client_viewed"] = viewed_el is not None

    # Interview status
    interview_el = await el.query_selector('[data-test="interview-status"], .interview')
    if interview_el:
        proposal["interview_status"] = (await interview_el.text_content() or "").strip()

    # Connects used
    connects_el = await el.query_selector('[data-test="connects-used"], .connects')
    if connects_el:
        text = (await connects_el.text_content() or "").strip()
        import re
        numbers = re.findall(r'\d+', text)
        if numbers:
            proposal["connects_used"] = int(numbers[0])

    return proposal


async def get_proposal_details(proposal_url: str) -> dict:
    """Get detailed information about a specific proposal.

    Args:
        proposal_url: URL to the proposal

    Returns details including cover letter, bid, and any messages.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    await page.goto(proposal_url, wait_until="networkidle")

    details = {"url": proposal_url}

    # Job title
    title_el = await page.query_selector('[data-test="job-title"], h1, .job-title')
    if title_el:
        details["job_title"] = (await title_el.text_content() or "").strip()

    # Cover letter
    cover_el = await page.query_selector('[data-test="cover-letter"], .cover-letter')
    if cover_el:
        details["cover_letter"] = (await cover_el.text_content() or "").strip()

    # Bid/Rate
    bid_el = await page.query_selector('[data-test="bid-amount"], .bid-amount')
    if bid_el:
        details["bid"] = (await bid_el.text_content() or "").strip()

    # Status
    status_el = await page.query_selector('[data-test="proposal-status"], .status')
    if status_el:
        details["status"] = (await status_el.text_content() or "").strip()

    # Client response/messages
    messages = []
    message_els = await page.query_selector_all('[data-test="message"], .message-item')
    for el in message_els:
        msg_text = await el.text_content()
        if msg_text:
            messages.append(msg_text.strip())
    details["messages"] = messages

    return details


async def check_proposal_updates(limit: int = 20) -> dict:
    """Check submitted proposals for status changes since the last check.

    Opens each submitted proposal's detail page and reads its Insights panel
    (whether the client opened your proposal, plus the job's hiring activity:
    total / opened / shortlisted / messaged). Compares against the state saved
    on the previous run and reports what CHANGED — ideal for a daily check.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    # 1. List submitted proposals (id + title + detail URL).
    await page.goto("https://www.upwork.com/nx/proposals/", wait_until="domcontentloaded")
    for _ in range(12):
        await asyncio.sleep(2)
        body = await page.evaluate("() => document.body.innerText")
        if body and "ubmitted proposals" in body:
            break
    proposals = await page.evaluate(
        r"""() => {
            const seen = new Set(), out = [];
            for (const a of document.querySelectorAll("a[href*='/nx/proposals/']")) {
                const href = a.getAttribute('href') || '';
                const m = href.match(/\/nx\/proposals\/(\d{6,})/);
                if (!m) continue;
                const id = m[1];
                if (seen.has(id)) continue;
                seen.add(id);
                out.push({ id, title: (a.textContent || '').replace(/\s+/g, ' ').trim(), url: 'https://www.upwork.com/nx/proposals/' + id });
            }
            return out;
        }"""
    )
    proposals = proposals[:limit]

    results = []
    changes = []
    for p in proposals:
        try:
            await page.goto(p["url"], wait_until="domcontentloaded")
            state = None
            for _ in range(10):
                await asyncio.sleep(2)
                txt = await page.evaluate("() => document.body.innerText")
                if txt and ("proposal" in txt.lower()) and len(txt) > 200 and "verifying" not in txt.lower():
                    state = await page.evaluate(
                        r"""() => {
                            const b = document.body.innerText;
                            const openedNeg = /your proposal hasn'?t been opened yet/i.test(b);
                            const openedPos = /your proposal (was|has been) (opened|viewed)/i.test(b);
                            const m = b.match(/(\d+)\s+proposals?\s+(\d+)\s+unopened\s+(\d+)\s+opened\s+(\d+)\s+shortlisted\s+(\d+)\s+messaged/i);
                            const upd = b.match(/Updated\s+([A-Za-z]+ \d+,? \d+,?[^\n]{0,18}[AP]M)/);
                            return {
                                opened: openedPos ? true : (openedNeg ? false : null),
                                hiring: m ? {total:+m[1], unopened:+m[2], opened:+m[3], shortlisted:+m[4], messaged:+m[5]} : null,
                                interview: /interview|messaged you|sent you a message/i.test(b) && !/0\s+messaged/i.test(b) ? true : false,
                                updated: upd ? upd[1].trim() : null,
                            };
                        }"""
                    )
                    break
            if state is None:
                continue

            prev = tracker.get_proposal_state(p["id"])
            diff = []
            if prev:
                if prev.get("opened") != state.get("opened") and state.get("opened"):
                    diff.append("client OPENED your proposal")
                ph, ch = prev.get("hiring") or {}, state.get("hiring") or {}
                if ch.get("opened", 0) > ph.get("opened", 0):
                    diff.append(f"opened count {ph.get('opened',0)}→{ch.get('opened',0)}")
                if ch.get("shortlisted", 0) > ph.get("shortlisted", 0):
                    diff.append(f"shortlisted {ph.get('shortlisted',0)}→{ch.get('shortlisted',0)}")
                if ch.get("messaged", 0) > ph.get("messaged", 0):
                    diff.append(f"messaged {ph.get('messaged',0)}→{ch.get('messaged',0)}")
                if ch.get("total", 0) != ph.get("total", 0):
                    diff.append(f"total proposals {ph.get('total',0)}→{ch.get('total',0)}")
            else:
                diff.append("first check (baseline saved)")

            tracker.save_proposal_state(p["id"], p["title"], state)
            entry = {"title": p["title"], "url": p["url"], **state}
            if diff and diff != ["first check (baseline saved)"]:
                entry["changes"] = diff
                changes.append(entry)
            results.append(entry)
            await asyncio.sleep(3)  # pace to avoid rate-limiting
        except Exception:
            continue

    return {
        "checked": len(results),
        "with_changes": len(changes),
        "changes": changes,
        "all": results,
    }


def _apply_url_from_job(job_url: str) -> str | None:
    """Derive the proposal apply URL from a job URL.

    Job URLs end with the job id, e.g. ".../...~022071127945600288806/".
    The apply form lives at /nx/proposals/job/<id>/apply/.
    """
    m = re.search(r"~0[0-9a-z]+", job_url)
    if not m:
        return None
    return f"https://www.upwork.com/nx/proposals/job/{m.group(0)}/apply/"


async def submit_proposal(
    params: SubmitProposalParams, dry_run: bool = False, force: bool = False
) -> dict:
    """Submit a proposal to an Upwork job.

    IMPORTANT: This is a sensitive action that spends Connects and contacts a
    real client. Make sure the cover letter and rate/bid are correct first.

    Set dry_run=True to fill the form and report what *would* be submitted
    (connects, prefilled rate) WITHOUT clicking Send — used to verify safely.

    Duplicate guard: if a bid for this job is already recorded in the local
    tracker, returns status "already_applied" WITHOUT spending connects.
    Pass force=True to bypass the guard.

    Returns submission status and connects used.
    """
    # Duplicate guard — never bid on the same job twice.
    if not dry_run and not force and tracker.has_bid(params.job_url):
        prev = tracker.get_bid(params.job_url) or {}
        return {
            "status": "already_applied",
            "message": "A bid for this job is already tracked; skipped to avoid a duplicate. Pass force=True to override.",
            "previous": {
                "title": prev.get("title"),
                "amount": prev.get("amount"),
                "submitted_at": prev.get("submitted_at"),
            },
        }

    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    # Go straight to the apply form; fall back to clicking "Apply now".
    apply_url = _apply_url_from_job(params.job_url)
    if apply_url:
        await page.goto(apply_url, wait_until="domcontentloaded")
    else:
        await page.goto(params.job_url, wait_until="domcontentloaded")
        await asyncio.sleep(3)
        apply_btn = await page.query_selector("button:has-text('Apply now'), [data-test='apply-button']")
        if not apply_btn:
            return {"status": "error", "message": "Apply button not found. Job may be closed or already applied to."}
        await apply_btn.click()

    # Wait for the proposal form to render (cover-letter textarea is the anchor).
    try:
        await page.wait_for_selector("textarea", timeout=20000)
    except Exception:
        return {"status": "error", "message": f"Apply form did not load (url: {page.url})."}
    await asyncio.sleep(2)

    # --- Rate / bid -------------------------------------------------------
    # Fixed-price forms have payment radios ("By milestone" / "By project") and a
    # required duration dropdown; hourly forms have a single #step-rate field.
    amount = params.bid if params.bid is not None else params.rate
    is_fixed = await page.query_selector("input[type='radio'][value='default']") is not None

    if is_fixed:
        # Choose "By project" (single payment at the end) — the real radio is
        # hidden behind a styled card, so click its label text.
        try:
            await page.get_by_text("By project", exact=False).first.click(timeout=8000)
            await asyncio.sleep(1)
        except Exception:
            pass

        if amount is not None:
            bid_input = await page.query_selector(
                "input#charged-amount-id, input[data-test='currency-input']:not(#earned-amount-id):not(#fee-rate)"
            )
            if bid_input:
                await bid_input.click()
                await bid_input.fill("")
                await bid_input.type(str(amount))
                await page.keyboard.press("Tab")

        # Required: project duration (custom air3 dropdown). This can be flaky —
        # retry opening it and verify the toggle text actually changed.
        toggle = await page.query_selector("[data-test='dropdown-toggle']")
        if toggle:
            for _ in range(4):
                cur = (await toggle.text_content() or "").strip().lower()
                if params.estimated_duration.lower() in cur:
                    break
                try:
                    await toggle.click()
                    await asyncio.sleep(1.2)
                    # The real options are li[role='option'] (a decoy
                    # [data-test='fixed-duration'] li exists too). Match by exact
                    # accessible name via get_by_role — most reliable.
                    clicked = False
                    try:
                        await page.get_by_role(
                            "option", name=params.estimated_duration, exact=True
                        ).first.click(timeout=3500)
                        clicked = True
                    except Exception:
                        pass
                    if not clicked:
                        await page.locator(
                            f"li[role='option']:has-text('{params.estimated_duration}')"
                        ).first.click(timeout=3500)
                    await asyncio.sleep(1)
                except Exception:
                    await asyncio.sleep(0.5)
    else:
        # Hourly: single rate field (#step-rate), usually prefilled with the
        # profile rate. NOTE: some long-term hourly jobs additionally require a
        # binding "rate increase" schedule we do not set — those will fail
        # validation and return validation_errors rather than submitting.
        if amount is not None:
            rate_input = await page.query_selector(
                "input#step-rate, input[data-test='currency-input']:not(#fee-rate):not(#receive-step-rate)"
            )
            if rate_input:
                await rate_input.click()
                await rate_input.fill("")
                await rate_input.type(str(amount))
                await page.keyboard.press("Tab")

        # Some long-term hourly jobs require a binding "rate increase" schedule.
        # We decline it by selecting "Never" (no commitment) so the form validates.
        body = await page.evaluate("() => document.body.innerText")
        if "rate increase" in (body or "").lower() or "select a frequency" in (body or "").lower():
            toggle = await page.query_selector("[data-test='dropdown-toggle']")
            if toggle:
                for _ in range(3):
                    cur = (await toggle.text_content() or "").strip().lower()
                    if "never" in cur:
                        break
                    try:
                        await toggle.click()
                        await asyncio.sleep(1.2)
                        await page.get_by_text("Never", exact=True).first.click(timeout=4000)
                        await asyncio.sleep(1)
                    except Exception:
                        await asyncio.sleep(0.5)

    # --- Cover letter -----------------------------------------------------
    cover_textarea = await page.query_selector("textarea")
    if not cover_textarea:
        return {"status": "error", "message": "Cover letter field not found."}
    await cover_textarea.fill(params.cover_letter)

    # --- Screening question answers (if any) ------------------------------
    if params.answers:
        # Question answer fields are additional textareas after the cover letter.
        q_textareas = await page.query_selector_all("textarea")
        # index 0 is the cover letter; answers map to the rest
        for i, answer in enumerate(params.answers):
            idx = i + 1
            if idx < len(q_textareas):
                await q_textareas[idx].fill(answer)

    # --- Connects required ------------------------------------------------
    # Some jobs label the button "Send for N Connects"; others just "Submit
    # proposal" and show the cost in the page body. Try the button, then body.
    connects_required = 0
    btn_text = await page.evaluate(
        r"""() => {
            const b = [...document.querySelectorAll('button')]
                .find(e => /send for\s+\d+\s+connects/i.test(e.textContent || ''));
            return b ? b.textContent.replace(/\s+/g, ' ').trim() : null;
        }"""
    )
    if btn_text:
        nums = re.findall(r"\d+", btn_text)
        if nums:
            connects_required = int(nums[0])
    if not connects_required:
        body = await page.evaluate("() => document.body.innerText")
        m = re.search(r"(\d+)\s*Connects", body or "")
        if m:
            connects_required = int(m.group(1))

    if dry_run:
        filled = await page.evaluate(
            """() => ({
                hourly_rate: document.querySelector('#step-rate')?.value || null,
                fixed_bid: document.querySelector('#charged-amount-id')?.value || null,
                duration: document.querySelector("[data-test='dropdown-toggle']")?.textContent?.replace(/\\s+/g,' ').trim() || null,
            })"""
        )
        return {
            "status": "dry_run",
            "apply_url": page.url,
            "job_type": "fixed" if is_fixed else "hourly",
            "connects_required": connects_required,
            "amount_field": filled,
            "cover_letter_chars": len(params.cover_letter),
            "submit_button": btn_text,
            "message": "Form filled; NOT submitted (dry run).",
        }

    # --- Submit -----------------------------------------------------------
    # The submit control's label varies by job: "Send for N Connects" or
    # "Submit proposal". Try specific labels in order (avoid bare "Submit"
    # which could match unrelated controls).
    submit_btn = None
    for sel in (
        "button:has-text('Send for')",
        "button:has-text('Submit proposal')",
        "[data-test='submit-proposal']",
        "button:has-text('Send')",
    ):
        submit_btn = await page.query_selector(sel)
        if submit_btn:
            break
    if not submit_btn:
        return {"status": "error", "message": "Submit button not found."}

    await submit_btn.scroll_into_view_if_needed()
    await submit_btn.click()

    # Many jobs pop a confirmation dialog ("Continue to submit") instead of
    # submitting directly. Poll for it (it can take a few seconds to appear) and
    # click through. If the click already navigated (success), querying the DOM
    # can throw "Execution context was destroyed" — that's fine, handled below.
    for _ in range(6):
        await asyncio.sleep(1.5)
        try:
            if "/apply" not in page.url:
                break
            # Fixed-price jobs interpose a "3 things you need to know" dispute-info
            # dialog whose "Continue" button stays DISABLED until a required
            # "Yes, I understand." checkbox is ticked. Tick any unchecked, visible
            # checkbox in the dialog first, otherwise Continue is a no-op.
            await page.evaluate(
                r"""() => {
                    const scope = document.querySelector("[role='dialog'], .air3-modal") || document;
                    for (const cb of scope.querySelectorAll("input[type='checkbox']")) {
                        if (cb.offsetParent !== null && !cb.checked) cb.click();
                    }
                }"""
            )
            await asyncio.sleep(0.4)
            # The dialog may be [role='dialog'] OR .air3-modal — match the button
            # directly ("Continue to submit" only exists in this confirmation).
            confirm = await page.query_selector(
                "button:has-text('Continue to submit'), "
                ".air3-modal button:has-text('Continue'), "
                "[role='dialog'] button:has-text('Continue'), "
                ".air3-modal button:has-text('Submit'), "
                "[role='dialog'] button:has-text('Send')"
            )
            if confirm:
                await confirm.click()
                break
        except Exception:
            break  # navigation in progress = likely submitted; verified below

    # On success Upwork navigates AWAY from the /apply/ page. On failure it stays
    # on /apply/ and renders validation messages. (Note: the apply URL itself
    # contains "/proposals/", so we key off the "/apply" segment, not "proposals".)
    def _record():
        tracker.record_bid(
            url=params.job_url,
            job_type="fixed" if is_fixed else "hourly",
            amount=amount,
            connects_used=connects_required,
            status="submitted",
            cover_letter=params.cover_letter,
            posted_age=params.posted_age,
        )

    for _ in range(12):
        await asyncio.sleep(2)
        if "/apply" not in page.url:
            _record()
            return {
                "status": "submitted",
                "connects_used": connects_required,
                "url": page.url,
                "message": "Proposal submitted successfully.",
            }

    # Still on the form. Collect messages, but ignore the ever-present boost
    # widget upsell ("Buy more Connects…", "Your bid is set to N Connect") — those
    # are NOT validation errors and previously caused false "failed" reports.
    raw = await page.evaluate(
        r"""() => {
            const els = [...document.querySelectorAll("[role='alert'],[aria-invalid='true'],[class*='error' i],[class*='invalid' i],.air3-form-message")];
            const msgs = els.map(e => (e.textContent || '').replace(/\s+/g, ' ').trim())
                            .filter(t => t && t.length < 160);
            return [...new Set(msgs)];
        }"""
    )
    benign = ("buy more connects", "your bid is set to", "boost", "rank in 1st")
    errors = [m for m in raw if not any(b in m.lower() for b in benign)]
    if errors:
        return {
            "status": "error",
            "message": "Not submitted — form validation failed.",
            "validation_errors": errors,
        }
    # No real errors, only the benign boost text → the confirmation went through
    # (the modal was clicked). Record it and flag for verification.
    _record()
    return {
        "status": "likely_submitted",
        "connects_used": connects_required,
        "message": "No validation errors; confirmation clicked. Likely submitted — recorded in tracker. Verify on Upwork if unsure.",
    }


async def withdraw_proposal(proposal_url: str) -> dict:
    """Withdraw a submitted proposal.

    Args:
        proposal_url: URL to the proposal to withdraw

    Returns withdrawal status.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    await page.goto(proposal_url, wait_until="networkidle")

    # Find withdraw button
    withdraw_btn = await page.query_selector('[data-test="withdraw-button"], button:has-text("Withdraw")')
    if not withdraw_btn:
        return {"status": "error", "message": "Withdraw button not found. Proposal may already be closed."}

    await withdraw_btn.click()

    # Confirm withdrawal in modal
    confirm_btn = await page.query_selector('[data-test="confirm-withdraw"], button:has-text("Yes"), button:has-text("Confirm")')
    if confirm_btn:
        await confirm_btn.click()

    try:
        await page.wait_for_selector('[data-test="withdrawal-confirmed"], .success', timeout=10000)
        return {"status": "withdrawn", "message": "Proposal withdrawn successfully"}
    except Exception:
        return {"status": "unknown", "message": "Could not confirm withdrawal"}
