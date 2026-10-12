"""Authenticated whole-scope priority drag endpoints."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.v1.admin import require_session, _mutation_audit
from app.services import priority_order
from app.services import priority_planner as planner

router = APIRouter()


class Move(BaseModel):
    scope: str = Field('', max_length=1024)
    kind: str = Field(pattern='^(media|directory)$')
    revision: str = Field(pattern='^[a-f0-9]{64}$')
    path: str = Field(min_length=1, max_length=1024)
    before: str = Field('', max_length=1024)
    after: str = Field('', max_length=1024)


@router.get('/priority-order')
async def read(scope: str = Query('', max_length=1024),
               kind: str = Query('media', pattern='^(media|directory)$'), actor: str = Depends(require_session)):
    try:
        return await priority_order.order(scope, kind)
    except planner.PriorityMoveError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/priority-order')
async def write(payload: Move, request: Request, actor: str = Depends(require_session)):
    try:
        return await priority_order.reorder(payload.scope, payload.kind, payload.revision, payload.path,
            payload.before, payload.after, _mutation_audit(actor, 'priority_reorder', [payload.path], request))
    except priority_order.StaleOrder as exc:
        raise HTTPException(409, str(exc)) from exc
    except planner.PriorityRangeError as exc:
        raise HTTPException(422, str(exc)) from exc
    except planner.PriorityMoveError as exc:
        raise HTTPException(400, str(exc)) from exc
