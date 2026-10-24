"""Pinned nonblocking OS leases join admitted native work before retirement."""
from __future__ import annotations

import os
from pathlib import Path
import stat

NAME='.frontiercloud-retirement-requests.lock'


class GateError(RuntimeError):
    def __init__(self,code):self.code=code;super().__init__(code)


def _identity(info):return (info.st_dev,info.st_ino,info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode),info.st_nlink,info.st_size)


class Lease:
    def __init__(self,gate,descriptor,exclusive,*,_issuer=None):
        if _issuer is not gate._issuer:raise GateError('retirement_lease_invalid')
        self.gate,self.descriptor,self.exclusive=gate,descriptor,exclusive
        self.process=os.getpid()
        gate._leases[self]=(descriptor,exclusive,self.process)

    def check(self,*,exclusive=False):
        self.gate.require(self,exclusive=exclusive)

    def close(self):
        if self.descriptor is not None:
            self.gate._leases.pop(self,None)
            os.close(self.descriptor);self.descriptor=None

    def __enter__(self):self.check();return self
    def __exit__(self,*_error):self.close()


class Gate:
    def __init__(self,root):
        from app.services.resource_inventory import _root
        self.root=Path(root).absolute()
        if '..' in self.root.parts or self.root==Path(self.root.anchor):raise GateError('retirement_root_unsafe')
        self._issuer=object();self._leases={}
        self.root_descriptor,self.exists=_root(self.root)
        self.descriptor=None;self.process=os.getpid()
        if not self.exists:
            os.close(self.root_descriptor);self.root_descriptor=None
            raise GateError('retirement_root_missing')
        try:
            self.root_identity=_identity(os.fstat(self.root_descriptor))[:4]
            try:
                self.descriptor=os.open(NAME,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=self.root_descriptor)
                os.fsync(self.root_descriptor)
            except FileExistsError:self.descriptor=os.open(NAME,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=self.root_descriptor)
            info=os.fstat(self.descriptor)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600 or info.st_uid!=os.geteuid()
                    or info.st_nlink!=1 or info.st_size!=0):raise GateError('retirement_gate_unsafe')
            self.identity=_identity(info);self.check()
        except BaseException:self.close();raise

    def check(self):
        if self.descriptor is None or os.getpid()!=self.process:raise GateError('retirement_gate_closed')
        from app.services.resource_inventory import _root
        descriptor,exists=_root(self.root)
        try:
            if not exists or _identity(os.fstat(descriptor))[:4]!=self.root_identity:raise GateError('retirement_root_changed')
            info=os.stat(NAME,dir_fd=descriptor,follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or _identity(info)!=self.identity
                    or _identity(os.fstat(self.descriptor))!=self.identity):raise GateError('retirement_gate_changed')
        except FileNotFoundError:raise GateError('retirement_gate_changed') from None
        finally:os.close(descriptor)

    def lease(self,*,exclusive=False):
        import fcntl
        self.check()
        descriptor=os.open(NAME,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=self.root_descriptor)
        try:
            if _identity(os.fstat(descriptor))!=self.identity:raise GateError('retirement_gate_changed')
            try:fcntl.flock(descriptor,(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)|fcntl.LOCK_NB)
            except BlockingIOError:raise GateError('retirement_requests_busy') from None
            self.check();return Lease(self,descriptor,exclusive,_issuer=self._issuer)
        except BaseException:os.close(descriptor);raise

    def require(self,lease,*,exclusive=False):
        record=self._leases.get(lease) if type(lease) is Lease else None
        if (record is None or lease.gate is not self or record!=(lease.descriptor,lease.exclusive,os.getpid())
                or lease.process!=os.getpid() or exclusive and not record[1]):raise GateError('retirement_lease_invalid')
        self.check()
        if _identity(os.fstat(lease.descriptor))!=self.identity:raise GateError('retirement_lease_changed')

    def close(self):
        for lease in list(getattr(self,'_leases',{})):lease.close()
        for name in ('descriptor','root_descriptor'):
            descriptor=getattr(self,name,None)
            if descriptor is not None:os.close(descriptor);setattr(self,name,None)
