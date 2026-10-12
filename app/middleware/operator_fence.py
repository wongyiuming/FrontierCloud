"""Enforce the offline operator fence before application reads or mutations."""
from __future__ import annotations

import re

from starlette.types import ASGIApp, Receive, Scope, Send

from app.services.site_control import hard_fence_active

READ_ONLY_PROBES = frozenset(("/health", "/health/live", "/health/ready", "/api/v1/health", "/metrics"))
IDENTITY_CHALLENGE = re.compile(rb"challenge=[a-f0-9]{32}")
UNAVAILABLE = b'{"detail":"Operator maintenance fence is active"}'


class OperatorFenceMiddleware:
    """Manual maintenance stays separate; only the persistent hard fence applies."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        kind = scope["type"]
        if kind not in {"http", "websocket"} or not hard_fence_active():
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1013, "reason": "Operator maintenance"})
            return
        method, path = scope.get("method", "GET"), scope.get("path", "")
        # Existing handlers still enforce TLS and any probe authentication.
        # HTTP-01 is served by the separate certificate controller, not ASGI.
        if ((method in {"GET", "HEAD"} and path in READ_ONLY_PROBES)
                or (method == "GET" and path == "/internal/v1/identity"
                    and IDENTITY_CHALLENGE.fullmatch(scope.get("query_string", b"")))):
            await self.app(scope, receive, send)
            return
        await send({"type": "http.response.start", "status": 503, "headers": [
            (b"content-type", b"application/json"), (b"cache-control", b"no-store"),
            (b"retry-after", b"30"), (b"content-length", str(len(UNAVAILABLE)).encode("ascii")),
        ]})
        await send({"type": "http.response.body", "body": b"" if method == "HEAD" else UNAVAILABLE})
