"""Runtime audit configuration: agents on hosts without env access can supply
the PSI key via the configure_audit tool, persisted under data_dir."""

import json

import pandas as pd
import pytest

from advertools_mcp.audit import lighthouse
from advertools_mcp.config import (
    RUNTIME_OVERRIDE_KEYS,
    load_runtime_overrides,
    save_runtime_overrides,
    with_runtime_overrides,
)
from advertools_mcp.jobs.models import JobRecord
from advertools_mcp.server import build_server

from conftest import call_tool


def _fake_done_job(srv_module, tmp_dir):
    """Register a completed crawl job without running a real crawl."""
    df = pd.DataFrame({
        "url": ["https://s.test/"], "status": [200],
        "resp_headers_Content-Type": ["text/html"], "depth": [0],
    })
    parquet = tmp_dir / "crawl.parquet"
    df.to_parquet(parquet, index=False)
    record = JobRecord(
        job_id="crawl-fake001", kind="crawl", state="done",
        params={"follow_links": True}, output_parquet=str(parquet),
    )
    srv_module._store.save(record)
    return record.job_id


@pytest.mark.asyncio
async def test_configure_audit_persists_and_masks(settings):
    mcp = build_server(settings)
    res = await call_tool(
        mcp, "configure_audit",
        lighthouse_api_key="AIzaSyFAKEKEY123456", psi_url_sample=5, psi_strategy="desktop",
    )
    assert res["status"] == "saved"
    # Full key never echoed back.
    assert "AIzaSyFAKEKEY123456" not in json.dumps(res)
    assert res["active_overrides"]["lighthouse_api_key"].endswith("3456")
    # ... but stored on disk and applied to settings.
    assert load_runtime_overrides(settings)["lighthouse_api_key"] == "AIzaSyFAKEKEY123456"
    eff = with_runtime_overrides(settings)
    assert eff.lighthouse_api_key == "AIzaSyFAKEKEY123456"
    assert eff.psi_url_sample == 5
    assert eff.psi_strategy == "desktop"


@pytest.mark.asyncio
async def test_stored_key_used_by_run_audit_and_survives_restart(settings, monkeypatch):
    from advertools_mcp import server as srv

    mcp = build_server(settings)
    await call_tool(mcp, "configure_audit", lighthouse_api_key="stored-key-9876")

    calls = {}

    def fake_psi(urls, key, strategy="mobile"):
        calls["key"] = key
        return {u: {"audits": {"largest-contentful-paint": {"score": 1.0, "numericValue": 900}}}
                for u in urls}

    monkeypatch.setattr(lighthouse, "run_psi_sample", fake_psi)
    job_id = _fake_done_job(srv, settings.data_dir)
    audit = await call_tool(mcp, "run_audit", job_id=job_id)
    assert calls["key"] == "stored-key-9876"
    assert audit["total_checks"] == 91

    # Simulate a restart: a fresh server on the same data_dir still sees the key.
    mcp2 = build_server(settings)
    calls.clear()
    job_id2 = _fake_done_job(srv, settings.data_dir)
    await call_tool(mcp2, "run_audit", job_id=job_id2)
    assert calls["key"] == "stored-key-9876"


@pytest.mark.asyncio
async def test_per_call_key_overrides_stored(settings, monkeypatch):
    from advertools_mcp import server as srv

    mcp = build_server(settings)
    await call_tool(mcp, "configure_audit", lighthouse_api_key="stored-key")
    calls = {}

    def fake_psi(urls, key, strategy="mobile"):
        calls["key"] = key
        return None

    monkeypatch.setattr(lighthouse, "run_psi_sample", fake_psi)
    job_id = _fake_done_job(srv, settings.data_dir)
    await call_tool(mcp, "run_audit", job_id=job_id, lighthouse_api_key="per-call-key")
    assert calls["key"] == "per-call-key"


@pytest.mark.asyncio
async def test_clear_resets_overrides(settings):
    mcp = build_server(settings)
    await call_tool(mcp, "configure_audit", lighthouse_api_key="k")
    res = await call_tool(mcp, "configure_audit", clear=True)
    assert res["active_overrides"] == {}
    assert load_runtime_overrides(settings) == {}


@pytest.mark.asyncio
async def test_invalid_values_rejected(settings):
    mcp = build_server(settings)
    res = await call_tool(mcp, "configure_audit", psi_strategy="tablet")
    assert "error" in res
    res = await call_tool(mcp, "configure_audit", psi_url_sample=0)
    assert "error" in res


def test_security_settings_cannot_be_overridden(settings):
    # The whitelist is the enforcement: even a crafted file can't smuggle
    # security-critical settings into the running configuration.
    save_runtime_overrides(settings, {"lighthouse_api_key": "k"})
    path = settings.data_dir / "_runtime_config.json"
    data = json.loads(path.read_text())
    data.update({"domain_allowlist": [], "remote_auth_token": "evil", "default_obey_robots": False})
    path.write_text(json.dumps(data))
    eff = with_runtime_overrides(settings)
    assert eff.remote_auth_token == settings.remote_auth_token
    assert eff.domain_allowlist == settings.domain_allowlist
    assert eff.default_obey_robots == settings.default_obey_robots
    assert not ({"domain_allowlist", "remote_auth_token"} & RUNTIME_OVERRIDE_KEYS)
