from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


LOG_PATTERN = re.compile(
    r'^(?P<ip>\S+) - (?P<user>\S+) \[(?P<timestamp>[^]]+)] '
    r'"(?P<method>\S+) (?P<path>\S+) (?P<protocol>[^"]+)" '
    r'(?P<status>\d{3}) (?P<bytes>\d+) "(?P<referer>[^"]*)" '
    r'"(?P<agent>[^"]*)"(?: rt=(?P<request_time>\S+) urt=(?P<upstream_time>\S+))?'
)
CHUNK_SIZE = 512 * 1024


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def secret(path_name: str) -> str:
    return Path(required(path_name)).read_text(encoding="utf-8").strip()


def auth_header(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


class State:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS offsets (environment TEXT PRIMARY KEY, offset_bytes INTEGER NOT NULL, partial TEXT NOT NULL)"
        )
        self.db.commit()

    def get(self, environment: str) -> tuple[int, str]:
        row = self.db.execute(
            "SELECT offset_bytes, partial FROM offsets WHERE environment=?", (environment,)
        ).fetchone()
        return (int(row[0]), str(row[1])) if row else (0, "")

    def put(self, environment: str, offset: int, partial: str) -> None:
        self.db.execute(
            "INSERT INTO offsets VALUES (?, ?, ?) ON CONFLICT(environment) DO UPDATE SET offset_bytes=excluded.offset_bytes, partial=excluded.partial",
            (environment, offset, partial),
        )
        self.db.commit()


def request(url: str, authorization: str, *, method: str = "GET", start: int = 0) -> urllib.request.Request:
    headers = {"Authorization": authorization, "User-Agent": "FrontierCloud-ELK-Collector/1.0"}
    if method == "GET":
        headers["Range"] = f"bytes={start}-{start + CHUNK_SIZE - 1}"
    return urllib.request.Request(url, headers=headers, method=method)


def parse(environment: str, environment_name: str, lines: list[str]) -> list[dict[str, object]]:
    documents = []
    for line in lines:
        match = LOG_PATTERN.match(line)
        if not match:
            continue
        values = match.groupdict()
        timestamp = datetime.strptime(values["timestamp"], "%d/%b/%Y:%H:%M:%S %z").isoformat()
        documents.append(
            {
                "@timestamp": timestamp,
                "environment": environment,
                "environment_name": environment_name,
                "client_ip": values["ip"],
                "http_method": values["method"],
                "url_path": values["path"],
                "http_status": int(values["status"]),
                "response_bytes": int(values["bytes"]),
                "request_time": float(values["request_time"]) if values["request_time"] not in (None, "-") else None,
                "upstream_time": float(values["upstream_time"]) if values["upstream_time"] not in (None, "-") else None,
                "user_agent": values["agent"],
                "referer": values["referer"],
            }
        )
    return documents


def send(documents: list[dict[str, object]]) -> None:
    if not documents:
        return
    body = json.dumps(documents, ensure_ascii=False).encode()
    req = urllib.request.Request(
        required("LOGSTASH_URL"), data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        if response.status >= 300:
            raise RuntimeError(f"Logstash returned {response.status}")


def collect(state: State, source: tuple[str, str, str, str]) -> int:
    environment, environment_name, url, authorization = source
    offset, partial = state.get(environment)
    with urllib.request.urlopen(request(url, authorization, method="HEAD"), timeout=20) as response:
        length = int(response.headers.get("Content-Length", "0"))
    if length < offset:
        offset, partial = 0, ""
    if length == offset:
        return 0
    try:
        with urllib.request.urlopen(request(url, authorization, start=offset), timeout=30) as response:
            payload = response.read(CHUNK_SIZE)
    except urllib.error.HTTPError as exc:
        if exc.code != 416:
            raise
        state.put(environment, 0, "")
        return 0
    text = partial + payload.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    trailing = ""
    if lines and not lines[-1].endswith(("\n", "\r")):
        trailing = lines.pop()
    documents = parse(environment, environment_name, [line.rstrip("\r\n") for line in lines])
    send(documents)
    state.put(environment, offset + len(payload), trailing)
    return len(documents)


def main() -> None:
    user = os.environ.get("REPORT_METRICS_USER", "frontiercloud_monitor")
    sources = (
        (
            "production",
            "生产环境",
            required("PRODUCTION_ACCESS_LOG_URL"),
            auth_header(user, secret("PRODUCTION_METRICS_PASSWORD_FILE")),
        ),
        (
            "preproduction",
            "RN预发布",
            required("PREPRODUCTION_ACCESS_LOG_URL"),
            auth_header(user, secret("PREPRODUCTION_METRICS_PASSWORD_FILE")),
        ),
    )
    state = State(required("COLLECTOR_DATABASE_PATH"))
    while True:
        for source in sources:
            try:
                count = collect(state, source)
                if count:
                    print(f"indexed environment={source[0]} documents={count}", flush=True)
            except Exception as exc:
                print(
                    f"collection failed environment={source[0]} "
                    f"error={type(exc).__name__}: {str(exc)[:160]}",
                    flush=True,
                )
        time.sleep(60)


if __name__ == "__main__":
    main()
