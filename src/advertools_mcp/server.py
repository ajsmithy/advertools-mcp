"""FastMCP server wiring. One core implementation shared by stdio and HTTP.

Crawls are offloaded to subprocess workers via :class:`JobManager`; results are
queried from parquet; the audit and robots/sitemap helpers run in a thread so the
event loop never blocks. Tools return compact summaries and job/audit IDs — never
raw crawl or audit file contents.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .config import Settings, get_settings
from .crawl import analysis, csv_export, parquet_query, summary
from .jobs import JobKind, JobManager, JobState, JobStore
from .security import enforce_domain_allowlist
from .validation import evaluate_crawl_config, needs_confirmation_response

# --------------------------------------------------------------------- context
_settings: Settings
_store: JobStore
_manager: JobManager


def _crawl_custom_settings(
    *,
    max_pages: Optional[int],
    max_depth: Optional[int],
    concurrent_requests: int,
    download_delay: float,
    obey_robots: bool,
    user_agent: str,
    passthrough: Optional[dict[str, Any]],
) -> dict[str, Any]:
    settings_dict: dict[str, Any] = {
        "USER_AGENT": user_agent,
        "ROBOTSTXT_OBEY": obey_robots,
        "CONCURRENT_REQUESTS": concurrent_requests,
        "CONCURRENT_REQUESTS_PER_DOMAIN": concurrent_requests,
        "DOWNLOAD_DELAY": download_delay,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": concurrent_requests,
        "LOG_LEVEL": "ERROR",
    }
    if max_pages:
        settings_dict["CLOSESPIDER_PAGECOUNT"] = max_pages
    if max_depth is not None:
        settings_dict["DEPTH_LIMIT"] = max_depth
    if passthrough:
        settings_dict.update(passthrough)  # user passthrough wins
    return settings_dict


def _as_list(url: Optional[str], urls: Optional[list[str]]) -> list[str]:
    out: list[str] = []
    if url:
        out.append(url)
    if urls:
        out.extend(urls)
    if not out:
        raise ValueError("Provide at least one URL via 'url' or 'urls'.")
    return out


def _require_done(job_id: str):
    record = _manager.status(job_id)
    if record is None:
        raise ValueError(f"Unknown job_id: {job_id}")
    if record.state != JobState.DONE.value:
        raise ValueError(
            f"Job {job_id} is '{record.state}', not 'done'. "
            f"Call crawl_status first; results are available only for completed crawls."
        )
    if not record.output_parquet or not Path(record.output_parquet).exists():
        raise ValueError(f"Job {job_id} produced no parquet output (0 pages or failed conversion).")
    return record


def build_server(settings: Optional[Settings] = None) -> FastMCP:
    global _settings, _store, _manager
    _settings = settings or get_settings()
    _settings.ensure_dirs()
    _store = JobStore(_settings.jobs_dir)
    _store.reconcile_orphans()
    _manager = JobManager(_settings, _store)

    mcp = FastMCP(_settings.server_name)

    # ============================================================== CRAWLING
    @mcp.tool()
    async def start_crawl(
        url: Optional[str] = None,
        urls: Optional[list[str]] = None,
        follow_links: bool = False,
        allowed_domains: Optional[list[str]] = None,
        include_url_regex: Optional[str] = None,
        exclude_url_regex: Optional[str] = None,
        max_pages: Optional[int] = None,
        max_depth: Optional[int] = None,
        concurrent_requests: Optional[int] = None,
        download_delay: Optional[float] = None,
        obey_robots: Optional[bool] = None,
        user_agent: Optional[str] = None,
        css_selectors: Optional[dict[str, str]] = None,
        xpath_selectors: Optional[dict[str, str]] = None,
        custom_settings: Optional[dict[str, Any]] = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Start a discovery (spider) or list-mode crawl as an async job.

        Set ``follow_links=True`` for discovery, ``False`` for list mode. Returns a
        ``job_id`` immediately. High-impact configs require ``confirm=true`` after
        reviewing the echoed resolved config.
        """
        url_list = _as_list(url, urls)
        if max_pages is None and not follow_links:
            max_pages = None  # list mode: bounded by url_list
        resolved = {
            "url_list": url_list,
            "follow_links": follow_links,
            "allowed_domains": allowed_domains,
            "include_url_regex": include_url_regex,
            "exclude_url_regex": exclude_url_regex,
            "max_pages": max_pages if max_pages is not None else _settings.default_max_pages
            if follow_links else max_pages,
            "max_depth": max_depth,
            "concurrent_requests": concurrent_requests or _settings.default_concurrent_requests,
            "download_delay": download_delay if download_delay is not None else _settings.default_download_delay,
            "obey_robots": obey_robots if obey_robots is not None else _settings.default_obey_robots,
            "user_agent": user_agent or _settings.default_user_agent,
        }

        enforce_domain_allowlist(url_list, _settings)
        warnings = evaluate_crawl_config(resolved, _settings)
        # Always confirm the crawl's user-agent before launching. If the caller
        # did not pass one, prompt for it (default 'intrepidbot').
        if user_agent is None:
            warnings.insert(0, {
                "code": "user_agent_unspecified",
                "message": (
                    f"No user-agent was specified. The default is "
                    f"'{_settings.default_user_agent}'. Reply with the user-agent you "
                    f"want for this crawl, or confirm to use the default."
                ),
            })
        advisories = _contact_advisory(resolved["user_agent"])
        if warnings and not confirm:
            resp = needs_confirmation_response(resolved, warnings)
            resp["advisories"] = advisories
            resp["user_agent"] = resolved["user_agent"]
            return resp

        cs = _crawl_custom_settings(
            max_pages=resolved["max_pages"],
            max_depth=resolved["max_depth"],
            concurrent_requests=resolved["concurrent_requests"],
            download_delay=resolved["download_delay"],
            obey_robots=resolved["obey_robots"],
            user_agent=resolved["user_agent"],
            passthrough=custom_settings,
        )
        spec = {
            "crawl": {
                "url_list": url_list,
                "follow_links": follow_links,
                "allowed_domains": allowed_domains,
                "include_url_regex": include_url_regex,
                "exclude_url_regex": exclude_url_regex,
                "css_selectors": css_selectors,
                "xpath_selectors": xpath_selectors,
                "custom_settings": cs,
            }
        }
        record = _manager.submit(JobKind.CRAWL, params=resolved, spec=spec)
        return {
            "status": "started",
            "job_id": record.job_id,
            "resolved_config": resolved,
            "warnings_acknowledged": warnings,
            "advisories": advisories,
            "message": "Crawl launched. Poll crawl_status with the job_id.",
        }

    @mcp.tool()
    async def start_header_crawl(
        urls: list[str],
        user_agent: Optional[str] = None,
        custom_settings: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """HEAD-only crawl (``crawl_headers``) for fast status/header checks. Async."""
        enforce_domain_allowlist(urls, _settings)
        cs = dict(custom_settings or {})
        cs.setdefault("USER_AGENT", user_agent or _settings.default_user_agent)
        cs.setdefault("LOG_LEVEL", "ERROR")
        spec = {"crawl": {"url_list": urls, "custom_settings": cs}}
        record = _manager.submit(
            JobKind.HEADER_CRAWL, params={"url_list": urls, "mode": "headers"}, spec=spec
        )
        return {"status": "started", "job_id": record.job_id, "message": "Header crawl launched."}

    @mcp.tool()
    async def start_image_crawl(
        urls: list[str],
        min_width: int = 0,
        min_height: int = 0,
        include_img_regex: Optional[str] = None,
        custom_settings: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Image discovery/metadata crawl (``crawl_images``). Async."""
        enforce_domain_allowlist(urls, _settings)
        cs = dict(custom_settings or {})
        cs.setdefault("USER_AGENT", _settings.default_user_agent)
        cs.setdefault("LOG_LEVEL", "ERROR")
        spec = {
            "crawl": {
                "url_list": urls,
                "min_width": min_width,
                "min_height": min_height,
                "include_img_regex": include_img_regex,
                "custom_settings": cs,
            }
        }
        record = _manager.submit(
            JobKind.IMAGE_CRAWL, params={"url_list": urls, "mode": "images"}, spec=spec
        )
        return {"status": "started", "job_id": record.job_id, "message": "Image crawl launched."}

    @mcp.tool()
    async def crawl_status(job_id: str) -> dict[str, Any]:
        """State, progress, errors, runtime, and output paths for a job."""
        record = _manager.status(job_id)
        if record is None:
            return {"error": f"Unknown job_id: {job_id}"}
        d = record.to_dict()
        return {
            "job_id": record.job_id,
            "kind": record.kind,
            "state": record.state,
            "pages": record.pages,
            "errors": record.errors,
            "runtime_seconds": d["runtime_seconds"],
            "error_message": record.error_message,
            "finish_reason": record.finish_reason,
            "output_parquet": record.output_parquet,
            "output_dir": record.output_dir,
        }

    @mcp.tool()
    async def list_jobs(limit: int = 50) -> dict[str, Any]:
        """List recent jobs (most recent first)."""
        records = _manager.list()[:limit]
        return {
            "count": len(records),
            "jobs": [
                {
                    "job_id": r.job_id,
                    "kind": r.kind,
                    "state": r.state,
                    "pages": r.pages,
                    "created_at": r.created_at,
                }
                for r in records
            ],
        }

    @mcp.tool()
    async def cancel_job(job_id: str) -> dict[str, Any]:
        """Cancel a queued or running job."""
        ok = await _manager.cancel(job_id)
        return {"job_id": job_id, "cancelled": ok}

    # ====================================================== RESULTS & ANALYSIS
    @mcp.tool()
    async def get_crawl_summary(job_id: str) -> dict[str, Any]:
        """Row count, status-code distribution, content types, depth, schema."""
        record = _require_done(job_id)
        return await asyncio.to_thread(summary.summarise, record.output_parquet)

    @mcp.tool()
    async def query_crawl(
        job_id: str,
        columns: Optional[list[str]] = None,
        filters: Optional[list[dict[str, Any]]] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Paginated, filtered rows from a crawl parquet (columnar slice).

        ``filters``: list of ``{"column", "op", "value"}`` (ops: ==, !=, <, <=, >,
        >=, in, not_in, contains, is_null, not_null), combined with AND.
        """
        record = _require_done(job_id)
        return await asyncio.to_thread(
            parquet_query.query, record.output_parquet, columns, filters, limit, offset
        )

    @mcp.tool()
    async def analyse_links(job_id: str) -> dict[str, Any]:
        """crawlytics link analysis: internal/external/nofollow + top targets."""
        record = _require_done(job_id)
        return await asyncio.to_thread(analysis.analyse_links, record.output_parquet)

    @mcp.tool()
    async def analyse_redirects(job_id: str) -> dict[str, Any]:
        """crawlytics redirect analysis over a saved crawl."""
        record = _require_done(job_id)
        return await asyncio.to_thread(analysis.analyse_redirects, record.output_parquet)

    @mcp.tool()
    async def analyse_images(job_id: str) -> dict[str, Any]:
        """crawlytics image analysis: counts, missing alt, by extension."""
        record = _require_done(job_id)
        return await asyncio.to_thread(analysis.analyse_images, record.output_parquet)

    @mcp.tool()
    async def export_crawl_csv(job_id: str) -> dict[str, Any]:
        """Write the flat Internal/All-style CSV; return its path and a summary."""
        if not _settings.csv_export:
            return {"error": "CSV export is disabled (csv_export=false)."}
        record = _require_done(job_id)
        out_path = str(Path(record.output_parquet).with_name(csv_export.OUTPUT_FILENAME))
        return await asyncio.to_thread(csv_export.export_csv, record.output_parquet, out_path)

    # ============================================================== ROBOTS
    @mcp.tool()
    async def parse_robots(robotstxt_urls: list[str]) -> dict[str, Any]:
        """Parse one or many robots.txt files into a table (``robotstxt_to_df``)."""
        return await asyncio.to_thread(_parse_robots_impl, robotstxt_urls)

    @mcp.tool()
    async def test_robots(
        robotstxt_url: str, user_agents: list[str], urls: list[str]
    ) -> dict[str, Any]:
        """Bulk can-fetch tester (``robotstxt_test``) over (user_agent, url) pairs."""
        return await asyncio.to_thread(_test_robots_impl, robotstxt_url, user_agents, urls)

    # ============================================================== SITEMAPS
    @mcp.tool()
    async def fetch_sitemap(
        sitemap_url: str,
        recursive: bool = True,
        request_headers: Optional[dict[str, str]] = None,
        max_rows: int = 200,
    ) -> dict[str, Any]:
        """Fetch a sitemap (index/news/video) via ``sitemap_to_df``. Returns a
        summary + sample rows; full table is saved to disk and can seed a crawl."""
        return await asyncio.to_thread(
            _fetch_sitemap_impl, sitemap_url, recursive, request_headers, max_rows
        )

    # ============================================================== AUDIT
    @mcp.tool()
    async def run_audit(job_id: str, has_backlinks: bool = False) -> dict[str, Any]:
        """Run the post-crawl SEO audit over a saved crawl. Returns audit_id + summary."""
        from .audit.engine import run_audit as _run

        record = _require_done(job_id)
        return await asyncio.to_thread(
            _run, record.output_parquet, _settings, _settings.audits_dir, has_backlinks
        )

    @mcp.tool()
    async def get_audit_summary(audit_id: str) -> dict[str, Any]:
        """Headline counts (by status and tier) for a completed audit."""
        return await asyncio.to_thread(_audit_summary_impl, audit_id)

    return mcp


# ----------------------------------------------------------- impl (threaded)
def _contact_advisory(user_agent: str) -> list[str]:
    out: list[str] = []
    if not _settings.contact_url_confirmed and _settings.contact_url in user_agent:
        out.append(
            f"contact_url '{_settings.contact_url}' is flagged 'to confirm' in the "
            "Inputs block — verify it before crawling third-party sites."
        )
    return out


def _parse_robots_impl(robotstxt_urls: list[str]) -> dict[str, Any]:
    import advertools as adv
    import pandas as pd

    frames = []
    errors = {}
    for u in robotstxt_urls:
        try:
            frames.append(adv.robotstxt_to_df(u))
        except Exception as exc:  # noqa: BLE001
            errors[u] = str(exc)
    if not frames:
        return {"error": "Could not parse any robots.txt.", "details": errors}
    df = pd.concat(frames, ignore_index=True)
    out_path = _settings.data_dir / "_robots" / "robots.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    return {
        "rows": int(len(df)),
        "columns": list(df.columns),
        "directives": {str(k): int(v) for k, v in df.get("directive", pd.Series()).value_counts().items()},
        "sample": df.head(50).to_dict(orient="records"),
        "saved_to": str(out_path),
        "errors": errors,
    }


def _test_robots_impl(robotstxt_url: str, user_agents: list[str], urls: list[str]) -> dict[str, Any]:
    import advertools as adv

    df = adv.robotstxt_test(robotstxt_url, user_agents, urls)
    can = "can_fetch"
    blocked = int((~df[can]).sum()) if can in df.columns else 0
    return {
        "rows": int(len(df)),
        "blocked": blocked,
        "allowed": int(len(df) - blocked),
        "results": df.to_dict(orient="records"),
    }


def _fetch_sitemap_impl(
    sitemap_url: str, recursive: bool, request_headers: Optional[dict[str, str]], max_rows: int
) -> dict[str, Any]:
    import advertools as adv

    df = adv.sitemap_to_df(sitemap_url, recursive=recursive, request_headers=request_headers)
    out_path = _settings.data_dir / "_sitemaps" / (
        "".join(c if c.isalnum() else "_" for c in sitemap_url)[:80] + ".parquet"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    locs = df["loc"].astype(str).head(max_rows).tolist() if "loc" in df.columns else []
    return {
        "url_count": int(len(df)),
        "columns": list(df.columns),
        "sample_urls": locs,
        "saved_to": str(out_path),
        "hint": "Seed a list-mode crawl with start_crawl(urls=<these>, follow_links=false).",
    }


def _audit_summary_impl(audit_id: str) -> dict[str, Any]:
    from openpyxl import load_workbook

    path = _settings.audits_dir / f"{audit_id}.xlsx"
    if not path.exists():
        return {"error": f"Unknown audit_id: {audit_id}"}
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb["Checklist"]
    by_status: dict[str, int] = {}
    by_tier: dict[str, dict[str, int]] = {}
    total = 0
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue
        status, tier = row[3], row[2]
        if status is None:
            continue
        total += 1
        by_status[status] = by_status.get(status, 0) + 1
        tier_counts = by_tier.setdefault(tier, {})
        tier_counts[status] = tier_counts.get(status, 0) + 1
    wb.close()
    return {
        "audit_id": audit_id,
        "audit_xlsx": str(path),
        "total_checks": total,
        "counts_by_status": by_status,
        "counts_by_tier": by_tier,
    }
