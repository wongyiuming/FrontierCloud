"""Root-only operator client for the persistent development environment.

Deployments always use the public Admin upgrade-and-distribute transaction.
Credentials are read from the running Master and kept only in memory.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import ssl
import subprocess
import time
from pathlib import Path

import httpx

ROOT = Path("/root/frontiercloud-dev")
BUSY = {"queued", "running", "distributing", "restarting"}


def compose(node, *args):
    result = subprocess.run([
        "docker", "exec", "fc-dev-host-" + node, "docker", "compose",
        "--project-directory", "/node/repo", "-p", "frontiercloud",
        "-f", "/node/repo/docker-compose.yaml", "-f", "/node/override.json", *args,
    ], check=True, capture_output=True, text=True)
    return result.stdout.strip()


class Client:
    def __init__(self, node):
        self.node = node
        inventory = json.loads((ROOT / "inventory.json").read_text())
        self.endpoint = next(n["endpoint"] for n in inventory["nodes"] if n["name"] == node)
        self.client = httpx.Client(verify=ssl.create_default_context(cafile=inventory["ca"]),
                                   trust_env=False, timeout=90, follow_redirects=False)
        key = compose(node, "exec", "-T", "web", "python", "-c",
                      "from app.core.config import ADMIN_KEY_FILE; print(ADMIN_KEY_FILE.read_text().strip())")
        response = self.client.post(self.endpoint + "/api/v1/media/admin/elevate", data={"token": key})
        response.raise_for_status()
        self.csrf = self.client.cookies.get("__Host-admin-csrf")
        if not self.csrf:
            raise RuntimeError("Admin CSRF cookie absent")

    def api(self, path, value=None):
        response = self.client.request("GET" if value is None else "POST", self.endpoint + path,
                                       json=value, headers={"X-CSRF-Token": self.csrf})
        if response.is_error:
            raise RuntimeError(f"{self.node}: {path}: HTTP {response.status_code}: {response.text[:500]}")
        return response.json()

    def nodes(self):
        return self.api("/api/v1/media/admin/nodes")

    def release(self):
        return self.api("/api/v1/media/admin/nodes/release")


def bootstrap():
    inventory = json.loads((ROOT / "inventory.json").read_text())
    master = Client("master")
    if master.nodes()["role"] == "Standalone":
        master.api("/api/v1/media/admin/nodes/promote", {
            "role": "Master", "endpoint": master.endpoint, "local_capacity_gib": 50,
        })
    for node in inventory["nodes"][1:]:
        follower = Client(node["name"])
        identity = follower.nodes()
        if identity["role"] == "Standalone":
            follower.api("/api/v1/media/admin/nodes/promote", {"role": "Follower", "endpoint": follower.endpoint})
        relations = master.nodes()["relationships"]
        existing = next((r for r in relations if r["peer_id"] == identity["node_id"] and r["state"] == "active"), None)
        if existing:
            relation = existing["relationship_id"]
        else:
            package = follower.api("/api/v1/media/admin/nodes/pair-package", {})
            relation = master.api("/api/v1/media/admin/nodes/pair", {"package": package})["relationship_id"]
        master.api(f"/api/v1/media/admin/nodes/{relation}/mode", {"mode": node["mode"]})
        master.api(f"/api/v1/media/admin/nodes/{relation}/resources", {
            "storage_enabled": True, "storage_capacity_gib": 50, "backup_enabled": True,
        })
        print(json.dumps({"paired": node["name"], "mode": node["mode"]}), flush=True)


def converged(status, target):
    rows = [status["local"], *(f["status"] for f in status["followers"] if f["reachable"])]
    return len(rows) == 10 and all(r.get("current_sha") == target and
                                  r.get("updater_runtime_sha") == target and r.get("state") == "success" for r in rows)


def reconcile(expected_sha=None, timeout=2400):
    if (ROOT / "cd-paused").exists():
        print("CD paused by operator")
        return
    master = Client("master")
    status = master.release()
    target = status["ci"].get("sha")
    if expected_sha and expected_sha != target:
        raise RuntimeError("CD trigger SHA differs from the release API's current main SHA")
    if not status["ci"].get("publishable"):
        raise RuntimeError("Current main has not passed the standard release provenance check")
    if converged(status, target):
        print(json.dumps({"result": "already-converged", "sha": target}))
        return
    if status["local"].get("state") not in BUSY:
        master.api("/api/v1/media/admin/nodes/release/upgrade", {})
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            status = master.release()
        except (httpx.TransportError, RuntimeError):
            time.sleep(10)
            continue
        summary = {"sha": target, "state": status["local"].get("state"),
                   "phase": status["local"].get("phase"),
                   "converged_followers": sum(f["status"].get("current_sha") == target and
                                               f["status"].get("state") == "success" for f in status["followers"])}
        if summary != last:
            print(json.dumps(summary), flush=True)
            last = summary
        (ROOT / "cd-status.json").write_text(json.dumps(status, indent=2))
        if converged(status, target):
            print(json.dumps({"result": "converged", "sha": target}), flush=True)
            return
        if status["local"].get("state") == "failed":
            raise RuntimeError("Release failed with maintenance retained; inspect cd-status.json")
        time.sleep(10)
    raise TimeoutError("Ten-node release convergence exceeded deadline")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["bootstrap", "status", "reconcile", "rollback"])
    parser.add_argument("--expected-sha")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("root is required")
    with (ROOT / "controller.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "bootstrap":
            bootstrap()
        elif args.action == "status":
            print(json.dumps(Client("master").release(), indent=2))
        elif args.action == "rollback":
            print(json.dumps(Client("master").api("/api/v1/media/admin/nodes/release/rollback", {})))
        else:
            reconcile(args.expected_sha)


if __name__ == "__main__":
    main()
