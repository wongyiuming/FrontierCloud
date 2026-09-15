from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.db import engine
from app.core.metrics import DEPENDENCY_READY
from app.core.redis import redis_client

DEPENDENCY_TIMEOUT_SECONDS = 2


def live_status() -> dict[str, object]:
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


async def readiness_response() -> JSONResponse:
    checks: dict[str, str] = {}

    async def database_ping():
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def check(name, operation):
        try:
            async with asyncio.timeout(DEPENDENCY_TIMEOUT_SECONDS):
                await operation()
            checks[name] = "ready"
            DEPENDENCY_READY.labels(dependency=name).set(1)
        except Exception:
            checks[name] = "unavailable"
            DEPENDENCY_READY.labels(dependency=name).set(0)

    await asyncio.gather(check("redis", redis_client.ping), check("mysql", database_ping))

    ready = all(value == "ready" for value in checks.values())
    return JSONResponse(
        {"status": "ready" if ready else "unavailable", "checks": checks},
        status_code=200 if ready else 503,
        headers={"Cache-Control": "no-store"},
    )
