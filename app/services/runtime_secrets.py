from __future__ import annotations

import logging
import os
import secrets

from app.core.config import ADMIN_KEY_FILE, MYSQL_PASSWORD_FILE, MYSQL_ROOT_PASSWORD_FILE, SECRET_DIR

ANNOUNCE_MARKER = SECRET_DIR / ".announce-once"
logger = logging.getLogger("frontiercloud.init")


def _new_secret(legacy_name: str) -> str:
    return os.getenv(legacy_name, "").strip() or secrets.token_urlsafe(48)


def _set_web_owner(path) -> None:
    if hasattr(os, "chown"):
        os.chown(path, 10001, 10001)


def initialize_runtime_secrets() -> None:
    SECRET_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SECRET_DIR, 0o700)
    _set_web_owner(SECRET_DIR)
    created = False
    managed = (
        (MYSQL_PASSWORD_FILE, "MYSQL_PASSWORD"),
        (MYSQL_ROOT_PASSWORD_FILE, "MYSQL_ROOT_PASSWORD"),
        (ADMIN_KEY_FILE, "ADMIN_BOOTSTRAP_TOKEN"),
    )
    for path, legacy_name in managed:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            _set_web_owner(path)
            continue
        path.write_text(_new_secret(legacy_name) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        _set_web_owner(path)
        created = True
    if created:
        ANNOUNCE_MARKER.touch(exist_ok=True)
        _set_web_owner(ANNOUNCE_MARKER)


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
