"""Flat, one-row-per-URL crawl detail export (``crawl-detail.csv``).

Modelled on Screaming Frog's Internal tab (All filter): a wide, human-readable
row per URL. List-type crawl columns (links, images, hreflang, headings,
structured-data types) are reduced to counts and/or the first value(s) so the
file stays one row per URL and opens cleanly in any spreadsheet.

The same :func:`build_export_frame` powers both the standalone CSV and the
"Crawl Detail" sheet embedded in the audit workbook.
"""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse, urlsplit, urlunsplit

import pandas as pd

from . import schema as S

# Fixed output column order (the deliverable's contract) — a fuller,
# Screaming-Frog-style column set derived from the advertools crawl.
COLUMNS = [
    "Address", "Status Code", "Status", "Indexability", "Indexability Reason",
    "Content-Type", "Content-Encoding", "Response Time", "Size (bytes)",
    "Crawl Depth", "Crawl Timestamp", "Language",
    "Title 1", "Title 1 Length",
    "Meta Description 1", "Meta Description 1 Length", "Meta Keywords 1",
    "H1-1", "H1-1 Length", "H1-2", "H1 Count",
    "H2-1", "H2-2", "H2 Count",
    "Meta Robots 1", "X-Robots-Tag 1", "Meta Refresh 1",
    "Canonical Link Element 1", "Canonical Is Self",
    "rel=next", "rel=prev", "amphtml Link Element",
    "Word Count", "Text Ratio",
    "Inlinks", "Unique Inlinks", "Outlinks", "Unique Outlinks",
    "External Outlinks", "Unique External Outlinks",
    "Images", "Images Missing Alt Text",
    "Hreflang 1", "Hreflang Count",
    "Redirect URL", "Redirect Type",
    "Last-Modified", "Server", "IP Address", "Cookies",
    "Structured Data Types", "Structured Data Count",
    "URL Length", "URL Encoded Address",
]

OUTPUT_FILENAME = "crawl-detail.csv"


def _normalise_url(url: str | None) -> str:
    if not url:
        return ""
    parts = urlsplit(str(url).strip())
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _status_phrase(code: Any) -> str:
    try:
        return HTTPStatus(int(code)).phrase
    except (ValueError, TypeError):
        return ""


def _word_count(text: Any) -> int:
    if text is None or (isinstance(text, float) and text != text):
        return 0
    return len(str(text).split())


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return "" if text.lower() == "nan" else text


def _build_inlink_counts(df: pd.DataFrame, internal_hosts: set[str]) -> tuple[dict[str, int], dict[str, int]]:
    """Return (total_inlinks, unique_source_inlinks) per normalised target URL."""
    totals: dict[str, int] = {}
    sources: dict[str, set[str]] = {}
    if S.COL_LINKS_URL not in df.columns:
        return totals, {}
    for src, raw in zip(df.get(S.COL_URL, []), df[S.COL_LINKS_URL]):
        for link in S.split_list(raw):
            host = urlparse(link).netloc.lower()
            if host in internal_hosts:
                key = _normalise_url(link)
                totals[key] = totals.get(key, 0) + 1
                sources.setdefault(key, set()).add(str(src))
    unique = {k: len(v) for k, v in sources.items()}
    return totals, unique


def build_export_frame(df: pd.DataFrame) -> pd.DataFrame:
    internal_hosts = {urlparse(u).netloc.lower() for u in df.get(S.COL_URL, []) if u}
    inlink_totals, inlink_unique = _build_inlink_counts(df, internal_hosts)
    jsonld_type_cols = [c for c in df.columns if c.startswith(S.JSONLD_PREFIX) and c.lower().endswith("@type")]

    rows: list[dict[str, Any]] = []
    # to_dict("records") is far faster than iterrows() and keeps .get() semantics.
    for r in df.to_dict("records"):
        url = r.get(S.COL_URL)
        status = r.get(S.COL_STATUS)
        h1_items = S.split_list(r.get(S.COL_H1))
        h2_items = S.split_list(r.get(S.COL_H2))
        link_items = S.split_list(r.get(S.COL_LINKS_URL))
        img_items = S.split_list(r.get(S.COL_IMG_SRC))
        img_alts = S.split_list(r.get(S.COL_IMG_ALT))
        hreflangs = S.split_list(r.get("hreflang_hreflang"))
        canonical = r.get(S.COL_CANONICAL)
        title = _clean(r.get(S.COL_TITLE))
        meta_desc = _clean(r.get(S.COL_META_DESC))

        ext_links = [l for l in link_items if urlparse(l).netloc and urlparse(l).netloc.lower() not in internal_hosts]
        images_missing_alt = sum(
            1 for i in range(len(img_items)) if (i >= len(img_alts) or not img_alts[i].strip())
        )
        canonical_abs = urljoin(str(url), str(canonical)) if canonical and str(canonical).lower() != "nan" else ""
        canonical_is_self = bool(canonical_abs) and _normalise_url(canonical_abs) == _normalise_url(url)
        indexable, reason = _indexability(status, r.get(S.COL_META_ROBOTS) or "", canonical, canonical_is_self)
        redirect_url, redirect_type = _redirect(r)

        sd_types = _structured_data_types(r, jsonld_type_cols)
        size = _to_int(r.get(S.COL_SIZE)) or 0
        body_len = len(str(_clean(r.get(S.COL_BODY_TEXT))))
        text_ratio = round(100.0 * body_len / size, 1) if size else 0.0
        setcookie = _header(r, "Set-Cookie")
        key = _normalise_url(url)

        rows.append({
            "Address": url,
            "Status Code": _to_int(status),
            "Status": _status_phrase(status),
            "Indexability": indexable,
            "Indexability Reason": reason,
            "Content-Type": _header(r, "Content-Type"),
            "Content-Encoding": _header(r, "Content-Encoding"),
            "Response Time": _to_float(r.get(S.COL_DOWNLOAD_LATENCY)),
            "Size (bytes)": size,
            "Crawl Depth": _to_int(r.get(S.COL_DEPTH)),
            "Crawl Timestamp": _clean(r.get("crawl_time")),
            "Language": _clean(r.get("html_lang")),
            "Title 1": title,
            "Title 1 Length": len(title),
            "Meta Description 1": meta_desc,
            "Meta Description 1 Length": len(meta_desc),
            "Meta Keywords 1": _clean(r.get("meta_keywords")),
            "H1-1": h1_items[0] if h1_items else "",
            "H1-1 Length": len(h1_items[0]) if h1_items else 0,
            "H1-2": h1_items[1] if len(h1_items) > 1 else "",
            "H1 Count": len(h1_items),
            "H2-1": h2_items[0] if h2_items else "",
            "H2-2": h2_items[1] if len(h2_items) > 1 else "",
            "H2 Count": len(h2_items),
            "Meta Robots 1": _clean(r.get(S.COL_META_ROBOTS)),
            "X-Robots-Tag 1": _header(r, "X-Robots-Tag"),
            "Meta Refresh 1": _clean(r.get("meta_refresh")),
            "Canonical Link Element 1": _clean(canonical),
            "Canonical Is Self": canonical_is_self,
            "rel=next": _clean(r.get("rel_next")),
            "rel=prev": _clean(r.get("rel_prev")),
            "amphtml Link Element": _clean(r.get("amphtml")),
            "Word Count": _word_count(r.get(S.COL_BODY_TEXT)),
            "Text Ratio": text_ratio,
            "Inlinks": inlink_totals.get(key, 0),
            "Unique Inlinks": inlink_unique.get(key, 0),
            "Outlinks": len(link_items),
            "Unique Outlinks": len(set(link_items)),
            "External Outlinks": len(ext_links),
            "Unique External Outlinks": len(set(ext_links)),
            "Images": len(img_items),
            "Images Missing Alt Text": images_missing_alt,
            "Hreflang 1": hreflangs[0] if hreflangs else "",
            "Hreflang Count": len(hreflangs),
            "Redirect URL": redirect_url,
            "Redirect Type": redirect_type,
            "Last-Modified": _header(r, "Last-Modified"),
            "Server": _header(r, "Server"),
            "IP Address": _clean(r.get("ip_address")),
            "Cookies": "Yes" if setcookie else "No",
            "Structured Data Types": "|".join(sd_types),
            "Structured Data Count": len(sd_types),
            "URL Length": len(str(url)) if url else 0,
            "URL Encoded Address": quote(str(url), safe=":/?#[]@!$&'()*+,;=") if url else "",
        })
    return pd.DataFrame(rows, columns=COLUMNS)


def _indexability(status, meta_robots, canonical, canonical_is_self):
    robots = str(meta_robots).lower()
    code = _to_int(status)
    if code is None or code >= 400:
        return "Non-Indexable", (f"Non-200 ({code})" if code else "No response")
    if 300 <= code < 400:
        return "Non-Indexable", "Redirect"
    if "noindex" in robots:
        return "Non-Indexable", "Noindex"
    if canonical and str(canonical).lower() != "nan" and not canonical_is_self:
        return "Non-Indexable", "Canonicalised"
    return "Indexable", ""


def _redirect(row) -> tuple[str, str]:
    urls = S.split_list(row.get(S.COL_REDIRECT_URLS))
    reasons = S.split_list(row.get(S.COL_REDIRECT_REASONS))
    if not urls:
        return "", ""
    return urls[-1], (reasons[-1] if reasons else "")


def _structured_data_types(row, jsonld_type_cols) -> list[str]:
    types: list[str] = []
    for col in jsonld_type_cols:
        for t in S.split_list(row.get(col)):
            if t and t not in types:
                types.append(t)
    return types


def _header(row, name: str) -> str:
    for key in (f"{S.RESP_HEADER_PREFIX}{name}", f"{S.RESP_HEADER_PREFIX}{name.lower()}"):
        val = row.get(key)
        if val is not None and str(val).lower() != "nan":
            return str(val)
    return ""


def _to_int(value) -> int | None:
    try:
        if value is None or (isinstance(value, float) and value != value):
            return None
        return int(float(value))
    except (ValueError, TypeError):
        return None


def _to_float(value) -> float | None:
    try:
        if value is None or (isinstance(value, float) and value != value):
            return None
        return round(float(value), 4)
    except (ValueError, TypeError):
        return None


def export_csv(parquet_path: str, out_path: str) -> dict[str, Any]:
    """Write the flat crawl-detail CSV and return a summary (never file contents)."""
    if not Path(parquet_path).exists():
        raise FileNotFoundError(parquet_path)
    df = pd.read_parquet(parquet_path)
    export = build_export_frame(df)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    export.to_csv(out_path, index=False)
    indexable = int((export["Indexability"] == "Indexable").sum())
    return {
        "csv_path": out_path,
        "rows": int(len(export)),
        "column_count": len(export.columns),
        "columns": list(export.columns),
        "indexable_urls": indexable,
        "non_indexable_urls": int(len(export) - indexable),
    }
