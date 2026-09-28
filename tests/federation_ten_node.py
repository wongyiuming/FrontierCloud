"""Root-run, isolated 1 Master + 3 Direct + 6 Relay acceptance with a private CA.

Build the three acceptance images first, then run on a disposable Docker host.
Use --keep to retain the ten-node fixture for inspection; never uses host data.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

from tests.federation_stack import (
    ACCEPTANCE_GATEWAY, ACCEPTANCE_SUBNET, ROOT, Checks, Node,
    browser_checks, command, wait_for, wav,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("Run this test harness as root")
    directory = args.directory.resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    nodes = []
    report = {"topology": "1 Master + 3 Direct + 6 Relay", "checks": Checks(), "transfers": []}
    network = "fc-ten-audit-gateway"
    command("docker", "network", "create", "--subnet", ACCEPTANCE_SUBNET, "--gateway", ACCEPTANCE_GATEWAY, network)
    try:
        ca = directory / "ca.pem"
        command("openssl", "req", "-x509", "-nodes", "-days", "7", "-newkey", "rsa:2048",
                "-keyout", str(ca.with_suffix(".key")), "-out", str(ca),
                "-subj", "/CN=FrontierCloud ten-node validation CA",
                "-addext", "basicConstraints=critical,CA:TRUE",
                "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        bundle = directory / "ca-certificates.crt"
        bundle.write_bytes(Path("/etc/ssl/certs/ca-certificates.crt").read_bytes() + b"\n" + ca.read_bytes())
        # Chromium trusts this CA in an isolated HOME; no TLS bypass switches.
        browser_home = directory / "browser-home"
        nss = browser_home / ".pki/nssdb"
        nss.mkdir(parents=True)
        command("certutil", "-N", "--empty-password", "-d", f"sql:{nss}")
        command("certutil", "-A", "-d", f"sql:{nss}", "-n", "FrontierCloud test CA", "-t", "C,,", "-i", str(ca))
        base = json.loads(command("docker", "compose", "-f", str(ROOT / "docker-compose.yaml"), "config", "--format", "json"))
        for index, name in enumerate(("ten-master", "ten-direct-1", "ten-direct-2", "ten-direct-3",
                                      "ten-relay-1", "ten-relay-2", "ten-relay-3",
                                      "ten-relay-4", "ten-relay-5", "ten-relay-6")):
            nodes.append(Node(name, directory, base, ca, bundle, index))
        master = nodes[0]
        wav(master.data / "media/music/shared/master.wav", seconds=3)
        source_path = directory / "source.wav"
        wav(source_path, seconds=3)
        source = source_path.read_bytes()
        with ThreadPoolExecutor(max_workers=3) as executor:
            list(executor.map(Node.start, nodes))
        report["checks"].append("all ten nodes: trusted HTTPS ready; unknown CA and wrong hostname rejected")

        master.promote("Master")
        relations = {}
        for index, follower in enumerate(nodes[1:]):
            follower.promote("Follower")
            master.pair(follower)
            master.wait_online()
            mode = "Direct" if index < 3 else "Relay"
            master.mode(mode)
            master.configure_resources(storage=True, capacity_gib=2, compute=False, backup=True)
            relations[follower.nodes()["node_id"]] = {"relation": master.relation, "mode": mode, "node": follower}

        def ready():
            pool = master.nodes()["storage_pool"]["members"]
            return len(pool) == 10 and all(row["health"] == "online" and row["writable"] for row in pool)

        wait_for(ready, seconds=240, description="ten writable storage members")
        current = master.nodes()["relationships"]
        assert len(current) == 9
        assert sum(row["mode"] == "Direct" for row in current) == 3
        assert sum(row["mode"] == "Relay" for row in current) == 6
        for follower in nodes[1:]:
            response = follower.client.get(follower.endpoint + "/api/v1/media", follow_redirects=False)
            assert response.status_code == 307 and response.headers["location"].startswith(master.endpoint)
            assert follower.client.post(follower.endpoint + "/api/v1/media/playback", json={}).status_code == 409
        report["checks"].append("one business Master, nine online Followers, exactly three Direct and six Relay")

        resources = {"primary": [master.upload_media(source, "primary.wav", site_type="primary")]}
        for site_type, count in (("direct", 3), ("relay", 6)):
            resources[site_type] = [master.upload_media(source, f"{site_type}-{i}.wav", site_type=site_type) for i in range(count)]
            placements = json.loads(master.web(
                "import asyncio,json; from sqlalchemy import text; from app.core.db import engine; "
                "exec(\"async def read():\\n async with engine.connect() as c:\\n  return [dict(r) for r in (await c.execute(text('SELECT media_path,storage_member_id FROM global_media_objects'))).mappings()]\"); "
                "print(json.dumps(asyncio.run(read())))"))
            selected = [row["storage_member_id"] for row in placements if row["media_path"].startswith(f"music/shared/{site_type}-")]
            assert len(set(selected)) == count, selected
            assert all(relations[member]["mode"].lower() == site_type for member in selected)
        report["checks"].append("site-type placement reaches all nine Direct/Relay members without naming a member")

        for site_type, items in resources.items():
            for item in items:
                started = time.monotonic()
                full = master.client.get(master.endpoint + item["url"])
                assert full.status_code == 200 and full.content == source
                head = master.client.head(master.endpoint + item["url"])
                assert head.status_code == 200 and int(head.headers["content-length"]) == len(source)
                partial = master.range(item["url"], 100, 199)
                assert partial.status_code == 206 and partial.content == source[100:200]
                report["transfers"].append({"type": site_type, "path": item["path"], "bytes": len(source),
                                             "full_head_range_seconds": round(time.monotonic() - started, 3)})
        report["checks"].append("Local and all nine Follower placements: byte-exact GET, HEAD, Range")

        lyric = master.client.post(master.endpoint + "/api/v1/media/admin/upload/lyric",
                                   headers={"X-CSRF-Token": master.csrf},
                                   files={"file": ("ten.lrc", b"[00:00.00]Ten node lyric\n", "text/plain")})
        assert lyric.status_code == 200
        for site_type in ("direct", "relay"):
            master.api("/api/v1/media/admin/lyrics/relations", {
                "origin_kind": "track", "origin_path": resources[site_type][0]["path"], "linked_paths": ["lyrics/ten.lrc"]})
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(env={**os.environ, "HOME": str(browser_home)}, args=[
                "--autoplay-policy=no-user-gesture-required", "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
            for site_type in ("direct", "relay"):
                browser_checks(browser, master, resources[site_type][0])
            browser.close()
        report["checks"].append("Chromium trusts the private CA without TLS bypass; Direct/Relay playback, lyrics and microphone recording")

        user = master.register_karaoke_user("ten_audit")
        master.karaoke_api("/password", {"current_password": "Huawei@123", "new_password": "weak"}, expected=400)
        malformed = b"{invalid"
        blob = b"AUDIO" + malformed + len(malformed).to_bytes(8, "big") + b"FRONTIERCLOUD-KARAOKE-V1"
        ticket = master.upload_recording(blob, "malformed optional metadata")
        assert master.client.get(master.endpoint + f"/api/v1/karaoke/account/recordings/{ticket['recording_id']}/stream").content == blob
        master.karaoke_api(f"/recordings/{ticket['recording_id']}", method="DELETE")
        report["checks"].append("real MySQL/Redis account: invalid password returns 400; malformed trailer upload/finalize/play/delete succeeds")

        with httpx.Client(verify=ssl.create_default_context(cafile=str(ca)), trust_env=False) as anonymous:
            response = anonymous.post(master.endpoint + "/api/v1/media/admin/upload/item",
                                      headers={"Content-Type": "multipart/form-data; boundary=invalid"}, content=b"invalid multipart")
            assert response.status_code == 401
        report["checks"].append("Nginx/Web anonymous malformed multipart rejected by authentication before parsing")

        for module in ("tests.security_mysql_smoke", "tests.sql_concurrency_smoke"):
            output = master.compose("exec", "-T", "web", "python", "-m", module)
            (directory / (module + ".log")).write_text(output)
        report["checks"].append("real MySQL security transactions and MySQL/Redis concurrency regressions")

        def backups_ready():
            pool = master.api("/api/v1/media/admin/storage-pool")
            members = pool.get("members", [])
            return sum(bool((row.get("backup") or {}).get("last_success")) for row in members) == 9

        wait_for(backups_ready, seconds=300, description="nine successful asynchronous business backups")
        report["checks"].append("all nine Followers have a successful business backup while heartbeats remain online")
        report["result"] = "passed"
    finally:
        (directory / "report.json").write_text(json.dumps(report, indent=2))
        if not args.keep:
            for node in reversed(nodes):
                node.stop()
            command("docker", "network", "rm", network)
        else:
            print(f"Fixture retained in {directory}", flush=True)


if __name__ == "__main__":
    main()
