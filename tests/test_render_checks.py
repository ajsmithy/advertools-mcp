"""RENDER-tier checks: unit tests over faked render results (no browser)."""

import pandas as pd

from advertools_mcp.audit.checks import CHECKS
from advertools_mcp.audit.context import AuditContext
from advertools_mcp.config import Settings

RENDER_CHECKS = (15, 16, 21, 22, 35, 71, 75, 90)


def _ctx(render_results, has_render=True, body_text="short static", nav_links=""):
    df = pd.DataFrame({
        "url": ["https://s.test/p"],
        "status": [200],
        "body_text": [body_text],
        "nav_links_url": [nav_links],
    })
    return AuditContext(df=df, settings=Settings(), has_render=has_render,
                        render_results=render_results)


def _r(**over):
    base = {
        "ok": True, "error": None, "page_errors": [], "console_errors": [],
        "title_at_load": "T", "title_after_wait": "T",
        "rendered_text_len": 100, "rendered_links": [], "nav_link_count": 0,
        "hidden_text_len": 0, "pseudo_content_count": 0, "overlay": False,
    }
    base.update(over)
    return {"https://s.test/p": base}


def test_disabled_render_all_not_assessed():
    ctx = _ctx(None, has_render=False)
    for num in RENDER_CHECKS:
        assert CHECKS[num](ctx).status == "Not assessed", f"#{num}"


def test_enabled_but_browser_unavailable_not_assessed():
    ctx = _ctx(None, has_render=True)
    for num in RENDER_CHECKS:
        res = CHECKS[num](ctx)
        assert res.status == "Not assessed", f"#{num}"


def test_15_uncaught_js_errors():
    assert CHECKS[15](_ctx(_r(page_errors=["Error: boom"]))).status == "Present"
    assert CHECKS[15](_ctx(_r())).status == "Not present"


def test_16_js_only_nav():
    # Nav present rendered, absent statically -> flagged (heuristic).
    res = CHECKS[16](_ctx(_r(nav_link_count=3), nav_links=""))
    assert res.status == "Heuristic"
    # Nav present in both -> clean.
    res = CHECKS[16](_ctx(_r(nav_link_count=3), nav_links="https://s.test/a"))
    assert res.status == "Not present"


def test_21_load_failure_and_blank_page():
    assert CHECKS[21](_ctx(_r(ok=False, error="TimeoutError"))).status == "Present"
    assert CHECKS[21](_ctx(_r(rendered_text_len=3))).status == "Present"
    assert CHECKS[21](_ctx(_r())).status == "Not present"


def test_22_hidden_content_behind_js():
    big = _ctx(_r(rendered_text_len=2000), body_text="tiny")
    assert CHECKS[22](big).status == "Heuristic"
    same = _ctx(_r(rendered_text_len=110), body_text="x" * 100)
    assert CHECKS[22](same).status == "Not present"


def test_35_pseudo_content():
    assert CHECKS[35](_ctx(_r(pseudo_content_count=2))).status == "Heuristic"
    assert CHECKS[35](_ctx(_r())).status == "Not present"


def test_71_interstitial_overlay():
    assert CHECKS[71](_ctx(_r(overlay=True))).status == "Heuristic"
    assert CHECKS[71](_ctx(_r())).status == "Not present"


def test_75_title_rotation():
    res = CHECKS[75](_ctx(_r(title_after_wait="Different")))
    assert res.status == "Present"
    assert CHECKS[75](_ctx(_r())).status == "Not present"


def test_90_display_none_volume():
    assert CHECKS[90](_ctx(_r(hidden_text_len=500))).status == "Heuristic"
    assert CHECKS[90](_ctx(_r(hidden_text_len=50))).status == "Not present"


def test_failed_render_not_counted_by_dom_checks():
    # A URL whose render failed must not produce DOM-based verdicts.
    failed = _r(ok=False, error="nav error", hidden_text_len=9999, overlay=True,
                pseudo_content_count=9)
    for num in (16, 22, 35, 71, 75, 90):
        res = CHECKS[num](_ctx(failed))
        assert res.status in ("Not present",), f"#{num} used a failed render"
