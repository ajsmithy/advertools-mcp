"""RENDER tier end-to-end: real Playwright/Chromium against JS-heavy fixtures.

Skipped automatically when the browser stack is unavailable.
"""

import functools
import http.server
import socketserver
import threading
from pathlib import Path

import pandas as pd
import pytest

from advertools_mcp.audit import render
from advertools_mcp.audit.checks import CHECKS
from advertools_mcp.audit.context import AuditContext
from advertools_mcp.config import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "render_site"

playwright_missing = False
try:
    import playwright.sync_api  # noqa: F401
except ImportError:
    playwright_missing = True

pytestmark = pytest.mark.skipif(playwright_missing, reason="playwright not installed")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def render_results():
    handler = functools.partial(_QuietHandler, directory=str(FIXTURES))
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    pages = ["errors", "jsnav", "hidden", "overlay", "blank", "plain"]
    urls = {name: f"{base}/{name}.html" for name in pages}
    results = render.run_render_sample(
        list(urls.values()), user_agent="render-test", settle_ms=1200
    )
    httpd.shutdown()
    if results is None:
        pytest.skip("Chromium unavailable in this environment")
    return urls, results


def test_all_pages_rendered(render_results):
    urls, results = render_results
    assert set(results) == set(urls.values())
    assert all(r["ok"] for r in results.values())


def test_js_error_captured(render_results):
    urls, results = render_results
    assert any("boom" in e for e in results[urls["errors"]]["page_errors"])
    assert not results[urls["plain"]]["page_errors"]


def test_js_nav_and_injected_content_observed(render_results):
    urls, results = render_results
    js = results[urls["jsnav"]]
    assert js["nav_link_count"] >= 3
    assert js["rendered_text_len"] > 1000  # JS-injected content visible
    assert js["title_at_load"] == "Static Title"
    assert js["title_after_wait"] == "Rotated Title"


def test_hidden_and_pseudo_content_observed(render_results):
    urls, results = render_results
    hidden = results[urls["hidden"]]
    assert hidden["hidden_text_len"] >= 200
    assert hidden["pseudo_content_count"] >= 1


def test_overlay_detected(render_results):
    urls, results = render_results
    assert results[urls["overlay"]]["overlay"] is True
    assert results[urls["plain"]]["overlay"] is False


def test_checks_fire_end_to_end(render_results):
    urls, results = render_results
    # Static crawl side: tiny static text, no static nav links for jsnav.
    df = pd.DataFrame({
        "url": list(urls.values()),
        "status": [200] * len(urls),
        "body_text": ["Page with JS error Some visible content here for the render.",
                      "JS-powered page Tiny static text.",
                      "Visible heading x",
                      "Content under an interstitial Real page text sits below. SIGN UP NOW",
                      "",
                      "home Plain page Nothing suspicious here"],
        "nav_links_url": ["", "", "", "", "", f"{urls['plain']}"],
    })
    ctx = AuditContext(df=df, settings=Settings(), has_render=True, render_results=results)

    assert urls["errors"] in CHECKS[15](ctx).affected_urls          # JS error
    assert urls["jsnav"] in CHECKS[16](ctx).affected_urls           # JS-only nav
    assert urls["blank"] in CHECKS[21](ctx).affected_urls           # blank render
    assert urls["jsnav"] in CHECKS[22](ctx).affected_urls           # JS content
    assert urls["hidden"] in CHECKS[35](ctx).affected_urls          # ::before text
    assert urls["overlay"] in CHECKS[71](ctx).affected_urls         # interstitial
    assert urls["jsnav"] in CHECKS[75](ctx).affected_urls           # rotated title
    assert urls["hidden"] in CHECKS[90](ctx).affected_urls          # display:none
    # The clean page trips nothing.
    for num in (15, 16, 21, 22, 35, 71, 75, 90):
        assert urls["plain"] not in CHECKS[num](ctx).affected_urls, f"#{num} false positive"
