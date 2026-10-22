"""Session/CSRF-protected migration controls; requests carry metadata only."""
import json
import re

from fastapi import APIRouter,Depends,HTTPException,Query,Request
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel,ConfigDict,Field,ValidationError

from app.api.v1.admin import require_session
from app.api.v1.admin_transport import require_secure_admin_transport
from app.services.federation import protocol as p
from app.services.resource_migration_admin import Admin
from app.services.resource_migration_runtime import runtime
from app.services.resource_migration_scheduler import error_code

router=APIRouter(prefix='/migrations',dependencies=[Depends(require_secure_admin_transport)])


class Model(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)


class Preview(Model):
    request_id:str=Field(pattern='^[a-f0-9]{32}$')
    source_id:str=Field(pattern='^[a-f0-9]{32}$')
    target_id:str=Field(pattern='^[a-f0-9]{32}$')


class Start(Model):
    approved_sha256:str=Field(pattern='^[a-f0-9]{64}$')
    confirm_source_id:str=Field(pattern='^[a-f0-9]{32}$')
    confirm_target_id:str=Field(pattern='^[a-f0-9]{32}$')


class Control(Model):
    revision:int=Field(ge=1)
    action:str=Field(pattern='^(pause|resume|cancel|retry)$')


async def body(request,model):
    raw=bytearray()
    async for chunk in request.stream():
        if len(raw)+len(chunk)>4096:raise HTTPException(413,'迁移参数过大')
        raw.extend(chunk)
    def unique(pairs):
        result={}
        for name,value in pairs:
            if name in result:raise ValueError('Duplicate JSON field')
            result[name]=value
        return result
    try:return model.model_validate(json.loads(raw,object_pairs_hook=unique))
    except (ValueError,TypeError,ValidationError):raise HTTPException(400,'迁移参数无效') from None


async def call(work):
    try:result=await work
    except (p.ProtocolError,httpx.HTTPError,OSError,TimeoutError) as exc:
        code=str(exc) if isinstance(exc,p.ProtocolError) and re.fullmatch(r'migration_[a-z0-9_]{1,96}',str(exc)) else error_code(exc)
        if code=='migration_execution_internal_error':code='migration_admin_state_conflict'
        return JSONResponse(dict(code=code,detail='迁移状态冲突，请刷新核查'),status_code=409,headers={'Cache-Control':'no-store'})
    return JSONResponse(result,headers={'Cache-Control':'no-store'})


@router.get('/members')
async def members(after:str=Query('',max_length=32),actor:str=Depends(require_session)):
    return await call(Admin(runtime).members(after))


@router.get('/tasks')
async def history(after:str=Query('',max_length=32),actor:str=Depends(require_session)):
    return await call(Admin(runtime).history(after))


@router.post('/plans')
async def preview(request:Request,actor:str=Depends(require_session)):
    value=await body(request,Preview)
    return await call(Admin(runtime).preview(value.request_id,value.source_id,value.target_id,actor))


@router.get('/plans/{plan_id}')
async def plan(plan_id:str,after:str=Query('',max_length=64),actor:str=Depends(require_session)):
    return await call(Admin(runtime).plan(plan_id,after))


@router.post('/plans/{plan_id}/start')
async def start(request:Request,plan_id:str,actor:str=Depends(require_session)):
    value=await body(request,Start)
    return await call(Admin(runtime).start(plan_id,value.approved_sha256,value.confirm_source_id,value.confirm_target_id,actor))


@router.get('/tasks/{task_id}')
async def task(task_id:str,after:str=Query('',max_length=64),actor:str=Depends(require_session)):
    return await call(Admin(runtime).task(task_id,after))


@router.post('/tasks/{task_id}')
async def control(request:Request,task_id:str,actor:str=Depends(require_session)):
    value=await body(request,Control)
    return await call(Admin(runtime).control(task_id,value.revision,value.action,actor))
