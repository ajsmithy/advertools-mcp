"""Check #91: orphaned sitemap URLs (sitemap vs crawl link graph)."""

import pandas as pd

from advertools_mcp.audit.checks import CHECKS
from advertools_mcp.audit.context import AuditContext
from advertools_mcp.config import Settings


def _ctx(is_discovery, sitemap_locs):
    df = pd.DataFrame(
        {
            "url": ["https://s.test/", "https://s.test/a"],
            "status": [200, 200],
            # /b is linked but was not crawled (e.g. max_pages hit) -> NOT an orphan.
            "links_url": ["https://s.test/a@@https://s.test/b", "https://s.test/"],
        }
    )
    ctx = AuditContext(df=df, settings=Settings(), is_discovery=is_discovery)
    if sitemap_locs is not None:
        ctx.sitemap_df = pd.DataFrame({"loc": sitemap_locs})
    return ctx


def test_orphan_found_on_discovery_crawl():
    # /orphan is in the sitemap but neither crawled nor linked from anywhere.
    ctx = _ctx(True, ["https://s.test/", "https://s.test/a", "https://s.test/b",
                      "https://s.test/orphan"])
    res = CHECKS[91](ctx)
    assert res.status == "Present"
    assert res.affected_urls == ["https://s.test/orphan"]


def test_linked_but_uncrawled_is_not_orphan():
    # /b was never fetched but IS linked -> not an orphan; all clean.
    ctx = _ctx(True, ["https://s.test/", "https://s.test/b"])
    res = CHECKS[91](ctx)
    assert res.status == "Not present"


def test_trailing_slash_normalised():
    ctx = _ctx(True, ["https://s.test/a/"])  # crawled as /a, listed as /a/
    res = CHECKS[91](ctx)
    assert res.status == "Not present"


def test_list_mode_is_not_assessed():
    ctx = _ctx(False, ["https://s.test/orphan"])
    res = CHECKS[91](ctx)
    assert res.status == "Not assessed"
    assert "discovery" in res.note.lower()


def test_unknown_mode_is_not_assessed():
    ctx = _ctx(None, ["https://s.test/orphan"])
    res = CHECKS[91](ctx)
    assert res.status == "Not assessed"


def test_no_sitemap_is_not_assessed():
    ctx = _ctx(True, None)
    res = CHECKS[91](ctx)
    assert res.status == "Not assessed"
