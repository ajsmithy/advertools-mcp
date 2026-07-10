"""Sampled, capped network helpers for CRAWL+ checks (asset HEADs, body fetches).

Every entry point is best-effort and degrades gracefully: if the network is
unavailable the caller receives ``None`` and reports "Not assessed" rather than a
false pass/fail. Total fetches are bounded by ``audit_url_sample``.
"""

from __future__ import annotations

from typing import Optional

import httpx

_TIMEOUT = httpx.Timeout(10.0)


def head_statuses(urls: list[str], cap: int, user_agent: str) -> Optional[dict[str, int]]:
    """HEAD a sample of URLs. Returns {url: status} or None if nothing resolved."""
    sample = list(dict.fromkeys(urls))[:cap]
    if not sample:
        return {}
    out: dict[str, int] = {}
    resolved_any = False
    headers = {"User-Agent": user_agent}
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=headers) as client:
            for url in sample:
                try:
                    resp = client.head(url)
                    if resp.status_code == 405:  # some servers reject HEAD
                        resp = client.get(url)
                    out[url] = resp.status_code
                    resolved_any = True
                except httpx.HTTPError:
                    continue
    except Exception:  # noqa: BLE001
        return None
    return out if resolved_any else None


def fetch_bodies(urls: list[str], cap: int, user_agent: str) -> Optional[dict[str, str]]:
    """GET a sample of URLs, returning {url: text}. None if nothing resolved."""
    sample = list(dict.fromkeys(urls))[:cap]
    if not sample:
        return {}
    out: dict[str, str] = {}
    resolved_any = False
    headers = {"User-Agent": user_agent}
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=headers) as client:
            for url in sample:
                try:
                    resp = client.get(url)
                    out[url] = resp.text
                    resolved_any = True
                except httpx.HTTPError:
                    continue
    except Exception:  # noqa: BLE001
        return None
    return out if resolved_any else None
