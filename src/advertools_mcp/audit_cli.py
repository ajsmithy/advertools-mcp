"""Terminal CLI for running the SEO audit outside the MCP request/response cycle.

For large datasets, a synchronous MCP audit can exceed a client's response
timeout. Run it here instead — it takes as long as it needs — then point your
MCP client at the resulting workbook.

    advertools-audit --folder /path/to/screamingfrog-export
    advertools-audit --crawl  /path/to/data/crawls/<job>/crawl.parquet
    advertools-audit --folder ./sf --lighthouse-key AIza... --enable-render

Prints a JSON summary (including the .xlsx path) to stdout.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from .config import Settings, with_runtime_overrides


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="advertools-audit", description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--folder", help="Folder of Screaming Frog exports to audit.")
    src.add_argument("--crawl", help="Path to a saved advertools crawl parquet.")
    parser.add_argument("--data-dir", help="Artefact/data directory (default: env or ./data/crawls).")
    parser.add_argument("--out-dir", help="Where to write the audit .xlsx (default: <data-dir>/_audits).")
    parser.add_argument("--lighthouse-key", help="PageSpeed Insights API key (enables the CWV tier).")
    parser.add_argument("--psi-sample", type=int, help="Max URLs given a Lighthouse pass.")
    parser.add_argument("--psi-strategy", choices=("mobile", "desktop"), help="PSI strategy.")
    parser.add_argument("--enable-render", action="store_true", help="Enable the RENDER tier (headless browser).")
    parser.add_argument("--render-sample", type=int, help="Max URLs rendered headlessly.")
    parser.add_argument("--has-backlinks", action="store_true", help="Signal that backlink data is available.")
    args = parser.parse_args(argv)

    settings = Settings(data_dir=Path(args.data_dir).resolve()) if args.data_dir else Settings()
    settings.ensure_dirs()
    settings = with_runtime_overrides(settings)

    overrides: dict = {}
    if args.lighthouse_key:
        overrides["lighthouse_api_key"] = args.lighthouse_key
    if args.psi_sample:
        overrides["psi_url_sample"] = args.psi_sample
    if args.psi_strategy:
        overrides["psi_strategy"] = args.psi_strategy
    if args.enable_render:
        overrides["enable_render"] = True
    if args.render_sample:
        overrides["render_url_sample"] = args.render_sample
    if overrides:
        settings = dataclasses.replace(settings, **overrides)

    out_dir = Path(args.out_dir).resolve() if args.out_dir else settings.audits_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    from .audit.engine import run_audit, run_audit_from_folder

    try:
        if args.folder:
            summary = run_audit_from_folder(args.folder, settings, out_dir, has_backlinks=args.has_backlinks)
        else:
            summary = run_audit(args.crawl, settings, out_dir, has_backlinks=args.has_backlinks)
    except (FileNotFoundError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2, default=str))
    print(f"\nAudit workbook: {summary['audit_xlsx']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
