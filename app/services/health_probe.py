from __future__ import annotations

import asyncio
import time
import urllib.request

import redis.asyncio as redis

from app.core.config import settings


HEALTH_URL = "http://127.0.0.1:8000/health/ready"
ROLLING_KEY = "health:checks:120m"
WINDOW_MINUTES = 120
ROLLING_TTL_SECONDS = WINDOW_MINUTES * 60
ROLLING_COUNTER_SCRIPT = """
local key = KEYS[1]
local slot = ARGV[1]
local minute = ARGV[2]
local outcome = ARGV[3]
local ttl = tonumber(ARGV[4])
local minute_field = 'minute:' .. slot
local success_field = 'success:' .. slot
local failure_field = 'failure:' .. slot

if redis.call('HGET', key, minute_field) ~= minute then
    redis.call('HSET', key, minute_field, minute, success_field, 0, failure_field, 0)
end
redis.call('HINCRBY', key, outcome .. ':' .. slot, 1)
redis.call('EXPIRE', key, ttl)
return 1
"""


async def record_health_result(success: bool, *, timestamp: float | None = None, client=None) -> None:
    epoch_minute = int((time.time() if timestamp is None else timestamp) // 60)
    slot = epoch_minute % WINDOW_MINUTES
    owned_client = client is None
    active_client = client or redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await active_client.eval(
            ROLLING_COUNTER_SCRIPT,
            1,
            ROLLING_KEY,
            slot,
            epoch_minute,
            "success" if success else "failure",
            ROLLING_TTL_SECONDS,
        )
    finally:
        if owned_client:
            await active_client.aclose()


def request_health() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


async def run_health_probe() -> int:
    success = request_health()
    try:
        await record_health_result(success)
    except Exception:
        # Redis accounting must never turn a healthy web process unhealthy.
        pass
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_health_probe()))
