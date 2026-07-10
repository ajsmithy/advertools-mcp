"""Fast, memory-frugal reads over crawl parquet files.

Queries push column projection and row filters into pyarrow so we never
materialise an entire crawl in memory to answer a sliced request.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq

# Supported filter operators mapped to pyarrow.compute field methods.
_OPS = {
    "==": lambda f, v: f == v,
    "!=": lambda f, v: f != v,
    "<": lambda f, v: f < v,
    "<=": lambda f, v: f <= v,
    ">": lambda f, v: f > v,
    ">=": lambda f, v: f >= v,
    "in": lambda f, v: f.isin(v),
    "not_in": lambda f, v: ~f.isin(v),
    "contains": lambda f, v: pc.match_substring(pc.cast(f, "string"), v),
    "is_null": lambda f, v: f.is_null(),
    "not_null": lambda f, v: f.is_valid(),
}


class CrawlNotReadyError(RuntimeError):
    """Raised when a crawl parquet file does not exist yet."""


def _require(parquet_path: str) -> Path:
    path = Path(parquet_path)
    if not path.exists():
        raise CrawlNotReadyError(f"No crawl parquet at {parquet_path}")
    return path


def schema_columns(parquet_path: str) -> list[str]:
    _require(parquet_path)
    return list(pq.read_schema(parquet_path).names)


def row_count(parquet_path: str) -> int:
    _require(parquet_path)
    return pq.read_metadata(parquet_path).num_rows


def _build_filter(filters: list[dict[str, Any]] | None, available: set[str]):
    if not filters:
        return None
    expr = None
    for flt in filters:
        col = flt["column"]
        if col not in available:
            raise ValueError(f"Unknown filter column: {col!r}")
        op = flt.get("op", "==")
        if op not in _OPS:
            raise ValueError(f"Unsupported operator: {op!r}")
        clause = _OPS[op](ds.field(col), flt.get("value"))
        expr = clause if expr is None else (expr & clause)
    return expr


def query(
    parquet_path: str,
    columns: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Return paginated rows as records, reading only the requested columns.

    ``filters`` is a list of ``{"column", "op", "value"}`` dicts combined with
    AND. Returns ``{"rows": [...], "returned": n, "offset", "limit",
    "has_more": bool}``.
    """
    _require(parquet_path)
    dataset = ds.dataset(parquet_path, format="parquet")
    available = set(dataset.schema.names)
    if columns:
        missing = [c for c in columns if c not in available]
        if missing:
            raise ValueError(f"Unknown columns: {missing}")
    expr = _build_filter(filters, available)

    # Stream batches; stop once we've collected offset+limit rows.
    want = offset + limit + 1  # +1 to detect has_more cheaply
    collected: list[dict[str, Any]] = []
    scanner = dataset.scanner(columns=columns, filter=expr)
    for batch in scanner.to_batches():
        if not batch.num_rows:
            continue
        collected.extend(batch.to_pylist())
        if len(collected) >= want:
            break

    window = collected[offset : offset + limit]
    has_more = len(collected) > offset + limit
    return {
        "rows": window,
        "returned": len(window),
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
    }
