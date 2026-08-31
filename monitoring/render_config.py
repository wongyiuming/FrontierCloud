from __future__ import annotations

import argparse
import os
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROMETHEUS_UID = 65534
PROMETHEUS_GID = 65534
GRAFANA_UID = 472
GRAFANA_GID = 0
NGINX_UID = 101
NGINX_GID = 101
REPORTER_GID = 10002


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def require(values: dict[str, str], name: str) -> str:
    value = values.get(name, "")
    if not value or value.startswith("REPLACE_WITH"):
        raise SystemExit(f"{name} is required")
    if "\n" in value or "\r" in value:
        raise SystemExit(f"{name} contains a newline")
    return value


def write_runtime_file(
    path: Path,
    value: str,
    *,
    uid: int,
    gid: int,
    mode: int = 0o400,
) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value + "\n")
    os.chown(temporary, uid, gid)
    os.chmod(temporary, mode)
    temporary.replace(path)


def copy_runtime_file(source: Path, destination: Path, *, uid: int, gid: int) -> None:
    if not source.is_absolute() or not source.is_file():
        raise SystemExit(f"runtime source is not a readable absolute file: {source}")
    value = source.read_bytes()
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(value)
    os.chown(temporary, uid, gid)
    os.chmod(temporary, 0o400)
    temporary.replace(destination)


def render(
    template_name: str,
    output_name: str,
    replacements: dict[str, str],
    *,
    runtime_root: Path,
    uid: int = PROMETHEUS_UID,
    gid: int = PROMETHEUS_GID,
) -> None:
    value = (ROOT / "templates" / template_name).read_text(encoding="utf-8")
    for key, replacement in replacements.items():
        value = value.replace(f"__{key}__", replacement)
    if re.search(r"__[A-Z0-9_]+__", value):
        raise SystemExit(f"unresolved placeholder in {template_name}")
    output = runtime_root / "generated" / output_name
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
    os.chown(temporary, uid, gid)
    os.chmod(temporary, 0o400)
    temporary.replace(output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render one isolated monitoring instance")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--runtime-dir", type=Path, default=ROOT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    env_file = args.env_file.expanduser().resolve()
    runtime_root = args.runtime_dir.expanduser().resolve()
    values = load_env(env_file)
    host = require(values, "PRODUCTION_METRICS_HOST")
    monitored_environment = require(values, "MONITORED_ENVIRONMENT")
    monitoring_role = require(values, "MONITORING_ROLE")
    target_job_prefix = require(values, "TARGET_JOB_PREFIX")
    username = values.get("METRICS_BASIC_USER", "frontiercloud_monitor")
    monitoring_user = values.get("MONITORING_BASIC_USER", "frontier_observer")
    monitoring_password = require(values, "MONITORING_BASIC_PASSWORD")
    monitoring_password_hash = require(values, "MONITORING_BASIC_PASSWORD_HASH")
    monitoring_server_name = require(values, "MONITORING_SERVER_NAME")
    monitoring_https_port = values.get("MONITORING_HTTPS_PORT", "8443")
    chat_id = require(values, "TG_CHAT_ID")
    if not re.fullmatch(r"[A-Za-z0-9.-]+(?::\d+)?", host):
        raise SystemExit("PRODUCTION_METRICS_HOST is invalid")
    for name, value in (
        ("MONITORED_ENVIRONMENT", monitored_environment),
        ("MONITORING_ROLE", monitoring_role),
        ("TARGET_JOB_PREFIX", target_job_prefix),
    ):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
            raise SystemExit(f"{name} is invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
        raise SystemExit("METRICS_BASIC_USER is invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", monitoring_user):
        raise SystemExit("MONITORING_BASIC_USER is invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", monitoring_password):
        raise SystemExit("MONITORING_BASIC_PASSWORD must be a 32-128 character random base64url value")
    if not re.fullmatch(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}", monitoring_password_hash):
        raise SystemExit("MONITORING_BASIC_PASSWORD_HASH must be a bcrypt hash")
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", monitoring_server_name):
        raise SystemExit("MONITORING_SERVER_NAME is invalid")
    if not monitoring_https_port.isdigit() or not 1 <= int(monitoring_https_port) <= 65535:
        raise SystemExit("MONITORING_HTTPS_PORT must be a valid TCP port")
    if not re.fullmatch(r"-?\d{1,20}", chat_id):
        raise SystemExit("TG_CHAT_ID is invalid")

    (runtime_root / "generated").mkdir(parents=True, mode=0o711, exist_ok=True)
    (runtime_root / "secrets").mkdir(parents=True, mode=0o711, exist_ok=True)
    (runtime_root / "server.d").mkdir(parents=True, mode=0o755, exist_ok=True)
    os.chmod(runtime_root / "generated", 0o711)
    os.chmod(runtime_root / "secrets", 0o711)
    write_runtime_file(
        runtime_root / "secrets" / "metrics_password",
        require(values, "METRICS_BASIC_PASSWORD"),
        uid=PROMETHEUS_UID,
        gid=REPORTER_GID,
        mode=0o440,
    )
    write_runtime_file(
        runtime_root / "secrets" / "tg_bot_token",
        require(values, "TG_BOT_TOKEN"),
        uid=PROMETHEUS_UID,
        gid=REPORTER_GID,
        mode=0o440,
    )
    write_runtime_file(
        runtime_root / "secrets" / "grafana_admin_password",
        require(values, "GRAFANA_ADMIN_PASSWORD"),
        uid=GRAFANA_UID,
        gid=GRAFANA_GID,
    )
    write_runtime_file(
        runtime_root / "secrets" / "monitoring_password",
        monitoring_password,
        uid=PROMETHEUS_UID,
        gid=REPORTER_GID,
        mode=0o440,
    )
    copy_runtime_file(
        Path(require(values, "MONITORING_TLS_CERT_PATH")),
        runtime_root / "secrets" / "tls_fullchain.pem",
        uid=NGINX_UID,
        gid=NGINX_GID,
    )
    copy_runtime_file(
        Path(require(values, "MONITORING_TLS_KEY_PATH")),
        runtime_root / "secrets" / "tls_privkey.pem",
        uid=NGINX_UID,
        gid=NGINX_GID,
    )
    render(
        "prometheus.yml.template",
        "prometheus.yml",
        {
            "PRODUCTION_METRICS_HOST": host,
            "METRICS_BASIC_USER": username,
            "MONITORING_BASIC_USER": monitoring_user,
            "MONITORED_ENVIRONMENT": monitored_environment,
            "MONITORING_ROLE": monitoring_role,
            "TARGET_JOB_PREFIX": target_job_prefix,
        },
        runtime_root=runtime_root,
    )
    render(
        "alertmanager.yml.template",
        "alertmanager.yml",
        {"TG_CHAT_ID": chat_id},
        runtime_root=runtime_root,
    )
    web_replacements = {
        "MONITORING_BASIC_USER": monitoring_user,
        "MONITORING_BASIC_PASSWORD_HASH": monitoring_password_hash,
    }
    render("web.yml.template", "prometheus-web.yml", web_replacements, runtime_root=runtime_root)
    render("web.yml.template", "alertmanager-web.yml", web_replacements, runtime_root=runtime_root)
    render(
        "grafana-prometheus.yml.template",
        "grafana-prometheus.yml",
        {
            "MONITORING_BASIC_USER": monitoring_user,
            "MONITORING_BASIC_PASSWORD": monitoring_password,
        },
        runtime_root=runtime_root,
        uid=GRAFANA_UID,
        gid=GRAFANA_GID,
    )
    render(
        "../nginx/nginx.conf.template",
        "nginx.conf",
        {
            "MONITORING_SERVER_NAME": monitoring_server_name,
            "MONITORING_HTTPS_PORT": monitoring_https_port,
        },
        runtime_root=runtime_root,
        uid=NGINX_UID,
        gid=NGINX_GID,
    )
    print(f"monitoring configuration rendered for {monitored_environment} without printing secrets")


if __name__ == "__main__":
    main()
