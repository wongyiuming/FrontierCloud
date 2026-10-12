#!/usr/bin/env python3
"""Local Nginx only: select controller-verified generations and graceful reload.

The issuer is the sole writer of the read-only published volume. Verification
receipts are committed with the complete pair; Nginx checks pair loading and
proves its actual serving certificate after reload. No host Docker/systemd API.
"""
import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import ssl
import stat
import subprocess
import sys
import time
from pathlib import Path

PUBLISHED = Path("/run/frontiercloud-tls")
RUNTIME = Path("/etc/nginx/runtime")
CONFIG = Path("/etc/nginx/nginx.conf")
FILES = ("extra-servers.conf", "public-tls.conf")
ID = re.compile(r"^[a-f0-9]{32}$")


def read(path, size):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > size:
        raise ValueError("unsafe TLS edge asset")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_ino, opened.st_dev) != (info.st_ino, info.st_dev):
            raise ValueError("TLS asset changed while opening")
        return stream.read(size + 1)


def write(path, body):
    import tempfile
    descriptor, temporary = tempfile.mkstemp(prefix=".tls-next-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def generation(host):
    value = json.loads(read(PUBLISHED / "current.json", 512))
    name = value.get("generation", "")
    if not ID.fullmatch(name) or not stat.S_ISDIR((PUBLISHED / name).lstat().st_mode):
        raise ValueError("invalid TLS generation")
    receipt = json.loads(read(PUBLISHED / name / "verified.json", 2048))
    now = time.time()
    certificate = read(PUBLISHED / name / "fullchain.pem", 65536).decode("ascii")
    leaf = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", certificate, re.S)
    if not leaf:
        raise ValueError("invalid managed leaf")
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(leaf.group())).hexdigest()
    if (receipt.get("hostname") != host or receipt.get("fingerprint") != fingerprint
            or not receipt.get("not_before", 0) <= now < receipt.get("not_after", 0)):
        raise ValueError("controller certificate verification receipt mismatch")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(PUBLISHED / name / "fullchain.pem", PUBLISHED / name / "privkey.pem")
    return name, fingerprint


def select(name):
    if not ID.fullmatch(name):
        raise ValueError("invalid generation")
    for file in FILES:
        template = read(RUNTIME / (file + ".automatic-template"), 1048576)
        template = template.replace(b"/etc/nginx/certs/fullchain.pem", str(PUBLISHED / name / "fullchain.pem").encode())
        template = template.replace(b"/etc/nginx/certs/privkey.pem", str(PUBLISHED / name / "privkey.pem").encode())
        write(RUNTIME / file, template)
    write(CONFIG, read(RUNTIME / "business.conf.automatic-template", 1048576))


def bootstrap(host, ipv6):
    extra = "listen [::]:80;" if ipv6 else ""
    return f"""user nginx;
worker_processes 1;
pid /var/run/nginx.pid;
error_log /dev/stderr warn;
events {{ worker_connections 128; }}
http {{
 server_tokens off;
 access_log off;
 server {{
  listen 80; {extra}
  server_name {host};
  location ^~ /.well-known/acme-challenge/ {{
   proxy_pass http://certificate-agent:8001;
   proxy_set_header Host $host;
   proxy_connect_timeout 2s;
   proxy_read_timeout 3s;
   add_header Cache-Control no-store always;
  }}
  location / {{ return 444; }}
 }}
}}
""".encode()


def execute(*args):
    if args not in (("-t",), ("-s", "reload")):
        raise ValueError("Nginx control command refused")
    subprocess.run(["/usr/sbin/nginx", *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=3, check=True)


def serving(host, expected):
    context = ssl.create_default_context()
    with socket.create_connection(("127.0.0.1", 443), timeout=3) as raw:
        with context.wrap_socket(raw, server_hostname=host) as stream:
            if hashlib.sha256(stream.getpeercert(binary_form=True)).hexdigest() != expected:
                raise ValueError("Nginx is serving another certificate")


def apply(host):
    name, fingerprint = generation(host)
    marker = RUNTIME / "tls-applied"
    try:
        if read(marker, 32).decode() == name:
            return
    except FileNotFoundError:
        pass
    with (RUNTIME / "tls-reload.lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old = {path: read(path, 1048576) for path in (CONFIG, *(RUNTIME / name for name in FILES))}
        try:
            select(name)
            execute("-t")
            execute("-s", "reload")
            for attempt in range(10):
                try:
                    serving(host, fingerprint)
                    break
                except (OSError, ValueError):
                    if attempt == 9:
                        raise
                    time.sleep(.2)
        except Exception:
            for path, body in old.items():
                write(path, body)
            # Restore configuration, and undo a completed reload if handshake proof failed.
            execute("-t")
            execute("-s", "reload")
            raise
        write(marker, name.encode())


def security_update():
    with (RUNTIME/"tls-reload.lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rules = RUNTIME/"ip-security.conf"
        old = read(rules, 2097152)
        try:
            write(rules, read(RUNTIME/"ip-security.candidate", 2097152))
            execute("-t")
            execute("-s", "reload")
        except Exception:
            write(rules, old)
            raise


def main():
    if sys.argv[1] == "security":
        security_update()
        return
    tls = os.environ.get("TLS_ENABLED", "false").lower()
    if tls not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
        raise ValueError("invalid TLS_ENABLED")
    if any(os.environ.get(name, "").strip() for name in ("HTTP_PORT", "HTTPS_PORT")):
        raise ValueError("HTTP_PORT/HTTPS_PORT are retired; public HTTP/HTTPS ports are fixed at 80/443")
    if tls in ("false", "0", "no", "off"):
        if sys.argv[1] == "ready":
            with socket.create_connection(("127.0.0.1", 80), timeout=3):
                pass
        return
    host = os.environ.get("SERVER_NAME", "").lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host) or "." not in host:
        raise ValueError("automatic TLS requires SERVER_NAME")
    command = sys.argv[1]
    RUNTIME.mkdir(exist_ok=True)
    if command == "initialize":
        for file, path in (("business.conf", CONFIG), *((file, RUNTIME / file) for file in FILES)):
            write(RUNTIME / (file + ".automatic-template"), read(path, 1048576))
        try:
            name, _fingerprint = generation(host)
            select(name)
        except (OSError, ValueError):
            write(CONFIG, bootstrap(host, os.environ.get("PUBLIC_BIND_ADDRESS") == "::"))
        return
    if command == "ready":
        _name, expected = generation(host)
        serving(host, expected)
        return
    if command != "watch":
        raise ValueError("TLS edge accepts initialize, ready, watch or security")
    stopped = False
    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    next_log = 0
    while not stopped:
        time.sleep(2)
        try:
            if Path("/var/run/nginx.pid").exists():
                apply(host)
        except Exception:
            if time.time() >= next_log:
                print("automatic Nginx TLS deployment pending; previous serving configuration retained", file=sys.stderr)
                next_log = time.time()+300


if __name__ == "__main__":
    main()
