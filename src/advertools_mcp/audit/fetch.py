"""Sampled, capped network helpers for CRAWL+ checks (asset HEADs, body fetches).

Requests run concurrently on a bounded thread pool, but politeness comes first:
a process-wide per-host rate limiter paces requests to any single host at the
configured crawl speed (default 5 URLs/second), so the audit can never hammer
the target harder than the crawl itself is allowed to. Parallelism therefore
only speeds up fetches that span *different* hosts (site + CDN + external
assets); same-host fetches are serialised onto the polite schedule.

Every entry point is best-effort and degrades gracefully: if the network is
unavailable the caller receives ``None`` and reports "Not assessed" rather than
a false pass/fail. Total fetches are bounded by ``audit_url_sample``.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional, TypeVar
from urllib.parse import urlparse

import httpx

_TIMEOUT = httpx.Timeout(10.0)
_MAX_WORKERS = 10
DEFAULT_RATE = 5.0  # URLs/second/host, mirrors the crawl-speed default

T = TypeVar("T")

# Process-wide per-host schedule so pacing holds across audit checks, not just
# within one call. Maps host -> the monotonic time of its next free slot.
_host_lock = threading.Lock()
_host_next_slot: dict[str, float] = {}


def _acquire_host_slot(host: str, interval: float) -> None:
    """Block until this host's next polite slot, then claim it (thread-safe)."""
    if interval <= 0:
        return
    with _host_lock:
        now = time.monotonic()
        slot = max(_host_next_slot.get(host, now), now)
        _host_next_slot[host] = slot + interval
    wait = slot - now
    if wait > 0:
        time.sleep(wait)


def _fetch_many(
    urls: list[str],
    cap: int,
    user_agent: str,
    fetch_one: Callable[[httpx.Client, str], T],
    rate: float = DEFAULT_RATE,
) -> Optional[dict[str, T]]:
    """Run ``fetch_one`` over a deduplicated sample, paced per host.

    Returns ``{url: value}`` for the URLs that resolved, ``{}`` for an empty
    sample, or ``None`` when nothing at all resolved (treated as "network
    unavailable" by callers).
    """
    sample = list(dict.fromkeys(urls))[:cap]
    if not sample:
        return {}
    interval = 1.0 / rate if rate and rate > 0 else 0.0
    out: dict[str, T] = {}
    headers = {"User-Agent": user_agent}

    def paced(client: httpx.Client, url: str) -> Optional[T]:
        _acquire_host_slot(urlparse(url).netloc.lower(), interval)
        try:
            return fetch_one(client, url)
        except httpx.HTTPError:
            return None

    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=headers) as client:
            workers = min(_MAX_WORKERS, len(sample))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for url, result in zip(sample, pool.map(lambda u: paced(client, u), sample)):
                    if result is not None:
                        out[url] = result
    except Exception:  # noqa: BLE001
        return None
    return out if out else None


def head_statuses(
    urls: list[str], cap: int, user_agent: str, rate: float = DEFAULT_RATE
) -> Optional[dict[str, int]]:
    """HEAD a sample of URLs. Returns {url: status} or None if nothing resolved."""

    def one(client: httpx.Client, url: str) -> int:
        resp = client.head(url)
        if resp.status_code == 405:  # some servers reject HEAD
            resp = client.get(url)
        return resp.status_code

    return _fetch_many(urls, cap, user_agent, one, rate)


def fetch_bodies(
    urls: list[str], cap: int, user_agent: str, rate: float = DEFAULT_RATE
) -> Optional[dict[str, str]]:
    """GET a sample of URLs, returning {url: text}. None if nothing resolved."""

    def one(client: httpx.Client, url: str) -> str:
        return client.get(url).text

    return _fetch_many(urls, cap, user_agent, one, rate)
