"""Ingest a Screaming Frog export folder into the advertools audit schema.

Point the audit at a folder of Screaming Frog CSV/Excel exports; this maps them
onto the same normalised schema an advertools crawl produces, so the existing
91 checks run over every URL in the dataset. It reports which data signals the
export actually provides, so checks whose inputs are absent report
**Not assessed** rather than a false verdict.

Supported exports (auto-detected by filename/headers):
  - Internal:All  (``internal_all.csv`` / any export with an "Address" column)
      → the core per-URL fields (status, titles, meta, headings, canonical,
        robots, redirects, indexability, word count, multiples, …).
  - All Outlinks  (``all_outlinks.csv`` / "Source"+"Destination"+"Type")
      → the per-page link graph and asset (JS/CSS/IMG) references.
  - Images        (``images*.csv`` / "Address"+"Image ...")
      → image sources and alt text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

from ..crawl import schema as S
from . import signals as SIG


@dataclass
class IngestResult:
    df: pd.DataFrame
    available_signals: set[str]
    source: str = "screamingfrog"
    row_count: int = 0
    files_used: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _read_table(path: Path) -> Optional[pd.DataFrame]:
    try:
        if path.suffix.lower() in (".xlsx", ".xlsm"):
            return pd.read_excel(path, dtype=str)
        # SF CSVs are UTF-8 (sometimes BOM); keep everything as strings.
        return pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    except Exception:  # noqa: BLE001
        try:
            return pd.read_csv(path, dtype=str, encoding="latin-1", keep_default_na=False)
        except Exception:  # noqa: BLE001
            return None


def _colmap(df: pd.DataFrame) -> dict[str, str]:
    """Case/whitespace-insensitive header lookup: normalised name -> real name."""
    out = {}
    for c in df.columns:
        out[str(c).strip().lower()] = c
    return out


def _get(row: dict, cmap: dict, *names: str) -> str:
    for n in names:
        real = cmap.get(n.lower())
        if real is not None:
            v = row.get(real)
            if v is not None and str(v).strip() != "":
                return str(v).strip()
    return ""


def _find_internal(tables: dict[Path, pd.DataFrame]) -> Optional[tuple[Path, pd.DataFrame]]:
    """The Internal export is the one keyed by an 'Address' + 'Status Code' pair."""
    for path, df in tables.items():
        cmap = _colmap(df)
        if "address" in cmap and ("status code" in cmap or "status" in cmap):
            # Prefer a file that also looks per-URL (has a title/indexability col).
            if any(k in cmap for k in ("title 1", "indexability", "meta description 1")):
                return path, df
    # Fallback: any table with an Address column.
    for path, df in tables.items():
        if "address" in _colmap(df):
            return path, df
    return None


def _find_by_headers(tables: dict[Path, pd.DataFrame], required: set[str]) -> Optional[tuple[Path, pd.DataFrame]]:
    for path, df in tables.items():
        cmap = _colmap(df)
        if required.issubset(set(cmap)):
            return path, df
    return None


def _count_nonempty(*vals: str) -> int:
    return sum(1 for v in vals if v and v.strip())


def _to_num(value: str):
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def ingest_folder(folder: str) -> IngestResult:
    root = Path(folder)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Not a folder: {folder}")

    tables: dict[Path, pd.DataFrame] = {}
    for path in sorted(root.iterdir()):
        if path.suffix.lower() in (".csv", ".xlsx", ".xlsm"):
            df = _read_table(path)
            if df is not None and len(df):
                tables[path] = df
    if not tables:
        raise ValueError(f"No CSV/Excel exports found in {folder}")

    internal = _find_internal(tables)
    if internal is None:
        raise ValueError(
            "Could not find a Screaming Frog 'Internal' export (a CSV with an "
            "'Address' column) in the folder."
        )
    internal_path, idf = internal
    cmap = _colmap(idf)
    warnings: list[str] = []
    files_used = [internal_path.name]

    rows: list[dict] = []
    for _, raw in idf.iterrows():
        row = raw.to_dict()
        title = _get(row, cmap, "Title 1", "Title")
        title2 = _get(row, cmap, "Title 2")
        md = _get(row, cmap, "Meta Description 1", "Meta Description")
        md2 = _get(row, cmap, "Meta Description 2")
        canon = _get(row, cmap, "Canonical Link Element 1", "Canonical Link Element", "Canonical")
        canon2 = _get(row, cmap, "Canonical Link Element 2")
        h1a, h1b = _get(row, cmap, "H1-1", "H1"), _get(row, cmap, "H1-2")
        h2a, h2b = _get(row, cmap, "H2-1", "H2"), _get(row, cmap, "H2-2")
        redirect = _get(row, cmap, "Redirect URL")
        status = _get(row, cmap, "Status Code", "Status Code 1")

        out = {
            S.COL_URL: _get(row, cmap, "Address", "URL"),
            S.COL_STATUS: _to_num(status),
            S.COL_TITLE: title,
            S.COL_META_DESC: md,
            S.COL_META_ROBOTS: _get(row, cmap, "Meta Robots 1", "Meta Robots"),
            S.COL_H1: "@@".join([x for x in (h1a, h1b) if x]),
            S.COL_H2: "@@".join([x for x in (h2a, h2b) if x]),
            S.COL_CANONICAL: canon,
            S.COL_DEPTH: _to_num(_get(row, cmap, "Crawl Depth")),
            S.COL_SIZE: _to_num(_get(row, cmap, "Size (bytes)", "Size")),
            S.COL_DOWNLOAD_LATENCY: _seconds(_get(row, cmap, "Response Time")),
            "meta_keywords": _get(row, cmap, "Meta Keywords 1"),
            "meta_refresh": _get(row, cmap, "Meta Refresh 1"),
            "rel_next": _get(row, cmap, 'rel="next" 1', "rel=next 1", "rel=“next” 1"),
            "rel_prev": _get(row, cmap, 'rel="prev" 1', "rel=prev 1"),
            "amphtml": _get(row, cmap, "amphtml Link Element"),
            "html_lang": _get(row, cmap, "Language"),
            "crawl_time": _get(row, cmap, "Crawl Timestamp"),
            "sf_indexability": _get(row, cmap, "Indexability"),
            "sf_indexability_status": _get(row, cmap, "Indexability Status"),
            "resp_headers_Content-Type": _get(row, cmap, "Content Type", "Content-Type"),
            "resp_headers_Last-Modified": _get(row, cmap, "Last Modified"),
            "resp_headers_X-Robots-Tag": _get(row, cmap, "X-Robots-Tag 1", "X-Robots-Tag"),
            # multiples counts (for #12/#56/#76)
            "title_count": _count_nonempty(title, title2),
            "meta_desc_total_count": _count_nonempty(md, md2),
            "canonical_total_count": _count_nonempty(canon, canon2),
            # redirect chain (single hop from SF)
            S.COL_REDIRECT_URLS: redirect,
            S.COL_REDIRECT_REASONS: _get(row, cmap, "Redirect Type"),
        }
        # Optional columns some SF configs include:
        vp = _get(row, cmap, "Meta Viewport", "Viewport")
        if vp:
            out[S.COL_VIEWPORT] = vp
        ce = _get(row, cmap, "Content Encoding", "Content-Encoding")
        if ce:
            out["resp_headers_Content-Encoding"] = ce
        cookies = _get(row, cmap, "Cookies")
        if cookies:
            out["resp_headers_Set-Cookie"] = cookies
        rows.append(out)

    df = pd.DataFrame(rows)
    df = df[df[S.COL_URL].astype(str).str.len() > 0].reset_index(drop=True)

    # ---- signals available from the Internal export ----
    available: set[str] = {
        SIG.TITLE, SIG.META_DESC, SIG.H1, SIG.META_ROBOTS, SIG.CANONICAL,
        SIG.REDIRECT, SIG.MULTIPLES,
    }
    if S.COL_VIEWPORT in df.columns and df[S.COL_VIEWPORT].astype(str).str.len().gt(0).any():
        available.add(SIG.VIEWPORT)
    if "resp_headers_Content-Encoding" in df.columns:
        available.add(SIG.CONTENT_ENCODING)

    # ---- optional bulk exports for the link/asset graph ----
    _ingest_outlinks(tables, df, available, files_used, warnings)
    _ingest_images(tables, df, available, files_used, warnings)

    return IngestResult(
        df=df, available_signals=available, row_count=len(df),
        files_used=files_used, warnings=warnings,
    )


def _seconds(value: str):
    """SF 'Response Time' is seconds already; keep numeric, else None."""
    return _to_num(value)


def _ingest_outlinks(tables, df, available, files_used, warnings) -> None:
    found = _find_by_headers(tables, {"source", "destination", "type"})
    if not found:
        return
    path, odf = found
    cmap = _colmap(odf)
    src_c, dst_c, type_c = cmap["source"], cmap["destination"], cmap["type"]
    follow_c = cmap.get("follow")
    anchor_c = cmap.get("anchor")

    links: dict[str, list[str]] = {}
    link_text: dict[str, list[str]] = {}
    link_nofollow: dict[str, list[str]] = {}
    scripts: dict[str, list[str]] = {}
    styles: dict[str, list[str]] = {}
    imgs: dict[str, list[str]] = {}
    for _, r in odf.iterrows():
        src = str(r.get(src_c) or "").strip()
        dst = str(r.get(dst_c) or "").strip()
        if not src or not dst:
            continue
        typ = str(r.get(type_c) or "").strip().lower()
        if typ in ("hyperlink", "ahref", "href", ""):
            links.setdefault(src, []).append(dst)
            link_text.setdefault(src, []).append(str(r.get(anchor_c) or "").strip() if anchor_c else "")
            nf = str(r.get(follow_c) or "").strip().lower() if follow_c else ""
            link_nofollow.setdefault(src, []).append("True" if nf in ("false", "no") else "False")
        elif "js" in typ or "script" in typ:
            scripts.setdefault(src, []).append(dst)
        elif "css" in typ or "style" in typ:
            styles.setdefault(src, []).append(dst)
        elif "img" in typ or "image" in typ:
            imgs.setdefault(src, []).append(dst)

    urls = df[S.COL_URL].astype(str)
    df[S.COL_LINKS_URL] = urls.map(lambda u: "@@".join(links.get(u, [])))
    df[S.COL_LINKS_TEXT] = urls.map(lambda u: "@@".join(link_text.get(u, [])))
    df[S.COL_LINKS_NOFOLLOW] = urls.map(lambda u: "@@".join(link_nofollow.get(u, [])))
    df["script_src"] = urls.map(lambda u: "@@".join(scripts.get(u, [])))
    df["stylesheet_href"] = urls.map(lambda u: "@@".join(styles.get(u, [])))
    available.add(SIG.LINKS)
    if any(scripts.values()) or any(styles.values()):
        available.add(SIG.SCRIPTS)
    if imgs and not any(SIG.IMAGES in available for _ in [0]):
        # image URLs from outlinks (no alt text); a dedicated Images export is better.
        df[S.COL_IMG_SRC] = urls.map(lambda u: "@@".join(imgs.get(u, [])))
        df[S.COL_IMG_ALT] = urls.map(lambda u: "@@".join("" for _ in imgs.get(u, [])))
        available.add(SIG.IMAGES)
    files_used.append(path.name)


def _ingest_images(tables, df, available, files_used, warnings) -> None:
    # A dedicated images export: rows of (page Address, Image src, Alt Text).
    found = None
    for path, tdf in tables.items():
        cmap = _colmap(tdf)
        has_src = any(k in cmap for k in ("image", "image address", "image url", "destination"))
        if "address" in cmap and has_src and path.name.lower().find("image") >= 0:
            found = (path, tdf, cmap)
            break
    if not found:
        return
    path, tdf, cmap = found
    addr_c = cmap["address"]
    src_c = next(cmap[k] for k in ("image", "image address", "image url", "destination") if k in cmap)
    alt_c = cmap.get("alt text") or cmap.get("alt")
    srcs: dict[str, list[str]] = {}
    alts: dict[str, list[str]] = {}
    for _, r in tdf.iterrows():
        page = str(r.get(addr_c) or "").strip()
        src = str(r.get(src_c) or "").strip()
        if not page or not src:
            continue
        srcs.setdefault(page, []).append(src)
        alts.setdefault(page, []).append(str(r.get(alt_c) or "").strip() if alt_c else "")
    urls = df[S.COL_URL].astype(str)
    df[S.COL_IMG_SRC] = urls.map(lambda u: "@@".join(srcs.get(u, [])))
    df[S.COL_IMG_ALT] = urls.map(lambda u: "@@".join(alts.get(u, [])))
    available.add(SIG.IMAGES)
    files_used.append(path.name)
