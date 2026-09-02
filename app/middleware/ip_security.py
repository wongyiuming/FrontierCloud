from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

import logging
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
            logging.getLogger("frontiercloud.security").warning(
                "trusted_proxy_identity_rejected", extra={"context": {"peer_ip": identity.peer_ip}}
            )
            await JSONResponse({"detail": "Invalid proxy identity"}, status_code=400)(scope, receive, send)
            return

        block = await get_ip_block(identity.ip)
        if block:
            logging.getLogger("frontiercloud.security").warning(
                "blocked_request", extra={"context": {"client_ip": identity.ip, "expires_at": block.get("expires_at")}}
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
                logging.getLogger("frontiercloud.security").exception(
                    "invalid_api_accounting_failed", extra={"context": {"client_ip": identity.ip}}
                )
