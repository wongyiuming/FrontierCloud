from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.admin_log import append_admin_log
from app.core.client_ip import resolve_client_identity
from app.services.ip_security import get_ip_block, record_invalid_api


class IPSecurityMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        identity = resolve_client_identity(scope)
        scope["verified_client_ip"] = identity.ip
        if identity.trusted_proxy_header_missing:
            append_admin_log(
                f"[IP_SECURITY] rejected trusted proxy request without one valid X-Real-IP peer={identity.peer_ip}"
            )
            await JSONResponse({"detail": "Invalid proxy identity"}, status_code=400)(scope, receive, send)
            return

        block = await get_ip_block(identity.ip)
        if block:
            append_admin_log(
                f"[IP_SECURITY] blocked ip={identity.ip} expires_at={block.get('expires_at')}"
            )
            await JSONResponse(
                {"detail": "Request blocked by API security policy", "expires_at": block.get("expires_at")},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return

        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)

        route_matched = scope.get("route") is not None
        invalid_api = status_code == 405 or (status_code == 404 and not route_matched)
        if invalid_api:
            try:
                await record_invalid_api(
                    identity.ip,
                    scope.get("method", "")[:16],
                    scope.get("path", "")[:2048],
                    next(
                        (
                            value.decode("utf-8", errors="replace")
                            for name, value in scope.get("headers", [])
                            if name.lower() == b"user-agent"
                        ),
                        "",
                    ),
                )
            except Exception as exc:
                append_admin_log(f"[IP_SECURITY] post-response accounting failed for {identity.ip}: {exc}")
