"""Kernel-owned upload leases survive refresh and release on process death.

An age threshold selects candidates; only an exclusive lease permits cleanup.
Lock files stay in place so two processes can never lock different inodes for
the same upload. They contain no media, credentials or keys.
"""
from __future__ import annotations

import os
import re
import stat
import asyncio
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

_ID = re.compile(r'^(?:[a-f0-9]{32}|[a-f0-9]{64})$')
ROOT = Path(__file__).resolve().parents[2] / 'data' / 'upload-leases'
_HELD = ContextVar('frontiercloud_upload_claims', default=())


@dataclass
class _HeldLease:
    identifier: str
    active: bool = True


class UploadBusy(RuntimeError):
    pass


def is_held(identifier: str) -> bool:
    return any(lease.identifier == identifier and lease.active for lease in _HELD.get())


async def chunks(request, *, timeout=None):
    """Raw PUT bodies have the same inactivity deadline as multipart uploads."""
    from app.core.config import settings
    from fastapi import HTTPException
    iterator = request.stream().__aiter__()
    inactivity = settings.ADMIN_UPLOAD_INACTIVITY_TIMEOUT if timeout is None else timeout
    try:
        while True:
            try:
                value = await asyncio.wait_for(anext(iterator), inactivity)
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                raise HTTPException(408, 'Upload connection exceeded its inactivity deadline') from exc
            if value:
                yield value
    finally:
        close = getattr(iterator, 'aclose', None)
        if close is not None:
            await close()


async def digest_file(path):
    """Cooperative integrity inspection; cancellation leaves no hashing thread."""
    size, digest = 0, hashlib.sha256()
    with path.open('rb') as stream:
        while value := stream.read(1024 * 1024):
            size += len(value)
            digest.update(value)
            await asyncio.sleep(0)
    checksum = digest.hexdigest()
    return {'size_bytes': size, 'sha256': checksum, 'etag': f'"{checksum}"'}


@contextmanager
def claim(identifier: str, *, root=None):
    from app.services.node_retirement_runtime import work
    from app.services.node_retirement_gate import GateError
    root=Path(root or ROOT)
    try:
        with work(root.parent),_claim(identifier,root=root):yield
    except GateError as exc:
        if exc.code=='retirement_requests_busy':raise UploadBusy('Node is completing retirement') from exc
        raise RuntimeError('Native retirement gate is unavailable') from exc


@contextmanager
def _claim(identifier: str, *, root):
    import fcntl
    if not _ID.fullmatch(identifier):
        raise ValueError('Invalid upload lease identity')
    root = Path(root or ROOT)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError('Upload lease directory is invalid')
    descriptor = os.open(root / identifier, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise RuntimeError('Upload lease ownership is invalid')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UploadBusy('Upload still has a live writer') from None
        lease = _HeldLease(identifier)
        token = _HELD.set((*_HELD.get(), lease))
        try:
            yield
        finally:
            lease.active = False
            _HELD.reset(token)
    finally:
        os.close(descriptor)
