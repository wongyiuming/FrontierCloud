"""Startup-bound native files and bounded physical retirement inspection.

This never authorizes retirement by itself. The terminal coordinator must also
own native mutation barriers and repeat the logical/history checks in SQL.
"""
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
from datetime import datetime,timezone

from cryptography.fernet import Fernet,InvalidToken
from sqlalchemy import select,text

from app.services import resource_inventory as inventory,runtime_gate
from app.services.federation import protocol as p
from app.services.node_retirement_fences import RetirementError
from app.services.node_retirement_gate import NAME,_identity


def _digest(value):return hashlib.sha256(value).hexdigest()
def _root_identity(info):return _identity(info)[:5]
def _file_identity(info):return _identity(info)[:6]


class Root:
    def __init__(self,path):
        self.path=Path(path).absolute()
        if '..' in self.path.parts:raise RetirementError('retirement_root_path_invalid')
        self.descriptor,self.exists=inventory._root(self.path)
        self.identity=_root_identity(os.fstat(self.descriptor))

    def check(self):
        descriptor,exists=inventory._root(self.path)
        try:
            if exists!=self.exists or _root_identity(os.fstat(descriptor))!=self.identity:
                raise RetirementError('retirement_root_changed')
        finally:os.close(descriptor)

    def binding(self):return dict(path=str(self.path),exists=self.exists,identity=list(self.identity))
    def close(self):
        if self.descriptor is not None:os.close(self.descriptor);self.descriptor=None


class File:
    def __init__(self,path,*,mode=0o600,maximum=None):
        self.path=Path(path).absolute();self.parent=Root(self.path.parent);self.descriptor=None;self.sha256=None
        try:
            if not self.parent.exists:raise RetirementError('retirement_control_file_missing')
            self.descriptor=os.open(self.path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=self.parent.descriptor)
            info=os.fstat(self.descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=mode
                    or info.st_uid not in {os.geteuid(),10001}):raise RetirementError('retirement_control_file_unsafe')
            self.identity=_file_identity(info);self.maximum=maximum;self.check()
        except BaseException:self.close();raise

    def read(self,maximum):
        before=os.fstat(self.descriptor)
        if before.st_size>maximum:raise RetirementError('retirement_control_file_invalid')
        value=os.pread(self.descriptor,maximum+1,0);after=os.fstat(self.descriptor)
        if (len(value)>maximum or len(value)!=before.st_size or inventory._version(before)!=inventory._version(after)
                or before.st_size!=after.st_size):raise RetirementError('retirement_control_file_changed')
        return value

    def seal(self,value):
        if self.read(len(value))!=value:raise RetirementError('retirement_control_bytes_invalid')
        self.maximum=len(value);self.sha256=_digest(value)

    def check(self):
        self.parent.check();info=os.stat(self.path.name,dir_fd=self.parent.descriptor,follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or _file_identity(info)!=self.identity
                or _file_identity(os.fstat(self.descriptor))!=self.identity):raise RetirementError('retirement_control_file_changed')
        if self.sha256 is not None and _digest(self.read(self.maximum))!=self.sha256:
            raise RetirementError('retirement_control_bytes_changed')
        return info

    def binding(self):return dict(path=str(self.path),parent=self.parent.binding(),identity=list(self.identity),sha256=self.sha256)
    def close(self):
        if self.descriptor is not None:os.close(self.descriptor);self.descriptor=None
        self.parent.close()


def _held_by_process(pin,excluded):
    # SQLite's actual engine must hold the selected inode independently of our
    # observer. A same-named ordinary file opened only by this census is not proof.
    with os.scandir('/proc/self/fd') as entries:
        for index,entry in enumerate(entries):
            if index>=1024:raise RetirementError('retirement_descriptor_inventory_limit')
            descriptor=int(entry.name)
            if descriptor in excluded:continue
            try:info=os.fstat(descriptor)
            except OSError:continue
            if stat.S_ISREG(info.st_mode) and _file_identity(info)==pin.identity:return
    raise RetirementError('retirement_selected_database_not_held')


class Authority:
    def __init__(self,runtime,store,paths,secrets,upload_leases):
        self.runtime,self.store,self.paths=runtime,store,{name:Path(value).absolute() for name,value in paths.items()}
        self.roots={};self.files={};self.node=None;self.selection=None;self.key_id=None;self.main=None;self.closed=False
        self.edge_root=None;self.edge_path=None
        self.secrets=Path(secrets).absolute();self.coordination={Path(upload_leases).absolute(),self.paths['recordings'].parent/'recording-leases'}
        try:
            if set(self.paths)!={'data','media','recordings'} or self.paths['data']!=runtime.root:
                raise RetirementError('retirement_data_root_changed')
            if any(path.parent!=runtime.root for path in self.coordination):raise RetirementError('retirement_lease_root_outside_data')
            for name,path in self.paths.items():self.roots[name]=Root(path)
        except BaseException:self.close();raise

    @classmethod
    async def capture(cls,runtime,store,*,paths=None,secrets=None,upload_leases=None):
        from app.services.federation.state import SECRET_DIR
        from app.services.upload_claims import ROOT
        authority=cls(runtime,store,inventory.roots() if paths is None else paths,SECRET_DIR if secrets is None else secrets,ROOT if upload_leases is None else upload_leases)
        try:
            async with store.database.begin() as conn:
                node=await store.lock(conn)
                if node['role']!='Follower' or node!=store.node:raise RetirementError('retirement_startup_identity_changed')
                authority.node=dict(node);authority.selection=authority._selection()
                vault=authority._pin(authority.secrets/'node-vault.key');raw=vault.read(44)
                if len(raw)!=44:raise RetirementError('retirement_vault_invalid')
                try:private=Fernet(raw).decrypt(node['private_key'].encode()).decode()
                except (InvalidToken,ValueError,UnicodeError):raise RetirementError('retirement_vault_identity_changed') from None
                if p.public_key(private)!=p.public_key(store.unseal(node['private_key'])):raise RetirementError('retirement_vault_identity_changed')
                vault.seal(raw)
                from app.services.media_crypto_schema import keys
                authority.key_id=await conn.scalar(select(keys.c.key_id).where(keys.c.singleton==1))
                premaster=authority.paths['data']/'media-keys/media-premaster.key'
                if authority.key_id is not None or premaster.exists() or premaster.is_symlink():
                    pin=authority._pin(premaster);raw=pin.read(32)
                    if len(raw)!=32 or hmac.digest(raw,b'frontiercloud:premaster-id:v1','sha256').hex()!=authority.key_id:
                        raise RetirementError('retirement_premaster_identity_changed')
                    pin.seal(raw)
                from app.services.lyrics import DEFAULT_LYRIC_CONTENT
                fallback=authority.paths['media']/'lyrics/default.lrc'
                if fallback.exists() or fallback.is_symlink():authority._pin(fallback,mode=0o644).seal(DEFAULT_LYRIC_CONTENT.encode())
                from app.services.edge_security import SNAPSHOT_PATH
                expected=authority.paths['data']/'.ip-security/active-bans.tsv'
                if SNAPSHOT_PATH.absolute()==expected and (expected.exists() or expected.is_symlink()):
                    authority.edge_root=Root(expected.parent);authority.edge_path=expected
                if store.database.dialect.name=='sqlite':
                    selected=Path(store.database.url.database).absolute()
                    if authority.paths['data'] not in selected.parents:raise RetirementError('retirement_database_outside_data')
                    authority.main=authority._pin(selected)
                await authority.verify_database(conn)
            authority.check();return authority
        except BaseException:authority.close();raise

    def _pin(self,path,**options):
        pin=File(path,**options);self.files[pin.path]=pin;return pin

    def _selection(self):
        url=self.store.database.url
        if url.get_backend_name() not in {'sqlite','mysql'}:raise RetirementError('retirement_database_kind_invalid')
        return _digest(p.canonical(dict(driver=url.drivername,host=url.host,port=url.port,database=url.database,username=url.username)))

    async def verify_database(self,conn):
        if self._selection()!=self.selection or self.store.node!=self.node or await self.store.lock(conn)!=self.node:
            raise RetirementError('retirement_selected_database_changed')
        if conn.dialect.name=='sqlite':
            selected=[row[2] for row in (await conn.exec_driver_sql('PRAGMA database_list')).all() if row[1]=='main']
            if selected!=[str(self.main.path)] or str(await conn.scalar(text('PRAGMA journal_mode'))).lower()!='wal':
                raise RetirementError('retirement_selected_database_changed')
            self.main.check();_held_by_process(self.main,self._descriptors())
        elif await conn.scalar(text('SELECT DATABASE()'))!=self.store.database.url.database:
            raise RetirementError('retirement_selected_database_changed')
        from app.services.media_crypto_schema import keys
        if await conn.scalar(select(keys.c.key_id).where(keys.c.singleton==1))!=self.key_id:
            raise RetirementError('retirement_premaster_identity_changed')

    def _descriptors(self):return {pin.descriptor for pin in self.files.values()}
    def check(self):
        if self.closed:raise RetirementError('retirement_native_authority_closed')
        self.runtime.check()
        for root in self.roots.values():root.check()
        if self.edge_root is not None:self.edge_root.check()
        for pin in self.files.values():pin.check()

    def binding(self):
        self.check()
        return dict(version=1,node_id=self.node['node_id'],selection_sha256=self.selection,
            roots={name:root.binding() for name,root in self.roots.items()},
            protected=[pin.binding() for _,pin in sorted(self.files.items(),key=lambda item:str(item[0]))],
            edge_snapshot=None if self.edge_root is None else dict(version=1,path=str(self.edge_path),parent=self.edge_root.binding()))

    async def _edge_snapshot(self,conn):
        # A single fixed runtime control projection, never a directory exemption.
        # A concurrent atomic publisher replacement invalidates this census pin.
        from app.services.edge_security import snapshot_content
        now=datetime.now(timezone.utc).replace(tzinfo=None)
        # SQLite's declared TIMESTAMP converter and the MySQL driver already
        # return datetime objects; do not add a second result conversion.
        rows=(await conn.execute(text("SELECT substr(ip_address,1,46) AS ip_address, expires_at, substr(ban_kind,1,17) AS ban_kind FROM ip_auto_ban_events "
            "WHERE status='active' AND expires_at > :now LIMIT 1025"),dict(now=now))).mappings().all()
        whitelist=(await conn.execute(text('SELECT substr(ip_address,1,46) FROM ip_permanent_whitelist LIMIT 1025'))).scalars().all()
        if len(rows)>1024 or len(whitelist)>1024:raise RetirementError('retirement_security_snapshot_limit')
        if any(row['ban_kind'] not in {'permanent','auto'} for row in rows):raise RetirementError('retirement_security_snapshot_invalid')
        try:expected=snapshot_content(rows,whitelist).encode('ascii')
        except (ValueError,TypeError,AttributeError,OverflowError):raise RetirementError('retirement_security_snapshot_invalid') from None
        if len(expected)>65536:raise RetirementError('retirement_security_snapshot_limit')
        pin=File(self.edge_path,maximum=65536)
        try:pin.seal(expected);return pin
        except BaseException:pin.close();raise

    async def census(self,conn,lease):
        self.runtime.gate.require(lease,exclusive=True);self.check();await self.verify_database(conn)
        sidecars={}
        try:
            if self.edge_path is not None:sidecars[self.edge_path]=await self._edge_snapshot(conn)
            if self.main is not None:
                for suffix in ('-wal','-shm'):
                    pin=File(str(self.main.path)+suffix);sidecars[pin.path]=pin
                    _held_by_process(pin,self._descriptors()|{value.descriptor for value in sidecars.values()})
            return Census(self,lease,sidecars)
        except BaseException:
            for pin in sidecars.values():pin.close()
            raise

    def close(self):
        for pin in self.files.values():pin.close()
        for root in self.roots.values():root.close()
        if self.edge_root is not None:self.edge_root.close()
        self.closed=True


class Census:
    def __init__(self,authority,lease,sidecars):
        self.authority,self.lease,self.sidecars=authority,lease,sidecars;self.index=0;self.walker=None;self.complete=False;self.closed=False
        self.kinds=('data','media','recordings');self.directories=0;self.files=0;self.bytes=0;self.versions={}

    def _check(self):
        if self.closed:raise RetirementError('retirement_census_closed')
        self.authority.runtime.gate.require(self.lease,exclusive=True);self.authority.check()
        for pin in self.sidecars.values():pin.check()
        for kind,version in self.versions.items():
            root=self.authority.roots[kind]
            if root.exists and inventory._version(os.fstat(root.descriptor))!=version:raise RetirementError('retirement_inventory_changed')

    def _file(self,path,entry):
        pin=self.authority.files.get(path) or self.sidecars.get(path)
        if pin is not None:info=pin.check()
        elif path in {self.authority.runtime.root/runtime_gate.LOCK_NAME,self.authority.runtime.root/NAME}:
            self.authority.runtime.check();info=os.stat(path,follow_symlinks=False)
        elif path.parent in self.authority.coordination and re.fullmatch(r'(?:[a-f0-9]{32}|[a-f0-9]{64})',path.name):
            pin=File(path,maximum=0)
            try:
                import fcntl
                if pin.read(0)!=b'':raise RetirementError('retirement_unknown_coordination_bytes')
                try:fcntl.flock(pin.descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise RetirementError('retirement_coordination_busy') from None
                info=pin.check()
            finally:pin.close()
        else:raise RetirementError('retirement_unknown_file')
        if (info.st_dev,info.st_ino,stat.S_IMODE(info.st_mode),info.st_size)!=(entry['device'],entry['inode'],entry['mode'],entry['bytes']):
            raise RetirementError('retirement_inventory_file_changed')

    def page(self):
        if self.complete:raise RetirementError('retirement_inventory_already_complete')
        self._check();kind=self.kinds[self.index];path=self.authority.paths[kind]
        if self.walker is None:self.walker=inventory.Walker(path)
        entries,finished=self.walker.page()
        counted=not any(other!=path and other in path.parents for other in [self.authority.paths[name] for name in self.kinds[:self.index]])
        for entry in entries:
            if entry['kind']=='directory':
                if counted:self.directories+=1
            elif entry['kind']=='file':
                self._file(path/entry['path'],entry)
                if counted:self.files+=1;self.bytes+=entry['bytes']
            else:raise RetirementError('retirement_unknown_resource')
        if finished:
            self.versions[kind]=self.walker.root_version;self.walker.close();self.walker=None;self.index+=1
            self.complete=self.index==len(self.kinds)
        self._check();return self.complete

    def summary(self):
        self._check()
        if not self.complete:raise RetirementError('retirement_inventory_incomplete')
        external=[pin for path,pin in self.authority.files.items() if not any(root==path or root in path.parents for root in self.authority.paths.values())]
        return dict(version=1,retained_files=self.files,retained_bytes=self.bytes,directories=self.directories,
            external_control_files=len(external),external_control_bytes=sum(pin.check().st_size for pin in external),
            roots_sha256=_digest(p.canonical(self.authority.binding())))

    def close(self):
        if self.walker is not None:self.walker.close();self.walker=None
        for pin in self.sidecars.values():pin.close()
        self.closed=True
