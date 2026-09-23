import asyncio
import logging
import os
import re
import secrets
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from contextlib import suppress
from functools import lru_cache

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.v1.endpoints import router as api_v1_router
from app.core.admin_log import sanitize_log_value
from app.core.config import settings
from app.core.client_ip import client_ip, resolve_client_identity
from app.core.db import close_db, init_db
from app.core.logging_config import bind_request_context, configure_logging, reset_request_context
from app.core.metrics import MetricsMiddleware
from app.core.static_assets import static_asset_url
from app.core.upload_lifecycle import install_upload_lifecycle_guard
from app.middleware.ip_security import IPSecurityMiddleware
from app.services import admin_service
from app.services.health import live_status, readiness_response
from app.services.ip_security import initialize_ip_security_cache, retry_edge_projection
from app.services.media_manager import recover_interrupted_media_deletions
from app.services.runtime_secrets import announce_initial_secrets_once
from app.services.upload_cleanup import cleanup_stale_upload_parts, run_stale_upload_cleanup
from app.api.internal_nodes import router as internal_nodes_router
from app.services.federation.state import state as node_state
from app.services.federation.runtime import runtime as node_runtime


install_upload_lifecycle_guard()
configure_logging()
logger = logging.getLogger("frontiercloud.http")


@asynccontextmanager
async def lifespan(app: FastAPI):
    upload_cleanup_task = None
    edge_projection_task = None
    try:
        await init_db()
        await recover_interrupted_media_deletions()
        await initialize_ip_security_cache()
        await node_state.initialize()
        pending_revocations = any(row["state"] == "revoked" and not row["summary"].get("revocation_acknowledged")
                                  for row in await node_state.list_relationships(include_revoked=True))
        node_runtime.start(revocations=pending_revocations)
        edge_projection_task = asyncio.create_task(retry_edge_projection(), name="edge-security-retry")
        announce_initial_secrets_once()
        await asyncio.to_thread(cleanup_stale_upload_parts)
        upload_cleanup_task = asyncio.create_task(
            run_stale_upload_cleanup(),
            name="stale-upload-cleanup",
        )
        yield
    finally:
        await node_runtime.stop()
        if edge_projection_task is not None:
            edge_projection_task.cancel()
            with suppress(asyncio.CancelledError):
                await edge_projection_task
        if upload_cleanup_task is not None:
            upload_cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await upload_cleanup_task
        await close_db()


app = FastAPI(
    title="FrontierCloud Media Service",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(internal_nodes_router)

QUIET_REQUEST_PATHS = frozenset({
    "/health",
    "/health/live",
    "/health/ready",
    "/metrics",
    "/api/v1/health",
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
        supplied_request_id = headers.get(b"x-request-id", b"").decode("ascii", errors="replace")
        request_id = (supplied_request_id if resolve_client_identity(scope).from_trusted_proxy
                      and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", supplied_request_id)
                      else uuid.uuid4().hex)
        traceparent = headers.get(b"traceparent", b"").decode("ascii", errors="replace")
        trace_parts = traceparent.split("-")
        valid_trace = (re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}", traceparent)
                       and trace_parts[1] != "0" * 32 and trace_parts[2] != "0" * 16)
        trace_id = trace_parts[1] if valid_trace else uuid.uuid4().hex
        scope["request_id"] = request_id
        scope["trace_id"] = trace_id
        context_tokens = bind_request_context(request_id, trace_id)
        verified_client_ip = scope.get("verified_client_ip") or client_ip(scope)
        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode("ascii")))
                audit_trace = (scope.get("media_audit") or {}).get("trace_id") or trace_id
                if audit_trace and not any(name.lower() == b"x-audit-trace-id" for name, _value in message["headers"]):
                    message["headers"].append((b"x-audit-trace-id", audit_trace.encode("ascii")))
                if scope.get("admin_authenticated"):
                    # Cookie expiry must slide with the authenticated Redis idle TTL.
                    session_ttl = int(scope.get("admin_session_ttl") or settings.ADMIN_SESSION_TTL)
                    cookie_response = Response()
                    cookie_response.set_cookie(
                        settings.ADMIN_COOKIE_NAME, scope["admin_session_cookie"],
                        max_age=session_ttl, httponly=True,
                        secure=settings.ADMIN_COOKIE_SECURE, samesite=settings.ADMIN_COOKIE_SAMESITE, path="/",
                    )
                    cookies = Request(scope).cookies
                    csrf = cookies.get(settings.ADMIN_CSRF_COOKIE_NAME)
                    if csrf:
                        cookie_response.set_cookie(
                            settings.ADMIN_CSRF_COOKIE_NAME, csrf, max_age=session_ttl,
                            secure=settings.ADMIN_COOKIE_SECURE, samesite=settings.ADMIN_COOKIE_SAMESITE, path="/",
                        )
                    message["headers"].extend((name, value) for name, value in cookie_response.raw_headers
                                              if name == b"set-cookie")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            method = sanitize_log_value(scope.get("method", ""), 16)
            path = sanitize_log_value(scope.get("path", ""), 2048)
            if path not in QUIET_REQUEST_PATHS or status_code >= 400:
                raw_query = scope.get("query_string", b"")
                raw_target = path + (("?" + raw_query.decode("utf-8", errors="replace")) if raw_query else "")
                observation = scope.get("webrtc_observation") or {}
                observed_addresses = observation.get("addresses") or []
                webrtc_ip = ",".join(observed_addresses) or "-"
                logger.info(
                    "request_completed",
                    extra={"context": {
                        "method": method,
                        "path": path,
                        "status": status_code,
                        "duration_ms": round(elapsed, 2),
                        "client_ip": verified_client_ip,
                        "webrtc_ip": webrtc_ip,
                        "webrtc_match": bool(observation.get("matches_verified")) if observation else None,
                        "webrtc_outcome": sanitize_log_value(observation.get("outcome", "-"), 32) if observation else None,
                        "media_audit": scope.get("media_audit"),
                        "range": sanitize_log_value(headers.get(b"range", b"").decode("ascii", errors="replace"), 256) or None,
                    }},
                )
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


app.add_middleware(IPSecurityMiddleware)
app.add_middleware(MetricsMiddleware)
app.add_middleware(RealIPLogMiddleware)
app.include_router(api_v1_router, prefix="/api/v1")


@app.get("/metrics", include_in_schema=False)
async def metrics(authorization: str | None = Header(None)):
    configured = settings.metrics_token
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
KARAOKE_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "media", "karaoke.html")


@lru_cache(maxsize=1)
def karaoke_template() -> str:
    with open(KARAOKE_TEMPLATE_PATH, encoding="utf-8") as template:
        return template.read()


@app.get("/favicon.ico", include_in_schema=False)
def get_favicon():
    if os.path.exists(FAVICON_PATH):
        return FileResponse(FAVICON_PATH)
    return Response(status_code=204)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/api/v1/media", status_code=307)


@app.get("/karaoke/", response_class=HTMLResponse, include_in_schema=False)
async def karaoke_page():
    content = karaoke_template()
    content = content.replace("{{KARAOKE_CSS_URL}}", static_asset_url("css/karaoke.css"))
    content = content.replace("{{KARAOKE_JS_URL}}", static_asset_url("js/karaoke.js"))
    return HTMLResponse(content, headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        proxy_headers=False,
        access_log=False,
    )
