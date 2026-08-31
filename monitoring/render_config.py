from __future__ import annotations

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


def write_runtime_file(path: Path, value: str, *, uid: int, gid: int) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value + "\n")
    os.chown(temporary, uid, gid)
    os.chmod(temporary, 0o400)
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
    uid: int = PROMETHEUS_UID,
    gid: int = PROMETHEUS_GID,
) -> None:
    value = (ROOT / "templates" / template_name).read_text(encoding="utf-8")
    for key, replacement in replacements.items():
        value = value.replace(f"__{key}__", replacement)
    if re.search(r"__[A-Z0-9_]+__", value):
        raise SystemExit(f"unresolved placeholder in {template_name}")
    output = ROOT / "generated" / output_name
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
    os.chown(temporary, uid, gid)
    os.chmod(temporary, 0o400)
    temporary.replace(output)


def main() -> None:
    values = load_env(ROOT / ".env")
    host = require(values, "PRODUCTION_METRICS_HOST")
    username = values.get("METRICS_BASIC_USER", "frontiercloud_monitor")
    monitoring_user = values.get("MONITORING_BASIC_USER", "frontier_observer")
    monitoring_password = require(values, "MONITORING_BASIC_PASSWORD")
    monitoring_password_hash = require(values, "MONITORING_BASIC_PASSWORD_HASH")
    monitoring_server_name = require(values, "MONITORING_SERVER_NAME")
    monitoring_https_port = values.get("MONITORING_HTTPS_PORT", "8443")
    chat_id = require(values, "TG_CHAT_ID")
    if not re.fullmatch(r"[A-Za-z0-9.-]+(?::\d+)?", host):
        raise SystemExit("PRODUCTION_METRICS_HOST is invalid")
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

    (ROOT / "generated").mkdir(mode=0o711, exist_ok=True)
    (ROOT / "secrets").mkdir(mode=0o711, exist_ok=True)
    os.chmod(ROOT / "generated", 0o711)
    os.chmod(ROOT / "secrets", 0o711)
    write_runtime_file(
        ROOT / "secrets" / "metrics_password",
        require(values, "METRICS_BASIC_PASSWORD"),
        uid=PROMETHEUS_UID,
        gid=PROMETHEUS_GID,
    )
    write_runtime_file(
        ROOT / "secrets" / "tg_bot_token",
        require(values, "TG_BOT_TOKEN"),
        uid=PROMETHEUS_UID,
        gid=PROMETHEUS_GID,
    )
    write_runtime_file(
        ROOT / "secrets" / "grafana_admin_password",
        require(values, "GRAFANA_ADMIN_PASSWORD"),
        uid=GRAFANA_UID,
        gid=GRAFANA_GID,
    )
    write_runtime_file(
        ROOT / "secrets" / "monitoring_password",
        monitoring_password,
        uid=PROMETHEUS_UID,
        gid=PROMETHEUS_GID,
    )
    copy_runtime_file(
        Path(require(values, "MONITORING_TLS_CERT_PATH")),
        ROOT / "secrets" / "tls_fullchain.pem",
        uid=NGINX_UID,
        gid=NGINX_GID,
    )
    copy_runtime_file(
        Path(require(values, "MONITORING_TLS_KEY_PATH")),
        ROOT / "secrets" / "tls_privkey.pem",
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
        },
    )
    render("alertmanager.yml.template", "alertmanager.yml", {"TG_CHAT_ID": chat_id})
    web_replacements = {
        "MONITORING_BASIC_USER": monitoring_user,
        "MONITORING_BASIC_PASSWORD_HASH": monitoring_password_hash,
    }
    render("web.yml.template", "prometheus-web.yml", web_replacements)
    render("web.yml.template", "alertmanager-web.yml", web_replacements)
    render(
        "grafana-prometheus.yml.template",
        "grafana-prometheus.yml",
        {
            "MONITORING_BASIC_USER": monitoring_user,
            "MONITORING_BASIC_PASSWORD": monitoring_password,
        },
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
        uid=NGINX_UID,
        gid=NGINX_GID,
    )
    print("monitoring configuration rendered without printing secrets")


if __name__ == "__main__":
    main()
