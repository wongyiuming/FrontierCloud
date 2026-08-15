import asyncio
import os
import time
import urllib.parse
from contextlib import asynccontextmanager
from contextlib import suppress

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.v1.endpoints import router as api_v1_router
from app.core.admin_log import append_admin_log, sanitize_log_value
from app.core.client_ip import client_ip
from app.core.db import init_db
from app.middleware.ip_security import IPSecurityMiddleware
from app.services.admin_service import issue_admin_token, run_admin_token_issuer
from app.services.ip_security import initialize_ip_security_cache


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await initialize_ip_security_cache()
    await issue_admin_token()
    token_issuer_task = asyncio.create_task(
        run_admin_token_issuer(),
        name="admin-token-issuer",
    )
    try:
        yield
    finally:
        token_issuer_task.cancel()
        with suppress(asyncio.CancelledError):
            await token_issuer_task


app = FastAPI(title="Office Automation Service", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


class RealIPLogMiddleware:
    """Keep the legacy log format and add a decoded, human-readable request line."""

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
            log_line = (
                f"[LOG] REAL_IP: {verified_client_ip} | PROXY_IP: {proxy_ip} | "
                f"{method} {path} - {status_code} ({elapsed:.2f}ms)"
            )
            request_line = (
                f"[REQUEST] REAL_IP: {verified_client_ip} | PROXY_IP: {proxy_ip} | "
                f"{method} {render_query_log(raw_target)} - {status_code} ({elapsed:.2f}ms)"
            )
            print(log_line, flush=True)
            print(request_line, flush=True)
            if path != "/api/v1/media/admin/logs":
                append_admin_log(log_line)
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
app.include_router(api_v1_router, prefix="/api/v1")

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
