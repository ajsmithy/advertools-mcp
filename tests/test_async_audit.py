"""Async audit jobs (start_audit / audit_status) and the audit CLI."""

import asyncio
import json

import pandas as pd
import pytest

from advertools_mcp.config import Settings
from advertools_mcp.server import build_server

from conftest import call_tool

SF_INTERNAL = [
    {"Address": "https://s.test/", "Content Type": "text/html; charset=UTF-8",
     "Status Code": "200", "Title 1": "Home", "H1-1": "Welcome",
     "Meta Description 1": "d", "Canonical Link Element 1": "https://s.test/",
     "Meta Robots 1": "index,follow", "Crawl Depth": "0"},
    {"Address": "https://s.test/no-h1", "Content Type": "text/html; charset=UTF-8",
     "Status Code": "200", "Title 1": "No H1", "H1-1": "",
     "Meta Description 1": "d", "Canonical Link Element 1": "https://s.test/no-h1",
     "Meta Robots 1": "index,follow", "Crawl Depth": "1"},
]


async def _await_audit(mcp, audit_job_id, timeout=60):
    loop = asyncio.get_event_loop()
    start = loop.time()
    while loop.time() - start < timeout:
        st = await call_tool(mcp, "audit_status", audit_job_id=audit_job_id)
        if st["state"] in ("done", "failed", "cancelled"):
            return st
        await asyncio.sleep(0.2)
    raise TimeoutError("audit did not finish")


@pytest.fixture
def settings(tmp_path):
    s = Settings(data_dir=tmp_path / "data")
    s.ensure_dirs()
    return s


@pytest.mark.asyncio
async def test_start_audit_from_folder_is_async(settings, tmp_path):
    folder = tmp_path / "sf"
    folder.mkdir()
    pd.DataFrame(SF_INTERNAL).to_csv(folder / "internal_all.csv", index=False)

    mcp = build_server(settings)
    started = await call_tool(mcp, "start_audit_from_folder", folder=str(folder))
    # Returns immediately with a job id, not the full audit.
    assert started["status"] == "started"
    aid = started["audit_job_id"]
    assert "summary" not in started

    st = await _await_audit(mcp, aid)
    assert st["state"] == "done", st.get("error_message")
    summ = st["summary"]
    assert summ["source"] == "screamingfrog"
    assert summ["urls_assessed"] == 2
    assert summ["total_checks"] == 91
    assert summ["audit_xlsx"].endswith(".xlsx")


@pytest.mark.asyncio
async def test_start_audit_over_crawl_job(settings, site_server):
    from advertools_mcp import server as srv
    from advertools_mcp.jobs import JobKind

    from conftest import await_job, make_crawl_spec

    mcp = build_server(settings)
    rec = srv._manager.submit(
        JobKind.CRAWL, params={"follow_links": False},
        spec=make_crawl_spec([f"{site_server}/index.html", f"{site_server}/products.html"]),
    )
    done = await await_job(srv._manager, rec.job_id)
    assert done.state == "done", done.error_message

    started = await call_tool(mcp, "start_audit", job_id=done.job_id)
    assert started["status"] == "started"
    st = await _await_audit(mcp, started["audit_job_id"])
    assert st["state"] == "done", st.get("error_message")
    assert st["summary"]["total_checks"] == 91


@pytest.mark.asyncio
async def test_audit_status_unknown_id(settings):
    mcp = build_server(settings)
    st = await call_tool(mcp, "audit_status", audit_job_id="nope")
    assert "error" in st


def test_cli_folder_audit(tmp_path, capsys):
    from advertools_mcp import audit_cli

    folder = tmp_path / "sf"
    folder.mkdir()
    pd.DataFrame(SF_INTERNAL).to_csv(folder / "internal_all.csv", index=False)
    rc = audit_cli.main(["--folder", str(folder), "--data-dir", str(tmp_path / "data")])
    assert rc == 0
    out = capsys.readouterr().out
    summary = json.loads(out)
    assert summary["source"] == "screamingfrog"
    assert summary["urls_assessed"] == 2
    assert summary["total_checks"] == 91
    from pathlib import Path
    assert Path(summary["audit_xlsx"]).exists()


def test_cli_bad_folder(tmp_path):
    from advertools_mcp import audit_cli

    empty = tmp_path / "empty"
    empty.mkdir()
    rc = audit_cli.main(["--folder", str(empty), "--data-dir", str(tmp_path / "d")])
    assert rc == 1
