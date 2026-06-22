"""Unit tests for the flat CSV export column derivations."""

import pandas as pd

from advertools_mcp.crawl.csv_export import COLUMNS, build_export_frame


def _df():
    return pd.DataFrame(
        {
            "url": ["https://s.test/a", "https://s.test/b", "https://s.test/c"],
            "status": [200, 200, 404],
            "title": ["A title", "B", "C"],
            "meta_desc": ["d", "", "d"],
            "meta_robots": ["index,follow", "noindex", ""],
            "h1": ["A@@A2", "B", ""],
            "h2": ["x@@y", "", ""],
            "canonical": ["/a", "https://other.test/b", ""],  # relative self, third-party, none
            "body_text": ["one two three", "w " * 10, ""],
            "resp_headers_Content-Type": ["text/html", "text/html", "text/html"],
            "resp_headers_Content-Encoding": ["gzip", "", ""],
            "download_latency": [0.1, 0.2, 0.3],
            "depth": [0, 1, 1],
            "size": [100, 200, 50],
            "links_url": ["https://s.test/b@@https://ext.test/x", "https://s.test/a", ""],
            "links_nofollow": ["False@@False", "False", ""],
            "img_src": ["https://s.test/i.png@@https://s.test/j.png", "", ""],
            "img_alt": ["alt@@", "", ""],
            "hreflang_hreflang": ["en@@fr", "", ""],
            "jsonld_@type": ["Organization", "", ""],
        }
    )


def test_columns_fixed_order_and_count():
    out = build_export_frame(_df())
    assert list(out.columns) == COLUMNS
    assert len(out) == 3


def test_relative_canonical_resolves_to_self():
    out = build_export_frame(_df())
    row_a = out.iloc[0]
    assert row_a["Canonical Is Self"] is True or row_a["Canonical Is Self"] == True  # noqa: E712
    assert row_a["Indexability"] == "Indexable"


def test_noindex_is_non_indexable():
    out = build_export_frame(_df())
    assert out.iloc[1]["Indexability"] == "Non-Indexable"
    assert out.iloc[1]["Indexability Reason"] == "Noindex"


def test_404_is_non_indexable():
    out = build_export_frame(_df())
    assert out.iloc[2]["Indexability"] == "Non-Indexable"


def test_list_columns_reduced_to_counts():
    out = build_export_frame(_df()).iloc[0]
    assert out["H1 Count"] == 2
    assert out["H2 Count"] == 2
    assert out["Images"] == 2
    assert out["Images Missing Alt"] == 1
    assert out["Outlinks"] == 2
    assert out["External Outlinks"] == 1
    assert out["Hreflang Count"] == 2
    assert out["Structured Data Types"] == "Organization"
