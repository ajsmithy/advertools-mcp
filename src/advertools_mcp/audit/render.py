"""Headless-browser render pass for the RENDER-tier audit checks.

Uses Playwright/Chromium (optional dependency: ``pip install
'advertools-mcp[render]'`` then ``playwright install chromium``). Pages render
**one at a time** — a render loads a page with all its subresources like a real
visitor, so the sample is small (``render_url_sample``, default 10) and
sequential, keeping pressure on the target far below the crawl politeness cap.

Every entry point degrades gracefully: if Playwright or a Chromium binary is
unavailable, callers receive ``None`` and the RENDER checks report
"Not assessed" — never a fabricated verdict.
"""

from __future__ import annotations

import glob
import os
from typing import Any, Optional

_TIMEOUT_MS = 30_000
_SETTLE_MS = 2_500  # wait after load to observe JS-driven changes (e.g. titles)

# One JS pass over the rendered DOM collecting everything the checks consume.
_METRICS_JS = """
() => {
  const textLen = (document.body ? document.body.innerText : "").trim().length;
  const links = Array.from(document.querySelectorAll("a[href]"))
      .map(a => a.href).slice(0, 500);
  const navLinks = document.querySelectorAll(
      "nav a[href], [role=navigation] a[href]").length;
  let hiddenLen = 0, pseudo = 0, overlay = false;
  const vw = window.innerWidth, vh = window.innerHeight;
  for (const el of document.querySelectorAll("body *")) {
    const cs = getComputedStyle(el);
    if (cs.display === "none") {
      const parent = el.parentElement;
      if (!parent || getComputedStyle(parent).display !== "none") {
        hiddenLen += (el.textContent || "").trim().length;
      }
      continue;
    }
    for (const pos of ["::before", "::after"]) {
      const c = getComputedStyle(el, pos).content;
      if (c && c !== "none" && c !== "normal" && !c.startsWith("url(")) {
        const t = c.replace(/^["']|["']$/g, "");
        if (t.trim().length >= 3) pseudo++;
      }
    }
    if ((cs.position === "fixed" || cs.position === "absolute") &&
        cs.visibility !== "hidden") {
      const r = el.getBoundingClientRect();
      const z = parseInt(cs.zIndex, 10);
      if (r.width * r.height >= 0.5 * vw * vh && !isNaN(z) && z > 10) {
        overlay = true;
      }
    }
  }
  return {textLen, links, navLinks, hiddenLen, pseudo, overlay};
}
"""


def _chromium_executable(configured: str = "") -> Optional[str]:
    """Find a Chromium binary when Playwright's own download is absent."""
    candidates = [configured] if configured else []
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if browsers_path:
        candidates += sorted(glob.glob(f"{browsers_path}/chromium*/chrome-linux/chrome"))
        candidates.append(f"{browsers_path}/chromium")
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _render_one(context, url: str, timeout_ms: int, settle_ms: int) -> dict[str, Any]:
    page = context.new_page()
    page_errors: list[str] = []
    console_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)[:200]))
    page.on(
        "console",
        lambda msg: console_errors.append(msg.text[:200]) if msg.type == "error" else None,
    )
    try:
        page.goto(url, wait_until="load", timeout=timeout_ms)
        title_at_load = page.title()
        page.wait_for_timeout(settle_ms)
        title_after_wait = page.title()
        metrics = page.evaluate(_METRICS_JS)
        return {
            "ok": True,
            "error": None,
            "page_errors": page_errors,
            "console_errors": console_errors,
            "title_at_load": title_at_load,
            "title_after_wait": title_after_wait,
            "rendered_text_len": int(metrics.get("textLen", 0)),
            "rendered_links": metrics.get("links", []),
            "nav_link_count": int(metrics.get("navLinks", 0)),
            "hidden_text_len": int(metrics.get("hiddenLen", 0)),
            "pseudo_content_count": int(metrics.get("pseudo", 0)),
            "overlay": bool(metrics.get("overlay", False)),
        }
    except Exception as exc:  # noqa: BLE001 - navigation/timeout failures are findings
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "page_errors": page_errors,
            "console_errors": console_errors,
        }
    finally:
        page.close()


def run_render_sample(
    urls: list[str],
    user_agent: str,
    chromium_path: str = "",
    timeout_ms: int = _TIMEOUT_MS,
    settle_ms: int = _SETTLE_MS,
) -> Optional[dict[str, dict[str, Any]]]:
    """Render the sample sequentially. Returns {url: metrics} or None when the
    browser stack is unavailable (callers report "Not assessed")."""
    sample = list(dict.fromkeys(urls))
    if not sample:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    out: dict[str, dict[str, Any]] = {}
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception:  # noqa: BLE001 - version-mismatched browser dir etc.
                exe = _chromium_executable(chromium_path)
                if exe is None:
                    return None
                browser = p.chromium.launch(headless=True, executable_path=exe)
            context = browser.new_context(
                user_agent=user_agent, viewport={"width": 1280, "height": 800}
            )
            for url in sample:
                out[url] = _render_one(context, url, timeout_ms, settle_ms)
            browser.close()
    except Exception:  # noqa: BLE001
        return out if out else None
    return out if out else None
