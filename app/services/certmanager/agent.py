"""Compose-confined issuer and Unix status/export RPC. No Docker or systemd API."""
from __future__ import annotations

import json
import os
import stat
import socket as sockets
import sys
import threading
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.logging_config import configure_logging
from . import assets
from .contract import profile
from .manager import Manager


def create_control_app(manager: Manager):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/control")
    async def control(request: Request):
        if request.url.query:
            raise HTTPException(400, "invalid control request")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024:
                raise HTTPException(413, "control request too large")
        try:
            value = json.loads(body)
            if (not isinstance(value, dict) or set(value) - {"action", "browser_public_key", "browser_nonce"}
                    or value.get("action") not in ("status", "download")):
                raise ValueError()
            if value["action"] == "status":
                if value.get("browser_public_key") or value.get("browser_nonce"):
                    raise ValueError()
                result = manager.status()
            else:
                result = manager.download(value.get("browser_public_key", ""), value.get("browser_nonce", ""))
        except (ValueError, TypeError, KeyError):
            raise HTTPException(409, "automatic certificate operation unavailable; only status and verified browser download are supported") from None
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return app


def main():
    configure_logging()
    command = sys.argv[1] if len(sys.argv) == 2 else "serve"
    if command == "initialize":
        assets.initialize()
        return
    if command == "healthcheck":
        import asyncio
        from .client import certificate_control
        value = asyncio.run(certificate_control())
        if not value.get("controller_running"):
            raise SystemExit("certificate controller unavailable")
        # Issuance failure does not crash the controller; TLS/business readiness is separate.
        return
    if command != "serve":
        raise SystemExit("certificate agent accepts initialize, serve or healthcheck")
    manager = Manager(settings.SERVER_NAME, settings.TLS_ENABLED, profile(settings), settings.STORAGE_PORT)
    socket = assets.RUNTIME / "control.sock"
    try:
        info = socket.lstat()
        if not stat.S_ISSOCK(info.st_mode):
            raise ValueError("unsafe certificate control socket")
        socket.unlink()
    except FileNotFoundError:
        pass
    app = create_control_app(manager)
    server = uvicorn.Config(app, uds=str(socket), workers=1, proxy_headers=False, access_log=False,
                            log_config=None, limit_concurrency=8, timeout_keep_alive=1)
    old_umask = os.umask(0o117)  # Unix socket 0660, group 10001; Web only sees this RPC volume.
    listener = sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM)
    listener.bind(str(socket))
    os.chmod(socket, 0o660)
    os.umask(old_umask)
    worker = threading.Thread(target=manager.run, daemon=True, name="automatic-certificate-controller")
    worker.start()
    try:
        uvicorn.Server(server).run(sockets=[listener])
    finally:
        os.umask(old_umask)
        listener.close()
        manager.stop_event.set()
        worker.join(timeout=5)
        if not worker.is_alive():
            manager.close()
        # A daemon issuer that is still in a bounded CA request exits with the process;
        # retain the OS lease until then, preventing another controller from racing it.
        socket.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
