"""Disposable two-node HTTPS and browser acceptance harness.

Infrastructure is test-only. Compose configurations and private CA material are
generated in a temporary directory; no hosted user data or deployment is used.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
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
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE_SUBNET = "172.30.251.0/24"
ACCEPTANCE_GATEWAY = "172.30.251.1"


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
    def __init__(self, name, directory, base, ca, bundle, index):
        self.name, self.directory = name, directory / name
        self.directory.mkdir()
        self.host = ACCEPTANCE_GATEWAY
        self.port = 14443 + index
        self.project = "fc-acceptance-" + name
        self.configuration = self.directory / "compose.json"
        self.ca, self.client = ca, None
        self.endpoint = f"https://{self.host}:{self.port}"
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
        extensions.write_text(
            f"subjectAltName=IP:{self.host}\n"
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectKeyIdentifier=hash\n"
            "authorityKeyIdentifier=keyid,issuer\n"
        )
        command("openssl", "x509", "-req", "-in", str(request), "-CA", str(ca), "-CAkey", str(ca.with_suffix(".key")),
                "-CAcreateserial", "-out", str(cert), "-days", "1", "-extfile", str(extensions))
        public_key = command("openssl", "x509", "-in", str(cert), "-pubkey", "-noout")
        public_key_der = subprocess.check_output(
            ["openssl", "pkey", "-pubin", "-outform", "DER"],
            input=public_key.encode(),
        )
        self.spki = base64.b64encode(hashlib.sha256(public_key_der).digest()).decode()
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
            if name in ("web", "secrets-init", "media-init"):
                service["image"] = "frontiercloud-acceptance-web"
            elif name == "nginx":
                service["image"] = "frontiercloud-acceptance-nginx"
                service["ports"] = [{"target": 443, "published": str(self.port), "host_ip": ACCEPTANCE_GATEWAY, "protocol": "tcp"}]
            if name in ("web", "nginx"):
                service.setdefault("environment", {}).update(TLS_ENABLED="true", SERVER_NAME=self.host, INSTANCE_NAME="acceptance")
                service.setdefault("volumes", []).append({
                    "type": "bind", "source": str(bundle),
                    "target": "/etc/ssl/certs/ca-certificates.crt", "read_only": True,
                })
            if name == "web":
                service["environment"]["SSL_CERT_FILE"] = "/etc/ssl/certs/ca-certificates.crt"
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
        with socket.create_connection((self.host, self.port), timeout=10) as connection:
            with trusted.wrap_socket(connection, server_hostname=self.host):
                pass
        unknown_ca = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        for context, hostname in ((unknown_ca, self.host), (trusted, "wrong.frontiercloud.local")):
            try:
                with socket.create_connection((self.host, self.port), timeout=10) as connection:
                    with context.wrap_socket(connection, server_hostname=hostname):
                        pass
            except ssl.SSLCertVerificationError:
                continue
            raise AssertionError("Untrusted CA or mismatched TLS hostname was accepted")
        self.client = httpx.Client(verify=ssl.create_default_context(cafile=str(self.ca)),
                                   trust_env=False, timeout=20, follow_redirects=True)
        wait_for(lambda: self.client.get(self.endpoint + "/health/ready").status_code == 200,
                 description=f"{self.name} verified HTTPS readiness")
        key = self.web("from app.core.config import ADMIN_KEY_FILE; print(ADMIN_KEY_FILE.read_text().strip())")
        response = self.client.post(self.endpoint + "/api/v1/media/admin/elevate", data={"token": key})
        assert response.status_code == 200, f"{self.name} admin login failed: {response.text[:300]}"
        self.csrf = self.client.cookies.get("__Host-admin-csrf")
        assert self.csrf
        identity = self.nodes()
        verified_id = self.web(
            "import asyncio; from app.services.federation.transport import transport; "
            f"print(asyncio.run(transport.identity({self.endpoint!r}, expected_id={identity['node_id']!r}, role='Standalone'))['node_id'])"
        )
        assert verified_id == identity["node_id"]

    def api(self, path, value=None, *, expected=200):
        response = (self.client.get(self.endpoint + path) if value is None else
                    self.client.post(self.endpoint + path, json=value, headers={"X-CSRF-Token": self.csrf}))
        assert response.status_code == expected, f"{self.name}: {path.split('?')[0]} status {response.status_code}, expected {expected}"
        return response.json()

    def nodes(self):
        return self.api("/api/v1/media/admin/nodes")

    def promote(self, role):
        self.api("/api/v1/media/admin/nodes/promote", {
            "role": role,
            "endpoint": self.endpoint,
            "local_capacity_gib": 1 if role == "Master" else None,
        })

    def pair(self, follower):
        package = follower.api("/api/v1/media/admin/nodes/pair-package", {})
        result = self.api("/api/v1/media/admin/nodes/pair", {"package": package})
        self.relation = result["relationship_id"]
        return package

    def resources(self):
        program = """
import asyncio, json
from app.services.federation.catalog import catalog
async def read():
    rows = await catalog.resources()
    return [{"resource_id": row["resource_id"], "path": row["path"],
             "size": int(row["payload"]["size"]),
             "url": "/api/v1/media/stream?resource_id=" + row["resource_id"]}
            for row in rows]
print(json.dumps(asyncio.run(read())))
"""
        return json.loads(self.web(program))

    def wait_online(self, after=0):
        def ready():
            rows = self.nodes()["relationships"]
            return any(row["relationship_id"] == self.relation and row["status"] == "online"
                       and (row["last_heartbeat"] or 0) > after for row in rows)
        wait_for(ready, description=f"{self.name} relationship recovery")

    def relationship(self):
        return next(row for row in self.nodes()["relationships"] if row["relationship_id"] == self.relation)

    def mode(self, value):
        self.api(f"/api/v1/media/admin/nodes/{self.relation}/mode", {"mode": value})

    def configure_resources(self, *, storage=True, capacity_gib=2, compute=True, backup=True):
        return self.api(f"/api/v1/media/admin/nodes/{self.relation}/resources", {
            "storage_enabled": storage,
            "storage_capacity_gib": capacity_gib if storage else 0,
            "compute_enabled": compute,
            "worker_slots": 1 if compute else 0,
            "backup_enabled": backup,
        })

    def upload_media(self, blob, filename, *, member_id):
        reservation = self.api("/api/v1/media/admin/upload/session", {
            "storage_member_id": member_id,
            "target_dir": "music/shared",
            "relative_path": None,
            "filename": filename,
            "size_bytes": len(blob),
        })
        headers = {"Content-Type": "application/octet-stream"}
        upload_url = reservation["upload_url"]
        if reservation["transport"] == "Direct":
            headers["Origin"] = self.endpoint
            response = self.client.put(upload_url, headers=headers, content=blob)
            assert response.status_code == 200, response.text[:300]
            self.api(f"/api/v1/media/admin/upload/session/{reservation['upload_id']}/finalize", {})
        else:
            headers["X-CSRF-Token"] = self.csrf
            response = self.client.put(self.endpoint + upload_url, headers=headers, content=blob)
            assert response.status_code == 200, response.text[:300]
        return next(row for row in self.resources() if row["resource_id"] == reservation["media_id"])

    def register_karaoke_user(self, username="ci_karaoke", password="Huawei@123"):
        challenge = self.client.get(self.endpoint + "/api/v1/karaoke/account/captcha").json()["challenge"]
        answer = self.web(
            "import asyncio; from app.core.redis import redis_client; "
            f"print(asyncio.run(redis_client.get('karaoke:captcha:{challenge}:image')))"
        )
        response = self.client.post(self.endpoint + "/api/v1/karaoke/account/register", json={
            "username": username, "password": password, "challenge": challenge,
            "captcha": answer, "webrtc_addresses": [],
        })
        assert response.status_code == 200, response.text
        self.kcsrf = self.client.cookies.get("__Host-karaoke_csrf")
        assert self.kcsrf
        return response.json()["user"]

    def karaoke_api(self, path, value=None, *, method="POST", expected=200):
        headers = {"X-Karaoke-CSRF": self.kcsrf}
        if value is not None:
            headers["Content-Type"] = "application/json"
        response = self.client.request(method, self.endpoint + "/api/v1/karaoke/account" + path,
                                       headers=headers, json=value)
        assert response.status_code == expected, f"karaoke {path}: {response.status_code} {response.text[:300]}"
        return response.json() if response.content else {}

    def upload_recording(self, blob, title):
        ticket = self.karaoke_api("/recordings/ticket", {
            "size_bytes": len(blob), "content_type": "audio/webm", "title": title,
        })
        headers = {"Content-Type": "audio/webm"}
        if ticket["direct"]:
            preflight = self.client.options(ticket["upload_url"], headers={
                "Origin": self.endpoint,
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "Content-Type, X-Recording-Capability",
            })
            assert preflight.status_code == 200, preflight.text[:300]
            assert preflight.headers.get("access-control-allow-origin") == self.endpoint
            headers["X-Recording-Capability"] = ticket["capability"]
            headers["Origin"] = self.endpoint
        else:
            headers["X-Karaoke-CSRF"] = self.kcsrf
        response = self.client.put(ticket["upload_url"] if ticket["direct"] else self.endpoint + ticket["upload_url"],
                                   headers=headers, content=blob)
        assert response.status_code == 200, response.text[:300]
        self.karaoke_api(f"/recordings/{ticket['recording_id']}/finalize", {})
        return ticket

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
        nginx_temporary = self.compose("exec", "-T", "nginx", "sh", "-c",
            "find /tmp /var/cache/nginx -type f -exec stat -c '%s' {} \\; 2>/dev/null | awk '{files++; bytes+=$1} END {printf \"%d %d\", files, bytes}'")
        files, size = map(int, nginx_temporary.split())
        samples["temporary"].update(nginx_files=files, nginx_bytes=size)
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
            "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream",
            "--ignore-certificate-errors-spki-list=" + ",".join(node.spki for node in nodes),
            "--log-net-log=" + str(nodes[0].directory.parent / "browser-network.json")]


def launch_browser(playwright, nodes):
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    options = {"args": browser_args(nodes)}
    if executable:
        options["executable_path"] = executable
    return playwright.chromium.launch(**options)


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
    page.goto(node.endpoint + "/api/v1/media/admin/", wait_until="domcontentloaded")
    page.locator('#nodesPanel .module-heading').click()
    page.wait_for_function("document.querySelector('#nodeIdentity').textContent.includes(' / v')")
    return context, page


def browser_promote(browser, node, role):
    context, page = admin_page(browser, node)
    page.select_option('#nodeRole', role)
    page.fill('#nodeEndpoint', node.endpoint)
    page.locator('#nodePromotion button[type="submit"]').click()
    page.wait_for_function("document.querySelector('#nodeOperationStatus').textContent && document.querySelector('#nodeOperationStatus').textContent !== '处理中'")
    operation = page.locator('#nodeOperationStatus').text_content()
    assert operation == '已完成', f"{node.name} promotion failed: {operation}"
    page.wait_for_function("role => document.querySelector('#nodeIdentity').textContent.startsWith(role)", arg=role)
    assert not page.locator('#nodePromotion').is_visible()
    context.close()


def browser_pair(browser, master, follower):
    follower_context, follower_page = admin_page(browser, follower)
    follower_page.locator('#nodeIssuePair').click()
    follower_page.wait_for_function("document.querySelector('#nodePairPackage').value.includes('signature')")
    package = json.loads(follower_page.input_value('#nodePairPackage'))
    master_context, master_page = admin_page(browser, master)
    master_page.fill('#nodePairPackage', json.dumps(package))
    master_page.locator('#nodeImportPair').click()
    master_page.wait_for_function("document.querySelector('#nodeOperationStatus').textContent === '已完成'", timeout=60000)
    master_page.wait_for_function(
        "peer => Array.from(document.querySelector('#nodeRelationships').rows)"
        ".some(row => row.textContent.includes(peer))",
        arg=follower.nodes()["node_id"], timeout=60000,
    )
    master.relation = master.nodes()["relationships"][0]["relationship_id"]
    follower_page.locator('#nodesRefresh').click()
    follower_page.wait_for_function("count => document.querySelector('#nodeRelationships').rows.length === count", arg=len(follower.nodes()["relationships"]))
    master_context.close()
    follower_context.close()
    return package


def browser_revoke(browser, master):
    context, page = admin_page(browser, master)
    page.locator('#nodeRelationships button').filter(has_text='撤销关系').click()
    page.wait_for_function("document.querySelector('#nodeRelationships').rows.length === 0", timeout=60000)
    context.close()


def browser_checks(browser, master, resource):
    context = browser.new_context()  # TLS errors are never ignored.
    context.grant_permissions(["microphone"], origin=master.endpoint)
    page = context.new_page()
    page.on("console", lambda message: print(f"browser-console: {message.type}: {message.text}", flush=True))
    page.on("pageerror", lambda error: print(f"browser-pageerror: {error}", flush=True))
    # Joining is accepted only with working media routes; exercise the actual public player.
    page.goto(master.endpoint + "/api/v1/media/music/category?path=music/shared", wait_until="domcontentloaded")
    page.wait_for_function("typeof art !== 'undefined' && art && art.video", timeout=60000)
    page.wait_for_function("currentMediaList.length > 0", timeout=30000)
    assert page.locator('.media-search-input, input[type="search"]').count() == 0
    assert page.locator(f'[data-media-id="{resource["resource_id"]}"]').count() == 1
    page.evaluate("id => selectMedia(currentMediaList.findIndex(item => item.resource_id === id))", resource["resource_id"])
    page.wait_for_function("art.video.currentTime > 0.1 && !art.video.paused", timeout=30000)
    page.wait_for_function("activeLyricEntries.length > 0", timeout=10000)
    player_url = page.url
    playback_position = page.evaluate("art.video.currentTime")
    page.locator('#lyricsLink').click()
    page.wait_for_function("!document.querySelector('#fullscreenLyrics').classList.contains('hidden')")
    assert page.url == player_url and page.locator('.fullscreen-lyrics-column').count() == 3
    page.locator('#fullscreenLyrics').click(position={"x": 12, "y": 12})
    page.wait_for_function("document.querySelector('#fullscreenLyrics').classList.contains('hidden')")
    page.wait_for_function("position => art.video.currentTime >= position && !art.video.paused", arg=playback_position)
    assert page.url == player_url
    page.evaluate("async () => { await art.video.play(); art.video.pause(); art.video.currentTime=1; await art.video.play(); }")
    page.wait_for_function("art.video.currentTime > 1", timeout=10000)
    karaoke_id = page.evaluate(
        "id => currentMediaList.find(item => item.resource_id === id).karaoke_id",
        resource["resource_id"],
    )
    assert karaoke_id and resource["resource_id"] not in karaoke_id
    page.goto(master.endpoint + "/karaoke/?media=" + karaoke_id, wait_until="domcontentloaded")
    expect(page.locator('#title')).not_to_have_text('正在载入当前媒体…', timeout=30000)
    expect(page.locator('#capabilities')).to_contain_text('输出设备选择', timeout=30000)
    assert page.locator('#inputDevice').count() == 1
    assert page.locator('#outputDevice').count() == 1
    assert page.locator('#play').count() == 0
    assert not page.locator('#previewCard').is_visible()
    assert "Web Audio 人声处理" in page.locator('.route').text_content()
    page.locator('#fullLyrics').click()
    expect(page.locator('#lyricsOverlay')).to_be_visible()
    page.locator('#lyricsOverlay').click(position={"x": 12, "y": 12})
    expect(page.locator('#lyricsOverlay')).to_be_hidden()
    # The first click must be a trusted browser gesture so microphone permission
    # follows the same path as production. The synthetic second click verifies
    # that the initialization guard rejects a concurrent start.
    page.locator('#record').click()
    page.locator('#record').dispatch_event('click')
    expect(page.locator('#status')).to_contain_text('正在录制纯人声支路', timeout=15000)
    expect(page.locator('#stop')).to_be_enabled()
    page.locator('#pauseResume').click()
    expect(page.locator('#pauseResume')).to_have_text('恢复')
    expect(page.locator('#status')).to_contain_text('已暂停')
    page.locator('#pauseResume').click()
    expect(page.locator('#pauseResume')).to_have_text('暂停')
    expect(page.locator('#status')).to_contain_text('已恢复')
    page.wait_for_timeout(1100)
    page.locator('#stop').click()
    expect(page.locator('#previewCard')).to_be_visible(timeout=10000)
    expect(page.locator('#status')).to_contain_text('录音已停止')
    assert page.locator('#upload').is_disabled()
    context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("/tmp/federation-acceptance.json"))
    arguments = parser.parse_args()
    report, nodes = {"checks": Checks(), "samples": []}, []
    network = "fc-acceptance-" + uuid.uuid4().hex[:12]
    command("docker", "network", "create", "--subnet", ACCEPTANCE_SUBNET,
            "--gateway", ACCEPTANCE_GATEWAY, network)
    with tempfile.TemporaryDirectory(prefix="frontiercloud-acceptance-") as temporary:
        directory = Path(temporary)
        ca = directory / "ca.pem"
        command("openssl", "req", "-x509", "-nodes", "-days", "1", "-newkey", "rsa:2048",
                "-keyout", str(ca.with_suffix(".key")), "-out", str(ca),
                "-subj", "/CN=FrontierCloud disposable test CA",
                "-addext", "basicConstraints=critical,CA:TRUE",
                "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                "-addext", "subjectKeyIdentifier=hash")
        bundle = directory / "ca-certificates.crt"
        bundle.write_bytes(Path("/etc/ssl/certs/ca-certificates.crt").read_bytes() + b"\n" + ca.read_bytes())
        base = json.loads(command("docker", "compose", "-f", str(ROOT / "docker-compose.yaml"),
                                  "config", "--format", "json"))
        try:
            for index, name in enumerate(("master-a", "follower-b")):
                node = Node(name, directory, base, ca, bundle, index)
                nodes.append(node)
            a, b = nodes
            # Master Local content is imported once during promotion. A Follower
            # must join empty and receives files only through Storage Pool jobs.
            wav(a.data / "media/music/shared/master.wav", seconds=20, tone=500)
            upload_source = directory / "upload-source.wav"
            wav(upload_source, seconds=20, tone=700)
            source = upload_source.read_bytes()
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(Node.start, nodes))
            report["checks"].append("trusted TLS succeeds; unknown CA and wrong hostname rejected on every node")

            with sync_playwright() as playwright:
                browser = launch_browser(playwright, nodes)
                browser_promote(browser, a, "Master")
                browser_promote(browser, b, "Follower")
                browser_pair(browser, a, b)
                browser.close()
            a.wait_online()
            assert len(a.nodes()["relationships"]) == 1 and len(b.nodes()["relationships"]) == 1
            assert len(a.resources()) == 1
            report["checks"].append("fixed Master/Follower roles, stable identities, one-to-many topology")

            redirected = b.client.get(b.endpoint + "/api/v1/media", follow_redirects=False)
            assert redirected.status_code == 307 and redirected.headers["location"].startswith(a.endpoint)
            blocked = b.client.post(b.endpoint + "/api/v1/media/playback", json={})
            assert blocked.status_code == 409
            assert blocked.json()["code"] == "NODE_BUSINESS_DISABLED_ON_FOLLOWER"
            assert a.client.get(b.endpoint + "/internal/v1/catalog").status_code == 404
            assert a.client.get(a.endpoint + "/_protected_media/music/shared/master.wav").status_code == 404
            report["checks"].append("Follower business gate redirects public HTML and rejects business APIs")

            follower_id = b.nodes()["node_id"]
            a.configure_resources(storage=True, capacity_gib=2, compute=True, backup=True)
            follower_enabled = f"""
import asyncio
from sqlalchemy import text
from app.core.db import engine
async def read_enabled():
    async with engine.connect() as connection:
        return int(await connection.scalar(text(
            'SELECT storage_enabled FROM cluster_storage_members WHERE member_id=:member_id'
        ), {{'member_id': {follower_id!r}}}) or 0)
print(asyncio.run(read_enabled()))
"""
            wait_for(lambda: b.web(follower_enabled).strip() == "1",
                     description="Follower resource configuration heartbeat")

            a.mode("Relay")
            a.upload_media(source, "relay.wav", member_id=follower_id)
            a.mode("Direct")
            direct_resource = a.upload_media(source, "direct.wav", member_id=follower_id)
            a.resource = direct_resource
            assert len(a.resources()) == 3
            a.api("/api/v1/media/admin/upload/session", {
                "storage_member_id": follower_id, "target_dir": "music/shared",
                "relative_path": None, "filename": "direct.wav", "size_bytes": len(source),
            }, expected=409)
            report["checks"].append("Master reservations place Relay/Direct uploads and enforce global path uniqueness")

            lyric = b"[00:01.00]MASTER OWNED LYRIC\n[00:05.00]next line\n"
            response = a.client.post(a.endpoint + "/api/v1/media/admin/upload/lyric",
                                     headers={"X-CSRF-Token": a.csrf},
                                     files={"file": ("owner.lrc", lyric, "text/plain")})
            assert response.status_code == 200, response.text[:300]
            a.api("/api/v1/media/admin/lyrics/relations", {
                "origin_kind": "track", "origin_path": direct_resource["path"],
                "linked_paths": ["lyrics/owner.lrc"],
            })
            entries = a.api("/api/v1/media/lyrics/content?track=" + direct_resource["path"]
                            + "&resource_id=" + direct_resource["resource_id"])["entries"]
            assert entries[0]["text"] == "MASTER OWNED LYRIC"
            public = a.client.get(a.endpoint + "/api/v1/media/music/category?path=music/shared").text
            assert follower_id not in public and b.endpoint not in public
            report["checks"].append("lyrics stay on Master and bind global media identity independent of placement")

            for mode in ("Relay", "Direct"):
                a.mode(mode)
                url = direct_resource["url"]
                full = a.client.get(a.endpoint + url)
                assert full.status_code == 200 and full.content == source
                head = a.client.head(a.endpoint + url)
                assert head.status_code == 200 and int(head.headers["content-length"]) == len(source) and not head.content
                if mode == "Direct":
                    cors_head = a.client.head(a.endpoint + url, headers={"Origin": a.endpoint})
                    assert cors_head.status_code == 200
                    assert cors_head.headers.get("access-control-allow-origin") == a.endpoint
                    capability = a.client.get(a.endpoint + url, follow_redirects=False).headers["location"]
                    assert a.client.head(capability, headers={"Origin": "https://invalid.example"}).status_code == 403
                    preflight = a.client.options(capability, headers={
                        "Origin": a.endpoint, "Access-Control-Request-Method": "GET",
                        "Access-Control-Request-Headers": "Range, If-Range",
                    })
                    assert preflight.status_code == 200
                    assert preflight.headers.get("access-control-allow-origin") == a.endpoint
                partial = a.range(url, 4000, 4095)
                assert partial.status_code == 206 and partial.content == source[4000:4096]
                conditional = a.client.get(a.endpoint + url, headers={"If-None-Match": full.headers["etag"]})
                assert conditional.status_code == 304
                stale = a.client.get(a.endpoint + url,
                                     headers={"If-Range": '"different"', "Range": "bytes=0-3"})
                assert stale.status_code == 200 and stale.content == source
                assert (urlsplit_origin(full.url) == a.endpoint) == (mode == "Relay")
            report["checks"].append("Relay/Direct media supports HEAD, Range, ETag, CORS and continuity")

            baseline_used = next(row for row in a.nodes()["storage_pool"]["members"]
                                 if row["member_id"] == follower_id)["used_bytes"]
            user = a.register_karaoke_user()
            status = a.client.get(a.endpoint + "/api/v1/karaoke/account/status").json()
            assert status["authenticated"] and status["user"]["quota_bytes"] == 200 * 1024 * 1024
            lyrics_metadata = json.dumps({
                "version": 1, "title": "round-trip",
                "lyrics": [{"time": 1.0, "text": "stored lyric"}],
            }, ensure_ascii=False, separators=(",", ":")).encode()
            trailer = len(lyrics_metadata).to_bytes(8, "big") + b"FRONTIERCLOUD-KARAOKE-V1"
            for mode in ("Relay", "Direct"):
                a.mode(mode)
                ticket = a.upload_recording(b"WEBM-VOICE-ONLY" + lyrics_metadata + trailer,
                                            f"{mode} recording")
                listing = a.client.get(a.endpoint + "/api/v1/karaoke/account/recordings").json()["items"]
                item = next(row for row in listing if row["recording_id"] == ticket["recording_id"])
                assert item["title"] == "round-trip" and item["lyrics"][0]["text"] == "stored lyric"
                streamed = a.client.get(a.endpoint +
                    f"/api/v1/karaoke/account/recordings/{ticket['recording_id']}/stream")
                assert streamed.status_code == 200 and streamed.content.startswith(b"WEBM-VOICE-ONLY")
                downloaded = a.client.get(a.endpoint +
                    f"/api/v1/karaoke/account/recordings/{ticket['recording_id']}/download")
                assert downloaded.status_code == 200 and "attachment" in downloaded.headers["content-disposition"]

            a.api(f"/api/v1/media/admin/users/{user['user_id']}", {"action": "ban", "quota_mib": None})
            assert a.client.get(a.endpoint + "/api/v1/karaoke/account/recordings").status_code == 401
            blocked_login = a.client.post(a.endpoint + "/api/v1/karaoke/account/login", json={
                "username": user["username"], "password": "Huawei@123", "webrtc_addresses": [],
            })
            assert blocked_login.status_code == 403
            a.api(f"/api/v1/media/admin/users/{user['user_id']}", {"action": "unban", "quota_mib": None})
            login = a.client.post(a.endpoint + "/api/v1/karaoke/account/login", json={
                "username": user["username"], "password": "Huawei@123", "webrtc_addresses": [],
            })
            assert login.status_code == 200
            a.kcsrf = a.client.cookies.get("__Host-karaoke_csrf")
            a.karaoke_api("/logout", {})
            a.api(f"/api/v1/media/admin/users/{user['user_id']}", {"action": "delete", "quota_mib": None})
            assert not any((b.data / "recordings").rglob("*.bin"))
            wait_for(lambda: next(row for row in a.nodes()["storage_pool"]["members"]
                                  if row["member_id"] == follower_id)["used_bytes"] == baseline_used,
                     description="recording capacity release")
            report["checks"].append("automatic recording placement, Relay/Direct, quota, lyrics, ban and deletion")

            with sync_playwright() as playwright:
                browser = launch_browser(playwright, nodes)
                for mode in ("Relay", "Direct"):
                    a.mode(mode)
                    browser_checks(browser, a, direct_resource)
                browser.close()
            report["checks"].append("real public browser playback and ephemeral karaoke work in both modes")

            session = str(uuid.uuid4())
            payload = {"media_path": direct_resource["path"],
                       "resource_id": direct_resource["resource_id"],
                       "playback_session_id": session, "played_seconds": 30, "duration": 60}
            with ThreadPoolExecutor(max_workers=8) as executor:
                counted = list(executor.map(
                    lambda _: a.api("/api/v1/media/playback", payload)["counted"], range(12)))
            assert sum(counted) == 1
            a.api("/api/v1/media/admin/media-priority", {
                "media_path": payload["media_path"], "resource_id": payload["resource_id"], "value": 123,
            })
            report["checks"].append("Master facts keep playback idempotent and preference authoritative")

            identity = follower_id
            b.api("/api/v1/media/admin/nodes/reinitialize", {"confirmation": identity}, expected=409)
            assert b.nodes()["role"] == "Follower"
            assert (b.data / "media/music/shared/direct.wav").read_bytes() == source
            assert a.range(direct_resource["url"]).status_code == 206
            report["checks"].append("Follower with placed files cannot reset; identities and media remain intact")
            report["result"] = "passed"
        finally:
            arguments.output.write_text(json.dumps(report, indent=2))
            if report.get("result") != "passed":
                browser_network_failure(directory)
                for node in nodes:
                    try:
                        node.failure_diagnostics()
                        logs = node.compose("logs", "--no-color", "--tail", "60", "web", "nginx")
                        print("\n".join(line for line in logs.splitlines()
                                         if "initial_runtime_secrets" not in line), flush=True)
                    except Exception:
                        pass
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(lambda node: node.stop(), reversed(nodes)))
            command("sudo", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(directory))
            subprocess.run(["docker", "network", "rm", network], capture_output=True)
    print(json.dumps({"result": report.get("result", "failed"), "checks": report["checks"],
                      "samples": len(report["samples"])}))

def urlsplit_origin(value):
    from urllib.parse import urlsplit
    parsed = urlsplit(str(value))
    return parsed.scheme + "://" + parsed.netloc


if __name__ == "__main__":
    main()
