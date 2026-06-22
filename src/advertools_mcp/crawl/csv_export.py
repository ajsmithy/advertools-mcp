"""Flat, one-row-per-URL CSV export modelled on Screaming Frog's Internal/All.

List-type crawl columns (links, images, hreflang, structured-data types) are
reduced to counts (plus a delimited string where useful) so the file stays one
row per URL and opens cleanly in any spreadsheet.
"""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

import pandas as pd

from . import schema as S

# Fixed output column order (the deliverable's contract).
COLUMNS = [
    "URL", "Status Code", "Status", "Indexability", "Indexability Reason",
    "Title", "Title Length", "Meta Description", "Meta Description Length",
    "H1", "H1 Count", "H2 Count", "Meta Robots", "Canonical", "Canonical Is Self",
    "Word Count", "Content-Type", "Content-Encoding", "Response Time",
    "Redirect URL", "Redirect Type", "Crawl Depth", "Inlinks", "Outlinks",
    "External Outlinks", "Images", "Images Missing Alt", "Size (bytes)",
    "Hreflang Count", "Structured Data Types",
]


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    if name in df.columns:
        return df[name]
    return pd.Series([None] * len(df), index=df.index)


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


def _build_inlink_counts(df: pd.DataFrame, internal_hosts: set[str]) -> dict[str, int]:
    """Count internal inlinks per target URL from the @@-delimited link columns."""
    counts: dict[str, int] = {}
    if S.COL_LINKS_URL not in df.columns:
        return counts
    for raw in df[S.COL_LINKS_URL]:
        for link in S.split_list(raw):
            host = urlparse(link).netloc.lower()
            if host in internal_hosts:
                key = _normalise_url(link)
                counts[key] = counts.get(key, 0) + 1
    return counts


def build_export_frame(df: pd.DataFrame) -> pd.DataFrame:
    internal_hosts = {urlparse(u).netloc.lower() for u in df.get(S.COL_URL, []) if u}
    inlink_counts = _build_inlink_counts(df, internal_hosts)

    rows: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        url = r.get(S.COL_URL)
        status = r.get(S.COL_STATUS)
        h1_items = S.split_list(r.get(S.COL_H1))
        h2_items = S.split_list(r.get(S.COL_H2))
        link_items = S.split_list(r.get(S.COL_LINKS_URL))
        img_items = S.split_list(r.get(S.COL_IMG_SRC))
        img_alts = S.split_list(r.get(S.COL_IMG_ALT))
        canonical = r.get(S.COL_CANONICAL)
        meta_robots = (r.get(S.COL_META_ROBOTS) or "")
        title = r.get(S.COL_TITLE) or ""
        meta_desc = r.get(S.COL_META_DESC) or ""

        external_outlinks = sum(
            1 for link in link_items if urlparse(link).netloc.lower() not in internal_hosts and urlparse(link).netloc
        )
        images_missing_alt = sum(
            1 for i in range(len(img_items)) if (i >= len(img_alts) or not img_alts[i].strip())
        )
        # Resolve relative canonicals against the page URL before comparing.
        canonical_abs = urljoin(str(url), str(canonical)) if canonical and str(canonical).lower() != "nan" else ""
        canonical_is_self = bool(canonical_abs) and _normalise_url(canonical_abs) == _normalise_url(url)

        # Indexability decision (CRAWL-tier signals only).
        indexable, reason = _indexability(status, meta_robots, canonical, url, canonical_is_self)

        redirect_url, redirect_type = _redirect(r)
        sd_types = _structured_data_types(r, df.columns)

        rows.append(
            {
                "URL": url,
                "Status Code": _to_int(status),
                "Status": _status_phrase(status),
                "Indexability": indexable,
                "Indexability Reason": reason,
                "Title": title,
                "Title Length": len(str(title)),
                "Meta Description": meta_desc,
                "Meta Description Length": len(str(meta_desc)),
                "H1": h1_items[0] if h1_items else "",
                "H1 Count": len(h1_items),
                "H2 Count": len(h2_items),
                "Meta Robots": meta_robots,
                "Canonical": canonical or "",
                "Canonical Is Self": canonical_is_self,
                "Word Count": _word_count(r.get(S.COL_BODY_TEXT)),
                "Content-Type": _header(r, "Content-Type"),
                "Content-Encoding": _header(r, "Content-Encoding"),
                "Response Time": _to_float(r.get(S.COL_DOWNLOAD_LATENCY)),
                "Redirect URL": redirect_url,
                "Redirect Type": redirect_type,
                "Crawl Depth": _to_int(r.get(S.COL_DEPTH)),
                "Inlinks": inlink_counts.get(_normalise_url(url), 0),
                "Outlinks": len(link_items),
                "External Outlinks": external_outlinks,
                "Images": len(img_items),
                "Images Missing Alt": images_missing_alt,
                "Size (bytes)": _to_int(r.get(S.COL_SIZE)),
                "Hreflang Count": len(S.split_list(r.get("hreflang_hreflang"))),
                "Structured Data Types": sd_types,
            }
        )
    return pd.DataFrame(rows, columns=COLUMNS)


def _indexability(status, meta_robots, canonical, url, canonical_is_self):
    robots = str(meta_robots).lower()
    code = _to_int(status)
    if code is None or code >= 400:
        return "Non-Indexable", f"Non-200 ({code})" if code else "No response"
    if 300 <= code < 400:
        return "Non-Indexable", "Redirect"
    if "noindex" in robots:
        return "Non-Indexable", "Noindex"
    if canonical and not canonical_is_self:
        return "Non-Indexable", "Canonicalised"
    return "Indexable", ""


def _redirect(row) -> tuple[str, str]:
    urls = S.split_list(row.get(S.COL_REDIRECT_URLS))
    reasons = S.split_list(row.get(S.COL_REDIRECT_REASONS))
    if not urls:
        return "", ""
    return urls[-1], (reasons[-1] if reasons else "")


def _structured_data_types(row, columns) -> str:
    types: list[str] = []
    for col in columns:
        if col.startswith(S.JSONLD_PREFIX) and col.lower().endswith("@type"):
            for t in S.split_list(row.get(col)):
                if t and t not in types:
                    types.append(t)
    return "|".join(types)


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
    """Write the flat CSV and return a summary (never the file contents)."""
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
        "columns": list(export.columns),
        "indexable_urls": indexable,
        "non_indexable_urls": int(len(export) - indexable),
    }
