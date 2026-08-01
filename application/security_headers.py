"""HTTP security response headers for the Web UI / API.

Applied via Starlette middleware so every response (including SPA and SSE)
gets a baseline set. CloudFront can layer Managed-SecurityHeadersPolicy as well;
CSP is defined here so Google Sign-In and same-origin APIs keep working.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

# Allow Google Identity Services (GSI) while locking down everything else.
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' https://accounts.google.com https://apis.google.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; "
    "font-src 'self' data:; "
    "connect-src 'self' https://accounts.google.com https://oauth2.googleapis.com "
    "https://www.googleapis.com; "
    "frame-src https://accounts.google.com; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'"
)

_BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": _CONTENT_SECURITY_POLICY,
}

_HSTS = "max-age=31536000; includeSubDomains"


def _viewer_is_https(request: Request) -> bool:
    proto = (
        request.headers.get("cloudfront-forwarded-proto")
        or request.headers.get("x-forwarded-proto")
        or request.url.scheme
        or ""
    ).lower()
    return proto == "https"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach baseline security headers; HSTS only for HTTPS viewers."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for name, value in _BASE_HEADERS.items():
            response.headers.setdefault(name, value)
        if _viewer_is_https(request):
            response.headers.setdefault("Strict-Transport-Security", _HSTS)
        return response
