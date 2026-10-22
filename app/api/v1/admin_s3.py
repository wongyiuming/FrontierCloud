"""Admin-only S3 metadata control. No route receives or proxies media bytes."""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from app.api.v1 import admin, admin_cluster_integrity as cluster
from app.services import media_crypto, s3_schema as s
from app.services.federation.state import state
from app.services.s3_control import Control, ControlError
from app.services.s3_members import Members, Conflict
from app.services.s3_signing import Credentials, Location, S3Error
from app.services.s3_uploads import Uploads
from app.services.upload_claims import UploadBusy


async def guarded(response: Response, actor: str = Depends(admin.require_session)):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    if state.node.get('role') != 'Master':
        raise HTTPException(409,'S3 需要主节点')
    return actor


router = APIRouter(prefix='/s3',dependencies=[Depends(guarded)])


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)


class Add(Model):
    name: str = Field(min_length=1,max_length=80)
    location: dict
    credentials: dict = Field(repr=False)
    quota_bytes: int = Field(gt=0,le=2**53-1)


class Revision(Model):
    revision: int = Field(ge=1)


class Configure(Revision):
    name: str = Field(min_length=1,max_length=80)
    quota_bytes: int = Field(gt=0,le=2**53-1)
    enabled: bool
    location: dict | None = None
    credentials: dict | None = Field(None,repr=False)


class RecoveryCredentials(Revision):
    credentials: dict = Field(repr=False)


class Part(Model):
    checksum_sha256: str = Field(min_length=44,max_length=44)


class Reservation(Model):
    target_dir: str = Field(max_length=1024)
    relative_path: str | None = Field(None,max_length=1024)
    filename: str = Field(min_length=1,max_length=255)
    size_bytes: int = Field(gt=0,le=10*1024**3)
    storage_mode: str = Field(pattern='^(encrypted|plain)$')
    encryption: dict | None = None
    preparation_token: str = Field('',max_length=4096,repr=False)


async def body(request,model):
    # Validate here so FastAPI's validation output cannot echo credentials or
    # preparation tokens. Bound the decoded control request as well as fields.
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw)+len(chunk) > 32*1024:
            raise HTTPException(413,'配置内容过大')
        raw.extend(chunk)
    try:
        return model.model_validate(json.loads(raw))
    except (ValueError,TypeError,ValidationError):
        raise HTTPException(400,'S3 参数无效') from None


async def call(work):
    try:
        return await work
    except UploadBusy:
        raise HTTPException(409,dict(code='s3_upload_busy',message='任务正在处理，请稍后刷新')) from None
    except S3Error as error:
        status = 409 if isinstance(error,Conflict) else 400
        if isinstance(error,ControlError):
            status = 503
        message = {
            's3_access_denied':'S3 权限不足',
            's3_credentials_invalid':'S3 凭据无效',
            's3_credentials_expired':'S3 凭据已过期',
            's3_tls_error':'S3 证书验证失败',
            's3_timeout':'S3 请求超时，事务已保留',
            's3_connection_failed':'S3 连接失败',
            's3_bucket_missing':'存储桶不存在',
            's3_region_mismatch':'存储桶区域不匹配',
            's3_probe_required':'请先测试连接',
            's3_upload_unreconciled':'尚有上传待回收',
            's3_objects_owned':'请先清空所属媒体',
            's3_path_exists':'路径已存在或正在上传',
            's3_parent_unavailable':'原目录所属存储暂不可写',
            's3_capacity_unavailable':'没有可用 S3 配额',
            's3_revision_conflict':'配置已变化，请刷新',
            's3_recovery_credentials_not_safe':'先停用节点、取消上传并等待旧授权失效',
            's3_disable_before_removal':'请先停用节点',
            's3_capacity_unreconciled':'仍有容量待核销',
            's3_namespace_conflict':'存储目录已由其他 S3 节点管理',
        }.get(error.code,'S3 操作未完成')
        raise HTTPException(status,dict(code=error.code,message=message)) from None


def location(value):
    try:
        return Location(**value)
    except (TypeError,ValueError):
        raise S3Error('s3_location_invalid') from None


def credentials(value):
    try:
        return Credentials(**value)
    except (TypeError,ValueError):
        raise S3Error('s3_credentials_invalid') from None


@router.get('/members')
async def members():
    return dict(members=await call(Members(state).list()))


@router.post('/members')
async def add(request: Request,actor: str = Depends(guarded)):
    value = await body(request,Add)
    async def execute():
        member_id = await Members(state).add(value.name,location(value.location),credentials(value.credentials),value.quota_bytes,actor)
        return dict(member_id=member_id,revision=1)
    return await call(execute())


@router.post('/members/{member_id}/configure')
async def configure(member_id: str,request: Request,actor: str = Depends(guarded)):
    value = await body(request,Configure)
    async def execute():
        revision = await Members(state).configure(member_id,value.revision,name=value.name,quota=value.quota_bytes,
            enabled=value.enabled,location=location(value.location) if value.location is not None else None,
            credentials=credentials(value.credentials) if value.credentials is not None else None,actor=actor)
        return dict(revision=revision)
    return await call(execute())


@router.post('/members/{member_id}/probe')
async def probe(member_id: str,request: Request,actor: str = Depends(guarded)):
    value = await body(request,Revision)
    async def execute():
        service = Members(state)
        signer,revision = await service.signer(member_id)
        if value.revision != revision:
            raise Conflict('s3_revision_conflict')
        control = Control(signer)
        try:
            async with asyncio.timeout(45):
                await control.head_bucket()
        except (ControlError,TimeoutError) as failure:
            failure = ControlError('timeout') if isinstance(failure,TimeoutError) else failure
            # Preserve the configuration-revision result even on connection
            # failure, without reflecting transport strings or signed URLs.
            await service.probed(member_id,revision,error_code=failure.code,actor=actor)
            raise failure
        finally:
            await control.close()
        revision = await service.probed(member_id,revision,actor=actor)
        return dict(revision=revision,state='ready',scope='bucket_connection')
    return await call(execute())


@router.post('/members/{member_id}/recovery-credentials')
async def recovery_credentials(member_id: str,request: Request,actor: str = Depends(guarded)):
    value = await body(request,RecoveryCredentials)
    async def execute():
        return dict(revision=await Members(state).recovery_credentials(member_id,value.revision,credentials(value.credentials),actor))
    return await call(execute())


@router.delete('/members/{member_id}')
async def remove(member_id: str,request: Request,actor: str = Depends(guarded)):
    value = await body(request,Revision)
    await call(Members(state).remove(member_id,value.revision,actor))
    return dict(state='removed')


@router.get('/uploads')
async def pending_uploads():
    async def execute():
        async with state.database.begin() as conn:
            await Members(state)._master(conn)
            rows = (await conn.execute(select(s.uploads.c.session_id.label('upload_id'),s.uploads.c.media_path.label('path'),
                s.uploads.c.state,s.uploads.c.expected_bytes,s.uploads.c.expires_at,s.uploads.c.error_code)
                .where(s.uploads.c.state.not_in(('published','aborted'))).order_by(s.uploads.c.updated_at,s.uploads.c.session_id).limit(51))).mappings()
            rows=[dict(row) for row in rows]
            visible=rows[:50]
            # Historical part rows can be large. Sum only this bounded page,
            # using the session/part primary key, never all completed uploads.
            confirmed=dict((await conn.execute(select(s.parts.c.session_id,func.sum(s.parts.c.size_bytes)).where(
                s.parts.c.session_id.in_([row['upload_id'] for row in visible]),s.parts.c.etag != '')
                .group_by(s.parts.c.session_id))).all()) if visible else {}
            return dict(uploads=[{**row,'confirmed_bytes':int(confirmed.get(row['upload_id'],0))} for row in visible],truncated=len(rows)>50)
    return await call(execute())


@router.post('/uploads')
async def reserve(request: Request,actor: str = Depends(guarded)):
    value = await body(request,Reservation)
    async def execute():
        path = await cluster._upload_logical_path(cluster.ClusterUploadReservation(**value.model_dump()))
        try:
            metadata = media_crypto.upload_encryption(value.storage_mode,value.encryption,value.preparation_token,'admin:'+actor)
        except media_crypto.MetadataError:
            raise HTTPException(400,'加密上传授权无效') from None
        except media_crypto.SessionError:
            raise HTTPException(409,'加密上传授权已失效，请重新选择文件') from None
        except media_crypto.PremasterError:
            raise HTTPException(503,'媒体密钥状态异常，请恢复正确的密钥备份') from None
        if metadata is not None and metadata.ciphertext_size != value.size_bytes:
            raise S3Error('s3_upload_encryption_invalid')
        size = metadata.plaintext_size if metadata is not None else value.size_bytes
        if size > admin.settings.ADMIN_MAX_UPLOAD_FILE_SIZE:
            raise HTTPException(413,'文件超过单文件上传限制')
        return {**await Uploads(state).reserve(path,value.size_bytes,metadata,actor=actor),'transport':'S3','site_type':'s3'}
    return await call(execute())


@router.get('/uploads/{upload_id}')
async def status(upload_id: str):
    return await call(Uploads(state).status(upload_id))


@router.post('/uploads/{upload_id}/{action}')
async def upload_action(upload_id: str,action: str,actor: str = Depends(guarded)):
    service = Uploads(state)
    if action == 'initialize':
        return await call(service.initialize(upload_id))
    if action == 'touch':
        return await call(service.touch(upload_id))
    if action == 'complete':
        return await call(service.complete(upload_id,actor=actor))
    if action == 'reconcile':
        return await call(service.reconcile(upload_id,actor=actor))
    raise HTTPException(404,'操作不存在')


@router.delete('/uploads/{upload_id}')
async def cancel(upload_id: str,actor: str = Depends(guarded)):
    return await call(Uploads(state).cancel(upload_id,actor=actor))


@router.post('/uploads/{upload_id}/parts/{number}')
async def part(upload_id: str,number: int,request: Request):
    value = await body(request,Part)
    grant = await call(Uploads(state).part_grant(upload_id,number,value.checksum_sha256))
    return dict(url=grant.url,headers=grant.headers,expires_at=grant.expires_at)


@router.post('/uploads/{upload_id}/parts/{number}/confirm')
async def confirm(upload_id: str,number: int):
    return await call(Uploads(state).acknowledge(upload_id,number))
