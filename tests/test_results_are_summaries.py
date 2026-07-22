"""Acceptance: large crawls/audits return summaries + IDs, never raw file data."""

import json

import pytest

from advertools_mcp.server import build_server

from conftest import await_job, call_tool, make_crawl_spec, TEST_CRAWL_SETTINGS


@pytest.mark.asyncio
async def test_crawl_then_summary_query_export_and_audit(site_server, settings):
    mcp = build_server(settings)
    from advertools_mcp import server as srv

    urls = [
        f"{site_server}/index.html",
        f"{site_server}/products.html",
        f"{site_server}/about.html",
    ]
    started = await call_tool(
        mcp, "start_crawl", urls=urls, follow_links=False,
        user_agent="testbot", crawl_speed=5, custom_settings=TEST_CRAWL_SETTINGS,
    )
    assert started["status"] == "started"
    job_id = started["job_id"]

    rec = await await_job(srv._manager, job_id)
    assert rec.state == "done", rec.error_message

    # get_crawl_summary returns compact stats, not rows.
    summ = await call_tool(mcp, "get_crawl_summary", job_id=job_id)
    assert summ["row_count"] >= 3
    assert "status_code_distribution" in summ
    assert "columns" in summ
    # No raw page bodies leaked in the summary payload.
    assert "body_text" not in json.dumps(summ).lower() or "body_text" in summ["columns"]

    # query_crawl is paginated/columnar.
    q = await call_tool(
        mcp, "query_crawl", job_id=job_id, columns=["url", "status"], limit=2
    )
    assert q["returned"] <= 2
    assert all(set(r.keys()) <= {"url", "status"} for r in q["rows"])

    # CSV export returns a path + summary, not contents.
    exp = await call_tool(mcp, "export_crawl_csv", job_id=job_id)
    assert exp["csv_path"].endswith("crawl-detail.csv")
    assert exp["rows"] >= 3
    assert "Indexability" in exp["columns"]
    assert exp["column_count"] >= 50
    assert "content" not in exp  # never inline file contents

    # Audit returns audit_id + summary, file stays on disk.
    audit = await call_tool(mcp, "run_audit", job_id=job_id)
    assert audit["audit_id"].startswith("audit-")
    assert audit["audit_xlsx"].endswith(".xlsx")
    assert audit["total_checks"] == 91

    # get_audit_summary re-reads the workbook and groups by tier without error.
    asum = await call_tool(mcp, "get_audit_summary", audit_id=audit["audit_id"])
    assert asum["total_checks"] == 91
    assert set(asum["counts_by_tier"]) == {"CRAWL", "CRAWL+", "RENDER", "CWV", "EXTERNAL"}
    assert sum(asum["counts_by_status"].values()) == 91
    assert "rows" not in audit  # no raw audit rows inline


@pytest.mark.asyncio
async def test_results_tools_reject_unfinished_job(settings):
    mcp = build_server(settings)
    # Results tools must refuse an unknown/unfinished job rather than return data.
    with pytest.raises(Exception) as exc:
        await call_tool(mcp, "get_crawl_summary", job_id="crawl-doesnotexist")
    assert "crawl-doesnotexist" in str(exc.value)
