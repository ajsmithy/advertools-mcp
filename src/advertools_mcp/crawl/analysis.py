"""crawlytics-backed analyses. Full results land on disk; tools get summaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import pyarrow.parquet as pq

import advertools.crawlytics as crawlytics

from . import schema as S


def _read_columns(parquet_path: str, exact: list[str], prefixes: tuple[str, ...] = ()) -> pd.DataFrame:
    """Read only the named columns (plus any matching a prefix) that exist.

    Avoids materialising heavyweight columns like ``body_text`` when an
    analysis only needs links/images/redirect data.
    """
    available = pq.read_schema(parquet_path).names
    wanted = [
        c for c in available
        if c in exact or any(c.startswith(p) for p in prefixes)
    ]
    return pd.read_parquet(parquet_path, columns=wanted)


def _internal_hosts(df: pd.DataFrame) -> set[str]:
    return {urlparse(u).netloc.lower() for u in df.get(S.COL_URL, []) if u}


def _write_side_table(df: pd.DataFrame, parquet_path: str, suffix: str) -> str | None:
    if df is None or df.empty:
        return None
    out = str(Path(parquet_path).with_name(f"analysis_{suffix}.parquet"))
    df.to_parquet(out, index=False)
    return out


def analyse_links(parquet_path: str) -> dict[str, Any]:
    df = _read_columns(parquet_path, [S.COL_URL], prefixes=("links_",))
    links = crawlytics.links(df)
    if links is None or links.empty:
        return {"total_links": 0, "note": "No links found in crawl."}
    hosts = _internal_hosts(df)
    links = links.copy()
    links["_host"] = links["link"].map(lambda x: urlparse(str(x)).netloc.lower())
    links["_internal"] = links["_host"].isin(hosts)
    nofollow = links["nofollow"].astype(str).str.lower().eq("true")
    top_targets = (
        links[links["_internal"]]["link"].value_counts().head(20).to_dict()
    )
    side = _write_side_table(links.drop(columns=["_host"]), parquet_path, "links")
    return {
        "total_links": int(len(links)),
        "internal_links": int(links["_internal"].sum()),
        "external_links": int((~links["_internal"]).sum()),
        "nofollow_links": int(nofollow.sum()),
        "unique_internal_targets": int(links[links["_internal"]]["link"].nunique()),
        "top_linked_pages": top_targets,
        "detail_parquet": side,
    }


def analyse_redirects(parquet_path: str) -> dict[str, Any]:
    df = _read_columns(
        parquet_path, [S.COL_URL, S.COL_STATUS, "download_latency"], prefixes=("redirect_",)
    )
    try:
        redirects = crawlytics.redirects(df)
    except Exception:  # noqa: BLE001
        redirects = None
    if redirects is None or redirects.empty or len(redirects.columns) == 0:
        return {"total_redirects": 0, "note": "No redirects found in crawl."}
    side = _write_side_table(redirects, parquet_path, "redirects")
    out: dict[str, Any] = {"total_redirects": int(len(redirects)), "detail_parquet": side}
    if "status" in redirects.columns:
        out["by_status"] = {str(k): int(v) for k, v in redirects["status"].value_counts().items()}
    if "type" in redirects.columns:
        out["by_type"] = {str(k): int(v) for k, v in redirects["type"].value_counts().items()}
    return out


def analyse_images(parquet_path: str) -> dict[str, Any]:
    df = _read_columns(parquet_path, [S.COL_URL], prefixes=("img_",))
    images = crawlytics.images(df)
    if images is None or images.empty:
        return {"total_images": 0, "note": "No images found in crawl."}
    images = images.dropna(subset=["img_src"])
    alt = images.get("img_alt")
    missing_alt = 0
    if alt is not None:
        missing_alt = int(alt.fillna("").astype(str).str.strip().eq("").sum())
    ext = images["img_src"].map(lambda u: Path(urlparse(str(u)).path).suffix.lower() or "(none)")
    side = _write_side_table(images, parquet_path, "images")
    return {
        "total_images": int(len(images)),
        "unique_images": int(images["img_src"].nunique()),
        "images_missing_alt": missing_alt,
        "by_extension": {str(k): int(v) for k, v in ext.value_counts().head(15).items()},
        "detail_parquet": side,
    }
