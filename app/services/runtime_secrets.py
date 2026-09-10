from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
from pathlib import Path

from app.core.config import (
    ADMIN_KEY_FILE,
    METRICS_TOKEN_FILE,
    MYSQL_PASSWORD_FILE,
    MYSQL_ROOT_PASSWORD_FILE,
    SECRET_DIR,
)

ANNOUNCE_MARKER = SECRET_DIR / ".announce-once"
INITIALIZING_MARKER = SECRET_DIR / ".initializing"
INITIALIZED_MARKER = SECRET_DIR / ".initialized"
logger = logging.getLogger("frontiercloud.init")


def _new_secret() -> str:
    return secrets.token_urlsafe(48)


def _set_web_owner(path) -> None:
    if hasattr(os, "chown"):
        os.chown(path, 10001, 10001)


def _fsync_directory(directory: Path) -> None:
    descriptor = -1
    try:
        descriptor = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _atomic_write(path: Path, value: str, mode: int = 0o600) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".new",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        _set_web_owner(temporary)
        os.replace(temporary, path)
        _set_web_owner(path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _write_marker(path: Path) -> None:
    _atomic_write(path, "1\n")


def _write_announcement(names: set[str]) -> None:
    _atomic_write(ANNOUNCE_MARKER, json.dumps(sorted(names)) + "\n")


def _pending_announcement_names(known_names: set[str]) -> set[str]:
    if not ANNOUNCE_MARKER.exists():
        return set()
    try:
        names = json.loads(ANNOUNCE_MARKER.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set(known_names)
    if not isinstance(names, list) or any(name not in known_names for name in names):
        return set(known_names)
    return {str(name) for name in names}


def initialize_runtime_secrets() -> None:
    SECRET_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SECRET_DIR, 0o700)
    _set_web_owner(SECRET_DIR)
    managed = {
        "mysql_password": MYSQL_PASSWORD_FILE,
        "mysql_root_password": MYSQL_ROOT_PASSWORD_FILE,
        "admin_key": ADMIN_KEY_FILE,
        "metrics_token": METRICS_TOKEN_FILE,
    }
    legacy_files = (MYSQL_PASSWORD_FILE, MYSQL_ROOT_PASSWORD_FILE, ADMIN_KEY_FILE)

    complete = all(path.exists() and path.read_text(encoding="utf-8").strip() for path in managed.values())
    if complete and not INITIALIZING_MARKER.exists() and not INITIALIZED_MARKER.exists():
        # Existing deployments predate the durable lifecycle marker. Preserve
        # their keys without re-emitting secrets into logs on upgrade.
        _write_marker(INITIALIZED_MARKER)
        for path in managed.values():
            _set_web_owner(path)
        return

    legacy_upgrade = (
        not INITIALIZED_MARKER.exists()
        and not INITIALIZING_MARKER.exists()
        and all(path.exists() and path.read_text(encoding="utf-8").strip() for path in legacy_files)
        and not (METRICS_TOKEN_FILE.exists() and METRICS_TOKEN_FILE.read_text(encoding="utf-8").strip())
    )
    first_initialization = not INITIALIZED_MARKER.exists() and not legacy_upgrade
    if first_initialization and not INITIALIZING_MARKER.exists():
        _write_marker(INITIALIZING_MARKER)

    created_names: set[str] = set()
    for name, path in managed.items():
        if path.exists() and path.read_text(encoding="utf-8").strip():
            _set_web_owner(path)
            continue
        _atomic_write(path, _new_secret() + "\n")
        created_names.add(name)

    pending_names = _pending_announcement_names(set(managed))
    pending_names.update(set(managed) if first_initialization else created_names)
    if pending_names:
        _write_announcement(pending_names)
    if not INITIALIZED_MARKER.exists():
        _write_marker(INITIALIZED_MARKER)
    INITIALIZING_MARKER.unlink(missing_ok=True)
    _fsync_directory(SECRET_DIR)


def announce_initial_secrets_once() -> None:
    if not ANNOUNCE_MARKER.exists():
        return
    managed = {
        "admin_key": ADMIN_KEY_FILE,
        "mysql_password": MYSQL_PASSWORD_FILE,
        "mysql_root_password": MYSQL_ROOT_PASSWORD_FILE,
        "metrics_token": METRICS_TOKEN_FILE,
    }
    names = _pending_announcement_names(set(managed))
    logger.warning(
        "initial_runtime_secrets",
        extra={"context": {
            name: managed[name].read_text(encoding="utf-8").strip()
            for name in sorted(names)
        }},
    )
    ANNOUNCE_MARKER.unlink(missing_ok=True)


if __name__ == "__main__":
    initialize_runtime_secrets()
