"""One TLS-only operation-bound route; no broad revoked node capability."""
import asyncio

from fastapi import APIRouter,HTTPException,Request
from fastapi.responses import JSONResponse,Response

from app.api.internal_nodes import require_https,control_path
from app.services.federation.state import state
from app.services import node_retirement_protocol as protocol
from app.services.node_retirement_auth import Authentication,AuthenticationError,header_shape
from app.services.node_retirement_complete import Completion
from app.services.node_retirement_preflight import Preflight
from app.services.node_retirement_fences import RetirementError
from app.services.node_retirement_gate import GateError
from app.services.node_retirement_runtime import current
from app.services.node_retirement_state import Journal
from app.services.resource_control_errors import response as error_response

router=APIRouter(include_in_schema=False)


class LeasedResponse(Response):
    def __init__(self,response,lease):
        self.lease=lease
        super().__init__(response.body,status_code=response.status_code,headers=dict(response.headers),background=response.background)

    async def __call__(self,scope,receive,send):
        try:await super().__call__(scope,receive,send)
        finally:self.lease.close()


async def body(request):
    data=bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                if len(data)+len(chunk)>protocol.LIMIT:raise HTTPException(413,'Retirement message too large')
                data.extend(chunk)
    except TimeoutError:raise HTTPException(408,'Retirement message timed out') from None
    return bytes(data)


@router.post(protocol.PATH)
async def retirement(request:Request):
    require_https(request)
    try:header_shape(request.headers)
    except AuthenticationError:raise HTTPException(401,'Retirement authentication refused') from None
    if request.url.query:raise HTTPException(401,'Retirement authentication refused')
    raw=await body(request)
    try:value=protocol.request(raw)
    except RetirementError:raise HTTPException(400,'Invalid retirement request') from None
    lease=None;relation=None
    try:
        runtime=current();runtime.check();lease=runtime.gate.lease()
        journal=Journal(state,gate=runtime.gate,authority=runtime.authority)
        relation=await Authentication(journal).check(request.headers,request.method,control_path(request),raw,value)
        try:
            if runtime.authority is None:raise RetirementError(runtime.authority_error)
            runtime.authority.check()
            if value['action']=='begin':row=await journal.begin_peer(relation['relationship_id'],value['binding'],authenticated_relation=relation)
            elif value['action']=='status':row=await journal.read(relation['relationship_id'],authenticated_relation=relation)
            else:
                lease.close();lease=None;lease=runtime.gate.lease(exclusive=True)
                if value['action']=='check':
                    observation=await Preflight(journal,runtime=runtime).peer(relation['relationship_id'],value['binding'],lease,authenticated_relation=relation)
                else:row=await Completion(journal,runtime=runtime).peer(relation['relationship_id'],value['binding'],lease,authenticated_relation=relation)
            if value['action']=='check':envelope=protocol.preflight_reply(state,value,raw,observation)
            else:
                if row['binding']!=value['binding']:raise RetirementError('retirement_operation_conflict')
                envelope=protocol.reply(state,value,raw,row)
            response=JSONResponse(envelope,headers={'Cache-Control':'no-store'})
        except (RetirementError,GateError,OSError,ValueError,TypeError,KeyError,RecursionError) as exc:
            if isinstance(exc,GateError):exc=RetirementError(exc.code)
            response=error_response(state,path=protocol.PATH,raw=raw,relation=relation,exc=exc,prefix='retirement')
        result=LeasedResponse(response,lease) if lease is not None else response
        lease=None;return result
    except AuthenticationError:raise HTTPException(401,'Retirement authentication refused') from None
    except (GateError,OSError):
        return JSONResponse({'code':'retirement_runtime_unavailable'},status_code=503,headers={'Cache-Control':'no-store'})
    finally:
        if lease is not None:lease.close()
