#!/usr/bin/env python3
"""Release agent: git + Docker Engine only, without the Compose CLI or systemd."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import queue
import re
import socketserver
import subprocess
import sys
import threading
import tempfile
import time

import docker

# The updater runs as an absolute script; import only the pure protocol/proof
# modules, never application configuration, SQL drivers or runtime startup.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.services.federation import protocol as protocol
from app.services.federation import release_manifest as manifests
from updater.release_evidence import verify_artifact

ROOT = pathlib.Path("/workspace")
CONTROL_DIR = pathlib.Path("/run/frontiercloud-updater")
SOCKET_PATH = CONTROL_DIR / "control.sock"
STATUS_PATH = CONTROL_DIR / "status.json"
REPLACEMENT_PATH = CONTROL_DIR / "replacement.json"
MAINTENANCE_DIR = pathlib.Path("/run/frontiercloud-maintenance")
MAINTENANCE_FLAG = MAINTENANCE_DIR / "enabled"
UPDATER_DATA_DIRECTORY = pathlib.Path(os.environ.get("UPDATER_DATA_DIRECTORY") or str(ROOT / "data"))
FORCE_OPEN_FLAG = UPDATER_DATA_DIRECTORY / ".frontiercloud-force-open"
RELEASE_BRANCH = "main"
UPDATER_PROJECT = os.environ.get("UPDATER_PROJECT", "").strip()
RELEASE_MANIFEST_VERSION = 1
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_IMAGE_TAG_RE = re.compile(r"^frontiercloud-(?:web|nginx):([0-9a-f]{40})(?:-([0-9a-f]{12}))?$")
TASKS: queue.Queue[tuple[str, str, bool, dict | None]] = queue.Queue(maxsize=1)
WRITE_LOCK = threading.Lock()
START_LOCK = threading.Lock()
SERVER_ACTIVE = False
RUNTIME_SHA = ""
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
        descriptor, name = tempfile.mkstemp(prefix=".status-", dir=CONTROL_DIR)
        temporary = pathlib.Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(current, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, STATUS_PATH)
            if os.name != "nt":
                directory = os.open(CONTROL_DIR, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return current


def manifest_changes(current: dict, manifest: dict | None, target: str, mode: str) -> dict:
    """Joint release history is independent of this implementation's SHA history."""
    changes = {}
    previous = current.get("current_manifest")
    if manifest is not None:
        manifest = manifests.parse(protocol.canonical(manifest))
        if previous is None or manifests.identifier(previous) != manifests.identifier(manifest):
            changes["previous_manifest"] = copy.deepcopy(previous) if mode == "upgrade" else None
            changes["current_manifest"] = manifest
    elif target != current.get("current_sha") and previous is not None:
        changes["previous_manifest"] = copy.deepcopy(previous) if mode == "upgrade" else None
        changes["current_manifest"] = None
    return changes


def require_manifest_web(container) -> None:
    container.reload()
    labels = (container.image.attrs.get("Config") or {}).get("Labels") or {}
    if labels.get("frontiercloud.release-manifest-version") != "1":
        raise ValueError("Web image lacks manifest capability")


def validate_manifest_state(current: dict) -> None:
    for field, sha in (("target_manifest", "target_sha"), ("current_manifest", "current_sha"), ("previous_manifest", None)):
        value = current.get(field)
        if value is None:
            continue
        artifact = manifests.select(value, RELEASE_BRANCH, "dev")
        if sha is not None and artifact["commit_sha"] != current.get(sha):
            raise ValueError("Persisted manifest does not match local artifact")


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
    if service not in {"web", "nginx", "secrets-init", "media-init"}:
        raise RuntimeError("unsupported reference service")
    if UPDATER_PROJECT:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", UPDATER_PROJECT):
            raise RuntimeError("invalid reference project identity")
        rows = engine.containers.list(all=True, filters={"label": [
            f"com.docker.compose.project={UPDATER_PROJECT}",
            f"com.docker.compose.service={service}",
        ]})
        if len(rows) != 1:
            raise RuntimeError(f"expected exactly one project-owned {service} container")
        return rows[0]
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
        try:
            old = engine.containers.get(name)
        except docker.errors.NotFound:
            old = None
        if old is not None:
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


def wait_nginx_ready(container, timeout: int = 60) -> None:
    """Docker start precedes template rendering; nginx -t alone is not readiness."""
    deadline = time.monotonic() + timeout
    command = ["/bin/sh", "-c",
        'test -f /etc/nginx/runtime/public-listen.conf && nginx -t && '
        'pid=$(cat /var/run/nginx.pid) && kill -0 "$pid"']
    while time.monotonic() < deadline:
        container.reload()
        state = container.attrs.get("State") or {}
        if state.get("Status") in {"dead", "exited"}:
            raise RuntimeError("nginx release startup failed")
        if state.get("Status") == "running":
            try:
                result = container.exec_run(command, demux=True)
            except Exception:
                # A process can exit between inspect and exec. Reinspect rather
                # than accepting start acknowledgement or a default config.
                result = None
            if result is not None and result.exit_code == 0:
                return
        time.sleep(1)
    raise TimeoutError("nginx release readiness deadline reached")


def release_image_tag(target: str, component: str) -> str:
    suffix = "-" + hashlib.sha256(UPDATER_PROJECT.encode()).hexdigest()[:12] if UPDATER_PROJECT else ""
    return f"frontiercloud-{component}:{target}{suffix}"


def build(engine, target: str) -> tuple[str, str]:
    # The native checkout's root recipe is Go. Select the explicit reference
    # recipe from the exact target; old main history retains its root recipe.
    recipe = "Dockerfile.python"
    contents = git("show", f"{target}:{recipe}", check=False)
    if not contents:
        recipe = "Dockerfile"
        contents = git("show", f"{target}:{recipe}")
    if not re.search(r"(?m)^FROM python:[^\s]+", contents):
        raise RuntimeError("reviewed target has no Python reference runtime recipe")
    web_tag = release_image_tag(target,"web")
    nginx_tag = release_image_tag(target,"nginx")
    ownership = {}
    if UPDATER_PROJECT:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", UPDATER_PROJECT):
            raise RuntimeError("invalid updater project")
        ownership["frontiercloud.project"] = UPDATER_PROJECT
    log(f"building web image {target}")
    engine.images.build(
        path=str(ROOT), dockerfile=recipe, tag=web_tag, rm=True, forcerm=True,
        labels={**ownership, "frontiercloud.revision": target, "frontiercloud.component": "web"},
    )
    log(f"building nginx image {target}")
    engine.images.build(
        path=str(ROOT), dockerfile="nginx/Dockerfile", tag=nginx_tag, rm=True, forcerm=True,
        labels={**ownership, "frontiercloud.revision": target, "frontiercloud.component": "nginx"},
    )
    return web_tag, nginx_tag


def release_image_revision(image) -> str:
    labels = (getattr(image, "attrs", {}) or {}).get("Config", {}).get("Labels") or {}
    value = str(labels.get("frontiercloud.revision") or "")
    if SHA_RE.fullmatch(value):
        return value
    for tag in getattr(image, "tags", []) or []:
        matched = RELEASE_IMAGE_TAG_RE.fullmatch(str(tag))
        if matched:
            return matched.group(1)
    return ""


def cleanup_release_images(engine, keep: set[str]) -> None:
    if UPDATER_PROJECT and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", UPDATER_PROJECT):
        raise RuntimeError("invalid updater project")
    retained = {item for item in keep if SHA_RE.fullmatch(item)}
    try:
        # The SDK inspects every listed image before returning the collection.
        # A different project retiring a stale image can make global listing
        # fail after this node's healthy generation has already committed.
        # Filter on the full owner before those SDK inspections; collection
        # failure is optional cleanup, never release/convergence failure.
        images = engine.images.list(filters={"label": f"frontiercloud.project={UPDATER_PROJECT}"}) if UPDATER_PROJECT else engine.images.list()
    except Exception as exc:
        log(f"release image inventory skipped: {type(exc).__name__}")
        return
    for image in images:
        for raw_tag in getattr(image, "tags", []) or []:
            tag = str(raw_tag)
            matched = RELEASE_IMAGE_TAG_RE.fullmatch(tag)
            if not matched or matched.group(1) in retained:
                continue
            if matched.group(2) and (not UPDATER_PROJECT or matched.group(2) != hashlib.sha256(UPDATER_PROJECT.encode()).hexdigest()[:12]):
                continue
            if UPDATER_PROJECT:
                labels = (getattr(image, "attrs", {}) or {}).get("Config", {}).get("Labels") or {}
                component = tag.split(":", 1)[0].removeprefix("frontiercloud-")
                if (labels.get("frontiercloud.project") != UPDATER_PROJECT
                        or labels.get("frontiercloud.component") != component
                        or labels.get("frontiercloud.revision") != matched.group(1)):
                    continue
            try:
                engine.images.remove(tag, force=False, noprune=bool(UPDATER_PROJECT))
                log(f"removed stale release image {tag}")
            except Exception as exc:
                log(f"release image cleanup skipped {tag}: {type(exc).__name__}: {exc}")


def validate_target(target: str, mode: str) -> None:
    if not SHA_RE.fullmatch(target):
        raise RuntimeError("target must be a full commit SHA")
    if git("status", "--porcelain", "--untracked-files=no", timeout=20):
        raise RuntimeError("tracked working tree changes block release")
    remote_ref = f"refs/remotes/origin/{RELEASE_BRANCH}"
    git(
        "fetch", "--no-tags", "origin",
        f"+refs/heads/{RELEASE_BRANCH}:{remote_ref}",
        timeout=120,
    )
    main_head = git("rev-parse", remote_ref, timeout=20)
    git("cat-file", "-e", f"{target}^{{commit}}", timeout=20)
    if mode == "upgrade" and target != main_head:
        raise RuntimeError("upgrade target is not the current origin/main head")
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", target, remote_ref],
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
                wait_nginx_ready(restored)
        except Exception as exc:
            log(f"restore {service} failed: {type(exc).__name__}: {exc}")
    if old_sha and SHA_RE.fullmatch(old_sha):
        try:
            git("reset", "--hard", old_sha, timeout=60)
        except Exception as exc:
            log(f"git restore failed: {type(exc).__name__}: {exc}")


def persist_replacement(snapshots: dict, old_sha: str, target: str) -> None:
    """Durable recovery input before the first existing container is removed."""
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CONTROL_DIR / "replacement.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"snapshots": snapshots, "old_sha": old_sha, "target": target}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, REPLACEMENT_PATH)


def recover_interrupted_release() -> bool:
    """A process crash loses the in-memory queue; never leave it permanently busy."""
    current = read_status()
    if current.get("state") not in {"queued", "running", "distributing"}:
        return False
    target = str(current.get("target_sha") or "")
    maintenance(True, target)
    FORCE_OPEN_FLAG.unlink(missing_ok=True)
    detail = "Updater process interrupted; maintenance retained; retry the standard release"
    engine = None
    try:
        if current.get("state") == "running" and current.get("phase") == "replacing":
            if current.get("current_sha") == target:
                REPLACEMENT_PATH.unlink(missing_ok=True)
            else:
                journal = json.loads(REPLACEMENT_PATH.read_text(encoding="utf-8"))
                if journal.get("target") != target or journal.get("old_sha") != current.get("current_sha"):
                    raise RuntimeError("replacement journal does not match interrupted release")
                engine = client()
                restore_local(engine, journal["snapshots"], journal["old_sha"])
                REPLACEMENT_PATH.unlink(missing_ok=True)
        elif current.get("state") == "running" and current.get("phase") == "building":
            old_sha = str(current.get("current_sha") or "")
            if SHA_RE.fullmatch(old_sha):
                git("reset", "--hard", old_sha, timeout=60)
    except Exception as exc:
        detail += f"; recovery requires attention: {type(exc).__name__}: {exc}"
    finally:
        if engine is not None:
            engine.close()
    write_status(state="failed", phase="interrupted", detail=detail, updater_runtime_sha=RUNTIME_SHA)
    return True


def request_runtime_restart(target: str, previous_sha: str) -> None:
    write_status(
        state="restarting", phase="updater-restart", current_sha=target,
        previous_sha=previous_sha, updater_runtime_sha=RUNTIME_SHA,
        detail="",
    )
    log(f"release services complete; restarting updater runtime at {target}")
    os._exit(0)


def complete_pending_restart() -> bool:
    current = read_status()
    validate_manifest_state(current)
    if current.get("state") != "restarting":
        return False
    target = str(current.get("target_sha") or "")
    compatible = True
    if any(current.get(field) is not None for field in ("target_manifest", "current_manifest", "previous_manifest")):
        engine = None
        try:
            engine = client()
            web = service_container(engine, "web")
            require_manifest_web(web)
            if release_image_revision(web.image) != target:
                raise ValueError("Web artifact differs from persisted release")
            wait_healthy(web)
        except Exception:
            compatible = False
        finally:
            if engine is not None:
                engine.close()
    if target and target == RUNTIME_SHA and compatible:
        maintenance(False)
        write_status(
            state="success", phase="complete", current_sha=target,
            updater_runtime_sha=RUNTIME_SHA, detail="", completed_at=int(time.time()),
        )
        log(f"updater runtime restarted at {RUNTIME_SHA}")
    else:
        maintenance(True, target)
        write_status(
            state="failed", phase="updater-restart-failed",
            updater_runtime_sha=RUNTIME_SHA,
            detail=f"runtime SHA {RUNTIME_SHA or 'unknown'} does not match target {target or 'unknown'}",
        )
    return True


def perform(target: str, mode: str, hold_maintenance: bool, manifest: dict | None = None) -> None:
    current = read_status()
    old_sha = str(current.get("current_sha") or initial_sha())
    previous_sha = str(current.get("previous_sha") or "")
    started = int(time.time())
    write_status(
        state="running", phase="validating", mode=mode, target_sha=target,
        current_sha=old_sha, previous_sha=previous_sha, detail="", started_at=started,
        updater_runtime_sha=RUNTIME_SHA,
    )
    engine = None
    snapshots: dict[str, dict] = {}
    local_replaced = False
    restart_runtime = False
    try:
        if manifest is not None:
            artifact = manifests.select(manifest, RELEASE_BRANCH, "dev")
            if artifact["commit_sha"] != target:
                raise ValueError("Master selected the wrong private artifact")
            verify_artifact(manifest, RELEASE_BRANCH)
        FORCE_OPEN_FLAG.unlink(missing_ok=True)
        maintenance(True, target)
        validate_target(target, mode)
        has_manifest = manifest is not None or any(current.get(field) is not None for field in ("current_manifest", "previous_manifest"))
        if has_manifest:
            # The mutable reference runtime restarts from this tracked source.
            # Refuse a downlevel source before resetting/replacing any service.
            if "RELEASE_MANIFEST_VERSION = 1" not in git("show", target + ":updater/server.py", timeout=20):
                raise ValueError("Target updater cannot recover manifest state")
            git("cat-file", "-e", target + ":updater/release_evidence.py", timeout=20)
        engine = client()
        if target != old_sha:
            write_status(phase="building")
            git("reset", "--hard", target, timeout=60)
            web_image, nginx_image = build(engine, target)
            if has_manifest:
                labels = engine.images.get(web_image).attrs.get("Config", {}).get("Labels") or {}
                if labels.get("frontiercloud.release-manifest-version") != "1":
                    raise ValueError("Target Web image lacks manifest capability")
            for service in ("secrets-init", "media-init", "web", "nginx"):
                try:
                    snapshots[service] = snapshot(service_container(engine, service))
                except Exception:
                    if service in {"web", "nginx"}:
                        raise
            persist_replacement(snapshots, old_sha, target)
            write_status(phase="replacing")
            for service in ("secrets-init", "media-init"):
                if service not in snapshots:
                    continue
                value = replace(engine, snapshots[service], web_image)
                wait_completed(value)
            web = replace(engine, snapshots["web"], web_image)
            wait_healthy(web)
            nginx = replace(engine, snapshots["nginx"], nginx_image)
            wait_nginx_ready(nginx)
            local_replaced = True
            previous_sha = old_sha if mode == "upgrade" else ""
            write_status(current_sha=target, previous_sha=previous_sha, **manifest_changes(current, manifest, target, mode))
            REPLACEMENT_PATH.unlink(missing_ok=True)
        else:
            web = service_container(engine, "web")
            if has_manifest:
                require_manifest_web(web)
            write_status(**manifest_changes(current, manifest, target, mode))
            local_replaced = True

        if hold_maintenance:
            write_status(state="distributing", phase="distributing")
            command = ["python", "-m", "app.services.cluster_update_coordinator", target, mode]
            if manifest is not None:
                command = ["python", "-m", "app.services.cluster_manifest_coordinator", protocol.encode(protocol.canonical(manifest)), mode]
            result = web.exec_run(command, demux=True)
            if result.exit_code != 0:
                stdout, stderr = result.output if isinstance(result.output, tuple) else (b"", result.output or b"")
                detail = ((stderr or b"") + b"\n" + (stdout or b"")).decode("utf-8", errors="replace")
                raise RuntimeError("cluster convergence failed: " + detail[-2500:])

        cleanup_release_images(engine, {target, previous_sha})
        if SERVER_ACTIVE and RUNTIME_SHA and RUNTIME_SHA != target:
            restart_runtime = True
        else:
            maintenance(False)
            write_status(
                state="success", phase="complete", current_sha=target,
                previous_sha=previous_sha, updater_runtime_sha=RUNTIME_SHA or target,
                detail="", completed_at=int(time.time()),
            )
            log(f"release complete: {target} ({mode})")
    except BaseException as exc:
        if engine is not None and not local_replaced:
            restore_local(engine, snapshots, old_sha)
        write_status(state="failed", phase="failed", detail=f"{type(exc).__name__}: {exc}")
        log(f"release failed; maintenance remains enabled: {type(exc).__name__}: {exc}")
    finally:
        if engine is not None:
            try:
                engine.close()
            except Exception:
                pass

    if restart_runtime:
        request_runtime_restart(target, previous_sha)


def worker() -> None:
    while True:
        target, mode, hold, manifest = TASKS.get()
        try:
            perform(target, mode, hold, manifest)
        finally:
            TASKS.task_done()


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            raw = self.rfile.readline(8192)
            if not raw.endswith(b"\n"):
                raise ValueError("invalid bounded updater request")
            request = json.loads(raw, object_pairs_hook=manifests._pairs)
            if not isinstance(request, dict) or set(request) - {"action", "target_sha", "mode", "hold_maintenance", "release_manifest"}:
                raise ValueError("invalid updater fields")
            action = str(request.get("action") or "status")
            if action == "status":
                current = read_status()
                validate_manifest_state(current)
                response = {"ok": True, "status": current, "capabilities": [manifests.MANIFEST_CAPABILITY]}
            elif action == "start":
                target = str(request.get("target_sha") or "")
                mode = str(request.get("mode") or "upgrade")
                hold = bool(request.get("hold_maintenance", False))
                manifest = None
                if "release_manifest" in request:
                    if "target_sha" in request:
                        raise ValueError("Master cannot select a manifest artifact")
                    manifest = manifests.parse(protocol.canonical(request["release_manifest"]))
                    target = manifests.select(manifest, RELEASE_BRANCH, "dev")["commit_sha"]
                if not SHA_RE.fullmatch(target) or mode not in {"upgrade", "rollback"}:
                    raise ValueError("invalid release request")
                with START_LOCK:
                    current = read_status()
                    validate_manifest_state(current)
                    if current.get("state") in {"queued", "running", "distributing", "restarting"} or TASKS.full():
                        response = {"ok": False, "reason": "release already running", "status": current}
                    else:
                        # Publish intent before waking the worker; concurrent starts
                        # cannot both pass and queued cannot overwrite running.
                        write_status(state="queued", phase="queued", target_sha=target, mode=mode, detail="", target_manifest=manifest)
                        TASKS.put_nowait((target, mode, hold, manifest))
                        response = {"ok": True, "accepted": True, "target_sha": target, "mode": mode}
                        if manifest is not None:
                            response["release_id"] = manifests.identifier(manifest)
            else:
                raise ValueError("unknown updater action")
        except queue.Full:
            response = {"ok": False, "reason": "release queue is full"}
        except Exception as exc:
            response = {"ok": False, "reason": str(exc)}
        self.wfile.write((json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode())


def main() -> None:
    global SERVER_ACTIVE, RUNTIME_SHA
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    MAINTENANCE_DIR.mkdir(parents=True, exist_ok=True)
    SOCKET_PATH.unlink(missing_ok=True)
    RUNTIME_SHA = initial_sha()
    SERVER_ACTIVE = True
    current = read_status()
    if not current:
        write_status(
            state="idle", phase="idle", current_sha=RUNTIME_SHA, previous_sha="",
            updater_runtime_sha=RUNTIME_SHA, detail="",
        )
    elif not complete_pending_restart() and not recover_interrupted_release():
        write_status(updater_runtime_sha=RUNTIME_SHA)
    threading.Thread(target=worker, name="frontiercloud-release-worker", daemon=True).start()
    with socketserver.ThreadingUnixStreamServer(str(SOCKET_PATH), Handler) as server:
        os.chmod(SOCKET_PATH, 0o666)
        log("release control socket ready")
        server.serve_forever()


if __name__ == "__main__":
    main()
