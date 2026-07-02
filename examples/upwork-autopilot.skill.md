# Upwork Autopilot — example Claude Code skill

An example [Claude Code](https://docs.claude.com/en/docs/claude-code) skill/command that drives this MCP server end to end: search jobs, evaluate fit, and (with human approval) submit tailored proposals. Drop it in `.claude/commands/upwork-autopilot.md` and invoke with `/upwork-autopilot`.

It's intentionally conservative — **Triage mode is the default and never submits anything**; applying requires an explicit request and per-proposal approval.

## Modes

- **Triage / List** (default) — search, evaluate, produce a ranked shortlist. No proposals submitted.
- **Apply** — everything in Triage, plus draft and (after explicit per-proposal approval) submit.
- **Status-check** — call `upwork_check_proposal_updates` to report what changed on submitted proposals (opened / shortlisted / messaged). Read-only.

## Prerequisites

1. The `upwork` MCP server is configured and available.
2. `upwork_check_session` returns a valid session (else run `uv run upwork-mcp --login`).

## Workflow

1. **Load profile** — `upwork_get_my_profile`, `upwork_get_profile_stats`, `upwork_get_connects_balance`.
2. **Duplicate check** — `upwork_list_bids` / `upwork_check_already_applied`. `upwork_submit_proposal` also refuses duplicates automatically (keyed by the `~0…` job id) and spends no connects.
3. **Search** — build 3–5 targeted queries from the profile's concrete skills. Run them **one at a time with pauses** (rapid back-to-back searches trip Cloudflare rate-limiting and silently return 0 results). Dedupe by job id, not URL slug.
4. **Evaluate fit (1–10)** on skill match, budget fit, experience level, project clarity, and client quality. Disqualify already-applied, skill/budget mismatches, unverified low-history clients, and obvious scams. Only apply to 7+.
5. **Craft & submit (Apply mode only)** — reference specific job details, write like a human (short, warm, no buzzwords/AI tells), sign with the freelancer's name, and end by proposing a short call. Always show the proposal to the user before submitting.
6. **Report** — searches run, jobs found/evaluated/qualified, proposals submitted, top skip reasons.

## Operational notes

- **Search endpoint** scrapes `/nx/search/jobs/?q=...`; tiles hydrate client-side (`article[data-test='JobTile']`), so the tool polls before reading. A `0` result is more often rate-limiting than an empty market — retry once.
- **Submit form** lives at `/nx/proposals/job/<id>/apply/`. Fixed-price selects "By project", fills the bid, and picks a required duration; it then ticks the "Yes, I understand." checkbox on the confirmation dialog before Continue. Hourly fills the rate; some long-term hourly jobs require a binding rate-increase schedule the tool won't set (returns `validation_errors`). A real success redirects to `…?success` — **verify the outcome, don't trust a success flag alone.**
- Use `dry_run=True` to fill-and-report without sending; dry-run a new job type before the first real submit.

## Safety rules

1. Never submit a proposal without showing it to the user and getting approval.
2. Never spend more than ~50% of remaining connects in one session; stop if connects run low.
3. Always check for duplicate applications before submitting.
4. Stop and report if the session expires mid-run.
5. Only submit proposals — never accept/modify contracts or message clients without explicit approval.

> This is an example. Adapt the voice, rate strategy, and thresholds to your own profile. Sensitive actions spend real Connects and contact real clients — keep a human in the loop and follow Upwork's Terms of Service.
