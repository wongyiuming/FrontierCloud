"""Browser media wire format and small, session-bound key envelopes.

Only browsers encrypt/decrypt content. This module derives 32-byte file keys,
wraps them for browser ECDH sessions, and persists descriptors. A handshake is
consumed once; its fixed 15-minute authorization covers multiple file requests.
Revocation stops future key delivery, not a browser saving an already given key
or plaintext. Content permissions must still be checked on every key request.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

CHUNK_SIZE = 1024 * 1024
SESSION_LIFETIME = 15 * 60
MAX_PUBLIC_SESSIONS = 3584
MAX_ADMIN_SESSIONS = 512
MAX_SESSIONS_PER_BINDING = 16
MAX_CIPHERTEXT_SIZE = 10 * 1024 ** 3
MAX_PLAINTEXT_SIZE = MAX_CIPHERTEXT_SIZE - 16 * ((MAX_CIPHERTEXT_SIZE + CHUNK_SIZE - 1) // CHUNK_SIZE)
_HEX_ID = re.compile(r"[0-9a-f]{32}\Z")
_OBJECT_ID = re.compile(r"[0-9a-f]{64}\Z")
_DESCRIPTOR_KEYS = frozenset({"version", "algorithm", "file_id", "nonce_prefix", "plaintext_size", "chunk_size", "ciphertext_size"})


class MetadataError(ValueError):
    pass


class SessionError(ValueError):
    pass


class SessionLimitError(SessionError):
    pass


class PremasterError(RuntimeError):
    pass


def _decode64(value: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError("Invalid base64")
    raw = base64.b64decode(value, validate=True)
    if base64.b64encode(raw).decode("ascii") != value:
        raise ValueError("Noncanonical base64")
    return raw


def _encode64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _url_decode(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid base64url")
    raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if _url_encode(raw) != value:
        raise ValueError("Noncanonical base64url")
    return raw


@dataclass(frozen=True)
class Metadata:
    version: int
    algorithm: str
    file_id: str
    nonce_prefix: str
    plaintext_size: int
    chunk_size: int
    ciphertext_size: int

    @classmethod
    def new(cls, size: int) -> "Metadata":
        if type(size) is not int or not 0 < size <= MAX_PLAINTEXT_SIZE:
            raise MetadataError("Invalid media encryption descriptor")
        return cls(1, "AES-256-GCM", secrets.token_hex(16), _encode64(secrets.token_bytes(8)), size,
                   CHUNK_SIZE, size + 16 * ((size + CHUNK_SIZE - 1) // CHUNK_SIZE))

    @classmethod
    def from_dict(cls, value: dict) -> "Metadata":
        if not isinstance(value, dict) or frozenset(value) != _DESCRIPTOR_KEYS:
            raise MetadataError("Invalid media encryption descriptor")
        try:
            result = cls(**value)
            result.validate()
            return result
        except (TypeError, ValueError, OverflowError) as exc:
            raise MetadataError("Invalid media encryption descriptor") from exc

    def validate(self) -> None:
        try:
            prefix = _decode64(self.nonce_prefix)
        except (ValueError, TypeError) as exc:
            raise MetadataError("Invalid media encryption descriptor") from exc
        if (type(self.version) is not int or self.version != 1 or self.algorithm != "AES-256-GCM"
                or not isinstance(self.file_id, str) or not _HEX_ID.fullmatch(self.file_id)
                or len(prefix) != 8 or type(self.chunk_size) is not int or self.chunk_size != CHUNK_SIZE
                or type(self.plaintext_size) is not int or not 0 < self.plaintext_size <= MAX_PLAINTEXT_SIZE
                or type(self.ciphertext_size) is not int
                or self.ciphertext_size != self.plaintext_size + 16 * ((self.plaintext_size + CHUNK_SIZE - 1) // CHUNK_SIZE)):
            raise MetadataError("Invalid media encryption descriptor")

    def to_dict(self) -> dict:
        self.validate()
        return asdict(self)

    def chunk_nonce(self, index: int) -> bytes:
        self.validate()
        if type(index) is not int or not 0 <= index < (self.plaintext_size + CHUNK_SIZE - 1) // CHUNK_SIZE:
            raise MetadataError("Invalid media chunk index")
        return _decode64(self.nonce_prefix) + index.to_bytes(4, "big")

    def chunk_aad(self, index: int) -> bytes:
        self.chunk_nonce(index)
        return f"frontiercloud:chunk:v1:{self.file_id}:{self.plaintext_size}:{index}".encode("ascii")


@dataclass(repr=False)
class _Session:
    binding: str
    key: bytes
    expires: float


class Manager:
    """Small key-envelope registry with separate public/Admin admission budgets."""

    def __init__(self, master: bytes, *, now: Callable[[], float] = time.time):
        if not isinstance(master, bytes) or len(master) != 32:
            raise PremasterError("Media premaster key must contain 32 bytes")
        self._master = bytes(master)
        self._now = now
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}

    def key_id(self) -> str:
        return hmac.digest(self._master, b"frontiercloud:premaster-id:v1", "sha256").hex()

    def file_key(self, meta: Metadata) -> bytes:
        meta.validate()
        return hmac.digest(self._master, b"frontiercloud:file-key:v1:" + meta.file_id.encode("ascii"), "sha256")

    def _admit(self, binding: str, now: float) -> None:
        if not binding or not isinstance(binding, str) or len(binding) > 256:
            raise SessionError("Browser encryption authorization invalid")
        expired = [identifier for identifier, entry in self._sessions.items() if entry.expires <= now]
        for identifier in expired:
            del self._sessions[identifier]
        own = sum(entry.binding == binding for entry in self._sessions.values())
        is_admin = binding.startswith("admin:")
        total = sum(entry.binding.startswith("admin:") == is_admin for entry in self._sessions.values())
        if own >= MAX_SESSIONS_PER_BINDING or total >= (MAX_ADMIN_SESSIONS if is_admin else MAX_PUBLIC_SESSIONS):
            raise SessionLimitError("Browser encryption authorization creation limit reached")

    def new_session(self, binding: str, client_public_key: str) -> dict:
        with self._lock:
            started = self._now()
            self._admit(binding, started)
        try:
            public_bytes = _decode64(client_public_key)
            if len(public_bytes) != 65 or public_bytes[0] != 4:
                raise ValueError("Uncompressed P-256 public key required")
            public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_bytes)
            private = ec.generate_private_key(ec.SECP256R1())
            shared = private.exchange(ec.ECDH(), public)
        except (ValueError, TypeError) as exc:
            raise SessionError("Invalid browser public key") from exc
        identifier, salt = secrets.token_hex(16), secrets.token_bytes(32)
        wrapping = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                        info=f"frontiercloud:browser-wrap:v1:{identifier}".encode("ascii")).derive(shared)
        with self._lock:
            self._admit(binding, self._now())
            expires = started + SESSION_LIFETIME
            expires_in = min(SESSION_LIFETIME, math.floor(math.floor(expires) - self._now()))
            if expires_in <= 0:
                raise SessionError("Browser encryption authorization already expired")
            self._sessions[identifier] = _Session(binding, wrapping, expires)
        public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return {"session_id": identifier, "public_key": _encode64(public), "salt": _encode64(salt),
                "expires_at": math.floor(expires), "expires_in": expires_in}

    def wrap(self, binding: str, identifier: str, meta: Metadata) -> dict:
        file_key = self.file_key(meta)
        with self._lock:
            session = self._sessions.get(identifier)
            if session is None or session.binding != binding or not binding or session.expires <= self._now():
                if session is not None and session.expires <= self._now():
                    self._sessions.pop(identifier, None)
                raise SessionError("Browser encryption authorization expired or invalid")
            iv = secrets.token_bytes(12)
            wrapped = AESGCM(session.key).encrypt(iv, file_key,
                f"frontiercloud:key-envelope:v1:{identifier}:{meta.file_id}".encode("ascii"))
            return {"iv": _encode64(iv), "wrapped_key": _encode64(wrapped), "expires_at": math.floor(session.expires)}

    def revoke_binding(self, binding: str) -> None:
        with self._lock:
            for identifier in [identifier for identifier, entry in self._sessions.items() if entry.binding == binding]:
                del self._sessions[identifier]

    def sign_preparation(self, binding: str, meta: Metadata) -> str:
        if not binding:
            raise SessionError("Browser encryption authorization invalid")
        raw = json.dumps({"binding": binding, "metadata": meta.to_dict(), "expires_at": math.floor(self._now() + SESSION_LIFETIME)},
                         separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        signature = hmac.digest(self._master, b"frontiercloud:preparation:v1:" + raw, "sha256")
        return _url_encode(raw) + "." + _url_encode(signature)

    def verify_preparation(self, binding: str, token: str, meta: Metadata) -> None:
        if not binding or not isinstance(token, str) or len(token) > 2048:
            raise SessionError("Invalid upload preparation")
        meta.validate()
        try:
            body, encoded_signature = token.split(".")
            raw, signature = _url_decode(body), _url_decode(encoded_signature)
            expected = hmac.digest(self._master, b"frontiercloud:preparation:v1:" + raw, "sha256")
            if not hmac.compare_digest(signature, expected):
                raise ValueError("Invalid signature")
            claim = json.loads(raw)
            if (not isinstance(claim, dict) or claim.get("binding") != binding
                    or type(claim.get("expires_at")) is not int or claim["expires_at"] <= self._now()
                    or Metadata.from_dict(claim.get("metadata")) != meta):
                raise ValueError("Invalid claim")
        except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
            raise SessionError("Invalid or expired upload preparation") from exc


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def open_premaster(directory: Path, *, has_encrypted_data: bool = False) -> Manager:
    """Never regenerate a missing secret for restored encrypted business data."""
    directory = Path(directory)
    if not has_encrypted_data:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        root_info = directory.lstat()
    except OSError as exc:
        raise PremasterError("Private media-key directory required") from exc
    if not stat.S_ISDIR(root_info.st_mode) or directory.is_symlink():
        raise PremasterError("Private media-key directory required")
    if os.name != "nt" and stat.S_IMODE(root_info.st_mode) != 0o700:
        raise PremasterError("Media-key directory permissions must be 0700")
    root_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)) if os.name != "nt" else None
    destination = directory / "media-premaster.key"
    try:
        if root_fd is not None and not os.path.samestat(root_info, os.fstat(root_fd)):
            raise PremasterError("Media-key directory changed while opening")
        def info():
            return os.stat("media-premaster.key", dir_fd=root_fd, follow_symlinks=False) if root_fd is not None else destination.lstat()
        try:
            key_info = info()
        except FileNotFoundError:
            if has_encrypted_data:
                raise PremasterError("Existing media premaster required for encrypted business data")
            temporary = ".media-premaster-" + secrets.token_hex(16)
            descriptor = os.open(temporary if root_fd is not None else directory / temporary,
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=root_fd)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(secrets.token_bytes(32))
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    if root_fd is not None:
                        os.link(temporary, "media-premaster.key", src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False)
                        os.fsync(root_fd)
                    else:
                        os.link(directory / temporary, destination)
                except FileExistsError:
                    pass
                _sync_directory(directory.parent)
            finally:
                os.unlink(temporary if root_fd is not None else directory / temporary, dir_fd=root_fd)
            key_info = info()
        if (not stat.S_ISREG(key_info.st_mode) or key_info.st_size != 32
                or (os.name != "nt" and stat.S_IMODE(key_info.st_mode) != 0o600)):
            raise PremasterError("Existing regular 32-byte media premaster with 0600 permissions required")
        descriptor = os.open("media-premaster.key" if root_fd is not None else destination,
                             os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
        with os.fdopen(descriptor, "rb") as stream:
            if not os.path.samestat(key_info, os.fstat(stream.fileno())):
                raise PremasterError("Media premaster changed while opening")
            key = stream.read(33)
        return Manager(key)
    except OSError as exc:
        raise PremasterError("Cannot safely open durable media premaster") from exc
    finally:
        if root_fd is not None:
            os.close(root_fd)


class CreationAdmission:
    """Bounded, fixed one-minute public handshake throttle; keys/Range are free."""
    def __init__(self, *, now: Callable[[], float] = time.time):
        self._now, self._lock, self._windows = now, threading.Lock(), {}

    def allow(self, ip: str, binding: str) -> bool:
        if not ip or not binding:
            return False
        with self._lock:
            now = self._now()
            for key in [key for key, (_, until) in self._windows.items() if until <= now]:
                del self._windows[key]
            pairs = (("ip:" + ip, 64), ("cookie:" + binding, 8))
            if any(self._windows.get(key, (0, 0))[0] >= limit for key, limit in pairs):
                return False
            if len(self._windows) + sum(key not in self._windows for key, _ in pairs) > 8192:
                return False
            for key, _ in pairs:
                count, until = self._windows.get(key, (0, now + 60))
                self._windows[key] = count + 1, until
            return True


def _validate_object_identity(object_id: str, object_kind: str) -> None:
    valid = _OBJECT_ID if object_kind == "media" else _HEX_ID if object_kind == "recording_lyric" else None
    if valid is None or not isinstance(object_id, str) or not valid.fullmatch(object_id):
        raise MetadataError("Invalid encrypted object identity")


async def encryption_for_id(conn, object_id: str, *, object_kind: str = "media") -> Metadata | None:
    from sqlalchemy import select
    from app.services.media_crypto_schema import encryption
    _validate_object_identity(object_id, object_kind)
    raw = await conn.scalar(select(encryption.c.descriptor_json).where(
        encryption.c.object_id == object_id, encryption.c.object_kind == object_kind))
    if raw is None:
        return None
    try:
        return Metadata.from_dict(json.loads(raw))
    except (ValueError, TypeError) as exc:
        raise MetadataError("Stored media encryption descriptor corrupt") from exc


async def encryption_for_ids(conn, object_ids: list[str]) -> dict[str, Metadata]:
    from sqlalchemy import select
    from app.services.media_crypto_schema import encryption
    result = {}
    for offset in range(0, len(object_ids), 500):
        batch = object_ids[offset:offset + 500]
        for identifier in batch:
            _validate_object_identity(identifier, "media")
        rows = (await conn.execute(select(encryption.c.object_id, encryption.c.descriptor_json).where(
            encryption.c.object_kind == "media", encryption.c.object_id.in_(batch)))).mappings()
        for row in rows:
            if row["descriptor_json"] is not None:
                try:
                    result[row["object_id"]] = Metadata.from_dict(json.loads(row["descriptor_json"]))
                except (ValueError, TypeError) as exc:
                    raise MetadataError("Stored media encryption descriptor corrupt") from exc
    return result


async def put_encryption(conn, object_id: str, meta: Metadata | None, *, object_kind: str = "media", restore: bool = False) -> None:
    """Caller owns the mutation fence and durable object transaction.

    Restoration/retried appliance publication must explicitly retain the same
    object ID and descriptor. New uploads never reactivate a tombstoned ID.
    """
    from sqlalchemy import insert, select, update
    from app.services.media_crypto_schema import encryption
    if meta is None:
        return
    _validate_object_identity(object_id, object_kind)
    meta.validate()
    where = (encryption.c.object_kind == object_kind, encryption.c.object_id == object_id)
    row = (await conn.execute(select(encryption).where(*where).with_for_update())).mappings().first()
    encoded = json.dumps(meta.to_dict(), separators=(",", ":"))
    if row is not None:
        if row["descriptor_json"] is None:
            if restore and row["file_id"] == meta.file_id:
                await conn.execute(update(encryption).where(*where).values(descriptor_json=encoded))
                return
            raise MetadataError("Encrypted object retired; a new upload requires a new identity")
        try:
            old = Metadata.from_dict(json.loads(row["descriptor_json"]))
        except (ValueError, TypeError) as exc:
            raise MetadataError("Stored encryption descriptor corrupt") from exc
        if old != meta:
            raise MetadataError("Encrypted object descriptor cannot be replaced")
        return
    used = await conn.scalar(select(encryption.c.file_id).where(encryption.c.file_id == meta.file_id).with_for_update())
    if used is not None:
        raise MetadataError("Encryption file ID already consumed")
    await conn.execute(insert(encryption).values(object_kind=object_kind, object_id=object_id,
        file_id=meta.file_id, descriptor_json=encoded, created_at=int(time.time())))


async def forget_encryption(conn, object_id: str, *, object_kind: str = "media") -> None:
    from sqlalchemy import update
    from app.services.media_crypto_schema import encryption
    _validate_object_identity(object_id, object_kind)
    await conn.execute(update(encryption).where(encryption.c.object_kind == object_kind,
        encryption.c.object_id == object_id).values(descriptor_json=None))


async def check_encryption_key(conn, identifier: str) -> None:
    from sqlalchemy import func, insert, select
    from app.services.media_crypto_schema import encryption, keys
    if not isinstance(identifier, str) or not _OBJECT_ID.fullmatch(identifier):
        raise PremasterError("Invalid media premaster identity")
    existing = await conn.scalar(select(keys.c.key_id).where(keys.c.singleton == 1).with_for_update())
    if existing is not None:
        if not hmac.compare_digest(str(existing), identifier):
            raise PremasterError("Media premaster does not match durable business key identity")
        return
    encrypted = await conn.scalar(select(func.count()).select_from(encryption).where(encryption.c.descriptor_json.is_not(None)))
    if encrypted:
        raise PremasterError("Restored encrypted database is missing its premaster identity")
    await conn.execute(insert(keys).values(singleton=1, key_id=identifier, created_at=int(time.time())))


manager: Manager | None = None
creation_admission = CreationAdmission()


async def business_role() -> str:
    """Read durable current role; an initialized Manager cannot bypass promotion."""
    from sqlalchemy import select
    from app.core.db import engine
    from app.services.federation.schema import identity
    async with engine.connect() as conn:
        role = await conn.scalar(select(identity.c.role).where(identity.c.singleton == 1))
    if role not in {"Standalone", "Master", "Follower"}:
        raise PremasterError("Business node identity is not initialized")
    return role


async def initialize() -> None:
    global manager
    from sqlalchemy import func, select
    from app.core.db import engine
    from app.services.media_crypto_schema import encryption, keys
    if await business_role() == "Follower":
        manager = None
        return
    async with engine.connect() as conn:
        has_encrypted = bool(await conn.scalar(select(func.count()).select_from(encryption).where(encryption.c.descriptor_json.is_not(None))))
        key_id = await conn.scalar(select(keys.c.key_id).where(keys.c.singleton == 1))
    # Same durable data root as media and database, separate from node-signing secrets.
    from app.services.media_manager import MEDIA_ROOT
    opened = open_premaster(MEDIA_ROOT.parent / "media-keys", has_encrypted_data=has_encrypted or key_id is not None)
    async with engine.begin() as conn:
        await check_encryption_key(conn, opened.key_id())
    manager = opened


def upload_encryption(mode: str, encoded: str | dict | None, preparation_token: str, binding: str) -> Metadata | None:
    if mode == "plain" and not encoded and not preparation_token:
        return None
    if mode != "encrypted":
        raise MetadataError("每次上传必须明确选择加密落盘或明文落盘")
    if manager is None:
        raise PremasterError("Storage nodes do not provide media keys")
    try:
        if isinstance(encoded, str) and len(encoded) > 2048:
            raise ValueError("Descriptor too large")
        meta = Metadata.from_dict(json.loads(encoded) if isinstance(encoded, str) else encoded)
    except (ValueError, TypeError) as exc:
        raise MetadataError("加密元数据无效") from exc
    manager.verify_preparation(binding, preparation_token, meta)
    return meta


def managed_path(value: str) -> tuple[str, str]:
    """Canonical bounded hierarchy shared by crypto authorization and download."""
    if not isinstance(value, str) or len(value) > 4096 or "\\" in value or value.startswith("/"):
        raise MetadataError("Invalid media path")
    parts = value.split("/")
    if any(not part or part in {".", ".."} or part.startswith(".") or "\x00" in part for part in parts):
        raise MetadataError("Invalid media path")
    kind = {"music": "audio", "vido": "video", "lyrics": "lyric"}.get(parts[0])
    allowed = {"audio": {".mp3", ".m4a", ".flac", ".wav"}, "video": {".mp4", ".webm", ".mkv"}, "lyric": {".lrc"}}
    if kind is None or len(parts) not in ({2, 3, 4} if kind == "lyric" else {3, 4}) or Path(value).suffix.lower() not in allowed[kind] or value == "lyrics/default.lrc":
        raise MetadataError("Invalid managed media path")
    return value, kind


async def crypto_object(file_path: str, resource_id: str = "", *, admin: bool = False) -> dict:
    """Acquire one shared mutation lease before resolving current permission."""
    from app.services.media_manager import ensure_media_mutations_ready, media_mutation_lock
    async with media_mutation_lock.shared():
        ensure_media_mutations_ready()
        return await _crypto_object(file_path, resource_id, admin=admin)


async def _crypto_object(file_path: str, resource_id: str = "", *, admin: bool = False) -> dict:
    from fastapi import HTTPException
    from app.core.db import engine
    from app.services import media_objects
    from app.services.media_manager import MEDIA_ROOT, MediaManager, resolve_safe_path
    from app.services.federation.catalog import catalog
    from app.services.federation import routing
    role = await business_role()
    if role == "Follower":
        raise HTTPException(409, "存储节点不提供媒体密钥，请访问主节点")
    if resource_id and not _OBJECT_ID.fullmatch(resource_id):
        raise MetadataError("Invalid resource identity")
    if role == "Master" and (resource_id or not file_path.startswith("lyrics/")):
        if not resource_id:
            normalized, _ = managed_path(file_path)
            rows = await catalog.resources(path=normalized)
            if len(rows) != 1:
                raise HTTPException(404, "Media object not found")
            resource_id = rows[0]["resource_id"]
        row, _relation = await routing.resolve(resource_id, file_path or None)
        path, kind = managed_path(row["path"])
        identifier, expected_size = row["resource_id"], row["payload"]["size"]
    else:
        if resource_id:
            raise MetadataError("Resource identity is invalid for local media")
        path, kind = managed_path(file_path)
        try:
            actual = resolve_safe_path(MEDIA_ROOT, path)
            info = actual.stat()
        except (OSError, ValueError) as exc:
            raise HTTPException(404, "Media object not found") from exc
        if not stat.S_ISREG(info.st_mode):
            raise HTTPException(404, "Media object not found")
        async with engine.begin() as conn:
            identifier = await media_objects.ensure_object(conn, path, kind)
        expected_size = info.st_size
    if not admin:
        hidden = await MediaManager.hidden_paths()
        if any("/".join(path.split("/")[:index]) in hidden for index in range(1, len(path.split("/")) + 1)):
            raise HTTPException(404, "Media object not found")
        if kind == "lyric":
            await _authorize_public_lyric(path, hidden)
    async with engine.connect() as conn:
        descriptor = await encryption_for_id(conn, identifier)
    if descriptor is not None and descriptor.ciphertext_size != expected_size:
        raise MetadataError("Stored ciphertext size does not match descriptor")
    return {"media_id": identifier, "path": path, "kind": kind, "size_bytes": expected_size,
            "encryption": descriptor, "resource_id": resource_id}


async def _authorize_public_lyric(path: str, hidden: set[str]) -> None:
    from fastapi import HTTPException
    from sqlalchemy import text
    from app.core.db import engine
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT media_path FROM media_lyric_links WHERE lyric_path=:path"), {"path": path})).scalars().all()
    unavailable = False
    for track in rows:
        if not str(track).startswith("music/") or any("/".join(str(track).split("/")[:index]) in hidden for index in range(1, len(str(track).split("/")) + 1)):
            continue
        try:
            object_ = await _crypto_object(str(track), admin=True)
            if object_["kind"] == "audio" and object_["path"] == track:
                return
        except MetadataError:
            continue
        except HTTPException as exc:
            if exc.status_code == 503:
                unavailable = True
            elif exc.status_code != 404:
                raise
    if unavailable:
        raise HTTPException(503, "Media temporarily unavailable", headers={"Retry-After": "30"})
    raise HTTPException(404, "Lyrics not found")


async def encrypted_lyric(track: str, resource_id: str = "") -> dict | None:
    """Authorize the current source before any plaintext/absent-lyric fallback."""
    from app.services.media_manager import ensure_media_mutations_ready, media_mutation_lock
    async with media_mutation_lock.shared():
        ensure_media_mutations_ready()
        return await _encrypted_lyric(track, resource_id)


async def _encrypted_lyric(track: str, resource_id: str = "") -> dict | None:
    """Caller holds the shared media mutation lease."""
    from sqlalchemy import text
    from app.core.db import engine
    # Preserve the existing plaintext lyric fallback for hidden tracks. Browser
    # grants for encrypted lyrics still require a currently public source below.
    source = await _crypto_object(track, resource_id, admin=True)
    if source["kind"] != "audio":
        return None
    async with engine.connect() as conn:
        lyric = await conn.scalar(text("SELECT lyric_path FROM media_lyric_links WHERE media_id=:id"), {"id": source["media_id"]})
    if not lyric or lyric == "lyrics/default.lrc":
        return None
    result = await _crypto_object(lyric, admin=True)
    if result["encryption"] is None:
        return None
    await _crypto_object(track, resource_id, admin=False)
    return await _crypto_object(lyric, admin=False)


def download_snapshot(identifier: str, path: str, size: int, revision: str) -> str:
    raw = json.dumps([identifier, path, size, revision], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _local_revision(info) -> str:
    return f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_ctime_ns}"


def _global_revision(row: dict) -> str:
    return json.dumps([row["storage_member_id"], row["object_id"], row["etag"]], separators=(",", ":"))


async def browser_download_plan(paths: list[str], *, limit: int = 5000) -> list[dict]:
    """Validate an entire selection, including plaintext entries, in bounded batches.

    Oversize selections containing only plaintext retain the existing server ZIP
    path. A mixed archive is limited to 5000 browser entries, never silently
    truncated. The caller authenticates Admin and audits the issued plan once.
    """
    from fastapi import HTTPException
    from sqlalchemy import select
    from app.core.db import engine
    from app.services import media_objects
    from app.services.federation import schema as fs
    from app.services.federation import routing
    from app.services.media_manager import MEDIA_ROOT, resolve_safe_path, ensure_media_mutations_ready, media_mutation_lock
    if not isinstance(paths, list) or not paths or len(paths) > 100 or limit <= 0:
        raise MetadataError("Invalid download selection")
    role = await business_role()
    if role == "Follower":
        raise HTTPException(409, "存储节点不提供业务下载")
    entries, pending, seen = [], [], set()
    encrypted, total = False, 0

    async def flush():
        nonlocal encrypted
        if not pending:
            return
        local = [(item["path"], item["kind"]) for item in pending if not item["media_id"]]
        identities = await media_objects.ensure_objects(local) if local else {}
        for item in pending:
            if not item["media_id"]:
                item["media_id"] = identities[item["path"]]
        async with engine.connect() as conn:
            descriptors = await encryption_for_ids(conn, [item["media_id"] for item in pending])
        for item in pending:
            descriptor = descriptors.get(item["media_id"])
            if descriptor is not None:
                encrypted = True
                if item["size_bytes"] != descriptor.ciphertext_size:
                    raise MetadataError("Download ciphertext size does not match descriptor")
                item["encryption"] = descriptor.to_dict()
            item["snapshot"] = download_snapshot(item["media_id"], item["path"], item["size_bytes"], item.pop("revision"))
            item.pop("kind")
            if len(entries) < limit:
                entries.append(item)
        pending.clear()

    async def add(path: str, *, row: dict | None = None):
        nonlocal total
        if path in seen:
            return
        canonical, kind = managed_path(path)
        if row is None:
            actual = resolve_safe_path(MEDIA_ROOT, canonical)
            info = actual.stat()
            if not stat.S_ISREG(info.st_mode):
                raise MetadataError("Invalid download file")
            identifier, size, revision = "", info.st_size, _local_revision(info)
        else:
            identifier, size, revision = row["media_id"], row["size_bytes"], _global_revision(row)
            # Resolve current placement and health; a visible catalog row alone
            # cannot promise bytes from an unavailable or revoked member.
            logical, relation = await routing.resolve(identifier, canonical)
            if relation is None:
                info = resolve_safe_path(MEDIA_ROOT, canonical).stat()
                if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                    raise MetadataError("Local global placement does not match catalog")
            if logical["object_id"] != row["object_id"]:
                raise HTTPException(409, "Download source changed")
        seen.add(path)
        total += 1
        pending.append({"path": path, "media_id": identifier, "filename": Path(path).name,
                        "size_bytes": size, "revision": revision, "kind": kind})
        if len(pending) == 500:
            await flush()

    async def local_selection(path: str):
        actual = resolve_safe_path(MEDIA_ROOT, path)
        if actual.is_file():
            await add(path)
            return
        if not actual.is_dir():
            raise HTTPException(404, "Download object not found")
        parts = path.split("/")
        if parts[0] not in {"music", "vido", "lyrics"} or len(parts) > 3:
            raise MetadataError("Invalid download directory")
        pending_directories = [actual]
        while pending_directories:
            directory = pending_directories.pop()
            with os.scandir(directory) as children:
                for child in children:
                    if child.is_symlink() or child.name.startswith("."):
                        continue
                    relative = Path(child.path).relative_to(MEDIA_ROOT).as_posix()
                    if child.is_dir(follow_symlinks=False) and len(relative.split("/")) <= 3:
                        pending_directories.append(Path(child.path))
                    elif child.is_file(follow_symlinks=False):
                        try:
                            managed_path(relative)
                        except MetadataError:
                            continue
                        await add(relative)

    async with media_mutation_lock.shared():
        ensure_media_mutations_ready()
        for path in paths:
            if (not isinstance(path, str) or len(path) > 4096 or "\\" in path or any(
                    not part or part in {".", ".."} or part.startswith(".") or "\x00" in part for part in path.split("/"))):
                raise MetadataError("Invalid download path")
            if role == "Master" and not path.startswith("lyrics"):
                if path.split("/", 1)[0] not in {"music", "vido"}:
                    raise MetadataError("Invalid download path")
                async with engine.connect() as conn:
                    rows = [dict(row) for row in (await conn.execute(select(fs.global_media).where(
                        fs.global_media.c.state == "active",
                        (fs.global_media.c.media_path == path) | fs.global_media.c.media_path.startswith(path.rstrip("/") + "/", autoescape=True)
                    ).order_by(fs.global_media.c.media_path, fs.global_media.c.media_id))).mappings()]
                if not rows:
                    raise HTTPException(404, "Download object not found")
                for row in rows:
                    await add(row["media_path"], row=row)
            else:
                await local_selection(path)
        await flush()
    if total > limit:
        if encrypted:
            raise HTTPException(413, "含加密文件的下载最多包含 5000 个文件，请分批下载")
        return []
    return entries


def safe_open_media(path: str):
    """Open a pinned file descriptor without following any symlink component."""
    from app.services.media_manager import MEDIA_ROOT, resolve_safe_path
    canonical, _kind = managed_path(path)
    if os.name == "nt":
        actual = resolve_safe_path(MEDIA_ROOT, canonical)
        before = actual.stat()
        stream = actual.open("rb")
        if not os.path.samestat(before, os.fstat(stream.fileno())):
            stream.close()
            raise MetadataError("Media object changed while opening")
        return stream
    directory = os.open(MEDIA_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = canonical.split("/")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        stream = os.fdopen(descriptor, "rb")
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            stream.close()
            raise MetadataError("A regular media file is required")
        return stream
    finally:
        os.close(directory)
