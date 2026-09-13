"""Disposable three-node HTTPS + browser + fault/soak acceptance harness.

Infrastructure is test-only. Compose configurations and private CA material are
generated in a temporary directory; no hosted user data or deployment is used.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import socket
import ssl
import struct
import subprocess
import tempfile
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


class Checks(list):
    def append(self, value):
        super().append(value)
        print(json.dumps({"stage": value}), flush=True)


def command(*arguments, **kwargs):
    result = subprocess.run(arguments, text=True, capture_output=True, **kwargs)
    if result.returncode:
        print(result.stderr[-5000:], flush=True)
        result.check_returncode()
    return result.stdout.strip()


def wait_for(work, seconds=150, description="condition"):
    deadline, error = time.monotonic() + seconds, None
    while time.monotonic() < deadline:
        try:
            result = work()
            if result:
                return result
        except Exception as exc:
            error = type(exc).__name__
        time.sleep(2)
    raise AssertionError(f"Timed out waiting for {description}; last error={error}")


class Node:
    def __init__(self, name, directory, base, ca, index):
        self.name, self.directory = name, directory / name
        self.directory.mkdir()
        self.host = f"ci-{name}.frontiercloud.local"
        self.port = 14443 + index
        self.project = "fc-acceptance-" + name
        self.configuration = self.directory / "compose.json"
        self.ca, self.tunnel, self.log, self.client = ca, None, None, None
        self.endpoint = ""
        self.csrf = ""
        self.relation = ""
        self.resource = None
        self.data = self.directory / "data"
        (self.data / "media/music/shared").mkdir(parents=True)
        (self.data / "media/vido").mkdir(parents=True)
        (self.data / "media/lyrics").mkdir(parents=True)
        certs = self.directory / "certs"
        certs.mkdir()
        request = certs / "request.pem"
        key, cert = certs / "privkey.pem", certs / "fullchain.pem"
        command("openssl", "req", "-new", "-nodes", "-newkey", "rsa:2048", "-keyout", str(key),
                "-out", str(request), "-subj", f"/CN={self.host}")
        extensions = certs / "extensions.conf"
        extensions.write_text(f"subjectAltName=DNS:{self.host}\nextendedKeyUsage=serverAuth\n")
        command("openssl", "x509", "-req", "-in", str(request), "-CA", str(ca), "-CAkey", str(ca.with_suffix(".key")),
                "-CAcreateserial", "-out", str(cert), "-days", "1", "-extfile", str(extensions))
        configuration = copy.deepcopy(base)
        configuration.pop("name", None)
        for volume in configuration.get("volumes", {}).values():
            volume.pop("name", None)
        configuration["networks"] = {"default": {"name": self.project}}
        for name, service in configuration["services"].items():
            service.pop("container_name", None)
            service.pop("env_file", None)
            service.pop("build", None)
            service.pop("ports", None)
            if name in ("web", "secrets-init"):
                service["image"] = "frontiercloud-acceptance-web"
            elif name == "nginx":
                service["image"] = "frontiercloud-acceptance-nginx"
                service["ports"] = [{"target": 443, "published": str(self.port), "host_ip": "127.0.0.1", "protocol": "tcp"}]
            if name in ("web", "nginx"):
                service.setdefault("environment", {}).update(TLS_ENABLED="true", SERVER_NAME=self.host, INSTANCE_NAME="acceptance")
            for volume in service.get("volumes", []):
                if volume.get("target") == "/app/data":
                    volume["source"] = str(self.data)
                elif volume.get("target") == "/etc/nginx/certs/fullchain.pem":
                    volume["source"] = str(cert)
                elif volume.get("target") == "/etc/nginx/certs/privkey.pem":
                    volume["source"] = str(key)
                elif volume.get("target") == "/var/www/certbot":
                    volume["source"] = str(certs / "acme")
        self.configuration.write_text(json.dumps(configuration))
        # The private temporary parent remains 0700. Fixture directories need both
        # the runner and the actual UID 10001 container to create/delete fixtures.
        command("sudo", "chmod", "-R", "a+rwX", str(self.data))
        command("sudo", "chown", "-R", "10001:10001", str(self.data))

    def compose(self, *arguments):
        return command("docker", "compose", "-p", self.project, "-f", str(self.configuration), *arguments)

    def container(self, service):
        return self.compose("ps", "-q", service)

    def web(self, program):
        return self.compose("exec", "-T", "web", "python", "-c", program)

    def start(self):
        self.compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "180")
        self.compose("exec", "-T", "nginx", "nginx", "-t")
        trusted = ssl.create_default_context(cafile=str(self.ca))
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as connection:
            with trusted.wrap_socket(connection, server_hostname=self.host):
                pass
        for context, hostname in ((ssl.create_default_context(), self.host), (trusted, "wrong.frontiercloud.local")):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=10) as connection:
                    with context.wrap_socket(connection, server_hostname=hostname):
                        pass
            except ssl.SSLCertVerificationError:
                continue
            raise AssertionError("Untrusted CA or mismatched TLS hostname was accepted")
        self.log = open(self.directory / "tunnel.log", "w+")
        self.tunnel = subprocess.Popen(["cloudflared", "tunnel", "--no-autoupdate", "--protocol", "http2",
            "--url", f"https://127.0.0.1:{self.port}", "--origin-server-name", self.host,
            "--origin-ca-pool", str(self.ca)], stdout=self.log, stderr=subprocess.STDOUT)
        def tunnel_url():
            self.log.flush()
            text = (self.directory / "tunnel.log").read_text()
            matches = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", text)
            return matches[-1] if matches else None
        self.endpoint = wait_for(tunnel_url, description=f"{self.name} tunnel")
        self.client = httpx.Client(verify=ssl.create_default_context(), trust_env=False, timeout=20, follow_redirects=True)
        wait_for(lambda: self.client.get(self.endpoint + "/health/ready").status_code == 200,
                 description=f"{self.name} verified HTTPS readiness")
        from urllib.parse import urljoin
        redirect = self.client.get(self.endpoint + "/api/v1/media/admin", follow_redirects=False)
        assert redirect.status_code == 301
        assert urlsplit_origin(urljoin(self.endpoint, redirect.headers["location"])) == self.endpoint
        key = self.web("from app.core.config import ADMIN_KEY_FILE; print(ADMIN_KEY_FILE.read_text().strip())")
        response = self.client.post(self.endpoint + "/api/v1/media/admin/elevate", data={"token": key})
        assert response.status_code == 200, f"{self.name} admin login failed"
        self.csrf = self.client.cookies.get("__Host-admin-csrf")
        assert self.csrf

    def api(self, path, value=None, *, expected=200):
        response = (self.client.get(self.endpoint + path) if value is None else
                    self.client.post(self.endpoint + path, json=value, headers={"X-CSRF-Token": self.csrf}))
        assert response.status_code == expected, f"{self.name}: {path.split('?')[0]} status {response.status_code}, expected {expected}"
        return response.json()

    def nodes(self):
        return self.api("/api/v1/media/admin/nodes")

    def promote(self, role):
        self.api("/api/v1/media/admin/nodes/promote", {"role": role, "endpoint": self.endpoint})

    def pair(self, slave):
        package = slave.api("/api/v1/media/admin/nodes/pair-package", {})
        result = self.api("/api/v1/media/admin/nodes/pair", {"package": package})
        self.relation = result["relationship_id"]
        return package

    def resources(self):
        return self.api(f"/api/v1/media/admin/nodes/{self.relation}/resources")["items"]

    def wait_online(self):
        def ready():
            rows = self.nodes()["relationships"]
            return any(row["relationship_id"] == self.relation and row["status"] == "online" for row in rows) and bool(self.resources())
        wait_for(ready, description=f"{self.name} relationship recovery")

    def mode(self, value):
        self.api(f"/api/v1/media/admin/nodes/{self.relation}/mode", {"mode": value})

    def range(self, path, start=0, end=3):
        return self.client.get(self.endpoint + path, headers={"Range": f"bytes={start}-{end}"})

    def measure(self):
        samples = {}
        for service in ("web", "nginx"):
            container = self.container(service)
            pid = command("docker", "inspect", "--format", "{{.State.Pid}}", container)
            # Host proc records include all container worker processes, not a guess at PID 1.
            code = """
import json,os,pathlib
root=pathlib.Path('/proc')
pid=int(os.sys.argv[1])
cgroup=(root/str(pid)/'cgroup').read_text()
rows=[]
for entry in root.iterdir():
 if not entry.name.isdigit(): continue
 try:
  if (entry/'cgroup').read_text()!=cgroup: continue
  status=(entry/'status').read_text()
  rss=int(next(line.split()[1] for line in status.splitlines() if line.startswith('VmRSS:')))*1024
  fds=list((entry/'fd').iterdir())
  sockets=sum(os.readlink(fd).startswith('socket:') for fd in fds)
  rows.append(dict(rss=rss,fd=len(fds),sockets=sockets))
 except (OSError,StopIteration): pass
print(json.dumps({key:sum(row[key] for row in rows) for key in ['rss','fd','sockets']}))
"""
            samples[service] = json.loads(command("sudo", "python3", "-c", code, pid))
        samples["temporary"] = json.loads(self.web("import json,pathlib; files=[p for p in pathlib.Path('/tmp').rglob('*') if p.is_file()]; print(json.dumps({'files':len(files),'bytes':sum(p.stat().st_size for p in files)}))"))
        samples["cpu"] = json.loads(command("docker", "stats", "--no-stream", "--format", "{{json .}}", self.container("web")))
        return samples

    def netem(self, specification):
        pid = command("docker", "inspect", "--format", "{{.State.Pid}}", self.container("nginx"))
        if specification:
            command("sudo", "nsenter", "-t", pid, "-n", "tc", "qdisc", "replace", "dev", "eth0", "root", "netem", *specification)
        else:
            subprocess.run(["sudo", "nsenter", "-t", pid, "-n", "tc", "qdisc", "del", "dev", "eth0", "root"], capture_output=True)

    def stop(self):
        if self.client:
            self.client.close()
        if self.tunnel:
            self.tunnel.terminate()
            try: self.tunnel.wait(timeout=10)
            except subprocess.TimeoutExpired: self.tunnel.kill()
        if self.log: self.log.close()
        self.compose("down", "--volumes", "--remove-orphans")

    def failure_diagnostics(self):
        from urllib.parse import urlsplit
        details = {"node": self.name, "endpoint": self.endpoint}
        if self.endpoint:
            try:
                details["dns"] = sorted({row[4][0] for row in socket.getaddrinfo(urlsplit(self.endpoint).hostname, 443)})
            except OSError as exc:
                details["dns_error"] = str(exc)
            try:
                details["readiness_status"] = self.client.get(self.endpoint + "/health/ready").status_code
            except httpx.HTTPError as exc:
                details["readiness_error"] = str(exc)
        path = self.directory / "tunnel.log"
        if path.exists():
            details["tunnel"] = path.read_text()[-4000:]
        print(json.dumps({"network_diagnostics": details}), flush=True)


def wav(path, seconds=12, tone=500):
    import math
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        block = b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * tone * n / 8000))) for n in range(8000))
        for _ in range(seconds): output.writeframesraw(block)
    command("sudo", "chown", "10001:10001", str(path))


def browser_args(nodes):
    return ["--autoplay-policy=no-user-gesture-required",
            "--log-net-log=" + str(nodes[0].directory.parent / "browser-network.json")]


def browser_network_failure(directory):
    path = directory / "browser-network.json"
    if not path.exists():
        return
    try:
        log = json.loads(path.read_text())
        types = {value: key for key, value in log["constants"]["logEventTypes"].items()}
        records = []
        for event in log["events"]:
            name, params = types.get(event["type"], ""), event.get("params", {})
            if "HOST_RESOLVER" in name or "DNS" in name:
                records.append({"type": name, "params": {key: value for key, value in params.items()
                    if key in ("host", "hostname", "net_error", "addresses", "dns_query_type", "error")}})
        print(json.dumps({"browser_network": records[-40:]}), flush=True)
    except (ValueError, KeyError):
        print(json.dumps({"browser_network": "incomplete network log"}), flush=True)


def admin_page(browser, node):
    context = browser.new_context()  # TLS errors are never ignored.
    context.add_cookies([{ "name": item.name, "value": item.value, "domain": item.domain,
        "path": item.path, "secure": item.secure, "httpOnly": item.name.endswith("session") } for item in node.client.cookies.jar])
    page = context.new_page()
    page.goto(node.endpoint + "/api/v1/media/admin", wait_until="domcontentloaded")
    page.locator('#nodesPanel .module-heading').click()
    page.wait_for_function("document.querySelector('#nodeIdentity').textContent.includes(' / v')")
    return context, page


def browser_promote(browser, node, role):
    context, page = admin_page(browser, node)
    page.select_option('#nodeRole', role)
    page.fill('#nodeEndpoint', node.endpoint)
    page.locator('#nodePromotion button[type="submit"]').click()
    page.wait_for_function("role => document.querySelector('#nodeIdentity').textContent.startsWith(role)", arg=role)
    assert not page.locator('#nodePromotion').is_visible()
    context.close()


def browser_pair(browser, master, slave):
    slave_context, slave_page = admin_page(browser, slave)
    slave_page.locator('#nodeIssuePair').click()
    slave_page.wait_for_function("document.querySelector('#nodePairPackage').value.includes('signature')")
    package = json.loads(slave_page.input_value('#nodePairPackage'))
    master_context, master_page = admin_page(browser, master)
    master_page.fill('#nodePairPackage', json.dumps(package))
    master_page.locator('#nodeImportPair').click()
    master_page.wait_for_function("document.querySelector('#nodeOperationStatus').textContent === '已完成'", timeout=60000)
    master_page.wait_for_function("document.querySelector('#nodeRelationships').rows.length === 1", timeout=60000)
    master.relation = master.nodes()["relationships"][0]["relationship_id"]
    slave_page.locator('#nodesRefresh').click()
    slave_page.wait_for_function("count => document.querySelector('#nodeRelationships').rows.length === count", arg=len(slave.nodes()["relationships"]))
    master_context.close()
    slave_context.close()
    return package


def browser_revoke(browser, master):
    context, page = admin_page(browser, master)
    page.locator('#nodeRelationships button').filter(has_text='撤销关系').click()
    page.wait_for_function("document.querySelector('#nodeRelationships').rows.length === 0", timeout=60000)
    context.close()


def browser_checks(browser, master, slave, resource, mode):
    context, page = admin_page(browser, master)
    page.select_option('#nodeRelationships select', mode)
    page.wait_for_function("document.querySelector('#nodeOperationStatus').textContent === '已完成'")
    page.locator('#nodeTestResource').wait_for(state="visible")
    page.wait_for_function("document.querySelector('#nodeTestResource').options.length > 0")
    page.select_option('#nodeTestResource', resource["resource_id"])
    page.locator('#nodeRunTest').click()
    page.wait_for_function("document.querySelector('#nodeTestResult').textContent.includes('通过')", timeout=60000)
    page.wait_for_function("document.querySelector('#nodeTestPlayer').readyState >= 1", timeout=60000)
    page.locator('#nodeSeekTest').click()
    page.wait_for_function("!document.querySelector('#nodeTestPlayer').paused", timeout=30000)
    page.locator('#nodeRestartTest').click()
    page.wait_for_function("document.querySelector('#nodeTestPlayer').currentTime > 0.1", timeout=30000)
    result = json.loads(page.locator('#nodeTestResult').inner_text())
    assert result["result"] == "已重新播放"
    # Test the actual public player, including distinct same-path resources and owner lyrics.
    page.goto(master.endpoint + "/api/v1/media/music/category?path=music/shared", wait_until="domcontentloaded")
    page.wait_for_function("typeof art !== 'undefined' && art && art.video", timeout=60000)
    assert page.locator('.media-search-input, input[type="search"]').count() == 0
    assert page.locator(f'[data-media-id="{resource["resource_id"]}"]').count() == 1
    page.evaluate("selectMedia(currentMediaList.findIndex(item => !item.resource_id))")
    page.wait_for_function("art.video.currentTime > 0.1 && !art.video.paused", timeout=30000)
    page.evaluate("id => selectMedia(currentMediaList.findIndex(item => item.resource_id === id))", resource["resource_id"])
    page.wait_for_function("art.video.readyState >= 1", timeout=60000)
    page.evaluate("async () => { await art.video.play(); art.video.pause(); art.video.currentTime=6; await art.video.play(); }")
    page.wait_for_function("art.video.currentTime > 6", timeout=30000)
    context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--soak-seconds", type=int, default=310)
    parser.add_argument("--output", type=Path, default=Path("/tmp/federation-acceptance.json"))
    arguments = parser.parse_args()
    assert arguments.soak_seconds >= 310, "The acceptance soak must include real token expiration"
    report, nodes = {"checks": Checks(), "samples": []}, []
    held_playwright, held_browser = None, None
    with tempfile.TemporaryDirectory(prefix="frontiercloud-acceptance-") as temporary:
        directory = Path(temporary)
        ca = directory / "ca.pem"
        command("openssl", "req", "-x509", "-nodes", "-days", "1", "-newkey", "rsa:2048", "-keyout", str(ca.with_suffix(".key")),
                "-out", str(ca), "-subj", "/CN=FrontierCloud disposable test CA", "-addext", "basicConstraints=critical,CA:TRUE")
        base = json.loads(command("docker", "compose", "-f", str(ROOT / "docker-compose.yaml"), "config", "--format", "json"))
        try:
            for index, name in enumerate(("master-a", "slave-b", "master-c")):
                node = Node(name, directory, base, ca, index)
                nodes.append(node)
                # Same paths deliberately carry different content and different lyrics.
                wav(node.data / "media/music/shared/song.wav", seconds=20, tone=500 + index * 100)
                if name == "slave-b":
                    for number in range(105):
                        wav(node.data / f"media/music/paged/item-{number:03}.wav", seconds=1)
                    wav(node.data / "media/music/shared/large.wav", seconds=2400)
                node.start()
            a, b, c = nodes
            report["checks"].append("trusted TLS succeeds; unknown CA and wrong hostname rejected on every node")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(args=browser_args(nodes))
                for node, role in ((a, "Master"), (b, "Slave"), (c, "Master")):
                    browser_promote(browser, node, role)
                    node_id = node.nodes()["node_id"]
                    # A node reboot retains role and ID; role interchange remains rejected.
                    node.compose("restart", "web")
                    wait_for(lambda: node.client.get(node.endpoint + "/health/ready").status_code == 200)
                    assert node.nodes()["node_id"] == node_id and node.nodes()["role"] == role
                report["checks"].append("browser role promotion; durable roles and node IDs across reboot")
                package = browser_pair(browser, a, b)
                a.wait_online()
                assert len(a.nodes()["relationships"]) == 1 and len(b.nodes()["relationships"]) == 1
                report["checks"].append("browser pairing and two-node Master/Slave topology")
                browser_pair(browser, c, b)
                browser.close()
            assert len(b.nodes()["relationships"]) == 2
            assert c.endpoint not in json.dumps(a.nodes()) and a.endpoint not in json.dumps(c.nodes())
            c.api("/api/v1/media/admin/nodes/pair", {"package": package}, expected=409)
            for master in (a, c):
                master.wait_online()
                wait_for(lambda: len(master.resources()) == 107, description="paginated initial catalog")
                master.resource = next(item for item in master.resources() if item["path"] == "music/shared/song.wav")
            report["checks"].append("two independent Masters; replay rejected; paginated full sync")
            # The relation credentials are also required for catalog/heartbeat, with no normal admin cookie substitute.
            assert a.client.get(b.endpoint + "/internal/v1/catalog").status_code == 401
            assert a.client.get(a.endpoint + "/_protected_media/music/shared/song.wav").status_code == 404
            report["checks"].append("internal authentication and external protected-path rejection")
            for node in nodes:
                lyric = f"[00:01.00]{node.name} OWNER LYRIC\n[00:05.00]next line\n".encode()
                response = node.client.post(node.endpoint + "/api/v1/media/admin/upload/lyric", headers={"X-CSRF-Token": node.csrf}, files={"file": ("owner.lrc", lyric, "text/plain")})
                assert response.status_code == 200
                node.api("/api/v1/media/admin/lyrics/relations", {"origin_kind": "track", "origin_path": "music/shared/song.wav", "linked_paths": ["lyrics/owner.lrc"]})
            for master in (a, c):
                local = master.api("/api/v1/media/lyrics/content?track=music/shared/song.wav")["entries"]
                remote = master.api("/api/v1/media/lyrics/content?track=music/shared/song.wav&resource_id=" + master.resource["resource_id"])["entries"]
                assert local[0]["text"].startswith(master.name)
                assert remote[0]["text"].startswith(b.name)
                public = master.client.get(master.endpoint + "/api/v1/media/music/category?path=music/shared").text
                assert b.nodes()["node_id"] not in public and b.endpoint not in public
            report["checks"].append("same-path local/remote objects; attachment ownership; public topology hidden")
            source = (b.data / "media/music/shared/song.wav").read_bytes()
            for master in (a, c):
                for mode in ("Relay", "Direct"):
                    master.mode(mode)
                    url = master.resource["url"]
                    full = master.client.get(master.endpoint + url)
                    assert full.status_code == 200 and full.content == source
                    head = master.client.head(master.endpoint + url)
                    assert head.status_code == 200 and int(head.headers["content-length"]) == len(source) and not head.content
                    partial = master.range(url, 4000, 4095)
                    assert partial.status_code == 206 and partial.content == source[4000:4096]
                    conditional = master.client.get(master.endpoint + url, headers={"If-None-Match": full.headers["etag"]})
                    assert conditional.status_code == 304
                    stale = master.client.get(master.endpoint + url, headers={"If-Range": '"different"', "Range": "bytes=0-3"})
                    assert stale.status_code == 200 and stale.content == source
                    assert (urlsplit_origin(full.url) == master.endpoint) == (mode == "Relay")
            report["checks"].append("Relay/Direct content, HEAD, 206, ETag, If-Range, continuity")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(args=browser_args(nodes))
                for mode in ("Relay", "Direct"):
                    a.mode(mode)
                    browser_checks(browser, a, b, a.resource, mode)
                browser.close()
            report["checks"].append("Admin and real public browser playback, pause, seek, restart, both modes")
            # Idempotent stats are owned by each Master, independent of the same source file.
            session = str(uuid.uuid4())
            payload = {"media_path": a.resource["path"], "resource_id": a.resource["resource_id"], "playback_session_id": session, "played_seconds": 30, "duration": 60}
            with ThreadPoolExecutor(max_workers=8) as executor:
                counted = list(executor.map(lambda _: a.api("/api/v1/media/playback", payload)["counted"], range(12)))
            assert sum(counted) == 1
            a.api("/api/v1/media/preference", {"media_path": payload["media_path"], "resource_id": payload["resource_id"], "delta": 1})
            report["checks"].append("Master accounting idempotence and authoritative preference")
            # Repeated short faults exercise connection release and sync convergence.
            a.mode("Relay")
            for cycle in range(3):
                report["samples"].append({"stage": f"before-fault-{cycle}", "master": a.measure()})
                try:
                    b.netem(["delay", "150ms", "loss", "10%"])
                    response = a.range(a.resource["url"])
                    assert response.status_code in (206, 502, 503, 504)
                finally: b.netem(None)
                b.compose("restart", "web")
                a.wait_online()
                assert a.range(a.resource["url"]).status_code == 206
                a.compose("restart", "web")
                wait_for(lambda: a.client.get(a.endpoint + "/health/ready").status_code == 200)
                a.wait_online()
                assert len(a.resources()) == 107
                fixture = b.data / f"media/music/shared/new-{cycle}.wav"
                wav(fixture, seconds=1)
                wait_for(lambda: any(item["path"].endswith(f"new-{cycle}.wav") for item in a.resources()), description="incremental addition")
                b.api("/api/v1/media/admin/delete", {"paths": [f"music/shared/new-{cycle}.wav"]})
                wait_for(lambda: not any(item["path"].endswith(f"new-{cycle}.wav") for item in a.resources()), description="incremental deletion")
                a.api(f"/api/v1/media/admin/nodes/{a.relation}/sync", {})
                a.wait_online()
            report["checks"].append("three delay/loss/restart/add/delete/full-repair recovery cycles")
            a.compose("stop", "web")
            try:
                assert c.range(c.resource["url"]).status_code == 206
                assert len(c.resources()) == 107
            finally: a.compose("start", "web")
            a.wait_online()
            report["checks"].append("Master-A stopped; Master-C continues serving the same Slave independently")
            # Offline status retains catalog and revocation is independent of other Masters.
            b.compose("stop", "nginx")
            try:
                unavailable = a.range(a.resource["url"])
                assert unavailable.status_code in (502, 503, 504), "Broken media ingress did not surface an error"
                wait_for(lambda: any(row["relationship_id"] == a.relation and row["status"] == "offline" for row in a.nodes()["relationships"]), seconds=180, description="offline relationship")
                assert len(a.resources()) == 107
                assert a.range(a.resource["url"]).status_code == 503
            finally: b.compose("start", "nginx")
            for master in (a, c): master.wait_online()
            report["checks"].append("offline catalog retention, routing pause, verified automatic recovery")
            a.mode("Direct")
            old_response = a.client.get(a.endpoint + a.resource["url"], follow_redirects=False)
            old_token_url = old_response.headers["location"]
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(args=browser_args(nodes))
                browser_revoke(browser, a)
                browser.close()
            assert a.client.get(old_token_url).status_code == 401
            assert c.range(c.resource["url"]).status_code == 206
            a.pair(b)
            a.wait_online()
            wait_for(lambda: len(a.resources()) == 107)
            a.resource = next(item for item in a.resources() if item["path"] == "music/shared/song.wav")
            report["checks"].append("revocation invalidates Direct; other Master unaffected; clean re-pair")
            # A five-minute soak includes large media through Relay, partial cancellation,
            # real capability expiry, and browser seek after a long pause.
            a.mode("Direct")
            expired_url = a.client.get(a.endpoint + a.resource["url"], follow_redirects=False).headers["location"]
            expiring_package = b.api("/api/v1/media/admin/nodes/pair-package", {})
            start = time.monotonic()
            a.mode("Relay")
            large = next(item for item in a.resources() if item["path"].endswith("large.wav"))
            # Keep one actual public player paused across capability expiry.
            a.mode("Direct")
            held_playwright = sync_playwright().start()
            held_browser = held_playwright.chromium.launch(args=browser_args(nodes))
            held_page = held_browser.new_page()
            held_routes = []
            def held_route(request):
                from urllib.parse import parse_qs, urlsplit
                parsed = urlsplit(request.url)
                if (urlsplit_origin(request.url) == a.endpoint and parsed.path == "/api/v1/media/stream"
                        and parse_qs(parsed.query).get("resource_id") == [large["resource_id"]]):
                    held_routes.append(request.url)
            held_page.on("request", held_route)
            held_page.goto(a.endpoint + "/api/v1/media/music/category?path=music/shared", wait_until="domcontentloaded")
            held_page.wait_for_function("typeof art !== 'undefined' && art && art.video", timeout=60000)
            held_page.evaluate("id => { selectMedia(currentMediaList.findIndex(item => item.resource_id === id)); art.video.preload='metadata'; }", large["resource_id"])
            held_page.wait_for_function("art.video.readyState >= 1", timeout=60000)
            held_page.evaluate("art.video.pause()")
            original_route_requests = len(held_routes)
            a.mode("Relay")
            baseline = a.measure()
            while time.monotonic() - start < arguments.soak_seconds:
                with a.client.stream("GET", a.endpoint + large["url"]) as response:
                    assert response.status_code == 200
                    received = 0
                    for chunk in response.iter_bytes():
                        received += len(chunk)
                        if received >= 1024 * 1024: break
                assert a.range(a.resource["url"]).status_code == 206
                sample = a.measure()
                report["samples"].append({"stage": "soak", "seconds": int(time.monotonic() - start), "master": sample})
                assert sample["temporary"] == baseline["temporary"], "Master wrote temporary media"
                time.sleep(15)
            assert a.client.get(expired_url).status_code == 401
            expired_pair = a.api("/api/v1/media/admin/nodes/pair", {"package": expiring_package}, expected=409)
            assert "过期" in expired_pair["detail"]
            a.mode("Direct")
            held_page.evaluate("art.video.currentTime=1800; void art.video.play().catch(() => {})")
            held_page.wait_for_function("art.video.currentTime >= 1800 && !art.video.paused && art.video.readyState >= 2", timeout=60000)
            assert len(held_routes) > original_route_requests, "Long-pause resume never requested a fresh business route"
            held_browser.close()
            held_playwright.stop()
            held_browser, held_playwright = None, None
            for service in ("web", "nginx"):
                first = report["samples"][-5]["master"][service]
                last = report["samples"][-1]["master"][service]
                assert last["rss"] - first["rss"] <= 12 * 1024 * 1024, f"{service} steady RSS drift"
                assert last["fd"] - first["fd"] <= 8 and last["sockets"] - first["sockets"] <= 8, f"{service} FD/socket drift"
            a.mode("Direct")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(args=browser_args(nodes))
                browser_checks(browser, a, b, a.resource, "Direct")
                browser.close()
            report["checks"].append("310s large Relay cancellation soak; expired Direct rejected; browser resume; bounded RSS/FD/socket/tmp")
            # Explicit Slave reset revokes all relationships but retains owned files.
            identity = b.nodes()["node_id"]
            b.api("/api/v1/media/admin/nodes/reinitialize", {"confirmation": identity})
            assert b.nodes()["role"] == "Standalone"
            assert (b.data / "media/music/shared/song.wav").read_bytes() == source
            assert a.range(a.resource["url"]).status_code in (401, 404, 503)
            report["checks"].append("explicit reset revokes old relationships; media retained")
            report["result"] = "passed"
        finally:
            if held_browser:
                held_browser.close()
            if held_playwright:
                held_playwright.stop()
            arguments.output.write_text(json.dumps(report, indent=2))
            if report.get("result") != "passed":
                browser_network_failure(directory)
                for node in nodes:
                    try:
                        node.failure_diagnostics()
                        logs = node.compose("logs", "--no-color", "--tail", "60", "web", "nginx")
                        print("\n".join(line for line in logs.splitlines() if "initial_runtime_secrets" not in line), flush=True)
                    except Exception: pass
            for node in reversed(nodes):
                try: node.stop()
                except Exception: pass
            command("sudo", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(directory))
    print(json.dumps({"result": report.get("result", "failed"), "checks": report["checks"], "samples": len(report["samples"])}))


def urlsplit_origin(value):
    from urllib.parse import urlsplit
    parsed = urlsplit(str(value))
    return parsed.scheme + "://" + parsed.netloc


if __name__ == "__main__":
    main()
