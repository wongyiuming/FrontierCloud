from __future__ import annotations

import ipaddress
from datetime import datetime, timezone
from typing import Iterable

from fastapi import HTTPException, Request
from redis.exceptions import RedisError
from sqlalchemy import text

from app.core.client_ip import resolve_client_identity
from app.core.config import settings
from app.core.db import engine
from app.core.redis import redis_client


REPORT_PREFIX = "webrtc:observation:"
ALLOWED_FAILURES = {"unsupported", "disabled", "timeout", "no_srflx", "ice_error"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    cooldown_key = REPORT_PREFIX + identity.ip
    cooldown_acquired = False
    try:
        accepted = await redis_client.set(
            cooldown_key,
            "1",
            ex=max(1, settings.WEBRTC_REPORT_COOLDOWN - 1),
            nx=True,
        )
        cooldown_acquired = bool(accepted)
    except RedisError:
        accepted = True
    if not accepted:
        raise HTTPException(status_code=429, detail="WebRTC observation rate limited")

    rows = [
        {
            "client_ip": identity.ip,
            "webrtc_ip": address,
            "outcome": outcome,
            "matches_verified": address == identity.ip,
            "observed_at": _utcnow(),
        }
        for address in normalized
    ] or [{
        "client_ip": identity.ip,
        "webrtc_ip": None,
        "outcome": outcome,
        "matches_verified": False,
        "observed_at": _utcnow(),
    }]
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO webrtc_observation_events
                    (client_ip, webrtc_ip, outcome, matches_verified, observed_at)
                VALUES
                    (:client_ip, :webrtc_ip, :outcome, :matches_verified, :observed_at)
            """), rows)
            await conn.execute(text("""
                INSERT INTO webrtc_observation_summary
                    (client_ip, webrtc_ip_key, webrtc_ip, observation_count,
                     matching_count, first_seen, last_seen, last_outcome)
                VALUES
                    (:client_ip, :webrtc_ip_key, :webrtc_ip, 1,
                     :matches_verified, :observed_at, :observed_at, :outcome)
                ON DUPLICATE KEY UPDATE
                    observation_count=observation_count + 1,
                    matching_count=matching_count + VALUES(matching_count),
                    last_seen=VALUES(last_seen),
                    last_outcome=VALUES(last_outcome)
            """), [
                {**row, "webrtc_ip_key": row["webrtc_ip"] or ""}
                for row in rows
            ])
    except BaseException:
        if cooldown_acquired:
            try:
                await redis_client.delete(cooldown_key)
            except RedisError:
                pass
        raise

    return {
        "status": "recorded",
        "address_count": len(normalized),
        "matches_verified": matches_verified,
        "outcome": outcome,
    }


def _optional_ip(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    try:
        return ipaddress.ip_address(str(value).strip()).compressed
    except ValueError as exc:
        raise ValueError("IP 地址无效") from exc


async def list_observation_summary(
    public_ip: str | None = None,
    webrtc_ip: str | None = None,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, object]:
    public_ip = _optional_ip(public_ip)
    webrtc_ip = _optional_ip(webrtc_ip)
    clauses: list[str] = []
    parameters: dict[str, object] = {
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }
    if public_ip:
        clauses.append("client_ip=:public_ip")
        parameters["public_ip"] = public_ip
    if webrtc_ip:
        clauses.append("webrtc_ip=:webrtc_ip")
        parameters["webrtc_ip"] = webrtc_ip
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    async with engine.connect() as conn:
        total = await conn.scalar(text(f"""
            SELECT COUNT(*)
            FROM webrtc_observation_summary
            {where}
        """), parameters)
        result = await conn.execute(text(f"""
            SELECT
                client_ip,
                webrtc_ip,
                observation_count,
                first_seen,
                last_seen,
                matching_count,
                last_outcome AS outcomes
            FROM webrtc_observation_summary
            {where}
            ORDER BY last_seen DESC, client_ip ASC, webrtc_ip ASC
            LIMIT :limit OFFSET :offset
        """), parameters)
        rows = result.mappings().all()
    total = int(total or 0)
    pages = max(1, (total + page_size - 1) // page_size)
    items = []
    for row in rows:
        item = dict(row)
        for field in ("first_seen", "last_seen"):
            if isinstance(item.get(field), datetime):
                item[field] = item[field].replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        for field in ("observation_count", "matching_count"):
            item[field] = int(item.get(field) or 0)
        items.append(item)
    return {
        "items": items,
        "pagination": {"page": page, "page_size": page_size, "pages": pages, "total": total},
        "filters": {"public_ip": public_ip, "webrtc_ip": webrtc_ip},
    }
