"""Authenticate upload requests before FastAPI consumes multipart bodies."""
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.v1.admin_transport import require_secure_admin_transport
from app.services import admin_service


class AdminUploadMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if (scope["type"] == "http"
                and scope.get("method") in {"POST", "PUT", "PATCH", "DELETE"}
                and scope.get("path", "").startswith("/api/v1/media/admin/upload/")):
            request = Request(scope)
            try:
                await require_secure_admin_transport(request)
                await admin_service.require_admin(request)
            except HTTPException as exc:
                await JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                   headers={**(exc.headers or {}), "Cache-Control": "no-store"})(scope, receive, send)
                return
        # Keep endpoint authorization too: keys/sessions may expire during upload.
        await self.app(scope, receive, send)
