"""Read-only certificate observations on the existing authenticated node channel."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.internal_nodes import authenticated
from app.services.certmanager import certificate_control
from app.services.federation.state import state

router = APIRouter(prefix="/internal/v1", include_in_schema=False)


@router.post("/certificate")
async def certificate(request: Request):
    relation = await authenticated(request)
    if state.node["role"] != "Follower" or relation["direction"] != "upstream":
        raise HTTPException(403, "Only the paired Master can observe this node certificate")
    try:
        value = json.loads(request.state.node_control_body or b"{}")
        if not isinstance(value, dict) or set(value) - {"action", "browser_public_key", "browser_nonce"}:
            raise ValueError()
        result = await certificate_control(action=value.get("action", "status"),
                browser_public_key=value.get("browser_public_key", ""), browser_nonce=value.get("browser_nonce", ""))
    except (ValueError, TypeError):
        raise HTTPException(409, "automatic certificate controller unavailable") from None
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
