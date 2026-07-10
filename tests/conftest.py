"""Shared test fixtures: a local fixture HTTP server + helpers.

We crawl a loopback server rather than the live web. Scrapy's offsite middleware
filters loopback-with-port hosts, so crawls in tests pass ``TEST_CRAWL_SETTINGS``
to disable it (a legitimate use of the custom_settings passthrough).
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import socketserver
import threading
from pathlib import Path

import pytest

from advertools_mcp.config import Settings
from advertools_mcp.jobs import JobKind, JobManager, JobStore

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "site"

# Make loopback crawls fast and unfiltered.
TEST_CRAWL_SETTINGS = {
    "ROBOTSTXT_OBEY": False,
    "RETRY_ENABLED": False,
    "DOWNLOAD_TIMEOUT": 10,
    "LOG_LEVEL": "ERROR",
    "DOWNLOADER_MIDDLEWARES": {
        "scrapy.downloadermiddlewares.offsite.OffsiteMiddleware": None
    },
}


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass


@pytest.fixture(scope="session")
def site_server():
    handler = functools.partial(_QuietHandler, directory=str(FIXTURE_DIR))
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "crawls", max_concurrent_jobs=2)


def make_crawl_spec(urls, follow_links=False, extra=None):
    cs = dict(TEST_CRAWL_SETTINGS)
    if extra:
        cs.update(extra)
    return {
        "crawl": {
            "url_list": urls,
            "follow_links": follow_links,
            "allowed_domains": None,
            "include_url_regex": None,
            "exclude_url_regex": None,
            "css_selectors": None,
            "xpath_selectors": None,
            "custom_settings": cs,
        }
    }


async def await_job(manager: JobManager, job_id: str, timeout: float = 90.0):
    """Poll until the job reaches a terminal state."""
    loop = asyncio.get_event_loop()
    start = loop.time()
    while loop.time() - start < timeout:
        rec = manager.status(job_id)
        if rec and rec.state in {"done", "failed", "cancelled"}:
            return rec
        await asyncio.sleep(0.3)
    raise TimeoutError(f"Job {job_id} did not finish within {timeout}s")


async def call_tool(mcp, name: str, **kwargs):
    """Invoke a registered FastMCP tool and return its structured dict result."""
    res = await mcp.call_tool(name, kwargs)
    if isinstance(res, tuple):
        res = res[0]
    if isinstance(res, dict):
        return res
    block = res[0]
    return json.loads(block.text)
