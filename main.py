import asyncio
import os
import secrets
import time
import urllib.parse
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
from app.core.metrics import PrometheusMetricsMiddleware
from app.middleware.ip_security import IPSecurityMiddleware
from app.services import admin_service
from app.services.admin_service import issue_admin_token, run_admin_token_issuer
from app.services.ip_security import initialize_ip_security_cache
from app.services.wall_store import wall_store


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await initialize_ip_security_cache()
    await wall_store.initialize()
    await issue_admin_token()
    token_issuer_task = asyncio.create_task(
        run_admin_token_issuer(),
        name="admin-token-issuer",
    )
    wall_cleanup_task = asyncio.create_task(
        wall_store.run_cleanup(),
        name="wall-ciphertext-cleanup",
    )
    try:
        yield
    finally:
        token_issuer_task.cancel()
        wall_cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await token_issuer_task
        with suppress(asyncio.CancelledError):
            await wall_cleanup_task
        await wall_store.shutdown()


app = FastAPI(
    title="Office Automation Service",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory="static"), name="static")


class RealIPLogMiddleware:
    """Emit one decoded request line with all per-request network identities."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        verified_client_ip = scope.get("verified_client_ip") or client_ip(scope)
        proxy_ip = scope.get("client")[0] if scope.get("client") else "127.0.0.1"
        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            method = sanitize_log_value(scope.get("method", ""), 16)
            path = sanitize_log_value(scope.get("path", ""), 2048)
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
            print(request_line, flush=True)
            if path != "/api/v1/media/admin/logs":
                append_admin_log(request_line)


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
app.add_middleware(PrometheusMetricsMiddleware)
app.include_router(api_v1_router, prefix="/api/v1")


@app.get("/internal/metrics", include_in_schema=False)
async def internal_metrics(x_metrics_token: str | None = Header(None)):
    configured = settings.INTERNAL_METRICS_TOKEN
    if not configured or not x_metrics_token or not secrets.compare_digest(configured, x_metrics_token):
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


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
        host=os.getenv("DEV_HOST", "127.0.0.1"),
        port=8000,
        reload=os.getenv("ENVIRONMENT", "development").lower() == "development",
        proxy_headers=False,
    )
