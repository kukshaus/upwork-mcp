"""In-memory protocol tests for the FastMCP server (no browser, no network)."""

import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from upwork_mcp import server, tracker

EXPECTED_TOOLS = {
    "upwork_search_jobs", "upwork_get_job_details", "upwork_get_best_matches",
    "upwork_get_my_profile", "upwork_get_connects_balance", "upwork_get_profile_stats",
    "upwork_get_proposals", "upwork_get_proposal_details", "upwork_submit_proposal",
    "upwork_withdraw_proposal", "upwork_get_messages", "upwork_get_conversation",
    "upwork_send_message", "upwork_get_unread_count", "upwork_get_contracts",
    "upwork_get_contract_details", "upwork_get_work_diary", "upwork_check_session",
    "upwork_check_proposal_updates", "upwork_list_bids", "upwork_check_already_applied",
    "upwork_close_session",
}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "bids.db")
    return tmp_path / "bids.db"


async def test_all_tools_registered():
    async with Client(server.mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert names == EXPECTED_TOOLS


async def test_server_info_version():
    async with Client(server.mcp) as c:
        info = c.server_info
    assert info.name == "upwork-mcp"
    assert info.version == server.__version__


async def test_spending_tools_marked_destructive():
    async with Client(server.mcp) as c:
        tools = {t.name: t for t in await c.list_tools()}
    for name in ("upwork_submit_proposal", "upwork_withdraw_proposal"):
        ann = tools[name].annotations
        assert ann.destructive_hint is True and ann.read_only_hint is False
    for name in ("upwork_search_jobs", "upwork_list_bids", "upwork_get_messages"):
        assert tools[name].annotations.read_only_hint is True


async def test_input_schema_keeps_constraints():
    async with Client(server.mcp) as c:
        tools = {t.name: t for t in await c.list_tools()}
    props = tools["upwork_search_jobs"].input_schema["properties"]
    assert props["limit"]["maximum"] == 50 and props["limit"]["default"] == 20
    assert tools["upwork_submit_proposal"].input_schema["required"] == ["job_url", "cover_letter"]


async def test_list_bids_returns_single_json_array(tmp_db):
    tracker.record_bid("https://www.upwork.com/jobs/~0aaa", title="A", amount=43)
    tracker.record_bid("https://www.upwork.com/jobs/~0bbb", title="B", amount=50)
    async with Client(server.mcp) as c:
        res = await c.call_tool("upwork_list_bids", {"limit": 10})
    # Whole list in the FIRST text block — what the watcher scripts parse.
    data = json.loads(res.content[0].text)
    assert {b["job_id"] for b in data} == {"~0aaa", "~0bbb"}


async def test_check_already_applied(tmp_db):
    tracker.record_bid("https://www.upwork.com/jobs/~0ccc", title="C")
    async with Client(server.mcp) as c:
        hit = (await c.call_tool("upwork_check_already_applied", {"job_url": "0ccc"})).data
        miss = (await c.call_tool("upwork_check_already_applied", {"job_url": "~0zzz"})).data
    assert hit["already_applied"] is True and hit["bid"]["title"] == "C"
    assert miss == {"already_applied": False, "bid": None}


async def test_out_of_range_arg_is_rejected(tmp_db):
    async with Client(server.mcp) as c:
        with pytest.raises(ToolError):
            await c.call_tool("upwork_list_bids", {"limit": 9999})


async def test_search_jobs_is_patchable(monkeypatch):
    """freelance-cloud swaps server.search_jobs for a Camoufox implementation."""
    seen = {}

    async def fake_search(params):
        seen["query"] = params.query
        return [{"title": "fake", "url": "https://www.upwork.com/jobs/~0f"}]

    monkeypatch.setattr(server, "search_jobs", fake_search)
    async with Client(server.mcp) as c:
        res = await c.call_tool("upwork_search_jobs", {"query": "mcp server", "limit": 5})
    assert seen["query"] == "mcp server"
    assert json.loads(res.content[0].text)[0]["title"] == "fake"


async def test_browser_tools_run_one_at_a_time(monkeypatch):
    """Parallel calls share one Chrome tab; overlapping navigations abort each other."""
    import asyncio
    active = {"now": 0, "max": 0}

    async def slow_search(params):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.05)
        active["now"] -= 1
        return [{"title": params.query}]

    monkeypatch.setattr(server, "search_jobs", slow_search)
    async with Client(server.mcp) as c:
        await asyncio.gather(*[c.call_tool("upwork_search_jobs", {"query": q}) for q in "abcd"])
    assert active["max"] == 1
