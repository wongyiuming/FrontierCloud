#!/usr/bin/env python3
"""Serialized host-Docker updater controlled through a private Unix socket."""
from __future__ import annotations

import json
import os
import pathlib
import queue
import re
import socketserver
import subprocess
import threading
import time

ROOT = pathlib.Path("/workspace")
CONTROL_DIR = pathlib.Path("/run/frontiercloud-updater")
SOCKET_PATH = CONTROL_DIR / "control.sock"
STATUS_PATH = CONTROL_DIR / "status.json"
MAINTENANCE_DIR = pathlib.Path("/run/frontiercloud-maintenance")
MAINTENANCE_FLAG = MAINTENANCE_DIR / "enabled"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
UPDATER_IMAGE = "frontiercloud-updater"
TASKS: queue.Queue[tuple[str, bool]] = queue.Queue(maxsize=1)


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), message, flush=True)


def run(*args: str, cwd: pathlib.Path = ROOT, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    log("run: " + " ".join(args))
    return subprocess.run(args, cwd=cwd, text=True, check=True, timeout=timeout)


def write_status(state: str, version: str, *, detail: str = "", propagate: bool = False) -> None:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "state": state,
        "version": version,
        "detail": detail[:1000],
        "propagate": propagate,
        "updated_at": int(time.time()),
    }, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, STATUS_PATH)


def host_project_dir() -> str:
    container = os.environ.get("HOSTNAME", "")
    if not container:
        raise RuntimeError("Updater container identity is unavailable")
    result = subprocess.run([
        "docker", "inspect", container,
        "--format", '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{.Source}}{{end}}{{end}}',
    ], text=True, check=True, capture_output=True, timeout=20)
    source = result.stdout.strip()
    if not source.startswith("/"):
        raise RuntimeError("Cannot resolve host project directory")
    return source


def compose_runner(project: str, *compose_args: str, timeout: int = 600) -> None:
    command = [
        "docker", "run", "--rm",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", f"{project}:{project}",
        "-w", project,
        UPDATER_IMAGE,
        "docker", "compose", *compose_args,
    ]
    run(*command, timeout=timeout)


def perform_update(version: str, propagate: bool) -> None:
    write_status("running", version, propagate=propagate)
    MAINTENANCE_DIR.mkdir(parents=True, exist_ok=True)
    MAINTENANCE_FLAG.write_text(version + "\n", encoding="utf-8")
    try:
        run("git", "fetch", "--no-tags", "origin", "dev", timeout=120)
        run("git", "cat-file", "-e", f"{version}^{{commit}}", timeout=20)
        run("git", "merge-base", "--is-ancestor", version, "origin/dev", timeout=20)
        run("git", "reset", "--hard", version, timeout=60)

        project = host_project_dir()
        compose_runner(project, "config", "--quiet", timeout=60)
        compose_runner(
            project,
            "up", "-d", "--build", "--wait", "--wait-timeout", "240",
            "secrets-init", "media-init", "mysql", "redis", "web", "nginx", "stun",
            timeout=420,
        )
        compose_runner(project, "exec", "-T", "nginx", "nginx", "-t", timeout=30)
        compose_runner(project, "exec", "-T", "web", "python", "-m", "app.services.health_probe", timeout=30)

        if propagate:
            compose_runner(
                project, "exec", "-T", "web", "python", "-m", "app.services.cluster_update", version,
                timeout=600,
            )

        write_status("success", version, propagate=propagate)
        MAINTENANCE_FLAG.unlink(missing_ok=True)
        log(f"update complete: {version}")
    except BaseException as exc:
        write_status("failed", version, detail=f"{type(exc).__name__}: {exc}", propagate=propagate)
        log(f"update failed; maintenance remains enabled: {type(exc).__name__}: {exc}")


def worker() -> None:
    while True:
        version, propagate = TASKS.get()
        try:
            perform_update(version, propagate)
        finally:
            TASKS.task_done()


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            request = json.loads(self.rfile.readline(4096))
            version = str(request.get("version", ""))
            propagate = bool(request.get("propagate", False))
            if not SHA_RE.fullmatch(version):
                raise ValueError("version must be a full 40-character commit SHA")
            current = json.loads(STATUS_PATH.read_text(encoding="utf-8")) if STATUS_PATH.exists() else {}
            if current.get("state") == "running":
                response = {"accepted": False, "reason": "update already running", "status": current}
            else:
                TASKS.put_nowait((version, propagate))
                write_status("queued", version, propagate=propagate)
                response = {"accepted": True, "version": version, "propagate": propagate}
        except queue.Full:
            response = {"accepted": False, "reason": "update queue is full"}
        except Exception as exc:
            response = {"accepted": False, "reason": str(exc)}
        self.wfile.write((json.dumps(response, separators=(",", ":")) + "\n").encode())


def main() -> None:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    MAINTENANCE_DIR.mkdir(parents=True, exist_ok=True)
    SOCKET_PATH.unlink(missing_ok=True)
    threading.Thread(target=worker, name="cluster-updater", daemon=True).start()
    with socketserver.ThreadingUnixStreamServer(str(SOCKET_PATH), Handler) as server:
        os.chmod(SOCKET_PATH, 0o666)
        log("updater control socket ready")
        server.serve_forever()


if __name__ == "__main__":
    main()
