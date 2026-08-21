from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import settings
from app.core.redis import redis_client


MESSAGE_PREFIX = "wall:message:"
MESSAGE_INDEX = "wall:message:index"
KEY_BYTES = 32
NONCE_BYTES = 12
AUTH_TAG_BYTES = 16
TEXT_CIPHERTEXT_LIMIT = 8 * 1024 + AUTH_TAG_BYTES
IO_CHUNK_SIZE = 64 * 1024


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _decode_base64url(value: str, expected_size: int) -> bytes:
    if not value or len(value) > ((expected_size + 2) // 3) * 4 + 2:
        raise ValueError("Invalid encrypted envelope")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid encrypted envelope") from exc
    if len(decoded) != expected_size:
        raise ValueError("Invalid encrypted envelope")
    return decoded


@dataclass(frozen=True)
class RevealEnvelope:
    message_id: str
    kind: str
    mime_type: str
    nonce: str
    key: str
    ciphertext: bytes


class VolatileMessageKeyStore:
    """Process-local one-time keys that never enter Redis, MySQL or backups."""

    def __init__(self) -> None:
        self._keys: dict[str, tuple[bytearray, float]] = {}
        self._lock = asyncio.Lock()

    async def put(self, message_id: str, encoded_key: str, expires_at: float) -> None:
        key = bytearray(_decode_base64url(encoded_key, KEY_BYTES))
        async with self._lock:
            old = self._keys.pop(message_id, None)
            if old:
                self._wipe(old[0])
            self._keys[message_id] = (key, expires_at)

    async def consume(self, message_id: str) -> str | None:
        async with self._lock:
            item = self._keys.pop(message_id, None)
            if not item:
                return None
            key, expires_at = item
            try:
                if expires_at <= time.time():
                    return None
                return base64.urlsafe_b64encode(bytes(key)).rstrip(b"=").decode("ascii")
            finally:
                self._wipe(key)

    async def delete(self, message_id: str) -> None:
        async with self._lock:
            item = self._keys.pop(message_id, None)
            if item:
                self._wipe(item[0])

    async def clear(self) -> None:
        async with self._lock:
            for key, _expires_at in self._keys.values():
                self._wipe(key)
            self._keys.clear()

    async def purge_expired(self) -> None:
        now = time.time()
        async with self._lock:
            expired = [message_id for message_id, (_key, expiry) in self._keys.items() if expiry <= now]
            for message_id in expired:
                key, _expiry = self._keys.pop(message_id)
                self._wipe(key)

    @staticmethod
    def _wipe(value: bytearray) -> None:
        for index in range(len(value)):
            value[index] = 0


class EncryptedWallStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path("data/wall")
        self.keys = VolatileMessageKeyStore()
        self._reveal_lock = asyncio.Lock()

    @property
    def maximum_image_ciphertext_size(self) -> int:
        return settings.WALL_MAX_IMAGE_SIZE + AUTH_TAG_BYTES

    async def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        # A process restart destroys every decryption key. Remove now-orphaned
        # ciphertext instead of retaining data that can never be revealed.
        await self.keys.clear()
        for path in self.root.glob("*.bin"):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        cursor: int | str = 0
        while True:
            cursor, keys = await redis_client.scan(cursor=cursor, match=f"{MESSAGE_PREFIX}*", count=200)
            if keys:
                await redis_client.delete(*keys)
            if int(cursor) == 0:
                break
        await redis_client.delete(MESSAGE_INDEX)

    async def shutdown(self) -> None:
        await self.keys.clear()

    async def publish(
        self,
        *,
        kind: str,
        mime_type: str,
        nonce: str,
        encoded_key: str,
        ciphertext: bytes,
        avatar_id: str,
    ) -> dict[str, str | int]:
        if kind not in {"text", "image"}:
            raise ValueError("Unsupported wall message type")
        _decode_base64url(nonce, NONCE_BYTES)
        _decode_base64url(encoded_key, KEY_BYTES)
        maximum = TEXT_CIPHERTEXT_LIMIT if kind == "text" else self.maximum_image_ciphertext_size
        if len(ciphertext) < AUTH_TAG_BYTES + 1 or len(ciphertext) > maximum:
            raise ValueError("Encrypted payload size is invalid")
        if kind == "text" and mime_type != "text/plain;charset=utf-8":
            raise ValueError("Invalid text envelope")
        if kind == "image" and mime_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValueError("Unsupported image type")

        message_id = str(uuid.uuid4())
        created_at = time.time()
        expires_at = created_at + settings.WALL_TTL
        final_path = self.root / f"{message_id}.bin"
        temporary_path = self.root / f".{message_id}.tmp"
        try:
            with temporary_path.open("xb") as stream:
                stream.write(ciphertext)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_path, 0o600)
            temporary_path.replace(final_path)
            await self.keys.put(message_id, encoded_key, expires_at)
            metadata = {
                "id": message_id,
                "kind": kind,
                "mime_type": mime_type,
                "nonce": nonce,
                "avatar_id": avatar_id,
                "created_at": _utc_iso(created_at),
                "expires_at": _utc_iso(expires_at),
            }
            async with redis_client.pipeline(transaction=True) as pipe:
                pipe.set(f"{MESSAGE_PREFIX}{message_id}", json.dumps(metadata), ex=settings.WALL_TTL)
                pipe.zadd(MESSAGE_INDEX, {message_id: created_at})
                await pipe.execute()
            return metadata
        except Exception:
            await self.keys.delete(message_id)
            for path in (temporary_path, final_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            raise

    async def list_messages(self, limit: int = 100) -> list[dict[str, str]]:
        await self.cleanup_expired()
        message_ids = await redis_client.zrevrange(MESSAGE_INDEX, 0, max(0, limit - 1))
        if not message_ids:
            return []
        values = await redis_client.mget([f"{MESSAGE_PREFIX}{message_id}" for message_id in message_ids])
        return [json.loads(value) for value in values if value]

    async def reveal(self, message_id: str) -> RevealEnvelope | None:
        try:
            normalized_id = str(uuid.UUID(message_id))
        except (ValueError, AttributeError) as exc:
            raise ValueError("Invalid message id") from exc

        # One process serves production. This lock makes key consumption,
        # metadata deletion and ciphertext removal one atomic application act.
        async with self._reveal_lock:
            key = await self.keys.consume(normalized_id)
            metadata_raw = await redis_client.get(f"{MESSAGE_PREFIX}{normalized_id}")
            path = self.root / f"{normalized_id}.bin"
            if not key or not metadata_raw:
                await self._burn(normalized_id, path)
                return None
            metadata = json.loads(metadata_raw)
            try:
                ciphertext = path.read_bytes()
            except FileNotFoundError:
                await self._burn(normalized_id, path)
                return None
            await self._burn(normalized_id, path)
            return RevealEnvelope(
                message_id=normalized_id,
                kind=metadata["kind"],
                mime_type=metadata["mime_type"],
                nonce=metadata["nonce"],
                key=key,
                ciphertext=ciphertext,
            )

    async def delete(self, message_id: str) -> None:
        try:
            normalized_id = str(uuid.UUID(message_id))
        except (ValueError, AttributeError) as exc:
            raise ValueError("Invalid message id") from exc
        async with self._reveal_lock:
            await self.keys.delete(normalized_id)
            await self._burn(normalized_id, self.root / f"{normalized_id}.bin")

    async def cleanup_expired(self) -> None:
        await self.keys.purge_expired()
        cutoff = time.time() - settings.WALL_TTL
        expired_ids = await redis_client.zrangebyscore(MESSAGE_INDEX, "-inf", cutoff)
        for message_id in expired_ids:
            await self.keys.delete(message_id)
            await self._burn(message_id, self.root / f"{message_id}.bin")

    async def run_cleanup(self) -> None:
        while True:
            await asyncio.sleep(min(60, max(5, settings.WALL_TTL // 4)))
            await self.cleanup_expired()

    async def _burn(self, message_id: str, path: Path) -> None:
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.delete(f"{MESSAGE_PREFIX}{message_id}")
            pipe.zrem(MESSAGE_INDEX, message_id)
            await pipe.execute()
        try:
            path.unlink()
        except FileNotFoundError:
            pass


wall_store = EncryptedWallStore()
