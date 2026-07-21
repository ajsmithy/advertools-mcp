"""Central configuration for the advertools MCP server.

Every value here is resolved from the project's single-source-of-truth Inputs
block, overridable via environment variables so the same image runs locally
(stdio) and remotely (HTTP) without code changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


# Resolved from the Inputs block. `contact_url` is flagged "to confirm" upstream;
# we surface that through `contact_url_confirmed` so tools can warn rather than
# silently bake an unverified contact into the crawler's User-Agent.
CONTACT_URL = os.getenv("ADVTOOLS_CONTACT_URL", "https://www.intrepidonline.com")
CONTACT_URL_CONFIRMED = _env_bool("ADVTOOLS_CONTACT_URL_CONFIRMED", False)

DEFAULT_USER_AGENT = os.getenv(
    "ADVTOOLS_USER_AGENT", f"intrepidbot (+{CONTACT_URL})"
)


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings shared by both transports."""

    server_name: str = "advertools-mcp"

    # Storage
    data_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("ADVTOOLS_DATA_DIR", "./data/crawls")
        ).resolve()
    )

    # Crawl defaults (politeness + safety caps)
    default_max_pages: int = field(
        default_factory=lambda: _env_int("ADVTOOLS_MAX_PAGES", 3000)
    )
    default_concurrent_requests: int = field(
        default_factory=lambda: _env_int("ADVTOOLS_CONCURRENT_REQUESTS", 6)
    )
    default_download_delay: float = field(
        default_factory=lambda: _env_float("ADVTOOLS_DOWNLOAD_DELAY", 0.25)
    )
    # Crawl speed as a per-host request-rate cap (URLs/second).
    default_crawl_speed: float = field(
        default_factory=lambda: _env_float("ADVTOOLS_CRAWL_SPEED", 5.0)
    )
    default_obey_robots: bool = field(
        default_factory=lambda: _env_bool("ADVTOOLS_OBEY_ROBOTS", True)
    )
    default_user_agent: str = DEFAULT_USER_AGENT
    contact_url: str = CONTACT_URL
    contact_url_confirmed: bool = CONTACT_URL_CONFIRMED

    # Job lifecycle
    max_concurrent_jobs: int = field(
        default_factory=lambda: _env_int("ADVTOOLS_MAX_CONCURRENT_JOBS", 2)
    )
    max_job_runtime_seconds: int = field(
        default_factory=lambda: _env_int("ADVTOOLS_MAX_JOB_RUNTIME", 3600)
    )

    # Audit optional tiers
    audit_url_sample: int = field(
        default_factory=lambda: _env_int("ADVTOOLS_AUDIT_URL_SAMPLE", 200)
    )
    lighthouse_api_key: str = field(
        default_factory=lambda: os.getenv("ADVTOOLS_LIGHTHOUSE_API_KEY", "")
    )
    enable_render: bool = field(
        default_factory=lambda: _env_bool("ADVTOOLS_ENABLE_RENDER", False)
    )
    gsc_credentials: str = field(
        default_factory=lambda: os.getenv("ADVTOOLS_GSC_CREDENTIALS", "")
    )

    # Transport / security
    transport: str = field(
        default_factory=lambda: os.getenv("ADVTOOLS_TRANSPORT", "stdio")
    )
    host: str = field(default_factory=lambda: os.getenv("ADVTOOLS_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("ADVTOOLS_PORT", 8000))
    remote_auth_token: str = field(
        default_factory=lambda: os.getenv("ADVTOOLS_BEARER_TOKEN", "")
    )
    domain_allowlist: list[str] = field(default_factory=lambda: _env_list("ADVTOOLS_DOMAIN_ALLOWLIST"))
    csv_export: bool = field(
        default_factory=lambda: _env_bool("ADVTOOLS_CSV_EXPORT", True)
    )

    @property
    def is_remote(self) -> bool:
        return self.transport.lower() in {"http", "streamable-http", "sse"}

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "_jobs"

    @property
    def audits_dir(self) -> Path:
        return self.data_dir / "_audits"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.audits_dir.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings singleton (created on first use)."""
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings
