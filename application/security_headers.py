"""HTTP security response headers for the Web UI / API.

Pure ASGI middleware (not BaseHTTPMiddleware): Starlette's BaseHTTPMiddleware
buffers/re-streams bodies and breaks FileResponse with
"Response content longer than Content-Length" (e.g. /api/graph iframe).

CloudFront can layer Managed-SecurityHeadersPolicy as well; CSP is defined
here so Google Sign-In and same-origin APIs keep working.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Allow Google Identity Services (GSI) while locking down everything else.
# frame-src includes 'self' so Knowledge/Wiki Graph modals can iframe HTML
# (without 'self' the graph iframe is blank).
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' https://accounts.google.com https://apis.google.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; "
    "font-src 'self' data:; "
    "connect-src 'self' https://accounts.google.com https://oauth2.googleapis.com "
    "https://www.googleapis.com; "
    "frame-src 'self' https://accounts.google.com; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'"
)

# Graph HTML loads vis-network from unpkg + inline scripts; must be frameable by this app.
_GRAPH_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; "
    "font-src 'self' data:; "
    "connect-src 'self' https://unpkg.com; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; "
    "object-src 'none'"
)

_BASE_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"content-security-policy", _CONTENT_SECURITY_POLICY.encode("latin-1")),
]

_GRAPH_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"SAMEORIGIN"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"content-security-policy", _GRAPH_CONTENT_SECURITY_POLICY.encode("latin-1")),
]

_HSTS = (b"strict-transport-security", b"max-age=31536000; includeSubDomains")


def _viewer_is_https(scope: Scope) -> bool:
    headers = {k.lower(): v for k, v in scope.get("headers", [])}
    proto = (
        headers.get(b"cloudfront-forwarded-proto")
        or headers.get(b"x-forwarded-proto")
        or scope.get("scheme", "")
        or b""
    )
    if isinstance(proto, bytes):
        proto = proto.decode("latin-1", errors="ignore")
    return str(proto).lower() == "https"


def _is_graph_html(scope: Scope) -> bool:
    """Exact Knowledge/Wiki graph HTML (not /status|/query|/rebuild)."""
    path = scope.get("path") or "/"
    path = path.rstrip("/") or "/"
    return path in {"/api/graph", "/api/wiki/graph"}


def _header_names(headers: list[tuple[bytes, bytes]]) -> set[bytes]:
    return {name.lower() for name, _ in headers}


class SecurityHeadersMiddleware:
    """Attach baseline security headers; HSTS only for HTTPS viewers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_graph = _is_graph_html(scope)
        add_https = _viewer_is_https(scope)
        extra = _GRAPH_HEADERS if is_graph else _BASE_HEADERS

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                if is_graph:
                    # Force framing/CSP suitable for same-origin iframe + unpkg.
                    drop = {b"x-frame-options", b"content-security-policy"}
                    headers = [(n, v) for n, v in headers if n.lower() not in drop]
                existing = _header_names(headers)
                for name, value in extra:
                    if name not in existing:
                        headers.append((name, value))
                        existing.add(name)
                if add_https and b"strict-transport-security" not in existing:
                    headers.append(_HSTS)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
