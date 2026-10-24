import asyncio
import json
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
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.v1.endpoints import router as api_v1_router
from app.api.v1.media_crypto import router as media_crypto_router
from app.core.admin_log import sanitize_log_value
from app.core.config import settings
from app.core.client_ip import client_ip, resolve_client_identity
from app.core.db import close_db, init_db
from app.core.logging_config import bind_request_context, configure_logging, reset_request_context
from app.core.metrics import MetricsMiddleware
from app.core.static_assets import static_asset_url
from app.core.upload_lifecycle import install_upload_lifecycle_guard
from app.middleware.ip_security import IPSecurityMiddleware
from app.middleware.admin_upload import AdminUploadMiddleware
from app.middleware.node_role import NodeRoleMiddleware
from app.middleware.html_security import HtmlSecurityMiddleware
from app.middleware.native_storage import NativeStorageDeliveryMiddleware
from app.middleware.node_retirement import NodeRetirementMiddleware
from app.middleware.operator_fence import OperatorFenceMiddleware
from app.services import admin_service
from app.services.business_traffic import BusinessTrafficMiddleware, monitor as traffic_monitor
from app.services.resource_inventory import inventory as resource_inventory
from app.services.resource_observation import observer as resource_observer
from app.services.business_lifecycle import (
    ensure_follower_business_empty, ensure_follower_business_files_empty,
    initialize_business_services,
)
from app.services.health import live_status, readiness_response
from app.services.ip_security import initialize_ip_security_cache, retry_edge_projection
from app.services.media_manager import recover_interrupted_media_deletions
from app.services.runtime_secrets import announce_initial_secrets_once
from app.services.upload_cleanup import cleanup_stale_upload_parts, run_stale_upload_cleanup
from app.api.internal_nodes import router as internal_nodes_router
from app.api.internal_certificate import router as internal_certificate_router
from app.api.internal_control import router as internal_control_router
from app.services.federation.state import state as node_state
from app.services.federation.runtime import runtime as node_runtime


install_upload_lifecycle_guard()
configure_logging()
logger = logging.getLogger("frontiercloud.http")


async def _storage_role_admission() -> str | None:
    """Inspect initialized storage input before migrations, recovery or writes."""
    if settings.DEPLOYMENT_MODE != "only_stroge":
        return None
    from sqlalchemy import text
    from sqlalchemy.engine import URL
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.api.v1.media import MEDIA_ROOT
    from app.core import db
    from app.services.federation.protocol import endpoint

    if not settings.TLS_ENABLED:
        raise RuntimeError("Storage deployment requires TLS before fixing a node role")
    expected = endpoint(settings.STORAGE_ENDPOINT)
    database = db.engine
    reader = None
    if database.dialect.name == "sqlite":
        destination = Path(database.url.database)
        if not destination.exists() or destination.stat().st_size == 0:
            if (db.SECRET_DIR / "node-vault.key").exists():
                raise RuntimeError("Existing node identity has no SQLite database; storage admission refused")
            ensure_follower_business_files_empty(MEDIA_ROOT)
            return expected
        # Do not switch journal mode or create an empty file while diagnosing an
        # existing directory. Normal mode=ro sees committed WAL data as well.
        reader = create_async_engine(URL.create("sqlite+aiosqlite",
            database=destination.absolute().as_uri(), query={"mode": "ro", "uri": "true"}))
        database = reader
    try:
        async with database.connect() as conn:
            tables = await db._table_names(conn)
            if not tables:
                if (db.SECRET_DIR / "node-vault.key").exists():
                    raise RuntimeError("Existing node identity has no database tables; storage admission refused")
                ensure_follower_business_files_empty(MEDIA_ROOT)
                return expected
            # Unknown/partial databases are not eligible for implicit conversion.
            if "node_identity" not in tables:
                raise RuntimeError("Storage admission requires a valid initialized node identity")
            row = (await conn.execute(text(
                "SELECT node_id,role,endpoint FROM node_identity WHERE singleton=1"
            ))).mappings().first()
            if row is None:
                raise RuntimeError("Storage admission requires a valid initialized node identity")
            if row["role"] == "Follower":
                if row["endpoint"] != expected:
                    raise RuntimeError("Storage deployment cannot change a paired endpoint")
                return expected
            if row["role"] != "Standalone":
                raise RuntimeError("Storage deployment cannot change an existing node role")
            await ensure_follower_business_empty(conn=conn, media_root=MEDIA_ROOT)
            return expected
    finally:
        if reader is not None:
            await reader.dispose()


@asynccontextmanager
async def _lifespan(app: FastAPI,*,native_lease):
    upload_cleanup_task = None
    edge_projection_task = None
    traffic_task = None
    inventory_task = None
    transfer_reaper_task = None
    s3_recovery_task = None
    retirement_runtime = None
    migration_runtime = None
    try:
        from app.services.site_control import hard_fence_active
        fenced = hard_fence_active()
        storage_endpoint = await _storage_role_admission()
        if fenced:
            from app.core import db
            await db.validate_existing_database()
        else:
            await init_db()
        if storage_endpoint is not None:
            if fenced:
                await node_state.initialize(read_only=True)
            else:
                await node_state.initialize()
            if node_state.node["role"] == "Standalone":
                if fenced:
                    raise RuntimeError('Fenced startup cannot promote or replace the existing node identity')
                from app.api.v1.media import MEDIA_ROOT
                await ensure_follower_business_empty(node_state.database, media_root=MEDIA_ROOT)
                await node_state.promote("Follower", storage_endpoint, "storage-deployment")
            if node_state.node["role"] != "Follower" or node_state.node["endpoint"] != storage_endpoint:
                raise RuntimeError("Storage deployment cannot change an existing node role or paired endpoint")
        await recover_interrupted_media_deletions()
        if fenced:
            await initialize_ip_security_cache(read_only=True)
        else:
            await initialize_ip_security_cache()
        if storage_endpoint is None:
            if fenced:
                await node_state.initialize(read_only=True)
            else:
                await node_state.initialize()
        if node_state.node["role"] != "Standalone" and not settings.TLS_ENABLED:
            raise RuntimeError("Master/Follower 节点必须启用 TLS；请恢复 TLS 配置或显式重新初始化节点")
        if fenced:
            await initialize_business_services(read_only=True)
        else:
            await initialize_business_services()
        inventory_task = asyncio.create_task(resource_inventory.reap(), name='native-inventory-reaper')
        from app.api.internal_resource_transfer import transfers
        transfer_reaper_task=asyncio.create_task(transfers.reap_cancel_census(),name='native-cancel-census-reaper')
        traffic_monitor.require_gateway(settings.DEPLOYMENT_MODE != "only_stroge")
        if settings.DEPLOYMENT_MODE != "only_stroge":
            traffic_task = asyncio.create_task(traffic_monitor.watch_gateway(), name="business-traffic-observer")
        from app.services.resource_migration_runtime import runtime as migration_runtime
        migration_runtime.start()
        pending_revocations = any(row["state"] == "revoked" and not row["summary"].get("revocation_acknowledged")
                                  for row in await node_state.list_relationships(include_revoked=True))
        node_runtime.start(revocations=pending_revocations)
        edge_projection_task = asyncio.create_task(retry_edge_projection(), name="edge-security-retry")
        if not fenced:
            announce_initial_secrets_once()
        await asyncio.to_thread(cleanup_stale_upload_parts)
        upload_cleanup_task = asyncio.create_task(
            run_stale_upload_cleanup(),
            name="stale-upload-cleanup",
        )
        from app.services.s3_recovery import Recovery
        s3_recovery_task = asyncio.create_task(Recovery(node_state).run(),name='s3-upload-recovery')
        from app.services import node_retirement_runtime,runtime_gate
        retirement_runtime=node_retirement_runtime.start(runtime_gate.DATA_ROOT,native_lease)
        if node_state.node['role']=='Follower':
            from app.services.node_retirement_files import Authority
            from app.services.node_retirement_fences import RetirementError
            try:
                retirement_runtime.authority=await Authority.capture(retirement_runtime,node_state)
                retirement_runtime.authority_error=''
            except RetirementError as exc:
                retirement_runtime.authority_error=exc.code
            except OSError:
                retirement_runtime.authority_error='retirement_native_files_unavailable'
        yield
    finally:
        try:
            migration_stop_error=None
            if migration_runtime is not None:
                try:await migration_runtime.stop()
                except Exception as error:migration_stop_error=error
            if s3_recovery_task is not None:
                s3_recovery_task.cancel()
                with suppress(asyncio.CancelledError):
                    await s3_recovery_task
            await resource_observer.stop()
            from app.api.internal_resource_transfer import transfers
            if transfer_reaper_task is not None:
                transfer_reaper_task.cancel()
                with suppress(asyncio.CancelledError):await transfer_reaper_task
            await transfers.close()
            if inventory_task is not None:
                inventory_task.cancel()
                with suppress(asyncio.CancelledError):
                    await inventory_task
            if traffic_task is not None:
                traffic_task.cancel()
                with suppress(asyncio.CancelledError):
                    await traffic_task
            await node_runtime.stop()
            if edge_projection_task is not None:
                edge_projection_task.cancel()
                with suppress(asyncio.CancelledError):
                    await edge_projection_task
            if upload_cleanup_task is not None:
                upload_cleanup_task.cancel()
                with suppress(asyncio.CancelledError):
                    await upload_cleanup_task
            if migration_stop_error is not None:raise migration_stop_error
        finally:
            try:
                if retirement_runtime is not None:
                    from app.services import node_retirement_runtime
                    node_retirement_runtime.stop(retirement_runtime)
            finally:await close_db()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services import runtime_gate
    with runtime_gate.acquire() as native_lease:
        async with _lifespan(app,native_lease=native_lease):
            yield


app = FastAPI(
    title="FrontierCloud Media Service",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(internal_nodes_router)
app.include_router(internal_certificate_router)
app.include_router(internal_control_router)
app.include_router(media_crypto_router)

QUIET_REQUEST_PATHS = frozenset({
    "/health",
    "/health/live",
    "/health/ready",
    "/metrics",
    "/api/v1/health",
})
CATALOG_CACHE_CONTROL = b"private, no-cache, must-revalidate"


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
                if scope.get("admin_authenticated") and scope.get("admin_session_refresh", True):
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


class CatalogFreshnessMiddleware:
    """Keep mutable catalog APIs fresh while Redis remains the expensive-work cache."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http" or not str(scope.get("path") or "").startswith("/api/v1/media/catalog/"):
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", CATALOG_CACHE_CONTROL))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


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


app.add_middleware(AdminUploadMiddleware)
app.add_middleware(IPSecurityMiddleware)
app.add_middleware(MetricsMiddleware)
app.add_middleware(RealIPLogMiddleware)
app.add_middleware(NodeRoleMiddleware)
app.add_middleware(CatalogFreshnessMiddleware)
app.add_middleware(NativeStorageDeliveryMiddleware)
app.add_middleware(NodeRetirementMiddleware)
app.add_middleware(HtmlSecurityMiddleware)
app.add_middleware(BusinessTrafficMiddleware, monitor=traffic_monitor)
app.add_middleware(OperatorFenceMiddleware)
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
    from app.api.v1.media import inject_page_runtime, node_stun_urls, safe_json_dumps
    stun_urls = await node_stun_urls()
    content = karaoke_template()
    content = content.replace("{{KARAOKE_CSS_URL}}", static_asset_url("css/karaoke.css"))
    content = content.replace("{{KARAOKE_JS_URL}}", static_asset_url("js/karaoke.js"))
    content = content.replace("{{NETWORK_OBSERVATION_JS_URL}}", static_asset_url("js/network-observation.js"))
    content = content.replace("{{STUN_URLS}}", safe_json_dumps(stun_urls))
    content = content.replace("{{WEBRTC_INTERVAL_MS}}", str(settings.WEBRTC_REPORT_COOLDOWN * 1000))
    return HTMLResponse(inject_page_runtime(content, stun_urls), headers={"Cache-Control": "no-cache"})


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
