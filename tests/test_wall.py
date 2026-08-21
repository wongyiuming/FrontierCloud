import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import Response
from starlette.requests import Request

from app.services.wall_session import SESSION_COOKIE, WallSessionService
from app.services.wall_store import EncryptedWallStore, VolatileMessageKeyStore


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.operations = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def set(self, *args, **kwargs):
        self.operations.append((self.redis.set, args, kwargs))
        return self

    def zadd(self, *args, **kwargs):
        self.operations.append((self.redis.zadd, args, kwargs))
        return self

    def delete(self, *args, **kwargs):
        self.operations.append((self.redis.delete, args, kwargs))
        return self

    def zrem(self, *args, **kwargs):
        self.operations.append((self.redis.zrem, args, kwargs))
        return self

    async def execute(self):
        return [await function(*args, **kwargs) for function, args, kwargs in self.operations]


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.sorted_sets = {}

    def pipeline(self, **_kwargs):
        return FakePipeline(self)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def mget(self, keys):
        return [self.values.get(key) for key in keys]

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.sorted_sets.pop(key, None)
        return len(keys)

    async def zadd(self, key, mapping):
        self.sorted_sets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def zrem(self, key, *members):
        values = self.sorted_sets.setdefault(key, {})
        for member in members:
            values.pop(member, None)
        return len(members)

    async def zrevrange(self, key, start, stop):
        members = sorted(self.sorted_sets.get(key, {}), key=self.sorted_sets.get(key, {}).get, reverse=True)
        return members[start : stop + 1]

    async def zrangebyscore(self, key, minimum, maximum):
        del minimum
        return [member for member, score in self.sorted_sets.get(key, {}).items() if score <= float(maximum)]

    async def scan(self, cursor=0, match=None, count=None):
        del cursor, count
        prefix = (match or "").rstrip("*")
        return 0, [key for key in self.values if key.startswith(prefix)]


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class WallStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_key_is_consumed_once_and_expired_key_is_destroyed(self):
        store = VolatileMessageKeyStore()
        key = encoded(bytes(range(32)))
        await store.put("one", key, time.time() + 30)
        self.assertEqual(await store.consume("one"), key)
        self.assertIsNone(await store.consume("one"))
        await store.put("expired", key, time.time() - 1)
        self.assertIsNone(await store.consume("expired"))

    async def test_publish_lists_only_metadata_and_reveal_burns_payload(self):
        redis = FakeRedis()
        key = encoded(bytes(range(32)))
        nonce = encoded(bytes(range(12)))
        ciphertext = b"encrypted-content-and-tag"
        with tempfile.TemporaryDirectory() as directory:
            store = EncryptedWallStore(Path(directory))
            with patch("app.services.wall_store.redis_client", redis):
                message = await store.publish(
                    kind="text",
                    mime_type="text/plain;charset=utf-8",
                    nonce=nonce,
                    encoded_key=key,
                    ciphertext=ciphertext,
                    avatar_id="cloud",
                )
                self.assertNotIn("key", message)
                self.assertNotIn("ciphertext", message)
                listed = await store.list_messages()
                self.assertEqual(listed[0]["id"], message["id"])
                self.assertNotIn("key", json.dumps(listed))

                envelope = await store.reveal(message["id"])
                self.assertEqual(envelope.key, key)
                self.assertEqual(envelope.ciphertext, ciphertext)
                self.assertFalse((Path(directory) / f"{message['id']}.bin").exists())
                self.assertIsNone(await store.reveal(message["id"]))

    async def test_rejects_malformed_key_and_oversized_text(self):
        redis = FakeRedis()
        with tempfile.TemporaryDirectory() as directory:
            store = EncryptedWallStore(Path(directory))
            with patch("app.services.wall_store.redis_client", redis):
                with self.assertRaises(ValueError):
                    await store.publish(
                        kind="text",
                        mime_type="text/plain;charset=utf-8",
                        nonce=encoded(bytes(12)),
                        encoded_key="short",
                        ciphertext=b"ciphertext-with-tag",
                        avatar_id="cloud",
                    )


class WallSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_cookie_resolves_avatar_without_storing_raw_cookie(self):
        redis = FakeRedis()
        service = WallSessionService()
        response = Response()
        with patch("app.services.wall_session.redis_client", redis):
            created = await service.create("penguin", response)
            cookie_header = response.headers["set-cookie"]
            cookie_value = cookie_header.split(f"{SESSION_COOKIE}=", 1)[1].split(";", 1)[0]
            self.assertNotIn(cookie_value, json.dumps(redis.values))
            request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/v1/wall/session",
                    "headers": [(b"cookie", f"{SESSION_COOKIE}={cookie_value}".encode())],
                    "client": ("192.0.2.10", 1234),
                }
            )
            current = await service.current(request)
            self.assertEqual(current.avatar_id, "penguin")
            self.assertEqual(current.csrf_token, created.csrf_token)


if __name__ == "__main__":
    unittest.main()
