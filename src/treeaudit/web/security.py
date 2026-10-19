"""HTTP hardening for the local server (docs/DECISIONS.md D-010, D-018).

Even when bound to 127.0.0.1, the server is reachable by any web page open in the
user's browser. These middlewares make sure such a page can neither read
responses (Host check against DNS rebinding, no CORS) nor trigger actions
(custom header required on state-changing requests).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

ALLOWED_HOSTS = ("127.0.0.1", "localhost")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "x-treeaudit-request"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}

CallNext = Callable[[Request], Awaitable[Response]]


async def _require_csrf_header(request: Request, call_next: CallNext) -> Response:
    if request.method not in SAFE_METHODS:
        content_type = request.headers.get("content-type", "")
        if request.headers.get(CSRF_HEADER) != "1" or not content_type.startswith(
            "application/json"
        ):
            return JSONResponse(
                {"detail": "missing X-TreeAudit-Request header or JSON content type"},
                status_code=403,
            )
    return await call_next(request)


async def _add_security_headers(request: Request, call_next: CallNext) -> Response:
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


def install_security_middlewares(app: FastAPI) -> None:
    """Install the middlewares. The last one added is the outermost."""
    app.middleware("http")(_require_csrf_header)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(ALLOWED_HOSTS))
    # Outermost, so that responses produced by the other middlewares get the headers.
    app.middleware("http")(_add_security_headers)
