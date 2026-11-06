"""Tests for the deterministic opportunity scorer."""

from datetime import datetime

from upwork_mcp.scoring import opportunity_score, score_budget, score_client, score_competition, score_job

NOW = datetime(2026, 11, 6)

GREAT_CLIENT = {"payment_verified": True, "hires": "24 hires", "total_spent": "$50K+",
                "rating": "4.9", "member_since": "Mar 2, 2019"}
BAD_CLIENT = {"payment_verified": False, "total_spent": "$0", "member_since": "Oct 30, 2026"}


def test_client_great_vs_bad():
    assert score_client(GREAT_CLIENT, NOW)[0] == 25
    score, flags = score_client(BAD_CLIENT, NOW)
    assert score == 1
    assert "red: payment not verified" in flags and "red: 0 hires" in flags
    assert score_client(None, NOW)[0] == 0


def test_budget_hourly_and_fixed():
    assert score_budget({"budget": "$40.00-$80.00 Hourly", "job_type": "hourly"}, 43)[0] == 25
    assert score_budget({"budget": "$20.00-$35.00 Hourly", "job_type": "hourly"}, 43)[0] == 20
    low, flags = score_budget({"budget": "$10.00-$15.00 Hourly"}, 43)
    assert low == 9 and flags[0].startswith("red")
    assert score_budget({"budget": "$1,500.00", "job_type": "fixed"}, 43)[0] == 25
    assert score_budget({"budget": "$100", "job_type": "fixed"}, 43)[0] == 10
    assert score_budget({}, 43)[0] == 15
    assert score_budget({"budget": "$500"}, None)[0] == 15


def test_competition_tiers_and_age():
    fresh = {"proposals": "Less than 5", "posted": "Posted 32 minutes ago", "bid_range": "Invites sent: 0"}
    assert score_competition(fresh)[0] == 25
    crowded = {"proposals": "50+", "posted": "Posted 3 weeks ago", "bid_range": "Invites sent: 12"}
    assert score_competition(crowded)[0] == 0 + 1 + 1
    assert score_competition({"proposals": "10 to 15", "posted": "Posted yesterday"})[0] == 8 + 4 + 3


def test_score_job_and_total():
    job = {"client": GREAT_CLIENT, "budget": "$40.00-$80.00 Hourly", "job_type": "hourly",
           "proposals": "Less than 5", "posted": "Posted 1 hour ago"}
    b = score_job(job, my_rate=43, now=NOW)
    assert b["subtotal_75"] == b["client_quality"] + b["budget_fit"] + b["competition"]
    assert opportunity_score(b, 100) == b["subtotal_75"] + 25
    assert opportunity_score(b, 0) == b["subtotal_75"]
    assert opportunity_score(b, 250) == b["subtotal_75"] + 25  # clamped
