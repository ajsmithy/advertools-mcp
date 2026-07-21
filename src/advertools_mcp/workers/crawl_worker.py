"""Subprocess entry point for a single advertools crawl.

Why a subprocess: advertools' crawler runs Scrapy on a Twisted reactor, which
cannot be restarted within one process. Calling ``adv.crawl`` a second time in a
long-lived server process raises ``ReactorNotRestartable``. Spawning a fresh
process per crawl sidesteps this entirely — the reactor lives and dies with the
worker. The server never imports Scrapy in its own process.

Invocation::

    python -m advertools_mcp.workers.crawl_worker <spec.json>

The worker reads a job spec, runs the crawl, converts ``.jl`` to parquet, and
writes a result JSON the parent process polls. All advertools imports happen
here, lazily, so the server stays light.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any


def _write_result(result_path: str, payload: dict[str, Any]) -> None:
    tmp = result_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    os.replace(tmp, result_path)


def _ensure_scrapy_on_path() -> None:
    """Make advertools' ``subprocess.run(["scrapy", ...])`` call resolvable.

    advertools shells out to the ``scrapy`` console script, which lives next to
    the running interpreter (``<venv>/bin``). When the server is launched by
    absolute path to the venv's python (e.g. by Claude Desktop) the venv is not
    "activated", so that bin dir is absent from PATH and the bare ``scrapy``
    command fails with FileNotFoundError. Prepend the interpreter's bin dir so
    the script is always found.
    """
    bindir = os.path.dirname(os.path.abspath(sys.executable))
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if bindir and bindir not in parts:
        os.environ["PATH"] = os.pathsep.join([bindir, *parts]) if parts else bindir


def _count_jl_lines(path: str) -> int:
    if not os.path.exists(path):
        return 0
    n = 0
    with open(path, "rb") as fh:
        for _ in fh:
            n += 1
    return n


def _merge_selectors(user: dict | None, defaults: dict) -> dict:
    """User selectors win on name collision; defaults fill the rest."""
    merged = dict(defaults)
    if user:
        merged.update(user)
    return merged


def run_crawl(spec: dict[str, Any]) -> dict[str, Any]:
    _ensure_scrapy_on_path()

    import advertools as adv
    import advertools.crawlytics as crawlytics

    from advertools_mcp.crawl.schema import (
        DEFAULT_CSS_SELECTORS,
        DEFAULT_XPATH_SELECTORS,
    )

    kind = spec["kind"]
    output_jl = spec["output_jl"]
    output_parquet = spec.get("output_parquet")
    Path(output_jl).parent.mkdir(parents=True, exist_ok=True)

    if kind == "crawl":
        cfg = spec["crawl"]
        css = _merge_selectors(cfg.get("css_selectors"), DEFAULT_CSS_SELECTORS)
        xpath = _merge_selectors(cfg.get("xpath_selectors"), DEFAULT_XPATH_SELECTORS)
        kwargs: dict[str, Any] = dict(
            url_list=cfg["url_list"],
            output_file=output_jl,
            follow_links=cfg.get("follow_links", False),
            css_selectors=css,
            xpath_selectors=xpath,
            custom_settings=cfg.get("custom_settings") or {},
        )
        for key in (
            "allowed_domains",
            "include_url_regex",
            "exclude_url_regex",
            "include_url_params",
            "exclude_url_params",
        ):
            if cfg.get(key) is not None:
                kwargs[key] = cfg[key]
        adv.crawl(**kwargs)

    elif kind == "header_crawl":
        cfg = spec["crawl"]
        adv.crawl_headers(
            url_list=cfg["url_list"],
            output_file=output_jl,
            custom_settings=cfg.get("custom_settings") or {},
        )

    elif kind == "image_crawl":
        cfg = spec["crawl"]
        adv.crawl_images(
            start_urls=cfg["url_list"],
            output_dir=spec["output_dir"],
            min_width=cfg.get("min_width", 0),
            min_height=cfg.get("min_height", 0),
            include_img_regex=cfg.get("include_img_regex"),
            custom_settings=cfg.get("custom_settings") or {},
        )
        # crawl_images writes a directory of jl shards; flatten to one parquet.
        import pandas as pd

        shards = sorted(Path(spec["output_dir"]).glob("*.jl"))
        frames = [pd.read_json(s, lines=True) for s in shards if s.stat().st_size]
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if output_parquet:
            df.to_parquet(output_parquet, index=False)
        return {
            "pages": int(len(df)),
            "errors": 0,
            "finish_reason": "finished",
            "output_parquet": output_parquet,
        }
    else:
        raise ValueError(f"Unknown crawl kind: {kind!r}")

    # Convert jl -> parquet for columnar querying (crawl / header crawl).
    pages = _count_jl_lines(output_jl)
    if output_parquet and pages:
        crawlytics.jl_to_parquet(output_jl, output_parquet)

    return {
        "pages": pages,
        "errors": 0,
        "finish_reason": "finished",
        "output_parquet": output_parquet if pages else None,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: crawl_worker <spec.json>", file=sys.stderr)
        return 2
    spec_path = argv[1]
    with open(spec_path) as fh:
        spec = json.load(fh)
    result_path = spec["result_path"]
    try:
        result = run_crawl(spec)
        result["state"] = "done"
        _write_result(result_path, result)
        return 0
    except Exception as exc:  # noqa: BLE001 - report any failure to the parent
        _write_result(
            result_path,
            {
                "state": "failed",
                "error_message": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
                "finish_reason": "exception",
            },
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
