"""Compact crawl summaries — the only crawl payload returned inline to a model.

Reads just the columns each statistic needs, keeping large crawls cheap to
summarise.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pyarrow.dataset as ds

from .parquet_query import _require, row_count, schema_columns
from .schema import RESP_HEADER_PREFIX


def _value_counts(parquet_path: str, column: str, top: int | None = None) -> dict[str, int]:
    dataset = ds.dataset(parquet_path, format="parquet")
    if column not in dataset.schema.names:
        return {}
    counts: Counter = Counter()
    for batch in dataset.scanner(columns=[column]).to_batches():
        for value in batch.column(0).to_pylist():
            if value is None:
                key = "null"
            elif isinstance(value, float) and value != value:  # NaN
                key = "null"
            else:
                key = str(int(value)) if isinstance(value, (int, float)) and float(value).is_integer() else str(value)
            counts[key] += 1
    items = counts.most_common(top) if top else sorted(counts.items())
    return dict(items)


def summarise(parquet_path: str) -> dict[str, Any]:
    """Headline crawl stats: counts, status codes, content types, depth, schema."""
    _require(parquet_path)
    columns = schema_columns(parquet_path)

    content_type_col = next(
        (c for c in columns if c.lower() == f"{RESP_HEADER_PREFIX}content-type".lower()),
        None,
    )

    summary: dict[str, Any] = {
        "row_count": row_count(parquet_path),
        "column_count": len(columns),
        "status_code_distribution": _value_counts(parquet_path, "status"),
        "depth_profile": _value_counts(parquet_path, "depth"),
        "columns": columns,
    }
    if content_type_col:
        breakdown = _value_counts(parquet_path, content_type_col, top=15)
        # Normalise "text/html; charset=utf-8" → "text/html" for a cleaner view.
        normalised: Counter = Counter()
        for key, count in breakdown.items():
            normalised[key.split(";")[0].strip()] += count
        summary["content_type_breakdown"] = dict(normalised.most_common(15))
    else:
        summary["content_type_breakdown"] = {}
    return summary
