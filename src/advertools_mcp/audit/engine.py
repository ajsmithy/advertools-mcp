"""Audit orchestration: build context, run every catalogue check, write xlsx."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from ..config import Settings
from ..crawl.csv_export import build_export_frame
from .catalogue import load_catalogue
from .checks import CHECKS
from .context import NOT_ASSESSED, AuditContext, CheckResult, not_assessed
from .descriptions import describe
from .report import write_report


@dataclass
class AuditConfig:
    """Records exactly which optional sources were active for an audit run."""

    audit_url_sample: int
    lighthouse_enabled: bool
    render_enabled: bool
    gsc_enabled: bool
    backlinks_enabled: bool
    robots_fetched: bool = False
    sitemap_fetched: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def as_rows(self) -> list[tuple[str, str]]:
        rows = [
            ("audit_url_sample", str(self.audit_url_sample)),
            ("Lighthouse (CWV tier)", "enabled" if self.lighthouse_enabled else "disabled"),
            ("Headless render (RENDER tier)", "enabled" if self.render_enabled else "disabled"),
            ("GSC / EXTERNAL tier", "enabled" if self.gsc_enabled else "disabled"),
            ("Backlink source", "enabled" if self.backlinks_enabled else "disabled"),
            ("robots.txt fetched", "yes" if self.robots_fetched else "no"),
            ("sitemap fetched", "yes" if self.sitemap_fetched else "no"),
        ]
        rows.extend((str(k), str(v)) for k, v in self.extra.items())
        return rows


def _load_robots(primary_host: str, ua: str) -> tuple[Optional[str], Optional[str]]:
    """Best-effort raw robots.txt fetch → (url, text). text='' means 404/empty."""
    if not primary_host:
        return None, None
    import httpx

    url = f"https://{primary_host.split(':')[0]}/robots.txt"
    if ":" in primary_host:  # preserve port for local hosts
        url = f"http://{primary_host}/robots.txt"
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True, headers={"User-Agent": ua}) as c:
            resp = c.get(url)
        return url, (resp.text if resp.status_code == 200 else "")
    except Exception:  # noqa: BLE001
        return None, None


def _load_sitemap(robots_text: Optional[str], primary_host: str) -> Optional[pd.DataFrame]:
    import re

    import advertools as adv

    candidates: list[str] = []
    if robots_text:
        candidates += re.findall(r"(?im)^\s*sitemap\s*:\s*(\S+)", robots_text)
    if primary_host:
        scheme = "http" if ":" in primary_host else "https"
        candidates.append(f"{scheme}://{primary_host}/sitemap.xml")
    for url in candidates:
        try:
            # max_workers=2 keeps recursive sitemap-index fetching polite
            # (advertools defaults to 8 concurrent requests against the host).
            df = adv.sitemap_to_df(url, max_workers=2)
            if df is not None and len(df):
                return df
        except Exception:  # noqa: BLE001
            continue
    return None


def _psi_sample_urls(ctx: AuditContext, n: int) -> list[str]:
    """Pick up to ``n`` crawled HTML 200 URLs for PSI, shallowest-first so the
    homepage and top-level templates are assessed before deep pages."""
    import pandas as pd

    status = pd.to_numeric(ctx.col("status"), errors="coerce")
    mask = (status == 200) & ctx.is_html()
    subset = ctx.df.loc[mask.fillna(False), ["url"]].copy()
    if "depth" in ctx.df.columns:
        subset["depth"] = pd.to_numeric(ctx.df.loc[subset.index, "depth"], errors="coerce").fillna(99)
        subset = subset.sort_values("depth", kind="stable")
    return list(dict.fromkeys(subset["url"].astype(str)))[:n]


def run_audit(
    crawl_parquet: str,
    settings: Settings,
    audits_dir: Path,
    has_backlinks: bool = False,
    catalogue_path: str | None = None,
    is_discovery: bool | None = None,
) -> dict[str, Any]:
    """Run the full audit over a saved crawl. Returns a compact summary dict."""
    if not Path(crawl_parquet).exists():
        raise FileNotFoundError(f"Crawl parquet not found: {crawl_parquet}")

    df = pd.read_parquet(crawl_parquet)
    checks = load_catalogue(catalogue_path)

    # Build context, fetching robots + sitemap best-effort.
    ctx = AuditContext(
        df=df,
        settings=settings,
        has_lighthouse=bool(settings.lighthouse_api_key),
        has_render=bool(settings.enable_render),
        has_gsc=bool(settings.gsc_credentials),
        has_backlinks=has_backlinks,
        is_discovery=is_discovery,
    )
    robots_url, robots_text = _load_robots(ctx.primary_host, settings.default_user_agent)
    ctx.robots_url, ctx.robots_text = robots_url, robots_text
    ctx.sitemap_df = _load_sitemap(robots_text, ctx.primary_host)

    psi_urls_assessed = 0
    if ctx.has_lighthouse:
        from . import lighthouse

        sample = _psi_sample_urls(ctx, min(settings.psi_url_sample, settings.audit_url_sample))
        ctx.psi_results = lighthouse.run_psi_sample(
            sample, settings.lighthouse_api_key, settings.psi_strategy
        )
        psi_urls_assessed = len(ctx.psi_results or {})

    render_urls_assessed = 0
    if ctx.has_render:
        from . import render as render_mod

        sample = _psi_sample_urls(ctx, min(settings.render_url_sample, settings.audit_url_sample))
        ctx.render_results = render_mod.run_render_sample(
            sample, settings.default_user_agent, chromium_path=settings.chromium_path
        )
        render_urls_assessed = len(ctx.render_results or {})

    config = AuditConfig(
        audit_url_sample=settings.audit_url_sample,
        lighthouse_enabled=ctx.has_lighthouse,
        render_enabled=ctx.has_render,
        gsc_enabled=ctx.has_gsc,
        backlinks_enabled=has_backlinks,
        robots_fetched=robots_url is not None,
        sitemap_fetched=ctx.sitemap_df is not None,
    )
    if ctx.has_lighthouse:
        config.extra["PSI strategy"] = settings.psi_strategy
        config.extra["PSI sample cap"] = settings.psi_url_sample
        config.extra["PSI URLs assessed"] = psi_urls_assessed
    if ctx.has_render:
        config.extra["Render sample cap"] = settings.render_url_sample
        config.extra["Render URLs assessed"] = render_urls_assessed

    results: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []
    for cdef in checks:
        fn = CHECKS.get(cdef.num)
        if fn is None:
            res: CheckResult = not_assessed("No implementation registered.", "—")
        else:
            try:
                res = fn(ctx)
            except Exception as exc:  # noqa: BLE001 - never let one check abort the audit
                res = not_assessed(f"Check raised {type(exc).__name__}: {exc}", "error")
        results.append(
            {
                "num": cdef.num,
                "check": cdef.check,
                "tier": cdef.tier,
                "status": res.status,
                "description": describe(cdef.num),
                "affected_count": res.affected_count,
                "example_urls": res.example_urls,
                "detection_source": res.detection_source,
                "note": res.note,
            }
        )
        for url in res.affected_urls:
            detail_rows.append(
                {"num": cdef.num, "check": cdef.check, "tier": cdef.tier, "url": url}
            )

    audit_id = f"audit-{uuid.uuid4().hex[:12]}"
    audits_dir.mkdir(parents=True, exist_ok=True)
    out_path = audits_dir / f"{audit_id}.xlsx"
    crawl_detail = build_export_frame(df)
    write_report(out_path, results, detail_rows, config, crawl_detail)

    summary = _summarise(results)
    summary.update(
        {
            "audit_id": audit_id,
            "audit_xlsx": str(out_path),
            "crawl_parquet": crawl_parquet,
            "total_checks": len(results),
            "config": dict(config.as_rows()),
        }
    )
    return summary


def _summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_tier: dict[str, dict[str, int]] = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        tier = by_tier.setdefault(r["tier"], {})
        tier[r["status"]] = tier.get(r["status"], 0) + 1
    return {"counts_by_status": by_status, "counts_by_tier": by_tier}
