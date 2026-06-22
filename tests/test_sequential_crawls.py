"""Acceptance: two sequential crawls succeed in one server session.

This is the proof that reactor isolation works — a single long-lived JobManager
runs two crawls back-to-back, each in its own subprocess. In-process advertools
would raise ReactorNotRestartable on the second.
"""

import pytest

from advertools_mcp.jobs import JobKind, JobManager, JobStore

from conftest import await_job, make_crawl_spec


@pytest.mark.asyncio
async def test_two_sequential_crawls_in_one_session(site_server, settings):
    settings.ensure_dirs()
    store = JobStore(settings.jobs_dir)
    manager = JobManager(settings, store)

    urls = [f"{site_server}/index.html", f"{site_server}/products.html"]

    rec1 = manager.submit(JobKind.CRAWL, params={}, spec=make_crawl_spec(urls))
    done1 = await await_job(manager, rec1.job_id)
    assert done1.state == "done", done1.error_message
    assert done1.pages >= 1

    # Second crawl in the SAME process/session — would fail in-process.
    rec2 = manager.submit(JobKind.CRAWL, params={}, spec=make_crawl_spec(urls))
    done2 = await await_job(manager, rec2.job_id)
    assert done2.state == "done", done2.error_message
    assert done2.pages >= 1

    assert rec1.job_id != rec2.job_id
    import pyarrow.parquet as pq

    assert pq.read_metadata(done1.output_parquet).num_rows >= 1
    assert pq.read_metadata(done2.output_parquet).num_rows >= 1
