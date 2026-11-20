"""HTTP hardening for the main API (production-readiness audit, 2026-11-17).

1. Request-body cap. Unauthenticated routes such as /api/login read the whole body, and the standalone
   Caddy profile has no request_body limit, so a client could make the process buffer an arbitrary body.
   The cap is applied in pure ASGI before any route runs: a declared Content-Length above the cap is
   refused with 413 immediately, and a chunked body is cut off once it passes the cap. The signed WAV
   route keeps its own, larger cap (voice_api.MAX_BODY_BYTES).
2. Security headers on every response, including the JSON 500 produced by the catch-all handler
   (Starlette's ServerErrorMiddleware runs outside @app.middleware, so errors used to go out bare).
"""
from __future__ import annotations

import json
import os

DEFAULT_MAX_BODY = 1 << 20          # 1 MiB: largest legitimate JSON body (CDR/verdict/report) is a few KB
LARGE_BODY_PATHS = ("/api/v1/audio",)

SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    # 20/11/2026: every page script now lives in a .js file (static/app/*.js, static/admin/assets/*.js), so
    # inline scripts and inline handlers are refused. Google Identity Services is the only external script.
    "Content-Security-Policy": ("frame-ancestors 'none'; object-src 'none'; base-uri 'self'; "
                                "script-src 'self' https://accounts.google.com"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), geolocation=(), payment=()",
}
HSTS = "max-age=31536000"


def max_body_bytes() -> int:
    raw = os.environ.get("FRAUD_MAX_BODY_BYTES")
    if raw is None:
        return DEFAULT_MAX_BODY
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("FRAUD_MAX_BODY_BYTES must be an integer number of bytes") from None
    if not 4096 <= value <= 64 << 20:
        raise ValueError("FRAUD_MAX_BODY_BYTES must be within [4096, 67108864]")
    return value


# FastAPI's interactive API docs load their own inline bootstrap and CDN scripts: they keep the previous,
# script-unrestricted policy rather than being broken by the page CSP.
DOCS_PATHS = ("/docs", "/redoc")
DOCS_CSP = "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"


def apply_security_headers(headers, scheme: str, path: str = "") -> None:
    """headers: a mutable mapping with setdefault (Starlette MutableHeaders)."""
    for name, value in SECURITY_HEADERS.items():
        if name == "Content-Security-Policy" and path.startswith(DOCS_PATHS):
            value = DOCS_CSP
        headers.setdefault(name, value)
    if scheme == "https":
        headers.setdefault("Strict-Transport-Security", HSTS)


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int | None = None, large_bytes: int | None = None):
        self.app = app
        self.max_bytes = max_body_bytes() if max_bytes is None else max_bytes
        if large_bytes is None:
            from voice_api import MAX_BODY_BYTES as large_bytes  # signed WAV upload (base64)
        self.large_bytes = large_bytes

    def _limit(self, path: str) -> int:
        return self.large_bytes if path.startswith(LARGE_BODY_PATHS) else self.max_bytes

    @staticmethod
    async def _reject(send):
        body = json.dumps({"detail": "Request body too large"}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        headers += [(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self._limit(scope.get("path", ""))
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared < 0 or declared > limit:
                    return await self._reject(send)
        seen = 0
        started = rejected = False

        async def capped_receive():
            # Chunked body past the cap: answer 413 ourselves and tell the app the client went away.
            # (Raising here did not work: routes that wrap request.json() in try/except turned it into 400.)
            nonlocal seen, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    rejected = True
                    if not started:
                        await self._reject(send)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            nonlocal started
            if rejected:
                return                      # the 413 already went out; drop the app's late response
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        await self.app(scope, capped_receive, guarded_send)
