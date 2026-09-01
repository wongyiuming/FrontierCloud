import asyncio
import logging
import os
import secrets
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from contextlib import suppress

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.v1.endpoints import router as api_v1_router
from app.core.admin_log import append_admin_log, sanitize_log_value
from app.core.config import settings
from app.core.client_ip import client_ip
from app.core.db import init_db
from app.core.logging_config import bind_request_context, configure_logging, reset_request_context
from app.core.metrics import MetricsMiddleware
from app.core.upload_lifecycle import install_upload_lifecycle_guard
from app.middleware.ip_security import IPSecurityMiddleware
from app.services import admin_service
from app.services.admin_service import issue_admin_token, run_admin_token_issuer
from app.services.health import live_status, readiness_response
from app.services.ip_security import initialize_ip_security_cache
from app.services.upload_cleanup import cleanup_stale_upload_parts, run_stale_upload_cleanup


install_upload_lifecycle_guard()
configure_logging()
logger = logging.getLogger("frontiercloud.http")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await initialize_ip_security_cache()
    await issue_admin_token()
    await asyncio.to_thread(cleanup_stale_upload_parts)
    token_issuer_task = asyncio.create_task(
        run_admin_token_issuer(),
        name="admin-token-issuer",
    )
    upload_cleanup_task = asyncio.create_task(
        run_stale_upload_cleanup(),
        name="stale-upload-cleanup",
    )
    try:
        yield
    finally:
        for task in (token_issuer_task, upload_cleanup_task):
            task.cancel()
        for task in (token_issuer_task, upload_cleanup_task):
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(
    title="FrontierCloud Media Service",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory="static"), name="static")

QUIET_REQUEST_PATHS = frozenset({
    "/health",
    "/health/live",
    "/health/ready",
    "/metrics",
    "/api/v1/health",
    "/api/v1/media/admin/logs",
})


class RealIPLogMiddleware:
    """Emit one decoded request line with all per-request network identities."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied_request_id = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore")
        request_id = supplied_request_id[:128] if supplied_request_id else uuid.uuid4().hex
        traceparent = headers.get(b"traceparent", b"").decode("ascii", errors="ignore")[:256]
        trace_parts = traceparent.split("-")
        trace_id = (
            trace_parts[1]
            if len(trace_parts) >= 4
            and len(trace_parts[1]) == 32
            and all(character in "0123456789abcdef" for character in trace_parts[1])
            else ""
        )
        context_tokens = bind_request_context(request_id, trace_id)
        verified_client_ip = scope.get("verified_client_ip") or client_ip(scope)
        proxy_ip = scope.get("client")[0] if scope.get("client") else "127.0.0.1"
        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode("ascii")))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            method = sanitize_log_value(scope.get("method", ""), 16)
            path = sanitize_log_value(scope.get("path", ""), 2048)
            if path not in QUIET_REQUEST_PATHS:
                raw_query = scope.get("query_string", b"")
                raw_target = path + (("?" + raw_query.decode("utf-8", errors="replace")) if raw_query else "")
                observation = scope.get("webrtc_observation") or {}
                observed_addresses = observation.get("addresses") or []
                webrtc_ip = ",".join(observed_addresses) or "-"
                request_line = (
                    f"[REQUEST] REAL_IP: {verified_client_ip} | PROXY_IP: {proxy_ip} | "
                    f"WEBRTC_IP: {webrtc_ip} | "
                    f"{method} {render_query_log(raw_target)} - {status_code} ({elapsed:.2f}ms)"
                )
                if observation:
                    request_line += (
                        f" | WEBRTC_MATCH: {str(bool(observation.get('matches_verified'))).lower()}"
                        f" | WEBRTC_OUTCOME: {sanitize_log_value(observation.get('outcome', '-'), 32)}"
                    )
                logger.info(
                    "request_completed",
                    extra={"context": {
                        "method": method,
                        "path": path,
                        "status": status_code,
                        "duration_ms": round(elapsed, 2),
                        "client_ip": verified_client_ip,
                        "proxy_ip": proxy_ip,
                        "webrtc_ip": webrtc_ip,
                    }},
                )
                append_admin_log(request_line)
            reset_request_context(context_tokens)


def render_query_log(target: str) -> str:
    """Decode URL query values for humans without turning separators into structure."""
    try:
        parsed = urllib.parse.urlsplit(target)
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
        if not pairs:
            return sanitize_log_value(parsed.path, 4000)
        query = "&".join(
            f"{k}={v}"
            for k, v in pairs
        )
        return sanitize_log_value(f"{parsed.path}?{query}", 4000)
    except ValueError:
        return sanitize_log_value(target, 4000)


app.add_middleware(RealIPLogMiddleware)
app.add_middleware(IPSecurityMiddleware)
app.add_middleware(MetricsMiddleware)
app.include_router(api_v1_router, prefix="/api/v1")


@app.get("/metrics", include_in_schema=False)
async def metrics(authorization: str | None = Header(None)):
    configured = settings.METRICS_TOKEN
    prefix = "Bearer "
    supplied = authorization[len(prefix):] if authorization and authorization.startswith(prefix) else ""
    if not configured or not supplied or not secrets.compare_digest(configured, supplied):
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health/live", include_in_schema=False)
async def health_live():
    return live_status()


@app.get("/health/ready", include_in_schema=False)
@app.get("/health", include_in_schema=False)
async def health_ready():
    return await readiness_response()


@app.get("/openapi.json", include_in_schema=False)
async def protected_openapi(_session: str = Depends(admin_service.require_admin)):
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False)
async def protected_docs(_session: str = Depends(admin_service.require_admin)):
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=f"{app.title} - Swagger UI",
    )


@app.get("/redoc", include_in_schema=False)
async def protected_redoc(_session: str = Depends(admin_service.require_admin)):
    return get_redoc_html(
        openapi_url="/openapi.json",
        title=f"{app.title} - ReDoc",
    )

FAVICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "favicon.ico")


@app.get("/favicon.ico", include_in_schema=False)
def get_favicon():
    if os.path.exists(FAVICON_PATH):
        return FileResponse(FAVICON_PATH)
    return Response(status_code=204)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/api/v1/media", status_code=307)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        proxy_headers=False,
    )
