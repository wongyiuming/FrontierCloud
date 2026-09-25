#!/usr/bin/env python3
"""Release agent: git + Docker Engine only, without the Compose CLI or systemd."""
from __future__ import annotations

import copy
import json
import os
import pathlib
import queue
import re
import socketserver
import subprocess
import threading
import time

import docker

ROOT = pathlib.Path("/workspace")
CONTROL_DIR = pathlib.Path("/run/frontiercloud-updater")
SOCKET_PATH = CONTROL_DIR / "control.sock"
STATUS_PATH = CONTROL_DIR / "status.json"
MAINTENANCE_DIR = pathlib.Path("/run/frontiercloud-maintenance")
MAINTENANCE_FLAG = MAINTENANCE_DIR / "enabled"
RELEASE_BRANCH = "main"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TASKS: queue.Queue[tuple[str, str, bool]] = queue.Queue(maxsize=1)
WRITE_LOCK = threading.Lock()
SERVICE_NAMES = {
    "web": "office_automation_web",
    "nginx": "office_automation_nginx",
}


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), message, flush=True)


def git(*args: str, check: bool = True, timeout: int = 120) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True,
        check=check, timeout=timeout,
    )
    if result.stderr.strip():
        log("git: " + result.stderr.strip().splitlines()[-1][:500])
    return result.stdout.strip()


def initial_sha() -> str:
    try:
        value = git("rev-parse", "HEAD", timeout=10)
        return value if SHA_RE.fullmatch(value) else ""
    except Exception:
        return ""


def read_status() -> dict:
    if not STATUS_PATH.exists():
        return {}
    try:
        value = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_status(**changes) -> dict:
    with WRITE_LOCK:
        current = read_status()
        current.update(changes)
        current["release_branch"] = RELEASE_BRANCH
        current["updated_at"] = int(time.time())
        CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        temporary = STATUS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, STATUS_PATH)
        return current


def maintenance(enabled: bool, target: str = "") -> None:
    MAINTENANCE_DIR.mkdir(parents=True, exist_ok=True)
    if enabled:
        MAINTENANCE_FLAG.write_text((target or "maintenance") + "\n", encoding="utf-8")
    else:
        MAINTENANCE_FLAG.unlink(missing_ok=True)


def client():
    value = docker.DockerClient(base_url="unix:///var/run/docker.sock")
    value.ping()
    return value


def service_container(engine, service: str):
    fixed = SERVICE_NAMES.get(service)
    if fixed:
        return engine.containers.get(fixed)
    rows = engine.containers.list(all=True, filters={"label": f"com.docker.compose.service={service}"})
    if len(rows) != 1:
        raise RuntimeError(f"expected exactly one {service} container, found {len(rows)}")
    return rows[0]


def snapshot(container) -> dict:
    container.reload()
    return copy.deepcopy(container.attrs)


def networking_config(api, snap: dict):
    endpoints = {}
    old_id = str(snap.get("Id") or "")
    for name, network in (snap.get("NetworkSettings", {}).get("Networks") or {}).items():
        aliases = []
        for alias in network.get("Aliases") or []:
            if alias and alias not in {old_id, old_id[:12]}:
                aliases.append(alias)
        endpoints[name] = api.create_endpoint_config(aliases=aliases or None)
    return api.create_networking_config(endpoints) if endpoints else None


def create_from_snapshot(engine, snap: dict, image: str):
    config = snap["Config"]
    name = snap.get("Name", "").lstrip("/")
    if not name:
        raise RuntimeError("container snapshot has no name")
    host_config = copy.deepcopy(snap.get("HostConfig") or {})
    host_config["AutoRemove"] = False
    exposed = list((config.get("ExposedPorts") or {}).keys())
    volumes = list((config.get("Volumes") or {}).keys())
    response = engine.api.create_container(
        image=image,
        command=config.get("Cmd"),
        hostname=config.get("Hostname") or None,
        user=config.get("User") or None,
        detach=True,
        environment=config.get("Env") or None,
        volumes=volumes or None,
        ports=exposed or None,
        name=name,
        entrypoint=config.get("Entrypoint"),
        working_dir=config.get("WorkingDir") or None,
        host_config=host_config,
        networking_config=networking_config(engine.api, snap),
        healthcheck=config.get("Healthcheck"),
        labels=config.get("Labels") or None,
        stop_signal=config.get("StopSignal") or None,
    )
    value = engine.containers.get(response["Id"])
    value.start()
    return value


def replace(engine, snap: dict, image: str):
    name = snap["Name"].lstrip("/")
    try:
        old = engine.containers.get(name)
        old.reload()
        if old.status == "running":
            old.stop(timeout=10)
        old.remove(force=True)
        return create_from_snapshot(engine, snap, image)
    except Exception:
        try:
            current = engine.containers.get(name)
            current.remove(force=True)
        except Exception:
            pass
        create_from_snapshot(engine, snap, snap["Image"])
        raise


def wait_completed(container, timeout: int = 120) -> None:
    result = container.wait(timeout=timeout)
    code = int(result.get("StatusCode", 1))
    if code != 0:
        logs = container.logs(stdout=True, stderr=True, tail=80).decode("utf-8", errors="replace")
        raise RuntimeError(f"{container.name} exited {code}: {logs[-2000:]}")


def wait_healthy(container, timeout: int = 240) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        container.reload()
        state = container.attrs.get("State") or {}
        health = (state.get("Health") or {}).get("Status")
        if state.get("Status") == "running" and health == "healthy":
            return
        if state.get("Status") in {"dead", "exited"} or health == "unhealthy":
            logs = container.logs(stdout=True, stderr=True, tail=100).decode("utf-8", errors="replace")
            raise RuntimeError(f"{container.name} failed health check: {logs[-2500:]}")
        time.sleep(2)
    raise TimeoutError(f"{container.name} did not become healthy")


def build(engine, target: str) -> tuple[str, str]:
    web_tag = f"frontiercloud-web:{target}"
    nginx_tag = f"frontiercloud-nginx:{target}"
    log(f"building web image {target}")
    engine.images.build(
        path=str(ROOT), dockerfile="Dockerfile", tag=web_tag, rm=True, forcerm=True,
        labels={"frontiercloud.revision": target},
    )
    log(f"building nginx image {target}")
    engine.images.build(
        path=str(ROOT), dockerfile="nginx/Dockerfile", tag=nginx_tag, rm=True, forcerm=True,
        labels={"frontiercloud.revision": target},
    )
    return web_tag, nginx_tag


def validate_target(target: str, mode: str) -> None:
    if not SHA_RE.fullmatch(target):
        raise RuntimeError("target must be a full commit SHA")
    if git("status", "--porcelain", "--untracked-files=no", timeout=20):
        raise RuntimeError("tracked working tree changes block release")
    git("fetch", "--no-tags", "origin", RELEASE_BRANCH, timeout=120)
    main_head = git("rev-parse", f"origin/{RELEASE_BRANCH}", timeout=20)
    git("cat-file", "-e", f"{target}^{{commit}}", timeout=20)
    if mode == "upgrade" and target != main_head:
        raise RuntimeError("upgrade target is not the current origin/main head")
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", target, f"origin/{RELEASE_BRANCH}"],
        cwd=ROOT, check=False, timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError("target is not part of origin/main history")


def restore_local(engine, snapshots: dict[str, dict], old_sha: str) -> None:
    log("restoring previous local containers after failed local replacement")
    for service in ("secrets-init", "media-init", "web", "nginx"):
        snap = snapshots.get(service)
        if not snap:
            continue
        try:
            restored = replace(engine, snap, snap["Image"])
            if service in {"secrets-init", "media-init"}:
                wait_completed(restored)
            elif service == "web":
                wait_healthy(restored)
            elif service == "nginx":
                restored.exec_run(["nginx", "-t"], demux=True)
        except Exception as exc:
            log(f"restore {service} failed: {type(exc).__name__}: {exc}")
    if old_sha and SHA_RE.fullmatch(old_sha):
        try:
            git("reset", "--hard", old_sha, timeout=60)
        except Exception as exc:
            log(f"git restore failed: {type(exc).__name__}: {exc}")


def perform(target: str, mode: str, hold_maintenance: bool) -> None:
    current = read_status()
    old_sha = str(current.get("current_sha") or initial_sha())
    previous_sha = str(current.get("previous_sha") or "")
    started = int(time.time())
    write_status(
        state="running", phase="validating", mode=mode, target_sha=target,
        current_sha=old_sha, previous_sha=previous_sha, detail="", started_at=started,
    )
    maintenance(True, target)
    engine = None
    snapshots: dict[str, dict] = {}
    local_replaced = False
    try:
        validate_target(target, mode)
        engine = client()
        if target != old_sha:
            write_status(phase="building")
            git("reset", "--hard", target, timeout=60)
            web_image, nginx_image = build(engine, target)
            for service in ("secrets-init", "media-init", "web", "nginx"):
                try:
                    snapshots[service] = snapshot(service_container(engine, service))
                except Exception:
                    if service in {"web", "nginx"}:
                        raise
            write_status(phase="replacing")
            for service in ("secrets-init", "media-init"):
                if service not in snapshots:
                    continue
                value = replace(engine, snapshots[service], web_image)
                wait_completed(value)
            web = replace(engine, snapshots["web"], web_image)
            wait_healthy(web)
            nginx = replace(engine, snapshots["nginx"], nginx_image)
            result = nginx.exec_run(["nginx", "-t"], demux=True)
            if result.exit_code != 0:
                raise RuntimeError("nginx configuration check failed")
            local_replaced = True
            previous_sha = old_sha if mode == "upgrade" else ""
            write_status(current_sha=target, previous_sha=previous_sha)
        else:
            web = service_container(engine, "web")
            local_replaced = True

        if hold_maintenance:
            write_status(state="distributing", phase="distributing")
            result = web.exec_run(
                ["python", "-m", "app.services.cluster_update_coordinator", target, mode],
                demux=True,
            )
            if result.exit_code != 0:
                stdout, stderr = result.output if isinstance(result.output, tuple) else (b"", result.output or b"")
                detail = ((stderr or b"") + b"\n" + (stdout or b"")).decode("utf-8", errors="replace")
                raise RuntimeError("cluster convergence failed: " + detail[-2500:])

        maintenance(False)
        write_status(state="success", phase="complete", current_sha=target,
                     previous_sha=previous_sha, detail="", completed_at=int(time.time()))
        log(f"release complete: {target} ({mode})")
    except BaseException as exc:
        if engine is not None and not local_replaced and snapshots:
            restore_local(engine, snapshots, old_sha)
        write_status(state="failed", phase="failed", detail=f"{type(exc).__name__}: {exc}")
        log(f"release failed; maintenance remains enabled: {type(exc).__name__}: {exc}")
    finally:
        if engine is not None:
            try:
                engine.close()
            except Exception:
                pass


def worker() -> None:
    while True:
        target, mode, hold = TASKS.get()
        try:
            perform(target, mode, hold)
        finally:
            TASKS.task_done()


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            request = json.loads(self.rfile.readline(8192))
            action = str(request.get("action") or "status")
            if action == "status":
                response = {"ok": True, "status": read_status()}
            elif action == "start":
                target = str(request.get("target_sha") or "")
                mode = str(request.get("mode") or "upgrade")
                hold = bool(request.get("hold_maintenance", False))
                if not SHA_RE.fullmatch(target) or mode not in {"upgrade", "rollback"}:
                    raise ValueError("invalid release request")
                current = read_status()
                if current.get("state") in {"queued", "running", "distributing"}:
                    response = {"ok": False, "reason": "release already running", "status": current}
                else:
                    TASKS.put_nowait((target, mode, hold))
                    write_status(state="queued", phase="queued", target_sha=target, mode=mode, detail="")
                    response = {"ok": True, "accepted": True, "target_sha": target, "mode": mode}
            else:
                raise ValueError("unknown updater action")
        except queue.Full:
            response = {"ok": False, "reason": "release queue is full"}
        except Exception as exc:
            response = {"ok": False, "reason": str(exc)}
        self.wfile.write((json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode())


def main() -> None:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    MAINTENANCE_DIR.mkdir(parents=True, exist_ok=True)
    SOCKET_PATH.unlink(missing_ok=True)
    current = read_status()
    if not current:
        write_status(state="idle", phase="idle", current_sha=initial_sha(), previous_sha="", detail="")
    else:
        write_status()
    threading.Thread(target=worker, name="frontiercloud-release-worker", daemon=True).start()
    with socketserver.ThreadingUnixStreamServer(str(SOCKET_PATH), Handler) as server:
        os.chmod(SOCKET_PATH, 0o666)
        log("release control socket ready")
        server.serve_forever()


if __name__ == "__main__":
    main()
