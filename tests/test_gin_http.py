"""Black-box Python tests of a freshly launched Gin binary, never FastAPI.

The driver owns a temporary data/secrets directory and a loopback process.
It accepts no target URL and cannot log in to an existing production service.
Database/Redis arguments must point to disposable fixtures supplied by the
development-host runner; the caller must never supply production credentials.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
OPTIONS = None


class GinHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if OPTIONS is None:
            raise unittest.SkipTest("development-host native binary fixture required")
        cls.directory = tempfile.TemporaryDirectory(prefix="fc-gin-http-")
        cls.work = Path(cls.directory.name)
        cls.log = (cls.work / "runtime.log").open("w+", encoding="utf-8")
        cls.addClassCleanup(cls.cleanup)
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        cls.base = f"http://127.0.0.1:{port}"
        cls.env = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG"}}
        cls.env.update(DATA_ROOT=str(cls.work / "data"),
                       STATIC_ROOT=str(ROOT / "static"),
                       SECRETS_DIR=str(cls.work / "secrets"),
                       SQLITE_PATH=str(cls.work / "data/frontiercloud.db"),
                       HTTP_ADDR=f"127.0.0.1:{port}", DB_TYPE=OPTIONS.database,
                       REDIS_URL=OPTIONS.redis_url, NGINX_MEDIA_ACCEL="false",
                       MYSQL_HOST=OPTIONS.mysql_host, MYSQL_PORT=str(OPTIONS.mysql_port),
                       MYSQL_USER="media_admin", MYSQL_DATABASE="fc_gin_http",
                       MYSQL_PASSWORD_FILE=str(OPTIONS.mysql_password_file or cls.work / "unused"),
                       TLS_ENABLED="false", SERVER_NAME="localhost",
                       RELEASE_BRANCH="gin_main", RELEASE_SOURCE_BRANCH="gin_dev")
        cls.binary = str(Path(OPTIONS.binary).resolve(strict=True))
        for command in ("init-secrets", "init-media", "migrate"):
            subprocess.run([cls.binary, command], env=cls.env, cwd=cls.work,
                           stdout=cls.log, stderr=cls.log, timeout=90, check=True)
        folder = cls.work / "data/media/music/fixture"
        folder.mkdir()
        cls.payload = b"ID3" + bytes(125)
        (folder / "song.mp3").write_bytes(cls.payload)
        (cls.work / "data/media/lyrics/fixture").mkdir()
        (cls.work / "data/media/lyrics/fixture/song.lrc").write_text(
            "[00:00.00]native fixture\n", encoding="utf-8")
        cls.key = (cls.work / "secrets/admin_key").read_text().strip()
        cls.start()

    @classmethod
    def cleanup(cls):
        cls.stop()
        cls.log.close()
        cls.directory.cleanup()

    @classmethod
    def start(cls):
        cls.process = subprocess.Popen([cls.binary, "serve"], env=cls.env, cwd=cls.work,
                                       stdout=cls.log, stderr=cls.log)
        for _ in range(100):
            try:
                with urllib.request.urlopen(cls.base + "/health", timeout=1) as response:
                    if response.status == 200:
                        return
            except (OSError, urllib.error.HTTPError):
                pass
            if cls.process.poll() is not None:
                break
            time.sleep(0.1)
        cls.log.flush()
        cls.log.seek(0)
        raise AssertionError("Gin readiness failed: " + cls.log.read()[-4000:])

    @classmethod
    def stop(cls):
        if getattr(cls, "process", None) and cls.process.poll() is None:
            cls.process.terminate()
            try:
                cls.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                cls.process.kill()
                cls.process.wait(timeout=5)

    def setUp(self):
        self.jar = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        status, _, body = self.request("POST", "/api/v1/media/admin/elevate",
                                      urllib.parse.urlencode({"token": self.key}).encode(),
                                      {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 200, body)
        status, _, body = self.request("GET", "/api/v1/media/admin/status")
        self.assertEqual(status, 200, body)
        name = json.loads(body)["csrf_cookie_name"]
        self.csrf = next(cookie.value for cookie in self.jar if cookie.name == name)

    def request(self, method, route, data=None, headers=None, csrf=False):
        fields = {"X-Admin-Activity": "passive", **(headers or {})}
        if csrf:
            fields["X-CSRF-Token"] = self.csrf
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            fields["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + route, data=data, headers=fields, method=method)
        try:
            response = self.client.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_public_shell_and_versioned_assets(self):
        status, _, body = self.request("GET", "/api/v1/media/music/category?path=music%2Ffixture")
        self.assertEqual(status, 200)
        self.assertNotIn(b"{{", body)
        self.assertRegex(body.decode(), r"audio-continuous-stream\.js\?v=[0-9a-f]{16}")
        self.assertRegex(body.decode(), r"player-directory-label\.js\?v=[0-9a-f]{16}")

    def test_stream_range_and_path_fence(self):
        status, headers, body = self.request(
            "GET", "/api/v1/media/stream?file_path=music%2Ffixture%2Fsong.mp3",
            headers={"Range": "bytes=3-9"})
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"], f"bytes 3-9/{len(self.payload)}")
        self.assertEqual(body, self.payload[3:10])
        status, _, _ = self.request("GET", "/api/v1/media/stream?file_path=..%2Fsecrets%2Fadmin_key")
        self.assertEqual(status, 403)

    def test_anonymous_and_csrf_rejected_before_mutation(self):
        status, _, body = self.request("POST", "/api/v1/media/admin/hide",
                                      {"paths": ["music/fixture/song.mp3"], "hidden": True})
        self.assertEqual(status, 403, body)
        self.jar.clear()
        status, _, _ = self.request("GET", "/api/v1/media/admin/status")
        self.assertEqual(status, 401)

    def test_priority_is_native_durable_state(self):
        status, _, body = self.request("POST", "/api/v1/media/admin/directory-priority",
                                      {"path": "music/fixture", "value": 77}, csrf=True)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["preference"], 77)
        self.stop()
        self.start()
        self.setUp()
        status, _, body = self.request("GET", "/api/v1/media/admin/directory-priorities?scope=music")
        self.assertEqual(status, 200, body)
        self.assertIn(b'"preference":77', body)
        self.assertEqual((self.work / "secrets/admin_key").read_text().strip(), self.key)

    def test_visibility_round_trip_and_reserved_lyric_protection(self):
        for hidden in (True, False):
            status, _, body = self.request("POST", "/api/v1/media/admin/hide",
                                          {"paths": ["music/fixture"], "hidden": hidden}, csrf=True)
            self.assertEqual(status, 200, body)
        status, _, body = self.request("POST", "/api/v1/media/admin/delete",
                                      {"paths": ["lyrics/default.lrc"]}, csrf=True)
        self.assertEqual(status, 400, body)
        self.assertTrue((self.work / "data/media/lyrics/default.lrc").is_file())

    def test_brand_upload_download_delete(self):
        png = b"\x89PNG\r\n\x1a\n" + bytes(20)
        boundary = "fc-native-test-boundary"
        multipart = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                     'filename="fixture.png"\r\nContent-Type: image/png\r\n\r\n').encode()
        multipart += png + f"\r\n--{boundary}--\r\n".encode()
        status, _, body = self.request("POST", "/api/v1/media/admin/upload/brand/music", multipart,
                                      {"Content-Type": f"multipart/form-data; boundary={boundary}"}, csrf=True)
        self.assertEqual(status, 200, body)
        self.assertIn(b'"custom":true', body)
        status, _, body = self.request("GET", "/api/v1/media/admin/brand/music/download")
        self.assertEqual(status, 200)
        self.assertEqual(body, png)
        status, _, body = self.request("DELETE", "/api/v1/media/admin/brand/music", csrf=True)
        self.assertEqual(status, 200, body)
        self.assertIn(b'"custom":false', body)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--database", choices=("sqlite", "mysql"), required=True)
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--mysql-host", default="127.0.0.1")
    parser.add_argument("--mysql-port", type=int, default=3306)
    parser.add_argument("--mysql-password-file", type=Path)
    OPTIONS = parser.parse_args()
    unittest.main(argv=["test_gin_http"], verbosity=2)
