"""Acceptance: the clarifying-confirmation path fires on each trigger."""

import pytest

from advertools_mcp.config import Settings
from advertools_mcp.server import build_server
from advertools_mcp.validation import evaluate_crawl_config

from conftest import await_job, call_tool, TEST_CRAWL_SETTINGS


def _base(**over):
    cfg = {
        "url_list": ["https://example.com/"],
        "follow_links": True,
        "allowed_domains": ["example.com"],
        "max_pages": 100,
        "max_depth": 3,
        "concurrent_requests": 6,
        "download_delay": 0.25,
        "obey_robots": True,
        "user_agent": "ua",
    }
    cfg.update(over)
    return cfg


def test_open_ended_crawl_triggers():
    s = Settings()
    w = evaluate_crawl_config(_base(max_pages=None, max_depth=None), s)
    assert any(x["code"] == "open_ended_crawl" for x in w)


def test_robots_disabled_triggers():
    s = Settings()
    w = evaluate_crawl_config(_base(obey_robots=False), s)
    assert any(x["code"] == "robots_disabled" for x in w)


def test_aggressive_throttle_triggers():
    s = Settings()
    w = evaluate_crawl_config(_base(download_delay=0, concurrent_requests=20), s)
    assert any(x["code"] == "aggressive_throttle" for x in w)


def test_no_allowed_domains_triggers():
    s = Settings()
    w = evaluate_crawl_config(_base(allowed_domains=None), s)
    assert any(x["code"] == "no_allowed_domains" for x in w)


def test_remote_outside_allowlist_triggers():
    s = Settings(transport="http", domain_allowlist=["allowed.com"], remote_auth_token="t")
    w = evaluate_crawl_config(_base(allowed_domains=["example.com"]), s)
    assert any(x["code"] == "outside_allowlist" for x in w)


def test_safe_config_no_triggers():
    s = Settings()
    assert evaluate_crawl_config(_base(), s) == []


@pytest.mark.asyncio
async def test_start_crawl_returns_needs_confirmation(settings):
    mcp = build_server(settings)
    # Discovery crawl with no bounds and robots off -> must not launch.
    res = await call_tool(
        mcp,
        "start_crawl",
        url="https://example.com/",
        follow_links=True,
        obey_robots=False,
        max_pages=None,
        max_depth=None,
    )
    assert res["status"] == "needs_confirmation"
    codes = {w["code"] for w in res["warnings"]}
    assert "robots_disabled" in codes
    assert "resolved_config" in res
    # No job should have been created.
    assert "job_id" not in res


@pytest.mark.asyncio
async def test_confirm_true_launches(settings, site_server):
    mcp = build_server(settings)
    from advertools_mcp import server as srv

    # obey_robots=False triggers a warning; confirm=True bypasses the gate and runs.
    res = await call_tool(
        mcp,
        "start_crawl",
        url=f"{site_server}/index.html",
        follow_links=False,
        obey_robots=False,
        max_pages=10,
        confirm=True,
        custom_settings=TEST_CRAWL_SETTINGS,
    )
    assert res["status"] == "started"
    assert res["job_id"].startswith("crawl-")
    # Drive it to completion so no subprocess lingers past the test.
    rec = await await_job(srv._manager, res["job_id"])
    assert rec.state == "done", rec.error_message
