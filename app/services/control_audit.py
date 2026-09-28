from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import Request

from app.services import admin_service


@asynccontextmanager
async def action(session_hash: str, action_name: str, request: Request, *,
                 target_count: int = 1, source_summary: str = "",
                 detail: str = "") -> AsyncIterator[None]:
    await admin_service.audit(
        session_hash, action_name, target_count, source_summary,
        "pending", detail, request,
    )
    try:
        yield
    except Exception as exc:
        failure_detail = f"{detail}; error={type(exc).__name__}" if detail else f"error={type(exc).__name__}"
        await admin_service.audit(
            session_hash, action_name, target_count, source_summary,
            "failed", failure_detail, request,
        )
        raise
    else:
        await admin_service.audit(
            session_hash, action_name, target_count, source_summary,
            "success", detail, request,
        )
