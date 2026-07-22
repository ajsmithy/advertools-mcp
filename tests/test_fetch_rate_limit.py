"""Politeness guarantees: audit fetches never exceed the per-host rate cap."""

import functools
import http.server
import socketserver
import threading
import time

from advertools_mcp.audit import fetch
from advertools_mcp.server import _crawl_custom_settings


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _serve(directory):
    handler = functools.partial(_QuietHandler, directory=str(directory))
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def test_same_host_fetches_are_paced_at_rate(tmp_path):
    (tmp_path / "x.txt").write_text("ok")
    httpd, port = _serve(tmp_path)
    try:
        # 8 URLs on ONE host at 5/s: slots at 0.0,0.2,...,1.4 -> must take >= ~1.4s
        urls = [f"http://127.0.0.1:{port}/x.txt?i={i}" for i in range(8)]
        start = time.monotonic()
        res = fetch.head_statuses(urls, cap=200, user_agent="t", rate=5.0)
        elapsed = time.monotonic() - start
        assert res is not None and len(res) == 8
        assert elapsed >= 1.2, f"same-host fetches ran too fast ({elapsed:.2f}s) — cap not enforced"
    finally:
        httpd.shutdown()


def test_different_hosts_still_parallel(tmp_path):
    (tmp_path / "x.txt").write_text("ok")
    servers = [_serve(tmp_path) for _ in range(4)]
    try:
        # 4 URLs across FOUR hosts at 5/s each: no shared schedule -> fast.
        urls = [f"http://127.0.0.1:{port}/x.txt" for _, port in servers]
        start = time.monotonic()
        res = fetch.head_statuses(urls, cap=200, user_agent="t", rate=5.0)
        elapsed = time.monotonic() - start
        assert res is not None and len(res) == 4
        assert elapsed < 1.0, f"cross-host fetches were serialised ({elapsed:.2f}s)"
    finally:
        for httpd, _ in servers:
            httpd.shutdown()


def test_crawl_delay_is_a_hard_cap():
    cs = _crawl_custom_settings(
        max_pages=100, max_depth=3, concurrent_requests=6,
        download_delay=0.2, obey_robots=True, user_agent="ua", passthrough=None,
    )
    # 5 URLs/s must be a ceiling, not an average: fixed delay, one request at a
    # time per host, and AutoThrottle can only slow things further.
    assert cs["DOWNLOAD_DELAY"] == 0.2
    assert cs["RANDOMIZE_DOWNLOAD_DELAY"] is False
    assert cs["CONCURRENT_REQUESTS_PER_DOMAIN"] == 1
    assert cs["AUTOTHROTTLE_ENABLED"] is True
