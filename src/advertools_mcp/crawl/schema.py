"""Knowledge of the advertools crawl output schema (advertools 0.18).

advertools writes one JSON line per crawled URL. List-type fields (links,
images, nav links) are stored as ``@@``-delimited strings in a single column.
We convert the ``.jl`` to parquet for columnar querying.

Robots meta and a handful of structural signals are *not* extracted by the
default crawl, so the worker always injects the selectors in
:data:`DEFAULT_CSS_SELECTORS` / :data:`DEFAULT_XPATH_SELECTORS` to enrich the
parquet without a second pass.
"""

from __future__ import annotations

# advertools joins repeated extractions with this token.
LIST_DELIMITER = "@@"

# Default extractions injected into every discovery/list crawl so the parquet
# carries the SEO signals advertools does not emit by default. These never
# overwrite user-supplied selectors of the same name.
DEFAULT_CSS_SELECTORS: dict[str, str] = {
    "meta_robots": "meta[name=robots]::attr(content)",
    "meta_desc_all": "meta[name=description]::attr(content)",
    "title_all": "title::text",
    "canonical_all": "link[rel=canonical]::attr(href)",
    "hreflang_hreflang": "link[rel=alternate]::attr(hreflang)",
    "hreflang_href": "link[rel=alternate]::attr(href)",
    # Asset / resource hints — power mixed-content, duplicate-script, broken-asset
    # and render-blocking / preconnect / preload checks without a second pass.
    "script_src": "script[src]::attr(src)",
    "stylesheet_href": "link[rel=stylesheet]::attr(href)",
    "preconnect_href": "link[rel=preconnect]::attr(href)",
    "preload_href": "link[rel=preload]::attr(href)",
    "head_script_src": "head script[src]::attr(src)",
    "head_stylesheet_href": "head link[rel=stylesheet]::attr(href)",
    # Richer per-URL detail (Screaming-Frog-style export columns).
    "html_lang": "html::attr(lang)",
    "meta_keywords": "meta[name=keywords]::attr(content)",
    "meta_refresh": "meta[http-equiv=refresh]::attr(content)",
    "rel_next": "link[rel=next]::attr(href)",
    "rel_prev": "link[rel=prev]::attr(href)",
    "amphtml": "link[rel=amphtml]::attr(href)",
}

DEFAULT_XPATH_SELECTORS: dict[str, str] = {
    # Structural counts used by "outside <head>" / "multiple X" audit checks.
    "head_count": "count(//head)",
    "title_count": "count(//title)",
    "canonical_in_head_count": "count(//head//link[@rel='canonical'])",
    "canonical_total_count": "count(//link[@rel='canonical'])",
    "title_in_head_count": "count(//head//title)",
    "meta_desc_in_head_count": "count(//head//meta[@name='description'])",
    "meta_desc_total_count": "count(//meta[@name='description'])",
}

# Core single-value columns we rely on across summary / export / audit.
COL_URL = "url"
COL_STATUS = "status"
COL_TITLE = "title"
COL_META_DESC = "meta_desc"
COL_META_ROBOTS = "meta_robots"
COL_H1 = "h1"
COL_H2 = "h2"
COL_CANONICAL = "canonical"
COL_BODY_TEXT = "body_text"
COL_SIZE = "size"
COL_DEPTH = "depth"
COL_DOWNLOAD_LATENCY = "download_latency"
COL_VIEWPORT = "viewport"

# List columns (``@@``-delimited).
COL_LINKS_URL = "links_url"
COL_LINKS_TEXT = "links_text"
COL_LINKS_NOFOLLOW = "links_nofollow"
COL_IMG_SRC = "img_src"
COL_IMG_ALT = "img_alt"

# Redirect columns appear only when redirects occur.
COL_REDIRECT_URLS = "redirect_urls"
COL_REDIRECT_REASONS = "redirect_reasons"
COL_REDIRECT_TIMES = "redirect_times"

RESP_HEADER_PREFIX = "resp_headers_"
REQ_HEADER_PREFIX = "request_headers_"
JSONLD_PREFIX = "jsonld_"
OG_PREFIX = "og:"


def split_list(value) -> list[str]:
    """Split an advertools ``@@``-delimited list cell into items.

    Empty / NaN cells return an empty list. Item-level empties are preserved
    (an image with no alt text is an empty string, which we still need to
    count), but a wholly empty cell yields ``[]``.
    """
    if value is None:
        return []
    text = str(value)
    if text == "" or text.lower() == "nan":
        return []
    return text.split(LIST_DELIMITER)


def header_column(name: str) -> str:
    """Map an HTTP header name to its advertools response column."""
    return f"{RESP_HEADER_PREFIX}{name}"
