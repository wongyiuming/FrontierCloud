"""Disposable three-node HTTPS and browser acceptance harness.

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
import re
import socket
import ssl
import struct
import subprocess
import tempfile
import time
import traceback
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE_SUBNET = "172.30.251.0/24"
ACCEPTANCE_GATEWAY = "172.30.251.1"
DOMAIN_ADDRESSES = {}
_original_getaddrinfo = socket.getaddrinfo


def fixture_getaddrinfo(host, port, *arguments, **kwargs):
    # Resolver injection stays inside this disposable test process. Product
    # containers resolve the same names through isolated Docker network aliases.
    return _original_getaddrinfo(DOMAIN_ADDRESSES.get(host, host), port, *arguments, **kwargs)


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
    def __init__(self, name, directory, base, ca, bundle, index, *, network):
        self.name, self.directory = name, directory / name
        self.directory.mkdir()
        self.host = f"172.30.251.{11+index}"
        self.hostname = name+".4399.com"
        self.storage = index != 0
        self.profile = "relay" if index == 2 else "direct"
        self.port = 8443 if self.storage and self.profile == "relay" else 443
        DOMAIN_ADDRESSES[self.hostname] = self.host
        self.project = "fc-acceptance-" + directory.name[-12:] + "-" + name
        self.configuration = self.directory / "compose.json"
        self.ca, self.client = ca, None
        self.endpoint = f"https://{self.hostname}"+(f":{self.port}" if self.port!=443 else "")
        self.csrf = ""
        self.relation = ""
        self.resource = None
        self.data = self.directory / "data"
        (self.data / "media/music/shared").mkdir(parents=True)
        (self.data / "media/vido").mkdir(parents=True)
        (self.data / "media/lyrics").mkdir(parents=True)
        self.spki = ""
        if self.storage:
            environment={**os.environ,"TLS_ENABLED":"true","SERVER_NAME":self.hostname,"STORAGE_TRANSPORT":self.profile,
                         "STORAGE_PORT":str(self.port),"STORAGE_ENDPOINT":self.endpoint,"DATA_DIRECTORY":str(self.data)}
            base=json.loads(command("docker","compose","-f",str(ROOT/"docker-compose.storage.yaml"),"config","--format","json",env=environment))
        configuration = copy.deepcopy(base)
        configuration.pop("name", None)
        for volume in configuration.get("volumes", {}).values():
            volume.pop("name", None)
        configuration["networks"] = {"default": {"name": self.project+"-private"},"fleet":{"external":True,"name":network}}
        for name, service in configuration["services"].items():
            service.pop("container_name", None)
            service.pop("env_file", None)
            service.pop("build", None)
            service.pop("ports", None)
            service.pop("network_mode", None)
            if name in ("web", "secrets-init", "media-init", "certificate-init", "certificate-agent"):
                service["image"] = "frontiercloud-python-fleet:trusted-web"
            elif name == "nginx":
                service["image"] = "frontiercloud-python-fleet:trusted-nginx"
            if name in ("web", "nginx", "certificate-agent"):
                service.setdefault("environment", {}).update(TLS_ENABLED="true", SERVER_NAME=self.hostname, INSTANCE_NAME="acceptance")
                if self.storage:
                    service["environment"].update(STORAGE_TRANSPORT=self.profile,STORAGE_PORT=str(self.port),STORAGE_ENDPOINT=self.endpoint)
            if name == "web":
                service["cpus"] = 1.0
            if name == "certificate-agent":
                service["command"]=["python","-m","tests.storage_certificate_agent_fixture"]
                service.setdefault("volumes",[]).append({"type":"bind","source":str(ca.parent),"target":"/test-ca","read_only":True})
            if self.storage and name=="web":
                service["environment"]["TRUSTED_PROXY_NETWORKS"]=""
            service["networks"]={"default":{}}
            if name == "web" and not self.storage:
                # Business makes verified outbound requests to both appliances;
                # its Nginx remains the only public entry on this test network.
                service["networks"]["fleet"] = {}
            public_service = name == ("web" if self.storage else "nginx")
            if public_service:
                service["networks"]["default"]["aliases"]=[self.hostname]
                service["networks"]["fleet"]={"ipv4_address":self.host,"aliases":[self.hostname]}
            for volume in service.get("volumes", []):
                if volume.get("target") == "/app/data":
                    volume["source"] = str(self.data)
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
        if not self.storage:self.compose("exec", "-T", "nginx", "nginx", "-t")
        trusted = ssl.create_default_context(cafile=str(self.ca))
        def served():
            with socket.create_connection((self.host,self.port),timeout=3) as connection:
                with trusted.wrap_socket(connection,server_hostname=self.hostname) as stream:
                    return stream.getpeercert(binary_form=True)
        certificate=wait_for(served,description=f"{self.name} automatically issued TLS certificate")
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        public_key=x509.load_der_x509_certificate(certificate).public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
        self.spki=base64.b64encode(hashlib.sha256(public_key).digest()).decode()
        unknown_ca = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        for context, hostname in ((unknown_ca, self.hostname), (trusted, "wrong.frontiercloud.local")):
            try:
                with socket.create_connection((self.host, self.port), timeout=10) as connection:
                    with context.wrap_socket(connection, server_hostname=hostname):
                        pass
            except ssl.SSLError:
                # Native TLS may reject an unknown SNI before presenting any
                # certificate; that handshake refusal is also fail-closed.
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
            f"print(asyncio.run(transport.identity({self.endpoint!r}, expected_id={identity['node_id']!r}, role={('Follower' if self.storage else 'Standalone')!r}))['node_id'])"
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
        follower.relation = result["relationship_id"]
        return package

    def resources(self):
        program = """
import asyncio, json
from app.services.federation.catalog import catalog
from app.services.federation.state import state
async def read():
    await state.initialize()
    rows = await catalog.resources()
    return [{"resource_id": row["resource_id"], "path": row["path"],
             "size": int(row["payload"]["size"]),
             "url": "/api/v1/media/stream?resource_id=" + row["resource_id"]}
            for row in rows]
print(json.dumps(asyncio.run(read())))
"""
        return json.loads(self.web(program))

    def wait_online(self, after=0, relation=None):
        relationship_id = relation or self.relation
        def ready():
            rows = self.nodes()["relationships"]
            return any(row["relationship_id"] == relationship_id and row["status"] == "online"
                       and (row["last_heartbeat"] or 0) > after for row in rows)
        wait_for(ready, description=f"{self.name} relationship recovery")

    def relationship(self):
        return next(row for row in self.nodes()["relationships"] if row["relationship_id"] == self.relation)

    def mode(self, value, relation=None):
        self.api(f"/api/v1/media/admin/nodes/{relation or self.relation}/mode", {"mode": value})

    def configure_resources(self, *, storage=True, capacity_gib=2, backup=True, relation=None):
        return self.api(f"/api/v1/media/admin/nodes/{relation or self.relation}/resources", {
            "storage_enabled": storage,
            "storage_capacity_gib": capacity_gib if storage else 0,
            "backup_enabled": backup,
        })

    def upload_media(self, blob, filename, *, site_type, target_dir=None):
        target_dir = target_dir or ("music/relay-shared" if site_type == "relay" else "music/shared")
        reservation = self.api("/api/v1/media/admin/upload/session", {
            "site_type": site_type,
            "target_dir": target_dir,
            "relative_path": None,
            "filename": filename,
            "size_bytes": len(blob),
            "storage_mode": "plain",
        })
        expected_transport = {"primary": "Local", "direct": "Direct", "relay": "Relay"}[site_type]
        assert reservation["site_type"] == site_type
        assert reservation["transport"] == expected_transport
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
        self.compose("down", "--timeout", "5", "--volumes", "--remove-orphans")

    def failure_diagnostics(self):
        from urllib.parse import urlsplit
        details = {"node": self.name, "endpoint": self.endpoint}
        if self.endpoint and self.client is not None:
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
            "--host-resolver-rules="+",".join(f"MAP {node.hostname} {node.host}" for node in nodes),
            "--log-net-log=" + str(nodes[0].directory.parent / "browser-network.json")]


def launch_browser(playwright, nodes):
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    # Chromium's NSS trust belongs to this disposable process profile, never the
    # development user's global certificate store. No TLS-error bypass flags.
    browser_home = nodes[0].directory.parent / "browser-home"
    nss = browser_home / ".pki/nssdb"
    if not nss.exists():
        nss.mkdir(parents=True, mode=0o700)
        command("certutil", "-N", "--empty-password", "-d", "sql:" + str(nss))
        command("certutil", "-A", "-n", "FrontierCloud disposable ACME root", "-t", "C,,",
                "-i", str(nodes[0].ca), "-d", "sql:" + str(nss))
    options = {"args": browser_args(nodes), "env": {**os.environ, "HOME": str(browser_home)}}
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


def wait_page(page, expression, **options):
    # Playwright string predicates use eval and fail under the product nonce CSP.
    # Explicit function predicates exercise the same DOM without unsafe-eval.
    if not re.match(r"^(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>", expression.strip()):
        expression = "() => (" + expression + ")"
    return page.wait_for_function(expression, **options)


def admin_page(browser, node):
    context = browser.new_context()  # TLS errors are never ignored.
    context.add_cookies([{ "name": item.name, "value": item.value, "domain": item.domain,
        "path": item.path, "secure": item.secure, "httpOnly": item.name.endswith("session") } for item in node.client.cookies.jar])
    page = context.new_page()
    page.goto(node.endpoint + "/api/v1/media/admin/", wait_until="domcontentloaded")
    page.locator('#nodesPanel .module-heading').click()
    wait_page(page, "document.querySelector('#nodeIdentity').textContent.includes(' / v')")
    return context, page


def browser_promote(browser, node, role):
    context, page = admin_page(browser, node)
    page.select_option('#nodeRole', role)
    page.fill('#nodeEndpoint', node.endpoint)
    page.locator('#nodePromotion button[type="submit"]').click()
    wait_page(page, "document.querySelector('#nodeOperationStatus').textContent && document.querySelector('#nodeOperationStatus').textContent !== '处理中'")
    operation = page.locator('#nodeOperationStatus').text_content()
    assert operation == '已完成', f"{node.name} promotion failed: {operation}"
    wait_page(page, "role => document.querySelector('#nodeIdentity').textContent.startsWith(role)", arg=role)
    assert not page.locator('#nodePromotion').is_visible()
    context.close()


def browser_pair(browser, master, follower):
    # The native appliance issues its five-minute package only after HTTPS is
    # ready. It has no business AdminGUI; import is exercised on the Master UI.
    package = follower.api("/api/v1/media/admin/nodes/pair-package", {})
    master_context, master_page = admin_page(browser, master)
    master_page.fill('#nodePairPackage', json.dumps(package))
    master_page.locator('#nodeImportPair').click()
    wait_page(master_page, "document.querySelector('#nodeOperationStatus').textContent === '已完成'", timeout=60000)
    wait_page(master_page,
        "peer => Array.from(document.querySelector('#nodeRelationships').rows)"
        ".some(row => row.textContent.includes(peer))",
        arg=follower.nodes()["node_id"], timeout=60000,
    )
    follower.relation = next(
        row["relationship_id"] for row in master.nodes()["relationships"]
        if row["peer_id"] == follower.nodes()["node_id"]
    )
    master_context.close()
    return package


def browser_revoke(browser, master):
    context, page = admin_page(browser, master)
    page.locator('#nodeRelationships button').filter(has_text='撤销关系').click()
    wait_page(page, "document.querySelector('#nodeRelationships').rows.length === 0", timeout=60000)
    context.close()


def browser_checks(browser, master, resource):
    context = browser.new_context()  # TLS errors are never ignored.
    context.grant_permissions(["microphone"], origin=master.endpoint)
    page = context.new_page()
    page.on("console", lambda message: print(f"browser-console: {message.type}: {message.text}", flush=True))
    page.on("pageerror", lambda error: print(f"browser-pageerror: {error}", flush=True))
    page.add_init_script("""
        window.auditCspViolations = [];
        document.addEventListener('securitypolicyviolation', event => {
            window.auditCspViolations.push(event.violatedDirective);
        });
    """)
    # Joining is accepted only with working media routes; exercise the actual public player.
    page.goto(master.endpoint + "/api/v1/media/music/category?path=music/shared", wait_until="domcontentloaded")
    wait_page(page, "typeof art !== 'undefined' && art && art.video", timeout=60000)
    wait_page(page, "currentMediaList.length > 0", timeout=30000)
    assert page.locator('.media-search-input, input[type="search"]').count() == 0
    assert page.locator(f'[data-media-id="{resource["resource_id"]}"]').count() == 1
    page.evaluate("id => selectMedia(currentMediaList.findIndex(item => item.resource_id === id))", resource["resource_id"])
    wait_page(page, "art.video.currentTime > 0.1 && !art.video.paused", timeout=30000)
    wait_page(page, "activeLyricEntries.length > 0", timeout=10000)
    player_url = page.url
    playback_position = page.evaluate("art.video.currentTime")
    page.locator('#lyricsLink').click()
    wait_page(page, "!document.querySelector('#fullscreenLyrics').classList.contains('hidden')")
    assert page.url == player_url and page.locator('.fullscreen-lyrics-column').count() == 3
    page.locator('#fullscreenLyrics').click(position={"x": 12, "y": 12})
    wait_page(page, "document.querySelector('#fullscreenLyrics').classList.contains('hidden')")
    wait_page(page, "position => art.video.currentTime >= position && !art.video.paused", arg=playback_position)
    assert page.url == player_url
    page.evaluate("async () => { await art.video.play(); art.video.pause(); art.video.currentTime=1; await art.video.play(); }")
    wait_page(page, "art.video.currentTime > 1", timeout=10000)
    karaoke_id = page.evaluate(
        "id => currentMediaList.find(item => item.resource_id === id).karaoke_id",
        resource["resource_id"],
    )
    assert karaoke_id and resource["resource_id"] not in karaoke_id
    page.goto(master.endpoint + "/karaoke/?media=" + karaoke_id, wait_until="domcontentloaded")
    expect(page.locator('#title')).not_to_have_text('正在载入当前媒体…', timeout=30000)
    expect(page.locator('#capabilities')).to_contain_text('输出设备选择', timeout=30000)
    assert page.evaluate("JSON.parse(document.getElementById('frontierCloudNetworkConfig').textContent).stun_urls.length") > 0
    assert page.evaluate("window.auditCspViolations") == []
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
    with tempfile.TemporaryDirectory(prefix="frontiercloud-acceptance-") as temporary:
        directory = Path(temporary)
        from tests.certificate_fixtures import TestCA
        from cryptography.hazmat.primitives import serialization
        test_ca=TestCA()
        private=directory/"test-ca";private.mkdir(mode=0o700)
        ca = private / "root.crt"
        ca.write_bytes(test_ca.root.public_bytes(serialization.Encoding.PEM))
        (private/"private.pem").write_bytes(test_ca.key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        (private/"private.pem").chmod(0o600)
        bundle = directory / "ca-certificates.crt"
        bundle.write_bytes(Path("/etc/ssl/certs/ca-certificates.crt").read_bytes() + b"\n" + ca.read_bytes())
        command("docker","build","-t","frontiercloud-python-fleet:web","-f",str(ROOT/"Dockerfile"),str(ROOT))
        command("docker","build","-t","frontiercloud-python-fleet:nginx","-f",str(ROOT/"nginx/Dockerfile"),str(ROOT))
        report["clean_images"] = {}
        for kind in ("web", "nginx"):
            clean_tag = "frontiercloud-acceptance-" + kind
            command("docker", "tag", "frontiercloud-python-fleet:" + kind, clean_tag)
            report["clean_images"][kind] = command("docker", "image", "inspect", "--format", "{{.Id}}", clean_tag)
        for kind in ("web","nginx"):
            trust_file=private/("Dockerfile.trust-"+kind)
            trust_file.write_text(f"FROM frontiercloud-python-fleet:{kind}\nUSER 0\nCOPY root.crt /usr/local/share/ca-certificates/frontiercloud-test-only-root.crt\nRUN update-ca-certificates\n"+("USER 10001:10001\n" if kind=="web" else ""))
            command("docker","build","-t",f"frontiercloud-python-fleet:trusted-{kind}","-f",str(trust_file),str(private))
        socket.getaddrinfo=fixture_getaddrinfo
        base = json.loads(command("docker", "compose", "-f", str(ROOT / "docker-compose.yaml"),
                                  "config", "--format", "json"))
        try:
            command("docker", "network", "create", "--subnet", ACCEPTANCE_SUBNET,
                    "--gateway", ACCEPTANCE_GATEWAY, network)
            for index, name in enumerate(("master-a", "direct-b", "relay-c")):
                node = Node(name, directory, base, ca, bundle, index,network=network)
                nodes.append(node)
            a, b, c = nodes
            # Master Local content is imported once during promotion. A Follower
            # must join empty and receives files only through Storage Pool jobs.
            wav(a.data / "media/music/master-shared/master.wav", seconds=20, tone=500)
            upload_source = directory / "upload-source.wav"
            wav(upload_source, seconds=20, tone=700)
            source = upload_source.read_bytes()
            for node in nodes:node.start()
            report["checks"].append("trusted TLS succeeds; unknown CA and wrong hostname rejected on every node")

            with sync_playwright() as playwright:
                browser = launch_browser(playwright, nodes)
                browser_promote(browser, a, "Master")
                browser_pair(browser, a, b)
                browser_pair(browser, a, c)
                browser.close()
            a.relation = b.relation
            a.wait_online(relation=b.relation)
            a.wait_online(relation=c.relation)
            assert len(a.nodes()["relationships"]) == 2
            assert len(b.nodes()["relationships"]) == 1 and len(c.nodes()["relationships"]) == 1
            assert len(a.resources()) == 1
            report["checks"].append("fixed one-Master/two-Follower CI topology with stable identities")

            redirected = b.client.get(b.endpoint + "/api/v1/media", follow_redirects=False)
            assert redirected.status_code == 307 and redirected.headers["location"].startswith(a.endpoint)
            blocked = b.client.post(b.endpoint + "/api/v1/media/playback", json={})
            assert blocked.status_code == 409
            assert blocked.json()["code"] == "NODE_BUSINESS_DISABLED_ON_FOLLOWER"
            assert a.client.get(b.endpoint + "/internal/v1/catalog").status_code == 404
            assert a.client.get(a.endpoint + "/_protected_media/music/shared/master.wav").status_code == 404
            report["checks"].append("Follower business gate redirects public HTML and rejects business APIs")

            follower_id = b.nodes()["node_id"]
            relay_id = c.nodes()["node_id"]
            a.configure_resources(storage=True, capacity_gib=2, backup=True, relation=b.relation)
            # Direct has twice the allocation with the same media byte count;
            # recording placement therefore selects it deterministically.
            a.configure_resources(storage=True, capacity_gib=1, backup=True, relation=c.relation)
            def follower_enabled(member_id):
                return f"""
import asyncio
from sqlalchemy import text
from app.core.db import engine
async def read_enabled():
    async with engine.connect() as connection:
        return int(await connection.scalar(text(
            'SELECT storage_enabled FROM cluster_storage_members WHERE member_id=:member_id'
        ), {{'member_id': {member_id!r}}}) or 0)
print(asyncio.run(read_enabled()))
"""
            wait_for(lambda: b.web(follower_enabled(follower_id)).strip() == "1",
                     description="Direct Follower resource configuration heartbeat")
            wait_for(lambda: c.web(follower_enabled(relay_id)).strip() == "1",
                     description="Relay Follower resource configuration heartbeat")

            a.mode("Relay", relation=c.relation)
            a.upload_media(source, "relay.wav", site_type="relay")
            a.mode("Direct", relation=b.relation)
            direct_resource = a.upload_media(source, "direct.wav", site_type="direct")
            a.resource = direct_resource
            assert len(a.resources()) == 3
            a.api("/api/v1/media/admin/upload/session", {
                "site_type": "direct", "target_dir": "music/shared",
                "relative_path": None, "filename": "direct.wav", "size_bytes": len(source),
                "storage_mode": "plain",
            }, expected=409)
            report["checks"].append("site-type reservations place Relay/Direct uploads and enforce global path uniqueness")

            lyric = b"[00:01.00]MASTER OWNED LYRIC\n[00:05.00]next line\n"
            response = a.client.post(a.endpoint + "/api/v1/media/admin/upload/lyric",
                                     headers={"X-CSRF-Token": a.csrf},
                                     data={"storage_mode":"plain"},
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
            report["checks"].append("Relay/Direct media supports HEAD, Range, ETag and CORS")

            baseline_used = sum(row["used_bytes"] for row in a.nodes()["storage_pool"]["members"]
                                if row["member_id"] in {follower_id, relay_id})
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
                assert ticket["direct"] == (mode == "Direct"), ticket["direct"]
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
            assert not any(path for node in (b, c) for path in (node.data / "recordings").rglob("*.bin"))
            wait_for(lambda: sum(row["used_bytes"] for row in a.nodes()["storage_pool"]["members"]
                                 if row["member_id"] in {follower_id, relay_id}) == baseline_used,
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
            from tests.federation_crypto_acceptance import check as crypto_check, priority as priority_check, certificates as certificate_check
            encrypted_source = directory / "encrypted-browser-source.wav"
            wav(encrypted_source, seconds=80, tone=840)
            with sync_playwright() as playwright:
                browser = launch_browser(playwright, nodes)
                crypto_check(browser, nodes, encrypted_source.read_bytes(), report)
                priority_check(browser, a, source, report)
                certificate_check(browser, nodes, report)
                browser.close()
            for node in nodes:
                node.web("import os,socket,struct; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(3); transaction=os.urandom(12); s.sendto(struct.pack('!HHI',1,0,0x2112a442)+transaction,('stun',3478)); reply,_=s.recvfrom(2048); assert reply[:2]==b'\\x01\\x01' and reply[8:20]==transaction; s.close(); print('one serial STUN Binding accepted')")
            report["checks"].append("one STUN Binding per node, executed strictly serially")
            # Business tests and STUN probes are complete before recovery checks.
            # Each real single-core Web is sampled independently, in sequence.
            for node in nodes:
                container = node.container("web")
                assert command("docker", "inspect", "--format", "{{.HostConfig.NanoCpus}}", container) == "1000000000"
                output = command("python3", str(ROOT / "scripts/check_cpu_quiescence.py"), container,
                                 "--threshold", "20", "--consecutive", "5", "--timeout", "30")
                values = json.loads(output.rsplit(":", 1)[1].strip())
                assert len(values) >= 5 and all(value < 20 for value in values[-5:]), values
                report["samples"].append({"node": node.name, "single_core": True, "cpu_percent": values,
                                          "threshold": 20, "consecutive": 5, "timeout_seconds": 30})
            report["checks"].append("three single-core Web processes each recover below 20 percent for five consecutive samples, checked serially")
            report["result"] = "passed"
        except BaseException as exc:
            report["failure"] = {"type": type(exc).__name__, "message": str(exc)}
            traceback.print_exc()
            raise
        finally:
            arguments.output.write_text(json.dumps(report, indent=2))
            if report.get("result") != "passed":
                browser_network_failure(directory)
                for node in nodes:
                    try:
                        node.failure_diagnostics()
                        logs = node.compose("logs", "--no-color", "--tail", "60", "web", "certificate-agent",*([] if node.storage else ["nginx"]))
                        print("\n".join(line for line in logs.splitlines()
                                         if "initial_runtime_secrets" not in line), flush=True)
                    except Exception:
                        pass
            for node in reversed(nodes):node.stop()
            command("sudo", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(directory))
            subprocess.run(["docker", "network", "rm", network], capture_output=True)
            socket.getaddrinfo=_original_getaddrinfo
    print(json.dumps({"result": report.get("result", "failed"), "checks": report["checks"],
                      "samples": len(report["samples"])}))

def urlsplit_origin(value):
    from urllib.parse import urlsplit
    parsed = urlsplit(str(value))
    return parsed.scheme + "://" + parsed.netloc


if __name__ == "__main__":
    main()
