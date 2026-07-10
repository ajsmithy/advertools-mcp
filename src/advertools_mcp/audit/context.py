"""Shared state and helpers for audit check implementations."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

import pandas as pd

from ..crawl import schema as S

# The four — and only — permitted check states.
PRESENT = "Present"
NOT_PRESENT = "Not present"
NOT_ASSESSED = "Not assessed"
HEURISTIC = "Heuristic"


@dataclass
class CheckResult:
    status: str
    detection_source: str = "crawl"
    affected_urls: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def example_urls(self) -> list[str]:
        return self.affected_urls[:5]

    @property
    def affected_count(self) -> int:
        return len(self.affected_urls)


def not_assessed(reason: str, source: str = "—") -> CheckResult:
    return CheckResult(status=NOT_ASSESSED, detection_source=source, note=reason)


def finding(affected: list[str], source: str, heuristic: bool = False, note: str = "") -> CheckResult:
    """Build a result: Present/Heuristic when matches exist, else Not present."""
    if affected:
        status = HEURISTIC if heuristic else PRESENT
    else:
        status = NOT_PRESENT
    return CheckResult(status=status, detection_source=source, affected_urls=affected, note=note)


@dataclass
class AuditContext:
    df: pd.DataFrame
    settings: Any
    robots_df: Optional[pd.DataFrame] = None
    robots_text: Optional[str] = None
    robots_url: Optional[str] = None
    sitemap_df: Optional[pd.DataFrame] = None
    second_ua_df: Optional[pd.DataFrame] = None  # diff crawl (cloaking/mobile)

    # Optional external sources.
    has_lighthouse: bool = False
    has_render: bool = False
    has_gsc: bool = False
    has_backlinks: bool = False

    columns: set[str] = field(default_factory=set)
    internal_hosts: set[str] = field(default_factory=set)
    primary_host: str = ""

    def __post_init__(self) -> None:
        self.columns = set(self.df.columns)
        self.internal_hosts = {
            urlparse(u).netloc.lower() for u in self.df.get(S.COL_URL, []) if u
        }
        if S.COL_URL in self.df.columns and len(self.df):
            host_counts = Counter(
                urlparse(u).netloc.lower() for u in self.df[S.COL_URL] if u
            )
            self.primary_host = host_counts.most_common(1)[0][0] if host_counts else ""

    # ----------------------------------------------------------- column access
    def col(self, name: str) -> pd.Series:
        if name in self.df.columns:
            return self.df[name]
        return pd.Series([None] * len(self.df), index=self.df.index)

    def urls_where(self, mask: pd.Series) -> list[str]:
        try:
            return self.df.loc[mask.fillna(False), S.COL_URL].astype(str).tolist()
        except Exception:  # noqa: BLE001
            return []

    def status_int(self) -> pd.Series:
        return pd.to_numeric(self.col(S.COL_STATUS), errors="coerce")

    def is_html(self) -> pd.Series:
        ct = self._content_type()
        return ct.str.contains("html", case=False, na=False)

    def _content_type(self) -> pd.Series:
        for c in self.df.columns:
            if c.lower() == f"{S.RESP_HEADER_PREFIX}content-type".lower():
                return self.col(c).astype(str)
        return pd.Series([""] * len(self.df), index=self.df.index)

    def header(self, name: str) -> pd.Series:
        for c in self.df.columns:
            if c.lower() == f"{S.RESP_HEADER_PREFIX}{name}".lower():
                return self.col(c).astype(str)
        return pd.Series([None] * len(self.df), index=self.df.index)

    def is_external(self, url: str) -> bool:
        host = urlparse(str(url)).netloc.lower()
        return bool(host) and host not in self.internal_hosts
