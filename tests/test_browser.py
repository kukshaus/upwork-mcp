"""Tests for browser client."""

import pytest
from pathlib import Path


@pytest.mark.asyncio
async def test_profile_dir_exists():
    """Test that profile directory can be created."""
    from upwork_mcp.browser.client import PROFILE_DIR

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    assert PROFILE_DIR.exists()


@pytest.mark.asyncio
async def test_browser_launch(browser):
    """Test that browser can launch."""
    page = await browser.start()
    assert page is not None


@pytest.mark.asyncio
async def test_browser_navigation(browser):
    """Test that browser can navigate."""
    page = await browser.start()
    await page.goto("https://www.upwork.com")
    title = await page.title()
    assert "Upwork" in title or "Work" in title


@pytest.mark.asyncio
async def test_browser_close(browser):
    """Test that browser closes cleanly."""
    await browser.start()
    await browser.close()
    assert browser._page is None
    assert browser._context is None


@pytest.mark.parametrize("targets,expect_open", [
    ([], True),                                        # Chrome alive, all tabs closed
    ([{"type": "service_worker"}], True),
    ([{"type": "page", "url": "about:blank"}], False),
])
def test_ensure_page_target(monkeypatch, targets, expect_open):
    """Zero page targets breaks connect_over_cdp; ensure_page_target opens one."""
    import io
    import json
    import urllib.request
    from upwork_mcp.browser import client

    calls = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        calls.append(url)
        return Resp(json.dumps(targets).encode() if url.endswith("/list") else b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert client.ensure_page_target() is expect_open
    assert any("/json/new" in u for u in calls) is expect_open
