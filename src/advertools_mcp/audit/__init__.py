"""Post-crawl SEO audit. Reads its check definitions from the xlsx catalogue."""

from .engine import run_audit, AuditConfig

__all__ = ["run_audit", "AuditConfig"]
