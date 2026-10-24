"""Signed, active-Master-only native registration pages."""
from fastapi import APIRouter, Request

from app.api.internal_resource_inventory import dispatch
from app.services.federation.state import state
from app.services.resource_inventory import inventory
from app.services.resource_observation import observer
from app.services.resource_registry import NativeRegistry, RegistryError

registry = NativeRegistry(state, observer, inventory)
router = APIRouter(prefix='/internal/v1/migration/registry', include_in_schema=False)


@router.post('/media')
async def media(request: Request):
    return await dispatch(request, registry.page, {'request_id','after'}, error_type=RegistryError, prefix='registry')


@router.post('/reserved-media')
async def reserved_media(request: Request):
    return await dispatch(request, registry.reserved_page, {'request_id','after','task_id','binding_sha256'},
                          error_type=RegistryError,prefix='registry')
