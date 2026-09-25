"""Admin controls for the current site's public availability."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.internal_nodes import require_https
from app.api.v1.admin import require_session
from app.services import site_control

router = APIRouter(prefix="/site")


class MaintenanceChange(BaseModel):
    enabled: bool


@router.get("/maintenance")
async def maintenance_status(actor: str = Depends(require_session)):
    return await site_control.status()


@router.post("/maintenance")
async def maintenance_change(request: Request, payload: MaintenanceChange,
                             actor: str = Depends(require_session)):
    require_https(request)
    try:
        return await site_control.set_maintenance(payload.enabled)
    except Exception as exc:
        raise HTTPException(409, str(exc)) from exc
