import hashlib
import io
import json
import stat
import zipfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile
from starlette.requests import Request

from app.api.v1 import admin
from app.services import admin_service
from app.services import media_manager


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.hashes = {}

    async def get(self, key): return self.values.get(key)
    async def incr(self, key):
        self.values[key] = str(int(self.values.get(key, "0")) + 1)
        return int(self.values[key])
    async def expire(self, *_args): return True
    async def delete(self, key):
        self.values.pop(key, None); self.hashes.pop(key, None); return 1
    async def hset(self, key, mapping): self.hashes.setdefault(key, {}).update(mapping)
    async def hgetall(self, key): return self.hashes.get(key, {})
    async def scan_iter(self, match=None):
        prefix = (match or "").rstrip("*")
        for key in list(self.hashes):
            if key.startswith(prefix): yield key


def _request(method="POST", cookies=None, headers=None):
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": method, "path": "/api/v1/media/admin/elevate",
             "headers": raw_headers, "client": ("203.0.113.8", 12345)}
    request = Request(scope)
    if cookies:
        request._cookies = cookies
    return request


class AdminKeyLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_key_has_no_redis_ttl_and_validates_directly(self):
        fake = _FakeRedis()
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "admin_key"
            key_file.write_text("stable-admin-key-123456789\n", encoding="utf-8")
            with patch.object(admin_service, "ADMIN_KEY_FILE", key_file), patch.object(admin_service, "redis_client", fake):
                digest = await admin_service.verify_admin_key("stable-admin-key-123456789", _request())
        self.assertEqual(digest, hashlib.sha256(b"stable-admin-key-123456789").hexdigest())
        self.assertFalse(any(key.startswith("admin:token:") for key in fake.values))

    async def test_invalid_key_is_rejected_and_rate_counted(self):
        fake = _FakeRedis()
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "admin_key"
            key_file.write_text("stable-admin-key-123456789\n", encoding="utf-8")
            with patch.object(admin_service, "ADMIN_KEY_FILE", key_file), patch.object(admin_service, "redis_client", fake):
                with self.assertRaises(HTTPException) as raised:
                    await admin_service.verify_admin_key("wrong", _request())
        self.assertEqual(raised.exception.detail["code"], "ADMIN_KEY_INVALID")
        self.assertEqual(fake.values[admin_service.FAIL_PREFIX + "203.0.113.8"], "1")

    async def test_random_rotation_replaces_file_and_invalidates_other_sessions(self):
        fake = _FakeRedis()
        current = admin_service.SESSION_PREFIX + "current"
        other = admin_service.SESSION_PREFIX + "other"
        fake.hashes = {current: {"key_hash": "old"}, other: {"key_hash": "old"}}
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "admin_key"
            key_file.write_text("stable-admin-key-123456789\n", encoding="utf-8")
            with patch.object(admin_service, "ADMIN_KEY_FILE", key_file), patch.object(admin_service, "redis_client", fake), \
                 patch.object(admin_service.secrets, "token_urlsafe", return_value="new-random-admin-key-123456789"):
                new_key = await admin_service.rotate_admin_key("current", None, None)
            self.assertEqual(key_file.read_text(encoding="utf-8").strip(), new_key)
        self.assertNotIn(other, fake.hashes)
        self.assertEqual(fake.hashes[current]["key_hash"], hashlib.sha256(new_key.encode()).hexdigest())

    async def test_custom_rotation_requires_confirmation_and_minimum_length(self):
        fake = _FakeRedis()
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "admin_key"
            key_file.write_text("stable-admin-key-123456789\n", encoding="utf-8")
            with patch.object(admin_service, "ADMIN_KEY_FILE", key_file), patch.object(admin_service, "redis_client", fake):
                with self.assertRaises(ValueError):
                    await admin_service.rotate_admin_key("current", "short", "short")
                with self.assertRaises(ValueError):
                    await admin_service.rotate_admin_key("current", "custom-admin-key-1234", "different-key-123456")


class AdminUploadContractTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _wav_bytes(marker: bytes) -> bytes:
        return b"RIFF" + marker[:4].ljust(4, b"0") + b"WAVEfmt "

    @staticmethod
    def _request() -> Request:
        return Request({
            "type": "http",
            "method": "POST",
            "path": "/api/v1/media/admin/upload/item",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        })

    async def test_folder_upload_accepts_empty_root_target_and_preserves_relative_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            upload = UploadFile(filename="一.wav", file=io.BytesIO(self._wav_bytes(b"one")))
            with (
                patch.object(media_manager, "MEDIA_ROOT", root),
                patch.object(admin.admin_service, "audit", new=AsyncMock()),
                patch.object(admin, "invalidate_media_catalog", new=AsyncMock()),
            ):
                result = await admin.upload_item(
                    request=self._request(),
                    file=upload,
                    target_dir="",
                    relative_path="music/测试目录/一.wav",
                    session_hash="test-session",
                )

            self.assertEqual(result["path"], "music/测试目录/一.wav")
            uploaded = root / "music" / "测试目录" / "一.wav"
            self.assertTrue(uploaded.is_file())
            self.assertTrue(uploaded.stat().st_mode & stat.S_IROTH)
            self.assertTrue(upload.file.closed)

    async def test_upload_closes_file_when_destination_validation_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            upload = UploadFile(filename="one.wav", file=io.BytesIO(self._wav_bytes(b"one")))
            with patch.object(media_manager, "MEDIA_ROOT", root):
                with self.assertRaises(HTTPException) as raised:
                    await admin.upload_item(
                        request=self._request(),
                        file=upload,
                        target_dir="missing",
                        relative_path=None,
                        session_hash="test-session",
                    )

            self.assertEqual(raised.exception.status_code, 404)
            self.assertTrue(upload.file.closed)

    async def test_upload_does_not_overwrite_a_concurrently_published_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            destination = root / "music" / "media"
            destination.mkdir(parents=True)
            upload = UploadFile(filename="one.wav", file=io.BytesIO(self._wav_bytes(b"one")))
            with (
                patch.object(media_manager, "MEDIA_ROOT", root),
                patch.object(media_manager.os, "link", side_effect=FileExistsError()),
            ):
                with self.assertRaises(HTTPException) as raised:
                    await media_manager.MediaManager.upload_one(upload, destination)

            await upload.close()
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(list(destination.glob(".upload-*.part")), [])

    async def test_multiple_files_use_independent_scalar_upload_requests(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            destination = root / "music" / "批量目录"
            destination.mkdir(parents=True)
            with (
                patch.object(media_manager, "MEDIA_ROOT", root),
                patch.object(admin.admin_service, "audit", new=AsyncMock()),
                patch.object(admin, "invalidate_media_catalog", new=AsyncMock()),
            ):
                results = [
                    await admin.upload_item(
                        request=self._request(),
                        file=UploadFile(filename=name, file=io.BytesIO(self._wav_bytes(name.encode()))),
                        target_dir="music/批量目录",
                        relative_path=None,
                        session_hash="test-session",
                    )
                    for name in ("一.wav", "二.wav")
                ]

            self.assertEqual([result["path"] for result in results], ["music/批量目录/一.wav", "music/批量目录/二.wav"])
            self.assertTrue((destination / "一.wav").is_file())
            self.assertTrue((destination / "二.wav").is_file())


class AdminDownloadContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_file_download_is_delegated_to_protected_nginx_location(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            folder = root / "music" / "测试目录"
            folder.mkdir(parents=True)
            media_file = folder / "一 首.wav"
            media_file.write_bytes(b"RIFF0000WAVEfmt ")
            media_file.chmod(0o600)
            request = Request({
                "type": "http",
                "method": "GET",
                "path": "/api/v1/media/admin/download",
                "headers": [],
                "client": ("127.0.0.1", 12345),
            })

            with (
                patch.object(media_manager, "MEDIA_ROOT", root),
                patch.object(admin.admin_service, "audit", new=AsyncMock()),
            ):
                response = await admin.download_objects(
                    request=request,
                    paths=json.dumps(["music/测试目录/一 首.wav"], ensure_ascii=False),
                    session_hash="test-session",
                )

            self.assertEqual(response.status_code, 200)
            self.assertTrue(media_file.stat().st_mode & stat.S_IROTH)
            self.assertEqual(
                response.headers["x-accel-redirect"],
                "/_protected_media/music/%E6%B5%8B%E8%AF%95%E7%9B%AE%E5%BD%95/%E4%B8%80%20%E9%A6%96.wav",
            )
            self.assertIn("filename*=UTF-8''", response.headers["content-disposition"])

    async def test_folder_download_streams_a_valid_zip_without_a_temporary_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            folder = root / "music" / "测试目录"
            folder.mkdir(parents=True)
            (folder / "一.wav").write_bytes(b"RIFF0000WAVEfmt ")
            (folder / "二.mp3").write_bytes(b"ID3test")

            with (
                patch.object(media_manager, "MEDIA_ROOT", root),
                patch.object(
                    media_manager.tempfile,
                    "mkstemp",
                    side_effect=AssertionError("download must not create a temporary archive"),
                ),
            ):
                stream = await media_manager.MediaManager.build_zip_stream(["music/测试目录"])
                payload = b"".join(stream)

            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {"music/测试目录/一.wav", "music/测试目录/二.mp3"},
                )
                self.assertEqual(archive.read("music/测试目录/一.wav"), b"RIFF0000WAVEfmt ")

    async def test_folder_download_does_not_follow_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            folder = root / "music" / "folder"
            outside = root / "outside"
            folder.mkdir(parents=True)
            outside.mkdir()
            (folder / "inside.mp3").write_bytes(b"ID3inside")
            (outside / "secret.mp3").write_bytes(b"ID3secret")

            try:
                (folder / "linked").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks require additional privileges")

            with patch.object(media_manager, "MEDIA_ROOT", root):
                stream = await media_manager.MediaManager.build_zip_stream(["music/folder"])
                payload = b"".join(stream)

            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                self.assertEqual(archive.namelist(), ["music/folder/inside.mp3"])


class AdminAuthenticationCoverageTests(unittest.TestCase):
    def test_every_admin_route_except_elevation_requires_session(self):
        missing = []
        for route in admin.router.routes:
            if route.path == "/elevate":
                continue
            dependencies = {dependency.call for dependency in route.dependant.dependencies}
            if admin.require_session not in dependencies:
                missing.append(route.path)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
