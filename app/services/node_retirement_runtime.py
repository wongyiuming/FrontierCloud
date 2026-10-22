"""One startup-bound native runtime and its complete-response request leases."""
import os
from pathlib import Path
import stat
from contextlib import contextmanager

from app.services import runtime_gate
from app.services.node_retirement_gate import Gate,GateError,NAME,_identity

_current=None


class Runtime:
    def __init__(self,root,native_lease):
        self.root=Path(root).absolute();self.native_lease=native_lease
        self.gate=None;self.process=os.getpid()
        self.authority=None;self.authority_error='retirement_native_observation_unavailable'
        if (type(native_lease) is not runtime_gate.Lease or native_lease.root!=self.root
                or native_lease.descriptor is None or native_lease.process!=self.process):
            raise GateError('retirement_runtime_lease_invalid')
        self.native_identity=_identity(os.fstat(native_lease.descriptor))
        try:self.gate=Gate(self.root);self.check()
        except BaseException:self.close();raise

    def check(self):
        if (self.gate is None or self.process!=os.getpid() or self.native_lease.descriptor is None
                or self.native_lease.process!=os.getpid()):raise GateError('retirement_runtime_unavailable')
        self.gate.check()
        info=os.stat(runtime_gate.LOCK_NAME,dir_fd=self.gate.root_descriptor,follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or _identity(info)!=self.native_identity
                or _identity(os.fstat(self.native_lease.descriptor))!=self.native_identity
                or self.native_identity[4:]!=(0o600,1,0)):
            raise GateError('retirement_runtime_lease_changed')

    def close(self):
        if self.authority is not None:self.authority.close();self.authority=None
        if self.gate is not None:self.gate.close();self.gate=None


def start(root,native_lease):
    global _current
    if _current is not None:raise GateError('retirement_runtime_already_started')
    _current=Runtime(root,native_lease)
    return _current


def current():
    if _current is None:raise GateError('retirement_runtime_unavailable')
    _current.check();return _current


def stop(runtime):
    global _current
    if _current is not runtime:raise GateError('retirement_runtime_owner_changed')
    _current=None;runtime.close()


@contextmanager
def work(root):
    """Queued/local file work joins the same inode, even outside an HTTP task."""
    root=Path(root).absolute();owned=None
    try:
        if _current is not None and _current.root==root:
            selected=current().gate
        elif (root/NAME).exists() or (root/NAME).is_symlink():
            owned=Gate(root);selected=owned
        else:
            yield;return
        with selected.lease():yield
    finally:
        if owned is not None:owned.close()
