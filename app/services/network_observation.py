from __future__ import annotations

import ipaddress
from typing import Iterable

from fastapi import HTTPException, Request
from redis.exceptions import RedisError

from app.core.client_ip import resolve_client_identity
from app.core.config import settings
from app.core.redis import redis_client


REPORT_PREFIX = "webrtc:observation:"
ALLOWED_FAILURES = {"unsupported", "disabled", "timeout", "no_srflx", "ice_error"}


def normalize_observed_addresses(values: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        try:
            address = ipaddress.ip_address(str(value).strip())
        except ValueError as exc:
            raise ValueError("Invalid observed IP address") from exc
        if address.is_unspecified or address.is_multicast:
            raise ValueError("Invalid observed IP address")
        canonical = address.compressed
        if canonical not in normalized:
            normalized.append(canonical)
        if len(normalized) > 8:
            raise ValueError("Too many observed IP addresses")
    return normalized


async def record_observation(
    request: Request,
    addresses: Iterable[str],
    failure: str | None,
) -> dict[str, object]:
    identity = resolve_client_identity(request.scope)
    normalized = normalize_observed_addresses(addresses)
    failure_value = str(failure or "").strip()
    if failure_value and failure_value not in ALLOWED_FAILURES:
        raise ValueError("Invalid WebRTC failure reason")
    if not normalized and not failure_value:
        raise ValueError("No WebRTC observation supplied")
    matches_verified = identity.ip in normalized
    outcome = failure_value or "ok"
    request.scope["webrtc_observation"] = {
        "addresses": normalized,
        "matches_verified": matches_verified,
        "outcome": outcome,
    }
    try:
        accepted = await redis_client.set(
            REPORT_PREFIX + identity.ip,
            "1",
            ex=settings.WEBRTC_REPORT_COOLDOWN,
            nx=True,
        )
    except RedisError:
        accepted = True
    if not accepted:
        raise HTTPException(status_code=429, detail="WebRTC observation rate limited")

    return {
        "status": "recorded",
        "address_count": len(normalized),
        "matches_verified": matches_verified,
        "outcome": outcome,
    }
