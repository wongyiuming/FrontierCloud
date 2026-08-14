import os
import time
import urllib.parse
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.v1.endpoints import router as api_v1_router
from app.core.admin_log import append_admin_log
from app.core.db import init_db
from app.services.admin_service import issue_admin_token


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await issue_admin_token()
    yield


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
        headers = dict(scope.get("headers", []))
        x_real_ip = headers.get(b"x-real-ip", b"").decode("utf-8", errors="replace")
        client_ip = x_real_ip or (scope.get("client")[0] if scope.get("client") else "127.0.0.1")
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
            method = scope.get("method", "")
            path = scope.get("path", "")
            raw_query = scope.get("query_string", b"")
            raw_target = path + (("?" + raw_query.decode("utf-8", errors="replace")) if raw_query else "")
            log_line = (
                f"[LOG] REAL_IP: {client_ip} | PROXY_IP: {proxy_ip} | "
                f"{method} {path} - {status_code} ({elapsed:.2f}ms)"
            )
            request_line = (
                f"[REQUEST] REAL_IP: {client_ip} | PROXY_IP: {proxy_ip} | "
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
            return parsed.path
        query = "&".join(
            f"{k}={v}"
            for k, v in pairs
        )
        return f"{parsed.path}?{query}"
    except Exception:
        return target


app.add_middleware(RealIPLogMiddleware)
app.include_router(api_v1_router, prefix="/api/v1")

FAVICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "favicon.ico")


@app.get("/favicon.ico", include_in_schema=False)
def get_favicon():
    if os.path.exists(FAVICON_PATH):
        return FileResponse(FAVICON_PATH)
    return Response(status_code=204)


@app.get("/", include_in_schema=False)
async def root():
    return {"message": "Welcome to Office Automation Service. Go to /docs for API testing."}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, proxy_headers=True, forwarded_allow_ips="10.0.0.0/8,172.16.0.0/12,192.168.0.0/16")
