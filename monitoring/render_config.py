from __future__ import annotations

import os
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROMETHEUS_UID = 65534
PROMETHEUS_GID = 65534
GRAFANA_UID = 472
GRAFANA_GID = 0


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


def render(template_name: str, output_name: str, replacements: dict[str, str]) -> None:
    value = (ROOT / "templates" / template_name).read_text(encoding="utf-8")
    for key, replacement in replacements.items():
        value = value.replace(f"__{key}__", replacement)
    if re.search(r"__[A-Z0-9_]+__", value):
        raise SystemExit(f"unresolved placeholder in {template_name}")
    output = ROOT / "generated" / output_name
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
    os.chown(temporary, PROMETHEUS_UID, PROMETHEUS_GID)
    os.chmod(temporary, 0o400)
    temporary.replace(output)


def main() -> None:
    values = load_env(ROOT / ".env")
    host = require(values, "PRODUCTION_METRICS_HOST")
    username = require(values, "METRICS_BASIC_USER")
    chat_id = require(values, "TG_CHAT_ID")
    if not re.fullmatch(r"[A-Za-z0-9.-]+(?::\d+)?", host):
        raise SystemExit("PRODUCTION_METRICS_HOST is invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
        raise SystemExit("METRICS_BASIC_USER is invalid")
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
    render(
        "prometheus.yml.template",
        "prometheus.yml",
        {"PRODUCTION_METRICS_HOST": host, "METRICS_BASIC_USER": username},
    )
    render("alertmanager.yml.template", "alertmanager.yml", {"TG_CHAT_ID": chat_id})
    print("monitoring configuration rendered without printing secrets")


if __name__ == "__main__":
    main()
