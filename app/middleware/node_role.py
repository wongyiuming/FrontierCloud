"""Uniform role boundary applied before any business router runs."""
from __future__ import annotations

import json

from starlette.types import ASGIApp, Receive, Scope, Send

from app.services.federation.state import state


class NodeRoleMiddleware:
    ALLOWED_PREFIXES = (
        "/static/", "/internal/v1/", "/health", "/metrics", "/api/v1/health",
        "/api/v1/media/admin/nodes", "/api/v1/media/admin/elevate",
        "/api/v1/media/admin/status", "/api/v1/media/admin/logout",
    )
    ADMIN_PAGE_PATHS = frozenset(("/api/v1/media/admin", "/api/v1/media/admin/"))
    PUBLIC_PAGE_PATHS = frozenset(("/", "/karaoke/", "/api/v1/media", "/api/v1/media/"))

    def __init__(self, app: ASGIApp):
        self.app = app

    async def _master_url(self) -> str:
        try:
            rows = await state.list_relationships()
            upstream = next((row for row in rows if row["direction"] == "upstream"
                             and row["state"] in ("pending", "active")), None)
            return upstream["peer_endpoint"] if upstream else ""
        except Exception:
            return ""

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http" or state.node.get("role") != "Follower":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/favicon.ico" or path in self.ADMIN_PAGE_PATHS or any(
                path == prefix or path.startswith(prefix) for prefix in self.ALLOWED_PREFIXES):
            await self.app(scope, receive, send)
            return
        master = await self._master_url()
        method = scope.get("method", "GET").upper()
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        accepts_html = b"text/html" in headers.get(b"accept", b"")
        if method in ("GET", "HEAD") and master and (path in self.PUBLIC_PAGE_PATHS or accepts_html):
            query = scope.get("query_string", b"")
            location = master.rstrip("/") + path
            if query:
                location += "?" + query.decode("ascii", errors="ignore")
            response_headers = [(b"location", location.encode("latin-1")),
                                (b"cache-control", b"no-store"), (b"content-length", b"0")]
            await send({"type": "http.response.start", "status": 307, "headers": response_headers})
            await send({"type": "http.response.body", "body": b""})
            return
        body = json.dumps({
            "code": "NODE_BUSINESS_DISABLED_ON_FOLLOWER",
            "detail": "业务由 Master 管理",
            "master_url": master,
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        await send({"type": "http.response.start", "status": 409, "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"cache-control", b"no-store"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]})
        await send({"type": "http.response.body", "body": body})
