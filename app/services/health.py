from __future__ import annotations

from datetime import datetime, timezone

from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.db import engine
from app.core.metrics import DEPENDENCY_READY
from app.core.redis import redis_client


def live_status() -> dict[str, object]:
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


async def readiness_response() -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        await redis_client.ping()
        checks["redis"] = "ready"
        DEPENDENCY_READY.labels(dependency="redis").set(1)
    except Exception:
        checks["redis"] = "unavailable"
        DEPENDENCY_READY.labels(dependency="redis").set(0)

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        checks["mysql"] = "ready"
        DEPENDENCY_READY.labels(dependency="mysql").set(1)
    except Exception:
        checks["mysql"] = "unavailable"
        DEPENDENCY_READY.labels(dependency="mysql").set(0)

    ready = all(value == "ready" for value in checks.values())
    return JSONResponse(
        {"status": "ready" if ready else "unavailable", "checks": checks},
        status_code=200 if ready else 503,
        headers={"Cache-Control": "no-store"},
    )
