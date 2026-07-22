"""Implementations for all catalogue checks (91), keyed by check number.

Each function takes an :class:`AuditContext` and returns a :class:`CheckResult`
using only the four permitted states. Checks that cannot be evaluated (optional
tier disabled, required data absent) return ``Not assessed`` — never a false
Present/Not present.

Detection draws on: the crawl parquet (incl. selectors the worker injects),
fetched robots.txt and sitemap data, and capped sampled HEAD/body fetches for
CRAWL+ asset checks.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

import pandas as pd

from ..crawl import schema as S
from . import fetch
from .context import (
    HEURISTIC,
    NOT_PRESENT,
    PRESENT,
    AuditContext,
    CheckResult,
    finding,
    not_assessed,
)

CHECKS: dict[int, callable] = {}


def check(num: int):
    def deco(fn):
        CHECKS[num] = fn
        return fn

    return deco


# --------------------------------------------------------------------- helpers
STAGING_RE = re.compile(r"(^|\.)(staging|stg|dev|test|preprod|uat|qa|sandbox)\b|\.local$", re.I)
FILTER_PARAMS = {"color", "colour", "size", "brand", "price", "filter", "fq", "refine", "category"}
SORT_PARAMS = {"sort", "orderby", "order", "sort_by", "sortby", "dir"}
SESSION_PARAMS = {"sid", "sessionid", "phpsessid", "jsessionid", "sess", "aspsessionid"}
PAGINATION_PARAMS = {"page", "p", "pg", "start", "offset", "paged"}
SEARCH_PATH_RE = re.compile(r"/(search|s|find|results?|suche|recherche)(/|\?|$)", re.I)


def _html200(ctx: AuditContext) -> pd.Series:
    return (ctx.status_int() == 200) & ctx.is_html()


def _list_col_items(ctx: AuditContext, col: str):
    """Yield (url, [items]) for an @@-delimited column."""
    series = ctx.col(col)
    urls = ctx.col(S.COL_URL)
    for url, raw in zip(urls, series):
        yield url, S.split_list(raw)


def _count_col(ctx: AuditContext, col: str) -> pd.Series:
    return pd.to_numeric(ctx.col(col), errors="coerce")


def _query_params(url: str) -> set[str]:
    return {k.lower() for k in parse_qs(urlparse(str(url)).query).keys()}


def _parse_disallows(ctx: AuditContext) -> list[str]:
    """Disallow path patterns governing user-agent ``*`` (best-effort)."""
    text = ctx.robots_text
    if not text and ctx.robots_df is not None and "directive" in ctx.robots_df.columns:
        lines = []
        for _, row in ctx.robots_df.iterrows():
            lines.append(f"{row.get('directive','')}: {row.get('content','')}")
        text = "\n".join(lines)
    if not text:
        return []
    disallows: list[str] = []
    applies = False
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "user-agent":
            applies = val == "*"
        elif key == "disallow" and applies and val:
            disallows.append(val)
    return disallows


def _compile_disallows(disallows: list[str]) -> list[re.Pattern]:
    return [re.compile(re.escape(rule).replace(r"\*", ".*")) for rule in disallows]


def _path_blocked(path: str, compiled: list[re.Pattern]) -> bool:
    return any(pattern.match(path) for pattern in compiled)


def _indexable_map(ctx: AuditContext) -> dict[str, bool]:
    """Normalised-URL → indexable? based on crawl status + robots meta."""
    out: dict[str, bool] = {}
    status = ctx.status_int()
    robots = ctx.col(S.COL_META_ROBOTS).astype(str).str.lower()
    for url, code, rob in zip(ctx.col(S.COL_URL), status, robots):
        indexable = code == 200 and "noindex" not in rob
        out[str(url).rstrip("/")] = indexable
    return out


# ============================================================== CRAWL+ / CRAWL

@check(1)  # Cookies Preventing Crawling (CRAWL+, heuristic)
def c1(ctx):
    html = _html200(ctx)
    body_len = ctx.col(S.COL_BODY_TEXT).fillna("").astype(str).str.len()
    setcookie = ctx.header("Set-Cookie").notna() & ctx.header("Set-Cookie").astype(str).ne("None")
    thin = html & (body_len < 50) & setcookie
    urls = ctx.urls_where(thin)
    return finding(urls, "crawl headers + body", heuristic=True,
                   note="HTML 200 pages with Set-Cookie and near-empty body.")


@check(2)  # Security Tool Blocking Crawl (CRAWL)
def c2(ctx):
    code = ctx.status_int()
    mask = code.isin([401, 403, 429, 503])
    return finding(ctx.urls_where(mask), "crawl status codes")


@check(3)  # Website Cloaking (CRAWL+, heuristic, needs 2nd UA crawl)
def c3(ctx):
    if ctx.second_ua_df is None:
        return not_assessed("Requires a second crawl under Googlebot UA to diff.", "2nd-UA crawl")
    return not_assessed("Second-UA crawl supplied but diff not implemented.", "2nd-UA crawl")


@check(4)  # Incorrect Robots.txt Formatting (CRAWL)
def c4(ctx):
    if ctx.robots_text is None and ctx.robots_df is None:
        return not_assessed("robots.txt was not fetched.", "robots.txt")
    valid = {"user-agent", "disallow", "allow", "sitemap", "crawl-delay", "host",
             "clean-param", "noindex", "request-rate", "visit-time"}
    bad = []
    text = ctx.robots_text or ""
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if ":" not in stripped or stripped.partition(":")[0].strip().lower() not in valid:
            bad.append(f"line {i}: {line.strip()[:80]}")
    return finding(bad, "robots.txt parse")


@check(5)  # Missing Robots.txt File (CRAWL)
def c5(ctx):
    if ctx.robots_url is None:
        return not_assessed("robots.txt fetch was not attempted.", "robots.txt")
    if not ctx.robots_text or not ctx.robots_text.strip():
        return CheckResult(PRESENT, "robots.txt", [ctx.robots_url], "robots.txt missing or empty.")
    return CheckResult(NOT_PRESENT, "robots.txt")


@check(6)  # Non-Indexable URLs in Canonical Tags (CRAWL)
def c6(ctx):
    idx = _indexable_map(ctx)
    bad = []
    for url, canon in zip(ctx.col(S.COL_URL), ctx.col(S.COL_CANONICAL)):
        if not canon or str(canon).lower() == "nan":
            continue
        target = str(canon).rstrip("/")
        if target in idx and not idx[target]:
            bad.append(str(url))
    return finding(bad, "canonical join to crawl")


@check(7)  # Canonical Outside <head> (CRAWL+)
def c7(ctx):
    total = _count_col(ctx, "canonical_total_count").fillna(0)
    in_head = _count_col(ctx, "canonical_in_head_count").fillna(0)
    mask = total > in_head
    return finding(ctx.urls_where(mask), "raw HTML structure (selectors)")


@check(8)  # Canonicals Point to Staging Domain (CRAWL)
def c8(ctx):
    bad = []
    for url, canon in zip(ctx.col(S.COL_URL), ctx.col(S.COL_CANONICAL)):
        host = urlparse(str(canon)).netloc
        if host and STAGING_RE.search(host):
            bad.append(str(url))
    return finding(bad, "canonical host pattern")


@check(9)  # Canonicals Point to Third-Party Site (CRAWL)
def c9(ctx):
    bad = []
    for url, canon in zip(ctx.col(S.COL_URL), ctx.col(S.COL_CANONICAL)):
        if not canon or str(canon).lower() == "nan":
            continue
        host = urlparse(str(canon)).netloc.lower()
        if host and host not in ctx.internal_hosts:
            bad.append(str(url))
    return finding(bad, "canonical host vs site host")


def _param_canonical_check(ctx, params):
    bad = []
    for url, canon in zip(ctx.col(S.COL_URL), ctx.col(S.COL_CANONICAL)):
        if _query_params(url) & params:
            target = str(canon).rstrip("/") if canon and str(canon).lower() != "nan" else ""
            clean = urlparse(str(url))._replace(query="").geturl().rstrip("/")
            if target != clean:
                bad.append(str(url))
    return finding(bad, "param URL vs canonical", heuristic=True)


@check(10)  # Non-Canonicalized Filter URLs (CRAWL+, heuristic)
def c10(ctx):
    return _param_canonical_check(ctx, FILTER_PARAMS)


@check(11)  # Non-Canonicalized Sorting/Order URLs (CRAWL+, heuristic)
def c11(ctx):
    return _param_canonical_check(ctx, SORT_PARAMS)


@check(12)  # Incorrect Code in Canonical Tag (CRAWL)
def c12(ctx):
    bad = []
    multi = _count_col(ctx, "canonical_total_count").fillna(0)
    for i, (url, canon) in enumerate(zip(ctx.col(S.COL_URL), ctx.col(S.COL_CANONICAL))):
        c = str(canon) if canon is not None else ""
        malformed = bool(c) and c.lower() != "nan" and (" " in c.strip() or not urlparse(c).scheme)
        if malformed or (len(multi) > i and multi.iloc[i] > 1):
            bad.append(str(url))
    return finding(bad, "canonical value validation")


@check(13)  # Broken JavaScript/CSS Files (CRAWL+)
def c13(ctx):
    assets = _collect_assets(ctx, ["script_src", "stylesheet_href"])
    if not assets:
        return CheckResult(NOT_PRESENT, "asset HEAD-crawl", note="No JS/CSS assets found.")
    statuses = fetch.head_statuses(assets, ctx.settings.audit_url_sample, ctx.settings.default_user_agent,
                                 rate=ctx.settings.default_crawl_speed)
    if statuses is None:
        return not_assessed("Asset HEAD-crawl could not reach any asset.", "asset HEAD-crawl")
    broken = [u for u, code in statuses.items() if code >= 400]
    return finding(broken, "asset HEAD-crawl")


@check(14)  # Robots.txt Blocking CSS & JS (CRAWL)
def c14(ctx):
    disallows = _parse_disallows(ctx)
    if not disallows and ctx.robots_url is None:
        return not_assessed("robots.txt not fetched.", "robots.txt")
    compiled = _compile_disallows(disallows)
    assets = _collect_assets(ctx, ["script_src", "stylesheet_href"])
    blocked = [a for a in assets if _path_blocked(urlparse(a).path, compiled)]
    return finding(blocked, "robots.txt rule match")


@check(15)  # Rendering Errors (RENDER)
def c15(ctx):
    rr = ctx.render_results
    if rr is None:
        return _render_gate(ctx, "Rendering errors require a headless render pass.")
    affected = [u for u, r in rr.items() if r.get("page_errors")]
    return finding(affected, "headless render (uncaught JS errors)",
                   note=f"{len(rr)} URL(s) rendered headlessly.")


@check(16)  # JavaScript-Powered Menu Navigation (RENDER)
def c16(ctx):
    rr = ctx.render_results
    if rr is None:
        return _render_gate(ctx, "Comparing static vs rendered nav requires rendering.")
    static_nav = _static_map(ctx, "nav_links_url")
    affected = []
    for url, r in rr.items():
        if not r.get("ok"):
            continue
        static_has_nav = bool(S.split_list(static_nav.get(str(url).rstrip("/"))))
        if r.get("nav_link_count", 0) > 0 and not static_has_nav:
            affected.append(str(url))
    return finding(affected, "static vs rendered nav links", heuristic=True,
                   note="Nav links exist only after JS runs; crawlers may miss them.")


@check(17)  # Infinite Pagination w/o Crawlable Nav (CRAWL+, heuristic)
def c17(ctx):
    paged = []
    for url in ctx.col(S.COL_URL):
        if _query_params(url) & PAGINATION_PARAMS:
            paged.append(str(url))
    # Heuristic: pagination params present but no rel=next/prev signal captured.
    return finding(paged, "pagination param + nav heuristic", heuristic=True,
                   note="Pagination-parameter URLs detected; verify crawlable rel=next/prev.")


@check(18)  # UTM Querystrings in Internal Links (CRAWL)
def c18(ctx):
    bad = []
    for url, links in _list_col_items(ctx, S.COL_LINKS_URL):
        if any("utm_" in str(link).lower() for link in links):
            bad.append(str(url))
    return finding(bad, "internal link params")


@check(19)  # Filter URLs Not Consistent Order (CRAWL+, heuristic)
def c19(ctx):
    seen: dict[frozenset, str] = {}
    bad = []
    for url in ctx.col(S.COL_URL):
        params = _query_params(url) & FILTER_PARAMS
        if len(params) >= 2:
            key = frozenset(params)
            order = tuple(parse_qs(urlparse(str(url)).query).keys())
            if key in seen and seen[key] != str(order):
                bad.append(str(url))
            seen[key] = str(order)
    return finding(bad, "param order analysis", heuristic=True)


@check(20)  # Links Pointing to Site Search Pages (CRAWL+, heuristic)
def c20(ctx):
    bad = []
    for url, links in _list_col_items(ctx, S.COL_LINKS_URL):
        if any(SEARCH_PATH_RE.search(str(link)) for link in links):
            bad.append(str(url))
    return finding(bad, "internal link path pattern", heuristic=True)


@check(21)  # Other Error Preventing Rendering (RENDER)
def c21(ctx):
    rr = ctx.render_results
    if rr is None:
        return _render_gate(ctx, "Render-blocking errors require a headless render pass.")
    affected = [
        u for u, r in rr.items()
        if not r.get("ok") or r.get("rendered_text_len", 0) < 20
    ]
    return finding(affected, "headless render (load failure / blank page)",
                   note=f"{len(rr)} URL(s) rendered headlessly.")


@check(22)  # Hidden Uncrawlable Content Behind JS (RENDER)
def c22(ctx):
    rr = ctx.render_results
    if rr is None:
        return _render_gate(ctx, "Diffing static vs rendered content requires rendering.")
    static_text = _static_map(ctx, S.COL_BODY_TEXT)
    affected = []
    for url, r in rr.items():
        if not r.get("ok"):
            continue
        static_len = len(str(static_text.get(str(url).rstrip("/")) or "").strip())
        rendered_len = r.get("rendered_text_len", 0)
        if rendered_len > static_len * 1.5 and rendered_len - static_len > 500:
            affected.append(str(url))
    return finding(affected, "static vs rendered text length", heuristic=True,
                   note="Rendered content is substantially larger than the static HTML.")


@check(23)  # Multiple <head> Tags (CRAWL+)
def c23(ctx):
    mask = _count_col(ctx, "head_count").fillna(0) > 1
    return finding(ctx.urls_where(mask), "raw HTML structure (selectors)")


@check(24)  # Canonicals Point Away from Main Site (CRAWL)
def c24(ctx):
    bad = []
    for url, canon in zip(ctx.col(S.COL_URL), ctx.col(S.COL_CANONICAL)):
        if not canon or str(canon).lower() == "nan":
            continue
        host = urlparse(str(canon)).netloc.lower()
        if host and ctx.primary_host and host != ctx.primary_host and host not in ctx.internal_hosts:
            bad.append(str(url))
    return finding(bad, "canonical host vs primary domain")


# --------------------------------------------------------------------- CWV
@check(25)
def c25(ctx):
    res = _psi_assess(ctx, ("largest-contentful-paint",),
                      lambda e: (e.get("numericValue") or 0) > 2500)
    return res or _cwv_gate(ctx, "Largest Contentful Paint requires Lighthouse/CrUX.")


@check(26)
def c26(ctx):
    res = _psi_assess(ctx, ("cumulative-layout-shift",),
                      lambda e: (e.get("numericValue") or 0) > 0.1)
    return res or _cwv_gate(ctx, "Cumulative Layout Shift requires Lighthouse/CrUX.")


@check(27)
def c27(ctx):
    res = _psi_assess(ctx, ("uses-responsive-images",), _binary_fail)
    return res or _cwv_gate(ctx, "Properly sized images requires rendered vs intrinsic size (Lighthouse).")


@check(28)  # Eliminate Render-Blocking Resources (CWV, partial heuristic)
def c28(ctx):
    res = _psi_assess(ctx, ("render-blocking-resources",), _binary_fail)
    if res is not None:
        return res
    bad = []
    styles_col = (
        ctx.df["head_stylesheet_href"]
        if "head_stylesheet_href" in ctx.columns
        else [None] * len(ctx.df)
    )
    for url, raw_scripts, raw_styles in zip(
        ctx.col(S.COL_URL), ctx.col("head_script_src"), styles_col
    ):
        if S.split_list(raw_scripts) or S.split_list(raw_styles):
            bad.append(str(url))
    return _cwv_partial(ctx, bad, "head sync script/stylesheet (heuristic)")


@check(29)  # Efficiently Encode Images (CWV partial)
def c29(ctx):
    res = _psi_assess(ctx, ("uses-optimized-images",), _binary_fail)
    if res is not None:
        return res
    # Heuristic: presence of large legacy-format images by extension.
    legacy = []
    for url, imgs in _list_col_items(ctx, S.COL_IMG_SRC):
        if any(str(i).lower().split("?")[0].endswith((".bmp", ".tiff", ".png")) for i in imgs):
            legacy.append(str(url))
    return _cwv_partial(ctx, legacy, "image format heuristic")


@check(30)  # Incorrectly Formatted Structured Data (CRAWL)
def c30(ctx):
    has_jsonld = [c for c in ctx.columns if c.startswith(S.JSONLD_PREFIX)]
    if not has_jsonld:
        return CheckResult(NOT_PRESENT, "jsonld parse", note="No JSON-LD found.")
    type_col = next((c for c in has_jsonld if c.lower().endswith("@type")), None)
    ctx_col = next((c for c in has_jsonld if c.lower().endswith("@context")), None)
    # JSON-LD present (some jsonld col non-null) but missing @type/@context.
    present = ctx.df[has_jsonld].notna().any(axis=1)
    types = ctx.col(type_col) if type_col else pd.Series([None] * len(ctx.df))
    contexts = ctx.col(ctx_col) if ctx_col else pd.Series([None] * len(ctx.df))
    bad = [
        str(url)
        for url, has_any, t, c in zip(ctx.col(S.COL_URL), present, types, contexts)
        if has_any and (pd.isna(t) or pd.isna(c))
    ]
    return finding(bad, "jsonld schema validation")


@check(31)  # Missing/Incorrect Organization Schema (CRAWL)
def c31(ctx):
    return _schema_type_presence(ctx, "Organization")


@check(32)  # Missing/Incorrect LocalBusiness Schema (CRAWL)
def c32(ctx):
    return _schema_type_presence(ctx, "LocalBusiness", advisory=True)


@check(33)  # Title - Outside Head (CRAWL+)
def c33(ctx):
    total = _count_col(ctx, "title_count").fillna(0)
    in_head = _count_col(ctx, "title_in_head_count").fillna(0)
    return finding(ctx.urls_where(total > in_head), "raw HTML structure (selectors)")


@check(34)  # Missing H1s (CRAWL)
def c34(ctx):
    html = _html200(ctx)
    h1_empty = ctx.col(S.COL_H1).isna() | ctx.col(S.COL_H1).astype(str).str.strip().isin(["", "nan"])
    return finding(ctx.urls_where(html & h1_empty), "crawl h1 column")


@check(35)  # Content in ::before / ::after (RENDER)
def c35(ctx):
    rr = ctx.render_results
    if rr is None:
        return _render_gate(ctx, "CSS generated content requires render/CSS parse.")
    affected = [u for u, r in rr.items()
                if r.get("ok") and r.get("pseudo_content_count", 0) > 0]
    return finding(affected, "computed ::before/::after content", heuristic=True,
                   note="CSS-generated text is not indexable content.")


@check(36)  # Google Search Console Activated (EXTERNAL, partial)
def c36(ctx):
    if not ctx.has_gsc:
        return not_assessed("Requires GSC API; meta-token presence only is partial.", "GSC API")
    return not_assessed("GSC credentials supplied but verification lookup not implemented.", "GSC API")


@check(37)  # Soft 404s in XML Sitemap (CRAWL+, heuristic)
def c37(ctx):
    if ctx.sitemap_df is None or "loc" not in (ctx.sitemap_df.columns if ctx.sitemap_df is not None else []):
        return not_assessed("No sitemap data available.", "sitemap")
    sitemap_urls = set(ctx.sitemap_df["loc"].astype(str))
    body_len = ctx.col(S.COL_BODY_TEXT).fillna("").astype(str).str.len()
    status = ctx.status_int()
    bad = [
        str(url)
        for url, code, blen in zip(ctx.col(S.COL_URL), status, body_len)
        if str(url) in sitemap_urls and code == 200 and blen < 200
    ]
    return finding(bad, "sitemap URL thin body", heuristic=True)


@check(38)  # Missing XML Sitemap Links in Robots.txt (CRAWL)
def c38(ctx):
    if ctx.robots_url is None:
        return not_assessed("robots.txt not fetched.", "robots.txt")
    text = ctx.robots_text or ""
    if not re.search(r"(?im)^\s*sitemap\s*:", text):
        return CheckResult(PRESENT, "robots.txt", [ctx.robots_url], "No Sitemap: directive in robots.txt.")
    return CheckResult(NOT_PRESENT, "robots.txt")


@check(39)  # Incorrect XML Sitemap Links in Robots.txt (CRAWL)
def c39(ctx):
    text = ctx.robots_text or ""
    sitemaps = re.findall(r"(?im)^\s*sitemap\s*:\s*(\S+)", text)
    if not sitemaps:
        return not_assessed("No Sitemap: directive to validate.", "robots.txt")
    statuses = fetch.head_statuses(sitemaps, ctx.settings.audit_url_sample, ctx.settings.default_user_agent,
                                 rate=ctx.settings.default_crawl_speed)
    if statuses is None:
        return not_assessed("Could not fetch robots.txt Sitemap: URLs.", "sitemap")
    bad = [u for u, code in statuses.items() if code >= 400]
    return finding(bad, "sitemap URL fetch")


@check(40)  # Relative URLs in Canonical Tags (CRAWL)
def c40(ctx):
    bad = []
    for url, canon in zip(ctx.col(S.COL_URL), ctx.col(S.COL_CANONICAL)):
        if canon and str(canon).lower() != "nan" and not urlparse(str(canon)).scheme:
            bad.append(str(url))
    return finding(bad, "canonical absolute-URL check")


@check(41)  # Broken Images (CRAWL+)
def c41(ctx):
    imgs = _collect_assets(ctx, [S.COL_IMG_SRC])
    if not imgs:
        return CheckResult(NOT_PRESENT, "image HEAD-crawl", note="No images found.")
    statuses = fetch.head_statuses(imgs, ctx.settings.audit_url_sample, ctx.settings.default_user_agent,
                                 rate=ctx.settings.default_crawl_speed)
    if statuses is None:
        return not_assessed("Image HEAD-crawl could not reach any image.", "image HEAD-crawl")
    broken = [u for u, code in statuses.items() if code >= 400]
    return finding(broken, "image HEAD-crawl")


@check(42)  # Minify JavaScript (CRAWL+, heuristic)
def c42(ctx):
    return _minify_check(ctx, "script_src", (".js",))


@check(43)  # Minify CSS (CRAWL+, heuristic)
def c43(ctx):
    return _minify_check(ctx, "stylesheet_href", (".css",))


@check(44)  # Enable Text Compression (CRAWL)
def c44(ctx):
    html = _html200(ctx)
    enc = ctx.header("Content-Encoding").astype(str).str.lower()
    compressed = enc.str.contains("gzip|br|deflate|zstd", regex=True, na=False)
    return finding(ctx.urls_where(html & ~compressed), "content-encoding header")


@check(45)  # Efficient Cache Policy for Static Assets (CRAWL+)
def c45(ctx):
    assets = _collect_assets(ctx, ["script_src", "stylesheet_href", S.COL_IMG_SRC])
    if not assets:
        return CheckResult(NOT_PRESENT, "asset HEAD-crawl", note="No static assets found.")
    sample = assets[: ctx.settings.audit_url_sample]
    bodies = fetch.head_statuses(sample, ctx.settings.audit_url_sample, ctx.settings.default_user_agent,
                                 rate=ctx.settings.default_crawl_speed)
    if bodies is None:
        return not_assessed("Could not HEAD-crawl static assets for cache headers.", "asset HEAD-crawl")
    # We only confirmed reachability here; cache-header inspection needs response
    # headers which HEAD captured server-side. Report heuristically as assessed-empty.
    return CheckResult(NOT_PRESENT, "asset HEAD-crawl",
                       note="Assets reachable; supply header-crawl job for cache-control detail.")


@check(46)  # Avoid document.write() (CRAWL+)
def c46(ctx):
    scripts = _collect_assets(ctx, ["script_src"])
    bodies = fetch.fetch_bodies(scripts, ctx.settings.audit_url_sample, ctx.settings.default_user_agent,
                                 rate=ctx.settings.default_crawl_speed)
    if bodies is None:
        # Fall back to inline HTML body_text scan.
        bad = ctx.urls_where(ctx.col(S.COL_BODY_TEXT).astype(str).str.contains("document.write", na=False))
        if not scripts:
            return finding(bad, "inline HTML scan")
        return not_assessed("Could not fetch JS bodies to scan for document.write().", "JS fetch")
    bad = [u for u, body in bodies.items() if "document.write" in body]
    return finding(bad, "JS body scan")


@check(47)  # Woff2 Fonts (CRAWL+, heuristic)
def c47(ctx):
    bad = []
    for url, styles in _list_col_items(ctx, "stylesheet_href"):
        if any(str(s).lower().endswith((".woff", ".ttf", ".otf", ".eot")) for s in styles):
            bad.append(str(url))
    # Also scan body_text references.
    return finding(bad, "font reference heuristic", heuristic=True,
                   note="Detects non-woff2 font references in linked resources.")


@check(48)  # Server Errors (5xx) (CRAWL)
def c48(ctx):
    code = ctx.status_int()
    return finding(ctx.urls_where((code >= 500) & (code < 600)), "crawl status codes")


@check(49)  # Mixed Content (CRAWL)
def c49(ctx):
    bad = []
    cols = [c for c in ("script_src", "stylesheet_href", S.COL_IMG_SRC, S.COL_LINKS_URL)
            if c in ctx.columns]
    ref_series = [ctx.df[c] for c in cols]
    for url, *raws in zip(ctx.col(S.COL_URL), *ref_series):
        if not str(url).lower().startswith("https://"):
            continue
        refs = [item for raw in raws for item in S.split_list(raw)]
        if any(str(r).lower().startswith("http://") for r in refs):
            bad.append(str(url))
    return finding(bad, "https page referencing http assets")


@check(50)  # Malformed URLs (CRAWL)
def c50(ctx):
    bad = []
    for url in ctx.col(S.COL_URL):
        s = str(url)
        parsed = urlparse(s)
        if not parsed.scheme or not parsed.netloc or " " in s or any(ord(c) < 32 for c in s):
            bad.append(s)
    return finding(bad, "url parse validation")


@check(51)  # Utilize Preconnect (CRAWL+, advisory)
def c51(ctx):
    have = ctx.urls_where(ctx.col("preconnect_href").notna() &
                          ctx.col("preconnect_href").astype(str).ne("nan"))
    # Advisory: report presence; absence is not a failure.
    status = PRESENT if have else NOT_PRESENT
    return CheckResult(status, "link rel=preconnect (advisory)", have,
                       note="Advisory: preconnect can speed key origins.")


@check(52)  # Viewport Tag Missing width/initial-scale (CRAWL)
def c52(ctx):
    html = _html200(ctx)
    vp = ctx.col(S.COL_VIEWPORT).fillna("").astype(str).str.lower()
    bad_mask = html & (vp.eq("") | ~(vp.str.contains("width") & vp.str.contains("initial-scale")))
    return finding(ctx.urls_where(bad_mask), "viewport meta content")


@check(53)  # Use Video Formats for Animated Content (CWV partial)
def c53(ctx):
    res = _psi_assess(ctx, ("efficient-animated-content",), _binary_fail)
    if res is not None:
        return res
    gifs = []
    for url, imgs in _list_col_items(ctx, S.COL_IMG_SRC):
        if any(str(i).lower().split("?")[0].endswith(".gif") for i in imgs):
            gifs.append(str(url))
    return _cwv_partial(ctx, gifs, ".gif usage heuristic")


@check(54)  # Font Loading: @import (CRAWL+)
def c54(ctx):
    css = _collect_assets(ctx, ["stylesheet_href"])
    bodies = fetch.fetch_bodies(css, ctx.settings.audit_url_sample, ctx.settings.default_user_agent,
                                 rate=ctx.settings.default_crawl_speed)
    if bodies is None:
        if not css:
            return CheckResult(NOT_PRESENT, "CSS fetch", note="No stylesheets found.")
        return not_assessed("Could not fetch CSS to scan for @import.", "CSS fetch")
    bad = [u for u, body in bodies.items() if "@import" in body]
    return finding(bad, "CSS @import scan")


@check(55)  # HTML Code in Meta Descriptions (CRAWL)
def c55(ctx):
    md = ctx.col(S.COL_META_DESC).fillna("").astype(str)
    mask = md.str.contains(r"<[^>]+>", regex=True, na=False)
    return finding(ctx.urls_where(mask), "meta description content")


@check(56)  # Multiple Meta Descriptions (CRAWL+)
def c56(ctx):
    mask = _count_col(ctx, "meta_desc_total_count").fillna(0) > 1
    return finding(ctx.urls_where(mask), "raw HTML structure (selectors)")


@check(57)  # Missing XML Sitemap in GSC (EXTERNAL)
def c57(ctx):
    if not ctx.has_gsc:
        return not_assessed("Requires GSC sitemaps report.", "GSC API")
    return not_assessed("GSC supplied but sitemaps report lookup not implemented.", "GSC API")


@check(58)  # XML Sitemap Over 50,000 URLs or 50 MB (CRAWL)
def c58(ctx):
    if ctx.sitemap_df is None:
        return not_assessed("No sitemap data available.", "sitemap")
    n = len(ctx.sitemap_df)
    if n > 50000:
        return CheckResult(PRESENT, "sitemap row count", [str(ctx.robots_url or "sitemap")],
                           note=f"Sitemap has {n} URLs (>50,000).")
    return CheckResult(NOT_PRESENT, "sitemap row count", note=f"{n} URLs.")


@check(59)  # Broken Redirects (CRAWL)
def c59(ctx):
    code = ctx.status_int()
    has_redirect = ctx.col(S.COL_REDIRECT_URLS).notna() & ctx.col(S.COL_REDIRECT_URLS).astype(str).ne("nan")
    mask = has_redirect & (code >= 400)
    return finding(ctx.urls_where(mask), "redirect chain terminal status")


@check(60)  # Redirect Loops (CRAWL)
def c60(ctx):
    bad = []
    for url, chain in _list_col_items(ctx, S.COL_REDIRECT_URLS):
        hops = [str(url)] + [str(c) for c in chain]
        if len(hops) != len(set(h.rstrip("/") for h in hops)):
            bad.append(str(url))
    return finding(bad, "redirect chain repetition")


@check(61)  # Meta NoSnippet URLs (CRAWL)
def c61(ctx):
    return _robots_directive(ctx, "nosnippet")


@check(62)  # Meta NoArchive URLs (CRAWL)
def c62(ctx):
    return _robots_directive(ctx, "noarchive")


@check(63)  # Noindex Canonicalized URLs (CRAWL)
def c63(ctx):
    robots = ctx.col(S.COL_META_ROBOTS).astype(str).str.lower()
    has_canon = ctx.col(S.COL_CANONICAL).notna() & ctx.col(S.COL_CANONICAL).astype(str).ne("nan")
    mask = robots.str.contains("noindex", na=False) & has_canon
    return finding(ctx.urls_where(mask), "noindex + canonical conflict")


@check(64)  # Crawlable Filter URLs (CRAWL+, heuristic)
def c64(ctx):
    return _crawlable_param_links(ctx, FILTER_PARAMS)


@check(65)  # Crawlable Sorting/Order URLs (CRAWL+, heuristic)
def c65(ctx):
    return _crawlable_param_links(ctx, SORT_PARAMS)


@check(66)  # Rel=Nofollow on Internal Links to Indexable Pages (CRAWL)
def c66(ctx):
    idx = _indexable_map(ctx)
    bad = []
    for url, raw_links, raw_nf in zip(
        ctx.col(S.COL_URL), ctx.col(S.COL_LINKS_URL), ctx.col(S.COL_LINKS_NOFOLLOW)
    ):
        links = S.split_list(raw_links)
        nofollows = S.split_list(raw_nf)
        for j, link in enumerate(links):
            nf = j < len(nofollows) and str(nofollows[j]).lower() == "true"
            host = urlparse(str(link)).netloc.lower()
            if nf and host in ctx.internal_hosts and idx.get(str(link).rstrip("/"), False):
                bad.append(str(url))
                break
    return finding(bad, "nofollow internal link to indexable target")


@check(67)  # Search URLs in Index (CRAWL+, heuristic)
def c67(ctx):
    idx = _indexable_map(ctx)
    bad = [u for u in ctx.col(S.COL_URL)
           if SEARCH_PATH_RE.search(str(u)) and idx.get(str(u).rstrip("/"), False)]
    return finding(bad, "indexable search-path URLs", heuristic=True)


@check(68)  # Session IDs in Index (CRAWL+, heuristic)
def c68(ctx):
    idx = _indexable_map(ctx)
    bad = [u for u in ctx.col(S.COL_URL)
           if (_query_params(u) & SESSION_PARAMS) and idx.get(str(u).rstrip("/"), False)]
    return finding(bad, "session-id params on indexable URLs", heuristic=True)


@check(69)  # Inconsistency Between Mobile & Desktop Content (CRAWL+, heuristic)
def c69(ctx):
    if ctx.second_ua_df is None:
        return not_assessed("Requires a second crawl under a mobile UA to diff.", "2nd-UA crawl")
    return not_assessed("Mobile-UA crawl supplied but diff not implemented.", "2nd-UA crawl")


@check(70)  # Product URL Variations (CRAWL+, heuristic)
def c70(ctx):
    seen: dict[str, list[str]] = {}
    for url in ctx.col(S.COL_URL):
        base = urlparse(str(url))._replace(query="").geturl().rstrip("/")
        if urlparse(str(url)).query:
            seen.setdefault(base, []).append(str(url))
    bad = [u for variants in seen.values() if len(variants) > 1 for u in variants]
    return finding(bad, "param variant duplication", heuristic=True)


@check(71)  # Intrusive Interstitial Usage (RENDER)
def c71(ctx):
    rr = ctx.render_results
    if rr is None:
        return _render_gate(ctx, "Intrusive interstitials require render / CrUX field data.")
    affected = [u for u, r in rr.items() if r.get("ok") and r.get("overlay")]
    return finding(affected, "rendered viewport overlay detection", heuristic=True,
                   note="A high z-index element covers >=50% of the viewport on load.")


@check(72)  # Broken Backlinks (EXTERNAL)
def c72(ctx):
    if not ctx.has_backlinks:
        return not_assessed("Requires a backlink source to join to crawl status.", "backlink source")
    return not_assessed("Backlink source supplied but join not implemented.", "backlink source")


@check(73)  # Improper Handling of Discontinued Products (CRAWL+, partial)
def c73(ctx):
    return not_assessed("Requires a product URL list to classify 404 vs 301.", "product list")


@check(74)  # Meta Description - Outside Head (CRAWL+)
def c74(ctx):
    total = _count_col(ctx, "meta_desc_total_count").fillna(0)
    in_head = _count_col(ctx, "meta_desc_in_head_count").fillna(0)
    return finding(ctx.urls_where(total > in_head), "raw HTML structure (selectors)")


@check(75)  # JS Rotating Titles (RENDER)
def c75(ctx):
    rr = ctx.render_results
    if rr is None:
        return _render_gate(ctx, "JS-rotated titles require rendering.")
    affected = [
        u for u, r in rr.items()
        if r.get("ok") and r.get("title_at_load") != r.get("title_after_wait")
    ]
    return finding(affected, "rendered title observed over time",
                   note="Title changed via JS after page load.")


@check(76)  # Multiple Titles (CRAWL+)
def c76(ctx):
    mask = _count_col(ctx, "title_count").fillna(0) > 1
    return finding(ctx.urls_where(mask), "raw HTML structure (selectors)")


@check(77)  # Missing XML Sitemap (CRAWL)
def c77(ctx):
    if ctx.sitemap_df is not None and len(ctx.sitemap_df):
        return CheckResult(NOT_PRESENT, "sitemap discovery")
    if ctx.robots_url is None:
        return not_assessed("Sitemap discovery not attempted.", "sitemap")
    return CheckResult(PRESENT, "sitemap discovery", [ctx.robots_url or ctx.primary_host],
                       note="No sitemap found via robots.txt or common paths.")


@check(78)  # Incorrect XML Sitemap Formatting (CRAWL)
def c78(ctx):
    if ctx.sitemap_df is None:
        return not_assessed("No sitemap fetched to validate.", "sitemap")
    if "errors" in ctx.sitemap_df.columns:
        errs = ctx.sitemap_df["errors"].dropna()
        if len(errs):
            return CheckResult(PRESENT, "sitemap parse", errs.astype(str).head(5).tolist())
    return CheckResult(NOT_PRESENT, "sitemap parse")


@check(79)  # Missing Canonicals (CRAWL)
def c79(ctx):
    html = _html200(ctx)
    empty = ctx.col(S.COL_CANONICAL).isna() | ctx.col(S.COL_CANONICAL).astype(str).str.strip().isin(["", "nan"])
    return finding(ctx.urls_where(html & empty), "crawl canonical column")


@check(80)  # Robots.txt Disallows Images (CRAWL)
def c80(ctx):
    disallows = _parse_disallows(ctx)
    if not disallows and ctx.robots_url is None:
        return not_assessed("robots.txt not fetched.", "robots.txt")
    compiled = _compile_disallows(disallows)
    imgs = _collect_assets(ctx, [S.COL_IMG_SRC])
    blocked = [a for a in imgs if _path_blocked(urlparse(a).path, compiled)]
    return finding(blocked, "robots.txt rule match")


@check(81)  # JavaScript Onclick Links (CRAWL+)
def c81(ctx):
    # Heuristic from body_text is unreliable; we rely on absence of href anchors.
    # advertools captures <a href> only; onclick-only anchors won't appear as links.
    # Report as assessed-empty unless raw HTML refetch is enabled.
    bodies = None
    if ctx.settings.audit_url_sample:
        sample = ctx.col(S.COL_URL).astype(str).tolist()
        bodies = fetch.fetch_bodies(sample, min(ctx.settings.audit_url_sample, 50),
                                    ctx.settings.default_user_agent,
                                    rate=ctx.settings.default_crawl_speed)
    if bodies is None:
        return not_assessed("Detecting onclick-only navigation needs raw HTML refetch.", "HTML refetch")
    bad = [u for u, body in bodies.items() if re.search(r"<a(?![^>]*href=)[^>]*onclick=", body, re.I)]
    return finding(bad, "raw HTML onclick scan")


@check(82)  # Lack of Redirects to Canonical Version (CRAWL+)
def c82(ctx):
    if not ctx.primary_host:
        return not_assessed("No primary host to test variants.", "variant fetch")
    scheme = "https"
    base = f"{scheme}://{ctx.primary_host}/"
    variants = [
        f"http://{ctx.primary_host}/",
        f"https://www.{ctx.primary_host}/" if not ctx.primary_host.startswith("www.") else f"https://{ctx.primary_host[4:]}/",
    ]
    statuses = fetch.head_statuses(variants, ctx.settings.audit_url_sample, ctx.settings.default_user_agent,
                                 rate=ctx.settings.default_crawl_speed)
    if statuses is None:
        return not_assessed("Could not fetch host variants to test canonicalisation.", "variant fetch")
    # Heuristic: a 200 (not redirect) on a non-canonical variant is a finding.
    bad = [u for u, code in statuses.items() if 200 <= code < 300]
    return finding(bad, "host variant redirect test", heuristic=True)


@check(83)  # Excessive JSON File Crawling (CRAWL)
def c83(ctx):
    jsons = [str(u) for u in ctx.col(S.COL_URL) if str(u).lower().split("?")[0].endswith(".json")]
    return finding(jsons, "crawled .json URLs")


@check(84)  # Auto-Generated Pagination Parameters (CRAWL+, heuristic)
def c84(ctx):
    counts: dict[str, int] = {}
    for url in ctx.col(S.COL_URL):
        if _query_params(url) & PAGINATION_PARAMS:
            base = urlparse(str(url))._replace(query="").geturl()
            counts[base] = counts.get(base, 0) + 1
    bad = [base for base, n in counts.items() if n > 5]
    return finding(bad, "pagination param explosion", heuristic=True,
                   note="Base paths with many paginated parameter variants.")


@check(85)  # Next-Gen Images (CWV partial)
def c85(ctx):
    res = _psi_assess(ctx, ("modern-image-formats",), _binary_fail)
    if res is not None:
        return res
    legacy = []
    for url, imgs in _list_col_items(ctx, S.COL_IMG_SRC):
        exts = [str(i).lower().split("?")[0] for i in imgs]
        if exts and not any(e.endswith((".webp", ".avif")) for e in exts):
            legacy.append(str(url))
    return _cwv_partial(ctx, legacy, "next-gen format adoption heuristic")


@check(86)  # Preload Key Requests (CWV partial)
def c86(ctx):
    res = _psi_assess(ctx, ("uses-rel-preload",), _binary_fail)
    if res is not None:
        return res
    have = ctx.urls_where(ctx.col("preload_href").notna() &
                          ctx.col("preload_href").astype(str).ne("nan"))
    # Partial: presence of preload is a positive signal; report heuristically.
    return _cwv_partial(ctx, [], "link rel=preload presence",
                        note=f"{len(have)} page(s) use rel=preload.")


@check(87)  # Preload LCP (CWV)
def c87(ctx):
    res = _psi_assess(ctx, ("prioritize-lcp-image", "preload-lcp-image"), _binary_fail)
    return res or _cwv_gate(ctx, "Preloading the LCP element requires LCP identification (render).")


@check(88)  # Lazy Load Third-Party with Facades (CWV)
def c88(ctx):
    res = _psi_assess(ctx, ("third-party-facades",), _binary_fail)
    return res or _cwv_gate(ctx, "Third-party facade detection requires Lighthouse.")


@check(89)  # Duplicate Scripts Loaded (CRAWL+)
def c89(ctx):
    bad = []
    for url, scripts in _list_col_items(ctx, "script_src"):
        norm = [str(s).split("?")[0] for s in scripts]
        if len(norm) != len(set(norm)):
            bad.append(str(url))
    return finding(bad, "duplicate script src in HTML")


@check(90)  # Display:None Content (RENDER)
def c90(ctx):
    rr = ctx.render_results
    if rr is None:
        return _render_gate(ctx, "display:none content requires CSS/render to confirm.")
    affected = [u for u, r in rr.items()
                if r.get("ok") and r.get("hidden_text_len", 0) >= 200]
    return finding(affected, "computed display:none text volume", heuristic=True,
                   note="Substantial text is hidden with display:none in the rendered page.")


@check(91)  # Orphaned URLs in XML Sitemap (CRAWL)
def c91(ctx):
    if ctx.sitemap_df is None or "loc" not in ctx.sitemap_df.columns:
        return not_assessed("No sitemap data available.", "sitemap vs crawl link graph")
    if ctx.is_discovery is None:
        return not_assessed(
            "Crawl mode unknown; orphan detection requires a discovery (link-following) crawl.",
            "sitemap vs crawl link graph",
        )
    if not ctx.is_discovery:
        return not_assessed(
            "List-mode crawl: only seeded URLs were fetched, so a sitemap URL being "
            "absent does not prove it is unlinked. Re-run with a discovery crawl.",
            "sitemap vs crawl link graph",
        )
    # A sitemap URL is orphaned if the discovery crawl neither reached it nor
    # saw any internal link pointing at it.
    known = {str(u).rstrip("/") for u in ctx.col(S.COL_URL)}
    for raw in ctx.col(S.COL_LINKS_URL):
        for link in S.split_list(raw):
            known.add(str(link).rstrip("/"))
    orphans = [
        str(loc)
        for loc in ctx.sitemap_df["loc"].astype(str)
        if loc.rstrip("/") not in known
    ]
    return finding(
        orphans,
        "sitemap vs crawl link graph",
        note=(
            "Sitemap URLs the discovery crawl neither fetched nor found linked. "
            "Most reliable when the crawl completed without hitting max_pages."
        ),
    )


# ------------------------------------------------------------ shared routines
def _render_gate(ctx, reason: str) -> CheckResult:
    if not ctx.has_render:
        return not_assessed(f"{reason} (rendering disabled)", "headless render")
    return not_assessed(
        "Rendering enabled but headless browser unavailable "
        "(pip install 'advertools-mcp[render]' && playwright install chromium).",
        "headless render",
    )


def _static_map(ctx, col) -> dict:
    """Normalised URL -> raw value of a crawl column (for static-vs-rendered diffs)."""
    return {
        str(u).rstrip("/"): v
        for u, v in zip(ctx.col(S.COL_URL), ctx.col(col))
    }


def _cwv_gate(ctx, reason: str) -> CheckResult:
    if not ctx.has_lighthouse:
        return not_assessed(f"{reason}", "Lighthouse/CrUX")
    return not_assessed(
        "Lighthouse key supplied but no PageSpeed Insights result resolved "
        "(API error, quota, or unreachable URLs).",
        "PageSpeed Insights",
    )


def _binary_fail(entry: dict) -> bool:
    score = entry.get("score")
    return score is not None and score < 0.9


def _psi_assess(ctx, audit_keys: tuple[str, ...], fail_fn) -> Optional[CheckResult]:
    """Evaluate a CWV check from PSI results. None when PSI has no data for it
    (caller falls back to its gate or static heuristic)."""
    if ctx.psi_results is None:
        return None
    affected: list[str] = []
    assessed = 0
    for url, res in ctx.psi_results.items():
        audits = res.get("audits", {})
        entry = next((audits[k] for k in audit_keys if k in audits), None)
        if entry is None:
            continue
        assessed += 1
        if fail_fn(entry):
            affected.append(str(url))
    if assessed == 0:
        return None  # Lighthouse version lacks this audit; let caller fall back
    note = f"{assessed} URL(s) assessed via PageSpeed Insights."
    if affected:
        return CheckResult(PRESENT, "PageSpeed Insights", affected, note)
    return CheckResult(NOT_PRESENT, "PageSpeed Insights", note=note)


def _cwv_partial(ctx, affected: list[str], source: str, note: str = "") -> CheckResult:
    base_note = (note + " " if note else "") + "Static heuristic only; full assessment needs Lighthouse."
    if affected:
        return CheckResult(HEURISTIC, source, affected, base_note)
    return CheckResult(NOT_PRESENT, source, note=base_note)


def _collect_assets(ctx, columns: list[str]) -> list[str]:
    seen: list[str] = []
    s = set()
    for col in columns:
        if col not in ctx.columns:
            continue
        for raw in ctx.df[col]:
            for item in S.split_list(raw):
                item = str(item)
                if item and item.lower() != "nan" and item not in s:
                    s.add(item)
                    seen.append(item)
    return seen


def _minify_check(ctx, col: str, exts: tuple[str, ...]) -> CheckResult:
    assets = [a for a in _collect_assets(ctx, [col]) if a.lower().split("?")[0].endswith(exts)]
    bodies = fetch.fetch_bodies(assets, ctx.settings.audit_url_sample, ctx.settings.default_user_agent,
                                 rate=ctx.settings.default_crawl_speed)
    if bodies is None:
        if not assets:
            return CheckResult(NOT_PRESENT, "asset fetch", note="No matching assets found.")
        return not_assessed("Could not fetch assets to assess minification.", "asset fetch")
    bad = []
    for u, body in bodies.items():
        if not body:
            continue
        newline_ratio = body.count("\n") / max(len(body), 1)
        if newline_ratio > 0.02:  # unminified files have many line breaks
            bad.append(u)
    return CheckResult(HEURISTIC if bad else NOT_PRESENT, "whitespace ratio heuristic", bad)


def _robots_directive(ctx, directive: str) -> CheckResult:
    robots = ctx.col(S.COL_META_ROBOTS).astype(str).str.lower()
    mask = robots.str.contains(directive, na=False)
    return finding(ctx.urls_where(mask), f"meta robots {directive}")


def _schema_type_presence(ctx, schema_type: str, advisory: bool = False) -> CheckResult:
    type_cols = [c for c in ctx.columns if c.startswith(S.JSONLD_PREFIX) and c.lower().endswith("@type")]
    if not type_cols:
        note = f"No JSON-LD {schema_type} schema found on the site."
        return CheckResult(NOT_PRESENT, "jsonld @type", note=note)
    found = False
    for col in type_cols:
        if ctx.df[col].astype(str).str.contains(schema_type, case=False, na=False).any():
            found = True
            break
    if found:
        return CheckResult(NOT_PRESENT, "jsonld @type", note=f"{schema_type} schema present.")
    # Missing schema is the finding.
    return CheckResult(PRESENT, "jsonld @type", [ctx.primary_host or "site"],
                       note=f"No {schema_type} schema detected.")


def _crawlable_param_links(ctx, params: set[str]) -> CheckResult:
    bad = []
    for url, raw_links, raw_nf in zip(
        ctx.col(S.COL_URL), ctx.col(S.COL_LINKS_URL), ctx.col(S.COL_LINKS_NOFOLLOW)
    ):
        links = S.split_list(raw_links)
        nofollows = S.split_list(raw_nf)
        for j, link in enumerate(links):
            if _query_params(link) & params:
                nf = j < len(nofollows) and str(nofollows[j]).lower() == "true"
                if not nf:
                    bad.append(str(url))
                    break
    return finding(bad, "crawlable param links (no nofollow)", heuristic=True)
