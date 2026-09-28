"""Bounded destructive/load checks for the dedicated persistent dev cluster."""
from __future__ import annotations

import hashlib
import json
import os
import random
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from collections import Counter

from dev_control import Client, ROOT, compose

REPORT = ROOT / "chaos-report.json"


def outer(node: str, *args: str, check: bool = True):
    return subprocess.run(["docker", "exec", "fc-dev-host-" + node, "docker", *args],
                          check=check, text=True, capture_output=True)


def wait_for(work, seconds: int, name: str):
    deadline, error = time.monotonic() + seconds, None
    while time.monotonic() < deadline:
        try:
            value = work()
            if value:
                return value
        except Exception as exc:
            error = type(exc).__name__
        time.sleep(2)
    raise TimeoutError(f"{name} did not recover; last error={error}")


def resources():
    program = """
import asyncio,json
from app.services.federation.catalog import catalog
from app.services.federation.state import state
async def read():
 await state.initialize()
 rows=await catalog.resources()
 return [{"resource_id":r["resource_id"],"path":r["path"],"size":int(r["payload"]["size"]),"member":r["owner_id"]} for r in rows]
print(json.dumps(asyncio.run(read())))
"""
    return json.loads(compose("master", "exec", "-T", "web", "python", "-c", program))


def range_load(client: Client, items: list[dict], requests: int = 1200, workers: int = 48):
    random.seed(20260928)
    selected = [random.choice(items) for _ in range(requests)]

    def one(row):
        start = time.monotonic()
        size = min(1024 * 1024, row["size"])
        response = client.client.get(client.endpoint + "/api/v1/media/stream",
                                     params={"resource_id": row["resource_id"]},
                                     headers={"Range": f"bytes=0-{size - 1}"}, timeout=90,
                                     follow_redirects=True)
        return response.status_code, len(response.content), size, time.monotonic() - start

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = [future.result() for future in as_completed([pool.submit(one, row) for row in selected])]
    elapsed = time.monotonic() - started
    failures = [row for row in rows if row[0] != 206 or row[1] != row[2]]
    latencies = sorted(row[3] for row in rows)
    if failures:
        raise AssertionError(f"range load failures: {len(failures)}; outcomes={Counter((r[0], r[1], r[2]) for r in failures).most_common(10)}")
    return {"requests": requests, "workers": workers, "bytes": sum(row[1] for row in rows),
            "seconds": round(elapsed, 3), "requests_per_second": round(requests / elapsed, 2),
            "p50_ms": round(statistics.median(latencies) * 1000, 2),
            "p95_ms": round(latencies[int(len(latencies) * .95) - 1] * 1000, 2),
            "max_ms": round(max(latencies) * 1000, 2)}


def main():
    if os.geteuid() != 0:
        raise RuntimeError("root is required")
    client = Client("master")
    items = resources()
    if len(items) < 1000:
        raise RuntimeError(f"real catalog too small for load run: {len(items)}")
    result = {"started_at": int(time.time()), "catalog_objects": len(items), "checks": []}
    result["load"] = range_load(client, items)
    result["checks"].append("1,200 concurrent 1 MiB Range reads returned exact lengths")

    topology = client.nodes()
    target = next(row for row in topology["relationships"] if row["mode"] == "Direct")
    node_name = next(row["name"] for row in json.loads((ROOT / "inventory.json").read_text())["nodes"]
                     if row["endpoint"] == target["peer_endpoint"])
    target_items = [row for row in items if row["member"] == target["peer_id"]]
    control_items = [row for row in items if row["member"] != target["peer_id"]]
    if not target_items or not control_items:
        raise RuntimeError("fault target lacks representative objects")
    sample, control = target_items[0], control_items[0]

    outer(node_name, "stop", "office_automation_web")
    try:
        wait_for(lambda: next(r for r in client.nodes()["relationships"]
                              if r["relationship_id"] == target["relationship_id"])["status"] == "offline",
                 180, "Direct Follower offline state")
        unavailable = client.client.get(client.endpoint + "/api/v1/media/stream",
                                        params={"resource_id": sample["resource_id"]}, timeout=20,
                                        follow_redirects=True)
        available = client.client.get(client.endpoint + "/api/v1/media/stream",
                                      params={"resource_id": control["resource_id"]},
                                      headers={"Range": "bytes=0-65535"}, timeout=20,
                                      follow_redirects=True)
        if unavailable.status_code not in {502, 503, 504} or available.status_code != 206:
            raise AssertionError((unavailable.status_code, available.status_code))
    finally:
        outer(node_name, "start", "office_automation_web")
    wait_for(lambda: next(r for r in client.nodes()["relationships"]
                          if r["relationship_id"] == target["relationship_id"])["status"] == "online",
             120, "Direct Follower heartbeat")
    recovered = client.client.get(client.endpoint + "/api/v1/media/stream",
                                  params={"resource_id": sample["resource_id"]},
                                  headers={"Range": "bytes=0-65535"}, timeout=30,
                                  follow_redirects=True)
    if recovered.status_code != 206 or len(recovered.content) != min(65536, sample["size"]):
        raise AssertionError("media did not recover after Direct Follower restart")
    result["checks"].append("one Direct Web loss became offline; unrelated media stayed readable; owned media recovered")

    # Redis/MySQL are stopped one at a time and always restarted in finally blocks.
    for service in ("office_automation_redis", "office_automation_mysql"):
        outer("master", "stop", service)
        try:
            wait_for(lambda: outer("master", "inspect", "--format", "{{.State.Status}}", service).stdout.strip() == "exited",
                     30, service + " stop")
        finally:
            outer("master", "start", service)
        wait_for(lambda: outer("master", "inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{end}}", service).stdout.strip() == "healthy",
                 120, service + " health")
        wait_for(lambda: client.client.get(client.endpoint + "/health/ready", timeout=10).status_code == 200,
                 120, "Master readiness")
        result["checks"].append(service + " stop/start recovered to verified HTTPS readiness")

    verify = random.sample(items, 30)
    for row in verify:
        response = client.client.get(client.endpoint + "/api/v1/media/stream",
                                     params={"resource_id": row["resource_id"]},
                                     headers={"Range": "bytes=0-4095"}, timeout=30,
                                     follow_redirects=True)
        if response.status_code != 206 or len(response.content) != min(4096, row["size"]):
            raise AssertionError("post-chaos media verification failed")
    result["checks"].append("30 random real objects remained readable after fault recovery")
    result["completed_at"] = int(time.time())
    result["result"] = "passed"
    REPORT.write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
