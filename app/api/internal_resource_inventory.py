"""Signed read-only inventory for the node's current authenticated Master."""
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.internal_nodes import authenticated
from app.services.federation.state import state
from app.services.federation import protocol as p
from app.services.resource_inventory import InventoryError, inventory
from app.services.resource_control_errors import response as error_response

router = APIRouter(prefix='/internal/v1/migration/inventory', include_in_schema=False)


async def dispatch(request, operation, fields, *, error_type=InventoryError, prefix='inventory'):
    relation = await authenticated(request)
    try:
        value = json.loads(request.state.node_control_body)
        if not isinstance(value, dict) or set(value) != set(fields):
            raise error_type(prefix + '_request_invalid')
        result = await operation(state.node, relation, **value)
        if result is None:
            result = {'closed': True, 'session_id': value['session_id'], 'node_id': state.node['node_id']}
        envelope = p.sign(state.unseal(state.node['private_key']), result)
        return JSONResponse(envelope, headers={'Cache-Control': 'no-store'})
    except (ValueError, KeyError, TypeError, RecursionError) as exc:
        return error_response(state,path=request.url.path,raw=request.state.node_control_body,relation=relation,exc=exc,prefix=prefix)
    except OSError as exc:
        return error_response(state,path=request.url.path,raw=request.state.node_control_body,relation=relation,exc=exc,prefix=prefix)


@router.post('/start')
async def start(request: Request):
    return await dispatch(request, inventory.start, {'request_id', 'scope'})


@router.post('/page')
async def page(request: Request):
    return await dispatch(request, inventory.page, {'session_id', 'sequence'})


@router.post('/close')
async def close(request: Request):
    return await dispatch(request, inventory.close, {'session_id'})
