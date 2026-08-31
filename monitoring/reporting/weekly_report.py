from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import html
import ipaddress
import json
import os
import re
import sqlite3
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import maxminddb


LOG_PATTERN = re.compile(
    r'^(?P<ip>\S+) - \S+ \[(?P<timestamp>[^]]+)] '
    r'"(?P<method>\S+) (?P<path>\S+) [^"]+" '
    r'(?P<status>\d{3}) (?P<bytes>\d+) '
)
EXCLUDED_PATH_PREFIXES = (
    "/internal/",
    "/static/",
    "/api/v1/health",
    "/favicon.ico",
)


def read_secret(path: str) -> str:
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"empty secret: {path}")
    return value


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


@dataclass(frozen=True)
class Source:
    name: str
    job_prefix: str
    prometheus_url: str
    prometheus_user: str
    prometheus_password: str
    security_url: str
    access_log_url: str
    metrics_user: str
    metrics_password: str


@dataclass(frozen=True)
class Config:
    database_path: Path
    geoip_path: Path
    telegram_token: str
    telegram_chat_id: str
    timezone: ZoneInfo
    sources: tuple[Source, ...]
    github_repository: str

    @staticmethod
    def load() -> "Config":
        metrics_user = os.environ.get("REPORT_METRICS_USER", "frontiercloud_monitor")
        production_metrics_password = read_secret(required_env("PRODUCTION_METRICS_PASSWORD_FILE"))
        preproduction_metrics_password = read_secret(required_env("PREPRODUCTION_METRICS_PASSWORD_FILE"))
        sources = (
            Source(
                name="生产环境",
                job_prefix="production",
                prometheus_url=required_env("PRODUCTION_PROMETHEUS_URL").rstrip("/"),
                prometheus_user=os.environ.get("PRODUCTION_PROMETHEUS_USER", "frontier_observer"),
                prometheus_password=read_secret(required_env("PRODUCTION_PROMETHEUS_PASSWORD_FILE")),
                security_url=required_env("PRODUCTION_SECURITY_URL"),
                access_log_url=required_env("PRODUCTION_ACCESS_LOG_URL"),
                metrics_user=metrics_user,
                metrics_password=production_metrics_password,
            ),
            Source(
                name="预发布环境",
                job_prefix="preproduction",
                prometheus_url=required_env("PREPRODUCTION_PROMETHEUS_URL").rstrip("/"),
                prometheus_user=os.environ.get("PREPRODUCTION_PROMETHEUS_USER", "frontier_observer"),
                prometheus_password=read_secret(required_env("PREPRODUCTION_PROMETHEUS_PASSWORD_FILE")),
                security_url=required_env("PREPRODUCTION_SECURITY_URL"),
                access_log_url=required_env("PREPRODUCTION_ACCESS_LOG_URL"),
                metrics_user=metrics_user,
                metrics_password=preproduction_metrics_password,
            ),
        )
        return Config(
            database_path=Path(os.environ.get("REPORT_DATABASE_PATH", "/var/lib/frontiercloud-report/report.sqlite3")),
            geoip_path=Path(os.environ.get("CITY_DATABASE_PATH", "/var/lib/frontiercloud-report/dbip-city-lite.mmdb")),
            telegram_token=read_secret(required_env("TG_BOT_TOKEN_FILE")),
            telegram_chat_id=required_env("TG_CHAT_ID"),
            timezone=ZoneInfo(os.environ.get("REPORT_TIMEZONE", "Asia/Shanghai")),
            sources=sources,
            github_repository=os.environ.get("GITHUB_REPOSITORY", "wongyiuming/FrontierCloud"),
        )


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS collector_state (
                environment TEXT PRIMARY KEY,
                offset_bytes INTEGER NOT NULL DEFAULT 0,
                partial_line TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS legal_requests (
                fingerprint TEXT PRIMARY KEY,
                environment TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                status INTEGER NOT NULL,
                body_bytes INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_legal_requests_period
                ON legal_requests(environment, occurred_at);
            CREATE TABLE IF NOT EXISTS geo_cache (
                ip_address TEXT PRIMARY KEY,
                country TEXT NOT NULL,
                city TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sent_reports (
                report_key TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            );
        """)
        self.connection.commit()

    def state(self, environment: str) -> tuple[int, str]:
        row = self.connection.execute(
            "SELECT offset_bytes, partial_line FROM collector_state WHERE environment=?",
            (environment,),
        ).fetchone()
        return (int(row[0]), str(row[1])) if row else (0, "")

    def save_state(self, environment: str, offset: int, partial: str) -> None:
        self.connection.execute(
            """INSERT INTO collector_state(environment, offset_bytes, partial_line)
               VALUES(?, ?, ?)
               ON CONFLICT(environment) DO UPDATE SET
                 offset_bytes=excluded.offset_bytes,
                 partial_line=excluded.partial_line""",
            (environment, offset, partial),
        )
        self.connection.commit()

    def add_request(self, environment: str, line: str, match: re.Match[str]) -> None:
        ip = ipaddress.ip_address(match.group("ip"))
        status = int(match.group("status"))
        path = match.group("path").split("?", 1)[0]
        if not ip.is_global or status < 200 or status >= 400 or path.startswith(EXCLUDED_PATH_PREFIXES):
            return
        occurred = datetime.strptime(match.group("timestamp"), "%d/%b/%Y:%H:%M:%S %z")
        fingerprint = hashlib.sha256(f"{environment}\0{line}".encode("utf-8")).hexdigest()
        self.connection.execute(
            """INSERT OR IGNORE INTO legal_requests
               (fingerprint, environment, occurred_at, ip_address, method, path, status, body_bytes)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fingerprint,
                environment,
                occurred.astimezone(timezone.utc).isoformat(),
                ip.compressed,
                match.group("method")[:16],
                path[:2048],
                status,
                int(match.group("bytes")),
            ),
        )

    def finish_batch(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        self.connection.execute("DELETE FROM legal_requests WHERE occurred_at < ?", (cutoff,))
        self.connection.commit()

    def legal_users(self, environment: str, start: datetime, end: datetime) -> list[str]:
        rows = self.connection.execute(
            """SELECT DISTINCT ip_address FROM legal_requests
               WHERE environment=? AND occurred_at>=? AND occurred_at<?""",
            (environment, start.isoformat(), end.isoformat()),
        )
        return [str(row[0]) for row in rows]

    def geo(self, ip: str) -> tuple[str, str] | None:
        row = self.connection.execute(
            "SELECT country, city FROM geo_cache WHERE ip_address=?",
            (ip,),
        ).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def save_geo(self, ip: str, country: str, city: str) -> None:
        self.connection.execute(
            """INSERT INTO geo_cache(ip_address, country, city, updated_at)
               VALUES(?, ?, ?, ?)
               ON CONFLICT(ip_address) DO UPDATE SET
                 country=excluded.country, city=excluded.city, updated_at=excluded.updated_at""",
            (ip, country, city, datetime.now(timezone.utc).isoformat()),
        )
        self.connection.commit()

    def was_sent(self, key: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM sent_reports WHERE report_key=?",
            (key,),
        ).fetchone() is not None

    def mark_sent(self, key: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO sent_reports(report_key, sent_at) VALUES(?, ?)",
            (key, datetime.now(timezone.utc).isoformat()),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


def auth_header(username: str, password: str) -> str:
    value = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {value}"


def request_bytes(url: str, username: str, password: str, *, method: str = "GET", offset: int | None = None) -> tuple[int, bytes, dict[str, str]]:
    headers = {
        "Authorization": auth_header(username, password),
        "User-Agent": "FrontierCloud-RN-Reporter/1.0",
    }
    if offset is not None:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.status, response.read(), {key.lower(): value for key, value in response.headers.items()}


def collect_access_log(store: Store, source: Source) -> int:
    offset, partial = store.state(source.name)
    _, _, head = request_bytes(
        source.access_log_url,
        source.metrics_user,
        source.metrics_password,
        method="HEAD",
    )
    size = int(head.get("content-length", "0"))
    if size < offset:
        offset, partial = 0, ""
    if size == offset:
        return 0
    status, payload, _ = request_bytes(
        source.access_log_url,
        source.metrics_user,
        source.metrics_password,
        offset=offset,
    )
    if status == 200 and offset:
        offset, partial = 0, ""
    text = partial + payload.decode("utf-8", "replace")
    lines = text.splitlines(keepends=True)
    partial = ""
    if lines and not lines[-1].endswith(("\n", "\r")):
        partial = lines.pop()
    accepted = 0
    for raw in lines:
        line = raw.rstrip("\r\n")
        match = LOG_PATTERN.match(line)
        if match:
            store.add_request(source.name, line, match)
            accepted += 1
    store.finish_batch()
    store.save_state(source.name, offset + len(payload), partial)
    return accepted


def json_request(url: str, username: str = "", password: str = "") -> Any:
    headers = {"User-Agent": "FrontierCloud-RN-Reporter/1.0"}
    if username:
        headers["Authorization"] = auth_header(username, password)
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as response:
        return json.load(response)


def prometheus_query(source: Source, query: str, at: datetime) -> float | None:
    url = source.prometheus_url + "/api/v1/query?" + urllib.parse.urlencode({
        "query": query,
        "time": at.timestamp(),
    })
    payload = json_request(url, source.prometheus_user, source.prometheus_password)
    results = payload.get("data", {}).get("result", [])
    if not results:
        return None
    try:
        return float(results[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def prometheus_range(source: Source, query: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    url = source.prometheus_url + "/api/v1/query_range?" + urllib.parse.urlencode({
        "query": query,
        "start": start.timestamp(),
        "end": end.timestamp(),
        "step": 3600,
    })
    payload = json_request(url, source.prometheus_user, source.prometheus_password)
    results = payload.get("data", {}).get("result", [])
    values = results[0].get("values", []) if results else []
    return [
        (datetime.fromtimestamp(float(timestamp), timezone.utc), float(value))
        for timestamp, value in values
        if value not in {"NaN", "+Inf", "-Inf"}
    ]


def summarize_series(values: list[tuple[datetime, float]]) -> tuple[float | None, float | None]:
    numbers = [value for _, value in values]
    return ((sum(numbers) / len(numbers)), max(numbers)) if numbers else (None, None)


def daily_peaks(values: list[tuple[datetime, float]], target_timezone: ZoneInfo) -> list[tuple[datetime, float]]:
    peaks: dict[str, tuple[datetime, float]] = {}
    for timestamp, value in values:
        local = timestamp.astimezone(target_timezone)
        key = local.date().isoformat()
        if key not in peaks or value > peaks[key][1]:
            peaks[key] = (local, value)
    return [peaks[key] for key in sorted(peaks)]


def value_changes(values: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    changes = []
    previous: float | None = None
    for timestamp, value in values:
        if previous is not None and value != previous:
            changes.append((timestamp, value))
        previous = value
    return changes


def security_summary(source: Source) -> dict[str, Any]:
    separator = "&" if "?" in source.security_url else "?"
    return json_request(
        source.security_url + separator + "days=7",
        source.metrics_user,
        source.metrics_password,
    )


def localized_name(record: dict[str, Any], key: str, fallback: str) -> str:
    names = record.get(key, {}).get("names", {})
    return names.get("zh-CN") or names.get("en") or fallback


def location_for(store: Store, reader: Any, ip: str) -> tuple[str, str]:
    cached = store.geo(ip)
    if cached:
        return cached
    record = reader.get(ip)
    if record:
        country = localized_name(record, "country", "未知国家")
        city = localized_name(record, "city", "未知城市")
    else:
        country, city = "未知国家", "未知城市"
    store.save_geo(ip, country, city)
    return country, city


def format_number(value: float | None, suffix: str = "") -> str:
    return "无数据" if value is None else f"{value:.1f}{suffix}"


def environment_report(config: Config, store: Store, reader: Any, source: Source, start: datetime, end: datetime) -> str:
    period = "7d"
    cpu = prometheus_range(
        source,
        f'100-(avg(rate(node_cpu_seconds_total{{job="{source.job_prefix}-node",mode="idle"}}[5m]))*100)',
        start,
        end,
    )
    memory = prometheus_range(
        source,
        f'(1-node_memory_MemAvailable_bytes{{job="{source.job_prefix}-node"}}/node_memory_MemTotal_bytes{{job="{source.job_prefix}-node"}})*100',
        start,
        end,
    )
    request_rate = prometheus_range(
        source,
        "sum(rate(frontiercloud_http_requests_total[5m]))",
        start,
        end,
    )
    web_start_time = prometheus_range(
        source,
        'max(container_start_time_seconds{container_label_com_docker_compose_service="web"})',
        start,
        end,
    )
    cpu_avg, cpu_peak = summarize_series(cpu)
    memory_avg, memory_peak = summarize_series(memory)
    requests = prometheus_query(source, f"sum(increase(frontiercloud_http_requests_total[{period}]))", end)
    errors = prometheus_query(source, f'sum(increase(frontiercloud_http_requests_total{{status=~"5.."}}[{period}]))', end)
    availability = prometheus_query(source, f"avg(avg_over_time(probe_success[{period}]))*100", end)
    p95 = prometheus_query(
        source,
        f"histogram_quantile(0.95,sum by(le)(rate(frontiercloud_http_request_duration_seconds_bucket[{period}])))",
        end,
    )
    received = prometheus_query(
        source,
        f'sum(increase(node_network_receive_bytes_total{{job="{source.job_prefix}-node",device!~"lo|veth.*|docker.*|br-.*"}}[{period}]))',
        end,
    )
    transmitted = prometheus_query(
        source,
        f'sum(increase(node_network_transmit_bytes_total{{job="{source.job_prefix}-node",device!~"lo|veth.*|docker.*|br-.*"}}[{period}]))',
        end,
    )
    security = security_summary(source)
    security_addresses = security.get("addresses", [])
    malicious = {item["ip"] for item in security.get("addresses", [])}
    legal_ips = [ip for ip in store.legal_users(source.name, start, end) if ip not in malicious]
    locations = Counter(location_for(store, reader, ip) for ip in legal_ips)
    city_lines = [
        f"  {html.escape(country)}/{html.escape(city)}: {count}"
        for (country, city), count in locations.most_common(8)
    ] or ["  暂无完整日志数据"]
    ban_lines = []
    for item in security_addresses[:12]:
        kind = "新增" if item.get("is_new") else "重复"
        state = "仍封禁" if item.get("active") else "已解除/过期"
        ban_lines.append(
            f"  <code>{html.escape(item['ip'])}</code> · {kind} · {item['event_count']}次 · {state}"
        )
    if not ban_lines:
        ban_lines.append("  本周无封禁事件")
    elif len(security_addresses) > 12:
        ban_lines.append(f"  另有 {len(security_addresses) - 12} 个恶意 IP，已省略明细")
    peaks = daily_peaks(request_rate, config.timezone)
    peak_lines = [
        f"  {timestamp.astimezone(config.timezone):%m-%d %H:00} · {value:.2f} req/s"
        for timestamp, value in peaks
    ] or ["  无数据"]
    rollout_events = value_changes(web_start_time)
    rollout_lines = [
        f"  {timestamp.astimezone(config.timezone):%m-%d %H:00} · web 容器启动"
        for timestamp, _ in rollout_events
    ] or ["  未观察到 web 容器更替"]
    new_malicious = sum(1 for item in security_addresses if item.get("is_new"))
    repeated_malicious = len(security_addresses) - new_malicious
    automatic_bans = sum(int(item.get("automatic_count", 0)) for item in security_addresses)
    manual_bans = sum(int(item.get("manual_count", 0)) for item in security_addresses)
    error_rate = (errors / requests * 100) if errors is not None and requests else None
    network_gib = lambda value: None if value is None else value / (1024 ** 3)
    return "\n".join([
        f"<b>{html.escape(source.name)}</b>",
        f"可用率: {format_number(availability, '%')}",
        f"请求量: {format_number(requests)} · 5xx: {format_number(errors)} ({format_number(error_rate, '%')})",
        f"P95 延迟: {format_number(None if p95 is None else p95 * 1000, 'ms')}",
        f"CPU 平均/峰值: {format_number(cpu_avg, '%')} / {format_number(cpu_peak, '%')}",
        f"内存平均/峰值: {format_number(memory_avg, '%')} / {format_number(memory_peak, '%')}",
        f"网络入/出: {format_number(network_gib(received), ' GiB')} / {format_number(network_gib(transmitted), ' GiB')}",
        f"合法用户: {len(legal_ips)} 个独立公网 IP",
        "城市分布:",
        *city_lines,
        f"恶意 IP: {security.get('unique_ip_count', 0)} · 封禁事件: {security.get('event_count', 0)} · 当前封禁: {security.get('active_ban_count', 0)}",
        f"新增/重复恶意 IP: {new_malicious}/{repeated_malicious} · 自动/人工封禁: {automatic_bans}/{manual_bans}",
        *ban_lines,
        "每日业务高峰:",
        *peak_lines,
        f"部署观察: web 容器更替 {len(rollout_events)} 次（含重启）",
        *rollout_lines,
    ])


def github_deployments(repository: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    url = f"https://api.github.com/repos/{repository}/deployments?" + urllib.parse.urlencode({
        "environment": "rn-preproduction",
        "per_page": 100,
    })
    deployments = json_request(url)
    result = []
    for item in deployments:
        created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
        if start <= created < end:
            statuses = json_request(item["statuses_url"])
            latest = statuses[0] if statuses else {}
            item["deployment_state"] = latest.get("state", "unknown")
            item["deployment_description"] = latest.get("description") or "无状态说明"
            result.append(item)
    return result


def ensure_city_database(path: Path, now: datetime) -> None:
    current_month = (now.year, now.month)
    if path.is_file():
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if (modified.year, modified.month) == current_month:
            return
    url = f"https://download.db-ip.com/free/dbip-city-lite-{now.year}-{now.month:02d}.mmdb.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".mmdb.tmp")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "FrontierCloud-RN-Reporter/1.0"})
        with urllib.request.urlopen(request, timeout=120) as response, gzip.GzipFile(fileobj=response) as source, temporary.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        with maxminddb.open_database(temporary) as reader:
            if not reader.metadata().database_type:
                raise RuntimeError("downloaded city database has no database type")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        if not path.is_file():
            raise


def build_report(config: Config, store: Store, end: datetime) -> list[str]:
    start = end - timedelta(days=7)
    ensure_city_database(config.geoip_path, end)
    with maxminddb.open_database(config.geoip_path) as reader:
        sections = []
        for source in config.sources:
            try:
                sections.append(environment_report(config, store, reader, source, start, end))
            except Exception as exc:
                sections.append(
                    f"<b>{html.escape(source.name)}</b>\n"
                    f"数据汇总暂不可用 ({html.escape(type(exc).__name__)})"
                )
    try:
        deployments = github_deployments(config.github_repository, start, end)
    except Exception:
        deployments = []
    deployment_lines = [
        f"  {item['created_at'][5:16].replace('T', ' ')} · "
        f"dev@<code>{item['sha'][:7]}</code> · {html.escape(item['deployment_state'])} · "
        f"{html.escape(item['deployment_description'][:80])}"
        for item in deployments[:12]
    ] or ["  本周无预发布部署"]
    header = (
        "📊 <b>FrontierCloud 每周运营报告</b>\n"
        f"周期: {start.astimezone(config.timezone):%Y-%m-%d %H:%M} — "
        f"{end.astimezone(config.timezone):%Y-%m-%d %H:%M}\n"
        f"预发布部署: {len(deployments)} 次\n"
        f'<a href="https://db-ip.com">IP Geolocation by DB-IP</a>\n' + "\n".join(deployment_lines)
    )
    return [header, *sections]


def send_telegram(config: Config, message: str) -> None:
    if len(message) > 4000:
        message = message[:4000].rsplit("\n", 1)[0] + "\n…内容已截断"
    payload = urllib.parse.urlencode({
        "chat_id": config.telegram_chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{config.telegram_token}/sendMessage",
        data=payload,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the weekly report")


def report_end(now: datetime) -> datetime:
    local = now
    monday = local - timedelta(days=local.weekday())
    return monday.replace(hour=9, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect bounded access data and send weekly reports")
    parser.add_argument("--once", action="store_true", help="collect and immediately build one report")
    parser.add_argument("--dry-run", action="store_true", help="print the report instead of sending it")
    args = parser.parse_args()
    config = Config.load()
    store = Store(config.database_path)

    while True:
        for source in config.sources:
            try:
                count = collect_access_log(store, source)
                print(f"collected environment={source.name} accepted_lines={count}", flush=True)
            except (OSError, RuntimeError, urllib.error.URLError, ValueError) as exc:
                print(f"collection failed environment={source.name} error={exc}", flush=True)

        now = datetime.now(config.timezone)
        due = now.weekday() == 0 and now.hour >= 9
        if args.once or due:
            end = report_end(now)
            key = end.strftime("%Y-%m-%d")
            if args.once or not store.was_sent(key):
                messages = build_report(config, store, end)
                for message in messages:
                    print(message, flush=True) if args.dry_run else send_telegram(config, message)
                if not args.dry_run:
                    store.mark_sent(key)
        if args.once:
            store.close()
            return 0
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
