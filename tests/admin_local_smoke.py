"""Manual local-only Admin integration smoke test.

Run from the repository root after starting the IDE server:
    python tests/admin_local_smoke.py

The script refuses non-loopback targets and removes its uniquely named test
directories through the Admin API before exiting.
"""

from __future__ import annotations

import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


BASE_URL = "http://127.0.0.1:8000"
WAV_ONE = b"RIFF0000WAVEfmt "
WAV_TWO = b"RIFF1111WAVEfmt "


class LoopbackCookiePolicy(http.cookiejar.DefaultCookiePolicy):
    """Permit the currently running pre-restart Secure cookie on loopback only."""

    def return_ok_secure(self, cookie, request):
        if urllib.parse.urlsplit(request.full_url).hostname in {"127.0.0.1", "localhost", "::1"}:
            return True
        return super().return_ok_secure(cookie, request)


def env_value(name: str) -> str:
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f".env 缺少 {name}")


def multipart(fields: dict[str, str], filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = f"----FrontierCloudSmoke{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode("utf-8"),
            b"\r\n",
        ])
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"),
        b"Content-Type: audio/wav\r\n\r\n",
        content,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def request_json(
    opener: urllib.request.OpenerDirector,
    path: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict:
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers=headers or {},
        method=method,
    )
    try:
        with opener.open(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} -> {error.code}: {detail}") from error


def main() -> int:
    if urllib.parse.urlsplit(BASE_URL).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("安全拒绝：本脚本只能测试本机回环地址")

    cookie_jar = http.cookiejar.CookieJar(policy=LoopbackCookiePolicy())
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    wall_token = env_value("WALL_ADMIN_TOKEN")
    suffix = uuid.uuid4().hex[:10]
    folder_dir = f"codex-smoke-folder-{suffix}"
    multiple_dir = f"codex-smoke-multiple-{suffix}"
    cleanup_paths = [folder_dir, multiple_dir]
    csrf_token = ""

    try:
        issued = request_json(
            opener,
            "/api/v1/media/admin/token/issue",
            method="POST",
            headers={"X-Token": wall_token},
        )
        elevate_body = urllib.parse.urlencode({"token": issued["token"]}).encode()
        request_json(
            opener,
            "/api/v1/media/admin/elevate",
            method="POST",
            data=elevate_body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        status = request_json(opener, "/api/v1/media/admin/status")
        csrf_name = status["csrf_cookie_name"]
        csrf_token = next(cookie.value for cookie in cookie_jar if cookie.name == csrf_name)

        uploads = [
            ({"target_dir": "", "relative_path": f"{folder_dir}/一.wav"}, "一.wav", WAV_ONE),
            ({"target_dir": "", "relative_path": f"{multiple_dir}/seed.wav"}, "seed.wav", WAV_ONE),
            ({"target_dir": multiple_dir}, "二.wav", WAV_TWO),
            ({"target_dir": multiple_dir}, "三.wav", WAV_ONE),
        ]
        for fields, filename, content in uploads:
            body, content_type = multipart(fields, filename, content)
            result = request_json(
                opener,
                "/api/v1/media/admin/upload/item",
                method="POST",
                data=body,
                headers={"Content-Type": content_type, "X-CSRF-Token": csrf_token},
            )
            print(f"UPLOAD_OK {result['path']}")

        logs = request_json(opener, "/api/v1/media/admin/logs?after=0&limit=20")
        if not logs.get("entries"):
            raise RuntimeError("日志控制台没有返回任何日志")
        print(f"LOGS_OK entries={len(logs['entries'])} secure={logs['secure_transport']}")
        return 0
    finally:
        if csrf_token:
            try:
                cleanup_body = json.dumps({"paths": cleanup_paths}, ensure_ascii=False).encode("utf-8")
                result = request_json(
                    opener,
                    "/api/v1/media/admin/delete",
                    method="POST",
                    data=cleanup_body,
                    headers={"Content-Type": "application/json", "X-CSRF-Token": csrf_token},
                )
                print(f"CLEANUP_OK deleted={result['deleted']}")
            except Exception as error:
                print(f"CLEANUP_FAILED {error}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
