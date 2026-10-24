"""Authenticated, signed native preparation; no transfer or activation route."""
from fastapi import APIRouter, Request

from app.api.internal_resource_inventory import dispatch
from app.services.federation.state import state
from app.services.resource_peer_reservations import NativeReservations, ReservationError

reservations=NativeReservations(state)
router=APIRouter(prefix='/internal/v1/migration/reservations',include_in_schema=False)


@router.post('/cancel-unstarted')
async def cancel_unstarted(request:Request):
    return await dispatch(request,reservations.cancel_unstarted,{'request_id','reservation'},
                          error_type=ReservationError,prefix='migration_reservation')


@router.post('/begin')
async def begin(request:Request):
    return await dispatch(request,reservations.begin,{'request_id','reservation'},error_type=ReservationError,prefix='migration_reservation')


@router.post('/unit')
async def unit(request:Request):
    return await dispatch(request,reservations.add_unit,{'request_id','task_id','unit'},error_type=ReservationError,prefix='migration_reservation')


@router.post('/seal')
async def seal(request:Request):
    return await dispatch(request,reservations.seal,{'request_id','task_id'},error_type=ReservationError,prefix='migration_reservation')


@router.post('/status')
async def status(request:Request):
    return await dispatch(request,reservations.status,{'request_id','task_id'},error_type=ReservationError,prefix='migration_reservation')


@router.post('/abort-unstarted')
async def abort_unstarted(request:Request):
    return await dispatch(request,reservations.abort_unstarted,{'request_id','task_id'},error_type=ReservationError,prefix='migration_reservation')
