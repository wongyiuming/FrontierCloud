"""Offline one-time adoption of the former external pair at a fixed import mount.

This is a deployment migration command, never a user-selectable runtime path.
The old stack must be stopped and its rollback deployment/database retained.
Only a public-CA verified matching pair can enter the managed volume.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

from . import assets
from .contract import hostname

IMPORT = Path("/run/frontiercloud-certificate-import")


def adopt(*, host, imported=IMPORT, private=assets.PRIVATE, published=assets.PUBLISHED, roots=None):
    certificate = assets.bounded(imported / "fullchain.pem", 65536)
    key = assets.bounded(imported / "privkey.pem", 16384)
    leaf = assets.validate(host, certificate, key, roots=roots)
    descriptor = os.open(private / "controller.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            _generation, current_cert, current_key = assets.material(published)
        except FileNotFoundError:
            current_cert = None
        if current_cert is not None:
            current_leaf = assets.validate(host, current_cert, current_key, roots=roots)
            if assets.fingerprint(current_leaf) != assets.fingerprint(leaf):
                raise ValueError("managed material already exists; migration cannot overwrite another valid identity")
            return assets.fingerprint(current_leaf)
        assets.publish(host, certificate, key, directory=published, roots=roots)
        return assets.fingerprint(leaf)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    # Offline import reads only domain/enablement. Old .env path fields are never
    # used to choose input files and do not prevent this one-time migration.
    if os.environ.get("TLS_ENABLED", "false").lower() not in ("true", "1", "yes", "on"):
        raise SystemExit("certificate migration requires TLS_ENABLED=true")
    print(adopt(host=hostname(os.environ.get("SERVER_NAME", ""))))
