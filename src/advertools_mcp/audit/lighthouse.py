"""PageSpeed Insights client for the CWV-tier audit checks.

Each call runs a full Lighthouse pass against one URL on Google's
infrastructure (typically 10–25 seconds), so the sample is small
(``psi_url_sample``, default 20) and calls run on a small thread pool. Only the
Lighthouse audits the catalogue checks actually consume are kept, so results
stay tiny regardless of the full PSI payload size.

Note on load: Lighthouse fetches the target page from Google's servers — one
PSI call is comparable to a single real visitor, and the pool is capped at 4,
so this never pressures the audited site beyond the politeness budget.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import httpx

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
_TIMEOUT = httpx.Timeout(90.0)  # a Lighthouse run routinely takes 15-25s
_MAX_WORKERS = 4

# Lighthouse audit ids consumed by the catalogue's CWV checks. #87 has two ids
# because Lighthouse renamed preload-lcp-image -> prioritize-lcp-image in v10.
AUDIT_IDS = frozenset({
    "largest-contentful-paint",      # 25
    "cumulative-layout-shift",       # 26
    "uses-responsive-images",        # 27
    "render-blocking-resources",     # 28
    "uses-optimized-images",         # 29
    "efficient-animated-content",    # 53
    "modern-image-formats",          # 85
    "uses-rel-preload",              # 86 (absent in Lighthouse >= 9)
    "prioritize-lcp-image",          # 87
    "preload-lcp-image",             # 87 (pre-v10 name)
    "third-party-facades",           # 88
})


def _fetch_one(client: httpx.Client, url: str, api_key: str, strategy: str) -> dict[str, Any]:
    resp = client.get(
        PSI_ENDPOINT,
        params={"url": url, "key": api_key, "strategy": strategy, "category": "performance"},
    )
    resp.raise_for_status()
    payload = resp.json()
    audits = payload.get("lighthouseResult", {}).get("audits", {})
    kept = {
        audit_id: {
            "score": entry.get("score"),
            "numericValue": entry.get("numericValue"),
        }
        for audit_id, entry in audits.items()
        if audit_id in AUDIT_IDS
    }
    return {"audits": kept}


def run_psi_sample(
    urls: list[str], api_key: str, strategy: str = "mobile"
) -> Optional[dict[str, dict[str, Any]]]:
    """Run PSI over the sample. Returns {url: {"audits": {...}}} for URLs that
    resolved, or None when the sample is empty or nothing resolved (callers
    report "Not assessed" rather than inventing a verdict)."""
    sample = list(dict.fromkeys(urls))
    if not sample or not api_key:
        return None
    out: dict[str, dict[str, Any]] = {}
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            workers = min(_MAX_WORKERS, len(sample))

            def safe(url: str):
                try:
                    return _fetch_one(client, url, api_key, strategy)
                except (httpx.HTTPError, ValueError, KeyError):
                    return None

            with ThreadPoolExecutor(max_workers=workers) as pool:
                for url, result in zip(sample, pool.map(safe, sample)):
                    if result is not None:
                        out[url] = result
    except Exception:  # noqa: BLE001
        return None
    return out if out else None
