"""Admin node observability endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.v1.admin import require_session
from app.services import node_observability

router = APIRouter(prefix="/nodes/observability")


@router.get("")
async def observability(actor: str = Depends(require_session)):
    return await node_observability.snapshot()
