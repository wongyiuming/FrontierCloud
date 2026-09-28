"""Streaming file inspection; async callers must offload this blocking work."""
import hashlib
from pathlib import Path


def file_digest(path: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    value = digest.hexdigest()
    return {"size_bytes": size, "sha256": value, "etag": f'"{value}"'}
