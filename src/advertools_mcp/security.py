"""Remote-transport security: bearer auth, domain allowlist (SSRF guard), rate limits.

For stdio (local) these are largely inert: an empty allowlist means unrestricted
local use. For HTTP, the bearer token is required on every request and the
allowlist must be non-empty (enforced at startup in ``__main__``).
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from .config import Settings


class SecurityError(Exception):
    """Raised when a request violates the security policy."""


class RateLimiter:
    """Simple sliding-window limiter keyed by client identity."""

    def __init__(self, max_events: int, window_seconds: float):
        self.max_events = max_events
        self.window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def check(self, client_id: str) -> None:
        now = time.time()
        q = self._events[client_id]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max_events:
            raise SecurityError(
                f"Rate limit exceeded ({self.max_events} requests / {self.window:.0f}s)."
            )
        q.append(now)


def host_allowed(host: str, allowlist: list[str]) -> bool:
    if not allowlist:
        return True
    host = host.lower()
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in allowlist)


def enforce_domain_allowlist(urls: list[str], settings: Settings) -> None:
    """Raise if any target host is outside a configured allowlist.

    Only enforced when the allowlist is non-empty (always required for remote).
    """
    if not settings.domain_allowlist:
        return
    offending = []
    for u in urls:
        host = urlparse(u).netloc.lower()
        if host and not host_allowed(host, settings.domain_allowlist):
            offending.append(host)
    if offending:
        raise SecurityError(
            f"Target host(s) {sorted(set(offending))} are not on the domain allowlist."
        )


def check_bearer(authorization: str | None, settings: Settings) -> None:
    """Validate an ``Authorization: Bearer <token>`` header for remote transport."""
    if not settings.is_remote:
        return
    if not settings.remote_auth_token:
        raise SecurityError("Server misconfigured: no bearer token set for remote transport.")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise SecurityError("Missing bearer token.")
    token = authorization.split(" ", 1)[1].strip()
    if token != settings.remote_auth_token:
        raise SecurityError("Invalid bearer token.")
