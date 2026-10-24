"""Authenticated metadata-only control for a source's joined content worker."""
from fastapi import APIRouter, Request

from app.api.internal_resource_inventory import dispatch
from app.services.resource_observation import ObservationError, observer

router = APIRouter(prefix='/internal/v1/migration/observe', include_in_schema=False)


@router.post('/start')
async def start(request: Request):
    return await dispatch(request, observer.start, {'request_id', 'resource_kind', 'object_id', 'user_id'},
                          error_type=ObservationError, prefix='observation')


@router.post('/status')
async def status(request: Request):
    return await dispatch(request, observer.status, {'observation_id'}, error_type=ObservationError, prefix='observation')


@router.post('/close')
async def close(request: Request):
    return await dispatch(request, observer.close, {'observation_id'}, error_type=ObservationError, prefix='observation')
