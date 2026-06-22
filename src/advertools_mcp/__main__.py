"""CLI entry point. Selects transport and enforces remote security at startup.

    advertools-mcp                      # stdio (local)
    ADVTOOLS_TRANSPORT=http advertools-mcp  # streamable HTTP (remote)
"""

from __future__ import annotations

import sys

from .config import get_settings
from .server import build_server


def _build_http_app(mcp, settings):
    """Wrap the streamable-HTTP ASGI app with a bearer-token guard."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    token = settings.remote_auth_token

    class BearerAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path.rstrip("/") in ("/health", ""):
                return await call_next(request)
            auth = request.headers.get("authorization", "")
            if not auth.lower().startswith("bearer ") or auth.split(" ", 1)[1].strip() != token:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return await call_next(request)

    inner = mcp.streamable_http_app()
    app = Starlette(
        routes=inner.routes,
        middleware=[Middleware(BearerAuthMiddleware)],
        lifespan=inner.router.lifespan_context,
    )
    return app


def main() -> int:
    settings = get_settings()
    mcp = build_server(settings)

    if settings.is_remote:
        # Fail fast on insecure remote configuration.
        if not settings.remote_auth_token:
            print("ERROR: remote transport requires ADVTOOLS_BEARER_TOKEN.", file=sys.stderr)
            return 2
        if not settings.domain_allowlist:
            print(
                "ERROR: remote transport requires a non-empty ADVTOOLS_DOMAIN_ALLOWLIST "
                "(SSRF/abuse guard).",
                file=sys.stderr,
            )
            return 2
        import uvicorn

        app = _build_http_app(mcp, settings)
        uvicorn.run(app, host=settings.host, port=settings.port)
        return 0

    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
