"""Clarifying-confirmation triggers.

High-impact or ambiguous crawl configuration must be confirmed before launch.
Each tool resolves its config, calls :func:`evaluate_crawl_config`, and — if any
trigger fires and the caller did not pass ``confirm=True`` — returns a structured
``needs_confirmation`` response (and may also raise an MCP elicitation) instead
of silently proceeding.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .config import Settings

# Above this, concurrency against a single host is considered aggressive.
HIGH_CONCURRENCY = 8


def _hosts(urls: list[str]) -> set[str]:
    out = set()
    for u in urls:
        netloc = urlparse(u).netloc.lower()
        if netloc:
            out.add(netloc)
    return out


def evaluate_crawl_config(resolved: dict[str, Any], settings: Settings) -> list[dict[str, str]]:
    """Return a list of ``{"code", "message"}`` warnings; empty means safe."""
    warnings: list[dict[str, str]] = []
    discovery = bool(resolved.get("follow_links"))
    urls = resolved.get("url_list") or []
    if isinstance(urls, str):
        urls = [urls]
    hosts = _hosts(urls)
    single_host = len(hosts) <= 1

    if discovery and not resolved.get("max_pages") and not resolved.get("max_depth"):
        warnings.append(
            {
                "code": "open_ended_crawl",
                "message": (
                    "Discovery crawl has neither max_pages nor max_depth set. "
                    "This can crawl an entire site without bound."
                ),
            }
        )

    if resolved.get("obey_robots") is False:
        warnings.append(
            {
                "code": "robots_disabled",
                "message": "obey_robots is OFF. The crawler will ignore robots.txt directives.",
            }
        )

    delay = resolved.get("download_delay")
    concurrency = resolved.get("concurrent_requests") or 0
    if single_host and (delay == 0 or concurrency > HIGH_CONCURRENCY):
        warnings.append(
            {
                "code": "aggressive_throttle",
                "message": (
                    f"High request pressure on a single host "
                    f"(concurrent_requests={concurrency}, download_delay={delay}). "
                    "This may overload the target."
                ),
            }
        )

    if discovery and not resolved.get("allowed_domains"):
        warnings.append(
            {
                "code": "no_allowed_domains",
                "message": (
                    "allowed_domains is unset on a discovery crawl; the crawler may "
                    "wander onto unintended hosts."
                ),
            }
        )

    if settings.is_remote and settings.domain_allowlist:
        allow = {d.lower() for d in settings.domain_allowlist}
        offending = [
            h for h in hosts if not any(h == d or h.endswith("." + d) for d in allow)
        ]
        if offending:
            warnings.append(
                {
                    "code": "outside_allowlist",
                    "message": (
                        f"Target host(s) {sorted(offending)} are not on the remote "
                        f"domain allowlist {sorted(allow)}."
                    ),
                }
            )
    return warnings


def needs_confirmation_response(
    resolved: dict[str, Any], warnings: list[dict[str, str]]
) -> dict[str, Any]:
    """Structured response returned when confirmation is required."""
    return {
        "status": "needs_confirmation",
        "message": (
            "This crawl configuration triggered safety checks. Review the resolved "
            "config and re-call the tool with confirm=true to proceed, or amend the "
            "parameters."
        ),
        "warnings": warnings,
        "resolved_config": resolved,
    }
