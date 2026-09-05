from __future__ import annotations

import logging
import os
import secrets
import tempfile
from pathlib import Path

from app.core.config import ADMIN_KEY_FILE, MYSQL_PASSWORD_FILE, MYSQL_ROOT_PASSWORD_FILE, SECRET_DIR

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


def initialize_runtime_secrets() -> None:
    SECRET_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SECRET_DIR, 0o700)
    _set_web_owner(SECRET_DIR)
    managed = (MYSQL_PASSWORD_FILE, MYSQL_ROOT_PASSWORD_FILE, ADMIN_KEY_FILE)

    complete = all(path.exists() and path.read_text(encoding="utf-8").strip() for path in managed)
    if complete and not INITIALIZING_MARKER.exists() and not INITIALIZED_MARKER.exists():
        # Existing deployments predate the durable lifecycle marker. Preserve
        # their keys without re-emitting secrets into logs on upgrade.
        _write_marker(INITIALIZED_MARKER)
        for path in managed:
            _set_web_owner(path)
        return

    first_initialization = not INITIALIZED_MARKER.exists()
    if first_initialization and not INITIALIZING_MARKER.exists():
        _write_marker(INITIALIZING_MARKER)

    created = False
    for path in managed:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            _set_web_owner(path)
            continue
        _atomic_write(path, _new_secret() + "\n")
        created = True

    if first_initialization or created:
        _write_marker(ANNOUNCE_MARKER)
    if not INITIALIZED_MARKER.exists():
        _write_marker(INITIALIZED_MARKER)
    INITIALIZING_MARKER.unlink(missing_ok=True)
    _fsync_directory(SECRET_DIR)


def announce_initial_secrets_once() -> None:
    if not ANNOUNCE_MARKER.exists():
        return
    logger.warning(
        "initial_runtime_secrets",
        extra={"context": {
            "admin_key": ADMIN_KEY_FILE.read_text(encoding="utf-8").strip(),
            "mysql_password": MYSQL_PASSWORD_FILE.read_text(encoding="utf-8").strip(),
            "mysql_root_password": MYSQL_ROOT_PASSWORD_FILE.read_text(encoding="utf-8").strip(),
        }},
    )
    ANNOUNCE_MARKER.unlink(missing_ok=True)


if __name__ == "__main__":
    initialize_runtime_secrets()
