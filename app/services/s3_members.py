"""Master-owned S3 configuration; credentials stay in the existing node vault.

This registry does not install an object store, import a bucket or authorize an
upload. A separate verified probe supplies its revision-bound result.
"""
from dataclasses import asdict
import hashlib
import json
import re
import time
import uuid

from cryptography.fernet import InvalidToken

from sqlalchemy import delete, func, insert, select, update

from app.services import s3_schema as s
from app.services.s3_signing import Credentials, Location, S3Error, Signer

MAX_MEMBERS = 64
TERMINAL_UPLOADS = ('aborted','published')


class Conflict(S3Error):
    pass


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-f0-9]{32}',value):
        raise S3Error('s3_member_identity_invalid')
    return value


def revision(value):
    if type(value) is not int or value < 1:
        raise S3Error('s3_revision_invalid')
    return value


def namespace(location):
    # AWS bucket names are global inside a partition. Endpoint style, region
    # spelling and path-style access must not admit a second owner of the prefix.
    origin = location.endpoint
    if re.fullmatch(r'https://s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com(?:\.cn)?',origin):
        origin = 'aws-cn' if origin.endswith('.cn') else ('aws-us-gov' if location.region.startswith('us-gov-') else 'aws')
    return origin,location.bucket,location.prefix


def _dump(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True)


def _configuration(name,location,credentials,quota):
    if (not isinstance(name,str) or not 1 <= len(name.strip()) <= 80 or
        any(ord(char) < 32 or ord(char) == 127 for char in name) or
        not isinstance(location,Location) or not isinstance(credentials,Credentials) or
        type(quota) is not int or not 1 <= quota <= 2**53-1):
        raise S3Error('s3_configuration_invalid')
    return name.strip(),_dump(asdict(location)),_dump(asdict(credentials))


class Members:
    def __init__(self,store,*,clock=time.time):
        self.store,self.clock = store,clock

    async def _master(self,conn):
        node = await self.store.lock(conn)
        if node['role'] != 'Master':
            raise S3Error('s3_master_required')
        return node

    async def _row(self,conn,member_id,expected=None):
        node = await self._master(conn)
        row = (await conn.execute(select(s.members).where(s.members.c.member_id == identifier(member_id))
                                  .with_for_update())).mappings().first()
        if row is None or row['master_id'] != node['node_id']:
            raise S3Error('s3_member_missing')
        if expected is not None and row['revision'] != revision(expected):
            raise Conflict('s3_revision_conflict')
        return dict(row)

    async def _namespace_free(self,conn,location,exclude=''):
        wanted = namespace(location)
        rows = (await conn.execute(select(s.members.c.member_id,s.members.c.location_json).with_for_update())).all()
        if len(rows) > MAX_MEMBERS:
            raise S3Error('s3_member_limit')
        for member_id,raw in rows:
            if member_id == exclude:
                continue
            other = namespace(Location(**json.loads(raw)))
            if other[:2] == wanted[:2] and (not other[2] or not wanted[2] or other[2] == wanted[2] or
                other[2].startswith(wanted[2]+'/') or wanted[2].startswith(other[2]+'/')):
                raise Conflict('s3_namespace_conflict')

    async def _busy(self,conn,member_id,*,objects=False):
        if await conn.scalar(select(s.uploads.c.session_id).where(s.uploads.c.member_id == member_id,
            s.uploads.c.state.not_in(TERMINAL_UPLOADS)).limit(1).with_for_update()):
            raise Conflict('s3_upload_unreconciled')
        if objects and await conn.scalar(select(s.objects.c.media_id).where(s.objects.c.member_id == member_id,
            s.objects.c.state != 'deleted').limit(1).with_for_update()):
            raise Conflict('s3_objects_owned')

    async def add(self,name,location,credentials,quota,actor):
        name,raw,secret = _configuration(name,location,credentials,quota)
        now = int(self.clock())
        member_id = uuid.uuid4().hex
        async with self.store.database.begin() as conn:
            node = await self._master(conn)
            from app.services.federation import schema as fs
            if (member_id == node['node_id'] or
                await conn.scalar(select(fs.storage_members.c.member_id).where(fs.storage_members.c.member_id == member_id)) or
                await conn.scalar(select(fs.relationships.c.relationship_id).where(fs.relationships.c.peer_id == member_id).limit(1))):
                raise Conflict('s3_member_identity_conflict')
            count = await conn.scalar(select(func.count()).select_from(s.members))
            if count >= MAX_MEMBERS:
                raise S3Error('s3_member_limit')
            await self._namespace_free(conn,location)
            await conn.execute(insert(s.members).values(member_id=member_id,master_id=node['node_id'],name=name,
                location_json=raw,credentials_sealed=self.store.seal(secret),
                namespace_sha256=hashlib.sha256(_dump(namespace(location)).encode()).hexdigest(),
                quota_bytes=quota,created_at=now,updated_at=now))
            await self.store.log(conn,'s3_member_added',actor,member_id=member_id)
        self.store._s3_configured = True
        return member_id

    async def configure(self,member_id,expected,*,name,quota,enabled,actor,location=None,credentials=None):
        if type(enabled) is not bool:
            raise S3Error('s3_configuration_invalid')
        async with self.store.database.begin() as conn:
            row = await self._row(conn,member_id,expected)
            previous_location = Location(**json.loads(row['location_json']))
            previous_credentials = Credentials(**json.loads(self.store.unseal(row['credentials_sealed'])))
            location = previous_location if location is None else location
            credentials = previous_credentials if credentials is None else credentials
            name,raw,secret = _configuration(name,location,credentials,quota)
            location_changed = location != previous_location
            secret_changed = credentials != previous_credentials
            if location_changed or secret_changed:
                await self._busy(conn,member_id,objects=location_changed)
                if row['reserved_bytes'] or (location_changed and row['used_bytes']):
                    raise Conflict('s3_capacity_unreconciled')
                await self._namespace_free(conn,location,member_id)
            if quota < row['used_bytes']+row['reserved_bytes']:
                raise Conflict('s3_quota_below_committed')
            if enabled and (row['probe_state'] != 'ready' or location_changed or secret_changed):
                raise Conflict('s3_probe_required')
            values = dict(name=name,quota_bytes=quota,enabled=int(enabled),revision=row['revision']+1,updated_at=int(self.clock()))
            if location_changed or secret_changed:
                values.update(location_json=raw,credentials_sealed=self.store.seal(secret),
                    namespace_sha256=hashlib.sha256(_dump(namespace(location)).encode()).hexdigest(),
                    probe_state='unchecked',error_code='',probed_at=0)
            await conn.execute(update(s.members).where(s.members.c.member_id == member_id).values(**values))
            await self.store.log(conn,'s3_member_configured',actor,member_id=member_id,revision=values['revision'],enabled=enabled)
            return values['revision']

    async def signer(self,member_id):
        # Internal service only: callers must perform their own Admin/media
        # authorization before choosing this member or exposing bounded grants.
        async with self.store.database.begin() as conn:
            row = await self._row(conn,member_id)
            signer = Signer(Location(**json.loads(row['location_json'])),
                Credentials(**json.loads(self.store.unseal(row['credentials_sealed']))),clock=self.clock)
            return signer,row['revision']

    async def probed(self,member_id,expected,*,error_code='',actor='system'):
        from app.services.s3_control import ERROR_CODES
        allowed = {'s3_'+value for value in ERROR_CODES.values()} | {
            's3_tls_error','s3_timeout','s3_connection_failed','s3_response_invalid','s3_response_too_large',
            's3_response_identity','s3_response_encoding','s3_not_found','s3_probe_cleanup_pending'}
        if error_code and error_code not in allowed:
            raise S3Error('s3_error_code_invalid')
        async with self.store.database.begin() as conn:
            row = await self._row(conn,member_id,expected)
            now = int(self.clock())
            await conn.execute(update(s.members).where(s.members.c.member_id == member_id).values(
                probe_state='error' if error_code else 'ready',error_code=error_code,probed_at=now,
                enabled=0 if error_code else row['enabled'],revision=row['revision']+1,updated_at=now))
            await self.store.log(conn,'s3_member_probed',actor,member_id=member_id,error_code=error_code)
            return row['revision']+1

    async def remove(self,member_id,expected,actor):
        async with self.store.database.begin() as conn:
            row = await self._row(conn,member_id,expected)
            if row['enabled']:
                raise Conflict('s3_disable_before_removal')
            await self._busy(conn,member_id,objects=True)
            if row['used_bytes'] or row['reserved_bytes']:
                raise Conflict('s3_capacity_unreconciled')
            await conn.execute(delete(s.members).where(s.members.c.member_id == member_id))
            await self.store.log(conn,'s3_member_removed',actor,member_id=member_id)

    async def recovery_credentials(self,member_id,expected,credentials,actor):
        # Revoked or expired keys must not permanently prevent controlled abort.
        # This path never changes namespace and cannot authorize more uploads.
        async with self.store.database.begin() as conn:
            row = await self._row(conn,member_id,expected)
            location = Location(**json.loads(row['location_json']))
            _,_,secret = _configuration(row['name'],location,credentials,row['quota_bytes'])
            pending = (await conn.execute(select(s.uploads.c.state,s.uploads.c.grant_until).where(
                s.uploads.c.member_id == member_id,s.uploads.c.state.not_in(TERMINAL_UPLOADS)).with_for_update())).all()
            now = int(self.clock())
            if row['enabled'] or not pending or any(state != 'aborting' or deadline > now for state,deadline in pending):
                raise Conflict('s3_recovery_credentials_not_safe')
            Signer(location,credentials,clock=self.clock)._time()
            await conn.execute(update(s.members).where(s.members.c.member_id == member_id).values(
                credentials_sealed=self.store.seal(secret),revision=row['revision']+1,probe_state='unchecked',
                probed_at=0,error_code='',updated_at=now))
            await self.store.log(conn,'s3_recovery_credentials_replaced',actor,member_id=member_id)
            return row['revision']+1

    async def list(self):
        async with self.store.database.begin() as conn:
            node = await self._master(conn)
            rows = (await conn.execute(select(s.members).where(s.members.c.master_id == node['node_id'])
                                      .order_by(s.members.c.created_at,s.members.c.member_id).limit(MAX_MEMBERS))).mappings()
            result=[]
            for row in rows:
                credential_state='valid'
                try:
                    signer=Signer(Location(**json.loads(row['location_json'])),
                        Credentials(**json.loads(self.store.unseal(row['credentials_sealed']))),clock=self.clock)
                    signer._time()
                except (S3Error,InvalidToken,ValueError,TypeError) as error:
                    credential_state='expired' if getattr(error,'code','')=='s3_credentials_expired' else 'unavailable'
                result.append(dict(member_id=row['member_id'],name=row['name'],kind='S3',enabled=bool(row['enabled']),
                    location=json.loads(row['location_json']),quota_bytes=row['quota_bytes'],used_bytes=row['used_bytes'],
                    reserved_bytes=row['reserved_bytes'],physical_free_bytes=None,traffic_bytes_per_second=None,
                    credential_state=credential_state,placement_ready=bool(row['enabled'] and row['probe_state']=='ready' and credential_state=='valid'),
                    revision=row['revision'],probe_state=row['probe_state'],error_code=row['error_code'],probed_at=row['probed_at']))
            return result
