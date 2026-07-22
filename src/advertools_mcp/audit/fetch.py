"""Sampled, capped network helpers for CRAWL+ checks (asset HEADs, body fetches).

Requests run concurrently on a bounded thread pool (httpx.Client is
thread-safe), so a 200-URL sample takes seconds rather than minutes. Every
entry point is best-effort and degrades gracefully: if the network is
unavailable the caller receives ``None`` and reports "Not assessed" rather than
a false pass/fail. Total fetches are bounded by ``audit_url_sample``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional, TypeVar

import httpx

_TIMEOUT = httpx.Timeout(10.0)
_MAX_WORKERS = 10

T = TypeVar("T")


def _fetch_many(
    urls: list[str], cap: int, user_agent: str, fetch_one: Callable[[httpx.Client, str], T]
) -> Optional[dict[str, T]]:
    """Run ``fetch_one`` over a deduplicated sample concurrently.

    Returns ``{url: value}`` for the URLs that resolved, ``{}`` for an empty
    sample, or ``None`` when nothing at all resolved (treated as "network
    unavailable" by callers).
    """
    sample = list(dict.fromkeys(urls))[:cap]
    if not sample:
        return {}
    out: dict[str, T] = {}
    headers = {"User-Agent": user_agent}
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=headers) as client:
            workers = min(_MAX_WORKERS, len(sample))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for url, result in zip(sample, pool.map(lambda u: _safe(fetch_one, client, u), sample)):
                    if result is not None:
                        out[url] = result
    except Exception:  # noqa: BLE001
        return None
    return out if out else None


def _safe(fetch_one: Callable[[httpx.Client, str], T], client: httpx.Client, url: str) -> Optional[T]:
    try:
        return fetch_one(client, url)
    except httpx.HTTPError:
        return None


def head_statuses(urls: list[str], cap: int, user_agent: str) -> Optional[dict[str, int]]:
    """HEAD a sample of URLs. Returns {url: status} or None if nothing resolved."""

    def one(client: httpx.Client, url: str) -> int:
        resp = client.head(url)
        if resp.status_code == 405:  # some servers reject HEAD
            resp = client.get(url)
        return resp.status_code

    return _fetch_many(urls, cap, user_agent, one)


def fetch_bodies(urls: list[str], cap: int, user_agent: str) -> Optional[dict[str, str]]:
    """GET a sample of URLs, returning {url: text}. None if nothing resolved."""

    def one(client: httpx.Client, url: str) -> str:
        return client.get(url).text

    return _fetch_many(urls, cap, user_agent, one)
