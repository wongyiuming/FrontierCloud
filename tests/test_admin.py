import asyncio
import hashlib
import io
import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile
from starlette.requests import Request

from app.api.v1 import admin
from app.services import admin_service
from app.services import media_manager


class _FakeResult:
    def fetchall(self):
        return []

    def mappings(self):
        return self

    def first(self):
        return None


class _FakeConnection:
    async def execute(self, *_args, **_kwargs):
        return _FakeResult()


class _FakeTransaction:
    async def __aenter__(self):
        return _FakeConnection()

    async def __aexit__(self, *_args):
        return False


class _FakeEngine:
    def begin(self):
        return _FakeTransaction()


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    async def set(self, key, value, ex=None, **_kwargs):
        self.values[key] = str(value)
        self.ttls[key] = ex
        return True

    async def get(self, key):
        return self.values.get(key)

    async def incr(self, key):
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value

    async def expire(self, key, seconds):
        if key not in self.values:
            return False
        self.ttls[key] = seconds
        return True

    async def delete(self, key):
        self.values.pop(key, None)
        self.ttls.pop(key, None)
        return 1


class AdminTokenLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_token_starts_with_and_keeps_its_own_sliding_ttl(self):
        fake_redis = _FakeRedis()
        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/api/v1/media/admin/elevate",
            "headers": [(b"user-agent", b"test")],
            "client": ("203.0.113.8", 12345),
        })
        token = "test-admin-token"
        token_key = admin_service.TOKEN_PREFIX + hashlib.sha256(token.encode()).hexdigest()
        fail_key = admin_service.FAIL_PREFIX + "203.0.113.8"

        with (
            patch.object(admin_service, "redis_client", fake_redis),
            patch.object(admin_service, "engine", _FakeEngine()),
            patch.object(admin_service.secrets, "token_urlsafe", return_value=token),
            patch.object(admin_service.settings, "ADMIN_TOKEN_TTL", 900),
            patch("builtins.print") as print_mock,
            patch.object(admin_service, "append_admin_log") as append_log_mock,
        ):
            issued = await admin_service.issue_admin_token()
            self.assertEqual(issued, token)
            self.assertEqual(fake_redis.values[token_key], "pending")
            self.assertEqual(
                fake_redis.ttls[token_key],
                900 + admin_service.TOKEN_ISSUE_OVERLAP_SECONDS,
            )
            self.assertIn(token, print_mock.call_args.args[0])
            self.assertNotIn(token, append_log_mock.call_args.args[0])

            fake_redis.values[fail_key] = "3"
            token_hash = await admin_service.verify_admin_token(token, request)

        self.assertEqual(token_hash, hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(fake_redis.values[token_key], "active")
        self.assertEqual(fake_redis.ttls[token_key], 900)
        self.assertNotIn(fail_key, fake_redis.values)

    async def test_new_tokens_coexist_without_replacing_an_active_older_token(self):
        fake_redis = _FakeRedis()
        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/api/v1/media/admin/elevate",
            "headers": [(b"user-agent", b"test")],
            "client": ("203.0.113.8", 12345),
        })
        tokens = ["token-a", "token-b", "token-c"]

        with (
            patch.object(admin_service, "redis_client", fake_redis),
            patch.object(admin_service, "engine", _FakeEngine()),
            patch.object(admin_service.secrets, "token_urlsafe", side_effect=tokens),
            patch.object(admin_service.settings, "ADMIN_TOKEN_TTL", 900),
            patch("builtins.print"),
            patch.object(admin_service, "append_admin_log"),
        ):
            for _token in tokens:
                await admin_service.issue_admin_token()
            await admin_service.verify_admin_token("token-a", request)

        for token in tokens:
            key = admin_service.TOKEN_PREFIX + hashlib.sha256(token.encode()).hexdigest()
            self.assertIn(key, fake_redis.values)
        active_key = admin_service.TOKEN_PREFIX + hashlib.sha256(b"token-a").hexdigest()
        self.assertEqual(fake_redis.values[active_key], "active")
        self.assertEqual(fake_redis.ttls[active_key], 900)
        for token in ("token-b", "token-c"):
            key = admin_service.TOKEN_PREFIX + hashlib.sha256(token.encode()).hexdigest()
            self.assertEqual(
                fake_redis.ttls[key],
                900 + admin_service.TOKEN_ISSUE_OVERLAP_SECONDS,
            )

    async def test_periodic_issuer_generates_the_next_independent_token(self):
        issue_mock = AsyncMock(side_effect=[None, asyncio.CancelledError])
        sleep_mock = AsyncMock(return_value=None)
        with (
            patch.object(admin_service, "issue_admin_token", new=issue_mock),
            patch.object(admin_service.asyncio, "sleep", new=sleep_mock),
            patch.object(admin_service.settings, "ADMIN_TOKEN_ISSUE_INTERVAL", 900),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await admin_service.run_admin_token_issuer()

        self.assertEqual(issue_mock.await_count, 2)
        sleep_mock.assert_any_await(900)

    async def test_unknown_token_has_specific_invalid_diagnostic(self):
        fake_redis = _FakeRedis()
        request = Request({
            "type": "http", "method": "POST", "path": "/api/v1/media/admin/elevate",
            "headers": [], "client": ("203.0.113.8", 12345),
        })
        with (
            patch.object(admin_service, "redis_client", fake_redis),
            patch.object(admin_service, "engine", _FakeEngine()),
        ):
            with self.assertRaises(HTTPException) as raised:
                await admin_service.verify_admin_token("never-issued", request)

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail["code"], "ADMIN_TOKEN_INVALID")

    async def test_expired_token_has_specific_expiry_diagnostic(self):
        fake_redis = _FakeRedis()
        request = Request({
            "type": "http", "method": "POST", "path": "/api/v1/media/admin/elevate",
            "headers": [], "client": ("203.0.113.8", 12345),
        })
        with (
            patch.object(admin_service, "redis_client", fake_redis),
            patch.object(
                admin_service,
                "_classify_missing_token",
                new=AsyncMock(return_value=(
                    401,
                    {"code": "ADMIN_TOKEN_EXPIRED", "message": "该特权凭证已过期，请获取新凭证"},
                )),
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await admin_service.verify_admin_token("previously-valid", request)

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail["code"], "ADMIN_TOKEN_EXPIRED")

    async def test_rate_limit_has_specific_diagnostic(self):
        fake_redis = _FakeRedis()
        fake_redis.values[admin_service.FAIL_PREFIX + "203.0.113.8"] = "10"
        request = Request({
            "type": "http", "method": "POST", "path": "/api/v1/media/admin/elevate",
            "headers": [], "client": ("203.0.113.8", 12345),
        })
        with (
            patch.object(admin_service, "redis_client", fake_redis),
            patch.object(admin_service.settings, "ADMIN_MAX_FAILED_ATTEMPTS_PER_IP", 10),
        ):
            with self.assertRaises(HTTPException) as raised:
                await admin_service.verify_admin_token("any-token", request)

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.detail["code"], "ADMIN_RATE_LIMITED")


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
                    relative_path="测试目录/一.wav",
                    session_hash="test-session",
                )

            self.assertEqual(result["path"], "测试目录/一.wav")
            uploaded = root / "测试目录" / "一.wav"
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
            destination = root / "media"
            destination.mkdir()
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
            destination = root / "批量目录"
            destination.mkdir()
            with (
                patch.object(media_manager, "MEDIA_ROOT", root),
                patch.object(admin.admin_service, "audit", new=AsyncMock()),
                patch.object(admin, "invalidate_media_catalog", new=AsyncMock()),
            ):
                results = [
                    await admin.upload_item(
                        request=self._request(),
                        file=UploadFile(filename=name, file=io.BytesIO(self._wav_bytes(name.encode()))),
                        target_dir="批量目录",
                        relative_path=None,
                        session_hash="test-session",
                    )
                    for name in ("一.wav", "二.wav")
                ]

            self.assertEqual([result["path"] for result in results], ["批量目录/一.wav", "批量目录/二.wav"])
            self.assertTrue((destination / "一.wav").is_file())
            self.assertTrue((destination / "二.wav").is_file())


class AdminDownloadContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_file_download_is_delegated_to_protected_nginx_location(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            folder = root / "测试目录"
            folder.mkdir()
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
                    paths=json.dumps(["测试目录/一 首.wav"], ensure_ascii=False),
                    session_hash="test-session",
                )

            self.assertEqual(response.status_code, 200)
            self.assertTrue(media_file.stat().st_mode & stat.S_IROTH)
            self.assertEqual(
                response.headers["x-accel-redirect"],
                "/_protected_media/%E6%B5%8B%E8%AF%95%E7%9B%AE%E5%BD%95/%E4%B8%80%20%E9%A6%96.wav",
            )
            self.assertIn("filename*=UTF-8''", response.headers["content-disposition"])

    async def test_folder_download_streams_a_valid_zip_without_a_temporary_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            folder = root / "测试目录"
            folder.mkdir()
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
                stream = await media_manager.MediaManager.build_zip_stream(["测试目录"])
                payload = b"".join(stream)

            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {"测试目录/一.wav", "测试目录/二.mp3"},
                )
                self.assertEqual(archive.read("测试目录/一.wav"), b"RIFF0000WAVEfmt ")

    async def test_folder_download_does_not_follow_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            folder = root / "folder"
            outside = root / "outside"
            folder.mkdir()
            outside.mkdir()
            (folder / "inside.mp3").write_bytes(b"ID3inside")
            (outside / "secret.mp3").write_bytes(b"ID3secret")

            try:
                (folder / "linked").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks require additional privileges")

            with patch.object(media_manager, "MEDIA_ROOT", root):
                stream = await media_manager.MediaManager.build_zip_stream(["folder"])
                payload = b"".join(stream)

            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                self.assertEqual(archive.namelist(), ["folder/inside.mp3"])


if __name__ == "__main__":
    unittest.main()
