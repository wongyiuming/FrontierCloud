"""Master metadata controls and separately authenticated direct peer byte ranges."""
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.internal_nodes import require_https
from app.api.internal_resource_inventory import dispatch
from app.services.federation import protocol as p
from app.services.federation.state import state
from app.services.resource_transfer_peers import TransferPeers
from app.services.resource_transfer_cancel import CancelPeers
from app.services.resource_cancel_target import TargetCancellation
from app.services.resource_cancel_source_peers import SourceCancellation
from app.services.resource_cancel_unit_peers import UnitSettlement
from app.services.resource_cancel_census import SourceCensus
from app.services.resource_cancel_source_release import SourceRelease
from app.services.resource_cancel_target_release import TargetRelease
from app.services.resource_publish_peers import PublishPeers
from app.services.resource_source_verify import SourceVerification
from app.services.resource_commit_peers import CommitPeers
from app.services.resource_target_verify import TargetVerification
from app.services.resource_source_cleanup import SourceCleanup
from app.services.resource_finalize_peers import FinalizePeers
from app.services.resource_transfer_protocol import CONTROL_BYTES,TransferError
from app.services.resource_control_errors import response as error_response

transfers=TransferPeers(state)
router=APIRouter(prefix='/internal/v1/migration/transfer',include_in_schema=False)


class RangeResponse(StreamingResponse):
    def __init__(self,handle,**kwargs):
        self.handle=handle
        super().__init__(handle.body,**kwargs)

    async def __call__(self,scope,receive,send):
        try:await super().__call__(scope,receive,send)
        finally:await self.handle.close()


@router.post('/source')
async def source(request:Request):
    require_https(request)
    body=bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk)>CONTROL_BYTES:raise HTTPException(413,'migration_transfer_control_too_large')
        body.extend(chunk)
    value=None
    try:
        value=json.loads(body)
        reply,handle=await transfers.source(value)
        if handle is None:return JSONResponse(reply,headers={'Cache-Control':'no-store'})
        return RangeResponse(handle,media_type='application/octet-stream',headers={'Cache-Control':'no-store',
            'Content-Length':str(reply['payload']['length']),'X-FrontierCloud-Transfer-Proof':p.encode(p.canonical(reply)),
            'X-Accel-Buffering':'no'})
    except (ValueError,KeyError,TypeError,OSError) as exc:
        envelope=value.get('grant',{}) if isinstance(value,dict) else {}
        payload=envelope.get('payload',{}) if isinstance(envelope,dict) else {}
        payload=payload if isinstance(payload,dict) else {}
        return error_response(transfers.store,path=request.url.path,raw=body,
            relation=dict(peer_id=payload.get('master_id'),relationship_id=payload.get('source_relationship')),
            exc=exc,prefix='migration_transfer')


@router.post('/probe')
async def probe(request:Request):
    return await dispatch(request,transfers.probe,{'request_id','grant'},error_type=TransferError,prefix='migration_transfer')


@router.post('/copy')
async def copy(request:Request):
    return await dispatch(request,transfers.copy,{'request_id','grant'},error_type=TransferError,prefix='migration_transfer')


@router.post('/register')
async def register(request:Request):
    return await dispatch(request,transfers.register,{'request_id','grant'},error_type=TransferError,prefix='migration_transfer')


@router.post('/abort-stop')
async def abort_stop(request:Request):
    return await dispatch(request,CancelPeers(transfers).stop,{'request_id','grant'},error_type=TransferError,prefix='migration_abort')


@router.post('/abort-clean')
async def abort_clean(request:Request):
    return await dispatch(request,CancelPeers(transfers).clean,{'request_id','grant'},error_type=TransferError,prefix='migration_abort')


@router.post('/cancel-stop')
async def cancel_stop(request:Request):
    return await dispatch(request,TargetCancellation(transfers).stop,{'request_id','grant'},error_type=TransferError,prefix='migration_cancel')


@router.post('/cancel-target')
async def cancel_target(request:Request):
    return await dispatch(request,TargetCancellation(transfers).advance,{'request_id','grant','unit_id','proof'},error_type=TransferError,prefix='migration_cancel')


@router.post('/cancel-source-stop')
async def cancel_source_stop(request:Request):
    return await dispatch(request,SourceCancellation(transfers).stop,{'request_id','grant'},error_type=TransferError,prefix='migration_cancel')


@router.post('/cancel-source-retain')
async def cancel_source_retain(request:Request):
    return await dispatch(request,SourceCancellation(transfers).advance,{'request_id','grant','unit_id','authorization','target_receipt'},error_type=TransferError,prefix='migration_cancel')


@router.post('/cancel-unit-settle')
async def cancel_unit_settle(request:Request):
    return await dispatch(request,UnitSettlement(transfers).advance,{'request_id','grant','unit_id','authorization','source_receipt','target_receipt','source_settlement'},
        error_type=TransferError,prefix='migration_cancel')


@router.post('/publish-metadata')
async def publish_metadata(request:Request):
    return await dispatch(request,PublishPeers(transfers).describe,{'request_id','grant','entries'},error_type=TransferError,prefix='migration_publish')


@router.post('/cancel-source-census')
async def cancel_source_census(request:Request):
    return await dispatch(request,SourceCensus(transfers).advance,{'request_id','grant'},error_type=TransferError,prefix='migration_cancel')


@router.post('/cancel-source-release')
async def cancel_source_release(request:Request):
    return await dispatch(request,SourceRelease(transfers).advance,{'request_id','grant','agreement','source_census'},
        error_type=TransferError,prefix='migration_cancel')


@router.post('/cancel-target-release')
async def cancel_target_release(request:Request):
    return await dispatch(request,TargetRelease(transfers).advance,{'request_id','grant','source_record'},
        error_type=TransferError,prefix='migration_cancel')


@router.post('/publish-unit')
async def publish_unit(request:Request):
    return await dispatch(request,PublishPeers(transfers).advance,{'request_id','grant','unit_id'},error_type=TransferError,prefix='migration_publish')


@router.post('/verify-source')
async def verify_source(request:Request):
    return await dispatch(request,SourceVerification(transfers).advance,{'request_id','grant','unit_id'},error_type=TransferError,prefix='migration_verify')


@router.post('/commit-ack')
async def commit_ack(request:Request):
    return await dispatch(request,CommitPeers(transfers).acknowledge,{'request_id','grant','commit'},error_type=TransferError,prefix='migration_commit')


@router.post('/verify-target')
async def verify_target(request:Request):
    return await dispatch(request,TargetVerification(transfers).advance,{'request_id','grant','unit_id'},error_type=TransferError,prefix='migration_target_verify')


@router.post('/cleanup-source')
async def cleanup_source(request:Request):
    return await dispatch(request,SourceCleanup(transfers).advance,{'request_id','grant','unit_id','target_proof'},error_type=TransferError,prefix='migration_cleanup')


@router.post('/finalize-source')
async def finalize_source(request:Request):
    return await dispatch(request,FinalizePeers(transfers).source,{'request_id','grant'},error_type=TransferError,prefix='migration_finalize')


@router.post('/finalize-target')
async def finalize_target(request:Request):
    return await dispatch(request,FinalizePeers(transfers).target,{'request_id','grant','source_receipt'},error_type=TransferError,prefix='migration_finalize')
