"""Temporary in-memory diagnostics for background playback continuity failures."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime
from typing import Any

RETIRE_AT_ISO = "2026-10-15T00:00:00+00:00"
RETIRE_AT = int(datetime.fromisoformat(RETIRE_AT_ISO).timestamp())
TTL_SECONDS = 5 * 60
MAX_REPORTS = 24
MAX_TIMELINE = 20
MAX_STRING = 512

_ALLOWED_TOP_LEVEL = {
    "diagnostic_id",
    "stage",
    "reason",
    "sent_at_ms",
    "client",
    "track",
    "sample",
    "timeline",
}
_SENSITIVE_KEYS = {
    "url", "src", "currentsrc", "cookie", "authorization", "token", "headers",
    "credential", "password", "query", "search", "href",
}


def enabled(now: int | None = None) -> bool:
    return (int(time.time()) if now is None else int(now)) < RETIRE_AT


def _bounded_string(value: Any, limit: int = MAX_STRING) -> str:
    return str(value or "")[:limit]


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        return value[:MAX_STRING]
    if isinstance(value, list):
        return [_sanitize(item, depth=depth + 1) for item in value[:MAX_TIMELINE]]
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:64]:
            key = _bounded_string(raw_key, 64)
            if key.lower() in _SENSITIVE_KEYS:
                continue
            clean[key] = _sanitize(raw_value, depth=depth + 1)
        return clean
    return _bounded_string(value)


def normalize_report(payload: dict[str, Any]) -> dict[str, Any]:
    clean = {
        key: _sanitize(payload[key])
        for key in _ALLOWED_TOP_LEVEL
        if key in payload
    }
    clean["diagnostic_id"] = _bounded_string(clean.get("diagnostic_id"), 96)
    clean["stage"] = _bounded_string(clean.get("stage"), 96)
    clean["reason"] = _bounded_string(clean.get("reason"), 256)
    timeline = clean.get("timeline")
    clean["timeline"] = timeline[-MAX_TIMELINE:] if isinstance(timeline, list) else []
    return clean


class EphemeralDiagnosticStore:
    """Bounded process memory only; restart or TTL expiration destroys every report."""

    def __init__(self) -> None:
        self._reports: deque[dict[str, Any]] = deque(maxlen=MAX_REPORTS)
        self._lock = asyncio.Lock()

    def _purge(self, now: int) -> None:
        cutoff = now - TTL_SECONDS
        while self._reports and int(self._reports[0]["received_at"]) < cutoff:
            self._reports.popleft()

    async def add(
        self,
        payload: dict[str, Any],
        *,
        source_node: str,
        delivery: str,
        now: int | None = None,
    ) -> dict[str, Any]:
        stamp = int(time.time()) if now is None else int(now)
        report = normalize_report(payload)
        report.update({
            "received_at": stamp,
            "expires_at": stamp + TTL_SECONDS,
            "source_node": _bounded_string(source_node, 64),
            "delivery": _bounded_string(delivery, 32),
        })
        async with self._lock:
            self._purge(stamp)
            self._reports.append(report)
        return report

    async def snapshot(self, *, now: int | None = None) -> dict[str, Any]:
        stamp = int(time.time()) if now is None else int(now)
        async with self._lock:
            self._purge(stamp)
            reports = list(self._reports)
        return {
            "enabled": enabled(stamp),
            "generated_at": stamp,
            "ttl_seconds": TTL_SECONDS,
            "max_reports": MAX_REPORTS,
            "retire_at": RETIRE_AT_ISO,
            "reports": reports,
        }

    async def clear(self) -> None:
        async with self._lock:
            self._reports.clear()


store = EphemeralDiagnosticStore()
