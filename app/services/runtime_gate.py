"""One business writer or offline operator per persistent DATA_ROOT inode."""
from __future__ import annotations

import os
from pathlib import Path
import stat

DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
LOCK_NAME = ".frontiercloud-runtime.lock"


class RuntimeBusy(RuntimeError):
    pass


class Lease:
    def __init__(self, descriptor):
        self.descriptor = descriptor

    def close(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        self.close()


def acquire(root=None, *, create=True) -> Lease:
    import fcntl
    root = Path(root or DATA_ROOT).absolute()
    if ".." in root.parts:
        raise RuntimeError("Runtime DATA_ROOT cannot traverse parent paths")
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current /= part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("Runtime DATA_ROOT components must be real directories")
    path = root / LOCK_NAME
    created = False
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        created = True
    except FileExistsError:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        if created and os.geteuid() == 0:
            # Preserve the production Web UID after an operator's first lease.
            os.fchown(descriptor, 10001, 10001)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid not in ({0, 10001} if os.geteuid() == 0 else {os.geteuid()})):
            raise RuntimeError("Runtime lease inode has unsafe type, owner or permissions")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeBusy("Another Web process or offline operator owns this DATA_ROOT") from None
        return Lease(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
