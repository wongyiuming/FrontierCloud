"""Test-only bridge driver for real Nginx bootstrap/issuance/reload/recreation."""
import hashlib
import http.client
import json
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from app.services.certmanager import assets
from app.services.certmanager.issuer import ACMEIssuer
from app.services.certmanager.manager import Manager
from tests.acme_test_server import ACMETestServer

BASE = Path("/test-certificate")
HOST = "node.4399.com"
for name, mode in (("private", 0o700), ("published", 0o750), ("runtime", 0o710), ("control", 0o700)):
    directory = BASE/name
    directory.mkdir(mode=mode, exist_ok=True)
    directory.chmod(mode)
ca = ACMETestServer(HOST, 80, challenge_host="nginx")
(BASE/"control"/"root.crt").write_bytes(ca.ca.root.public_bytes(serialization.Encoding.PEM))
context = ssl.create_default_context(cadata=ca.ca.root.public_bytes(serialization.Encoding.PEM).decode())
finish_stream = threading.Event()


class BusinessFixture(BaseHTTPRequestHandler):
    def log_message(self, *_args): pass
    def do_GET(self):
        self.send_response(200)
        data = b"first\nsecond\n" if self.path == "/long-stream" else b"business-ready"
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        if self.path == "/long-stream":
            self.wfile.write(b"first\n")
            self.wfile.flush()
            finish_stream.wait(30)
            self.wfile.write(b"second\n")
        else:
            self.wfile.write(data)


business = ThreadingHTTPServer(("0.0.0.0", 8000), BusinessFixture)
business_thread = threading.Thread(target=business.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
business_thread.start()


def connect():
    return context.wrap_socket(socket.create_connection(("nginx", 443), timeout=3), server_hostname=HOST)


def probe():
    with connect() as stream:
        return hashlib.sha256(stream.getpeercert(binary_form=True)).hexdigest()


def eventually(operation, *, seconds=30):
    deadline = time.monotonic()+seconds
    error = None
    while time.monotonic() < deadline:
        try:
            result = operation()
            if result: return result
        except (OSError, ValueError) as exc:
            error = type(exc).__name__
        time.sleep(.2)
    raise AssertionError("fixture wait timed out: "+str(error))


def bootstrap_closed():
    connection = http.client.HTTPConnection("nginx", 80, timeout=3)
    try:
        connection.request("GET", "/api/v1/media", headers={"Host": HOST})
        try:
            response = connection.getresponse()
        except http.client.RemoteDisconnected:
            return True
        return response.status in (403, 404, 503)
    finally: connection.close()


manager = Manager(HOST, True, "business", private=BASE/"private", published=BASE/"published", runtime=BASE/"runtime",
                  roots=ca.ca.store, challenge_address=("0.0.0.0", 8001), probe=probe,
                  lookup=lambda *_args, **_kwargs: ["test-address"],
                  issuer_factory=lambda private, host, responder: ACMEIssuer(private, host, responder, directory_url=ca.url+"/directory"))
try:
    # The driver starts before Nginx; no CA work is permitted during bootstrap.
    manager.step()
    assert manager.status()['state'] == 'PENDING', manager.status()
    assert manager.status()['last_error'] == 'http_ingress_unavailable', manager.status()
    assert ca.account_creations == 0 and len(ca.orders) == 0 and ca.validated_challenges == 0 and ca.failures == []
    assert not manager.active_challenge
    (BASE/'control'/'ingress-waiting').write_text('ok')
    eventually(bootstrap_closed)
    try:
        probe()
        raise AssertionError("fresh bootstrap exposed HTTPS before trusted issuance")
    except (OSError, ValueError): pass
    manager.journal['retry_at'] = 0
    manager.step()
    assert ca.failures == [], ca.failures
    first = manager.status()["fingerprint"]
    eventually(lambda: probe() == first)
    manager.step()
    assert manager.status()["state"] == "ACTIVE", manager.status()
    stream = connect()
    stream.sendall(b"GET /long-stream HTTP/1.1\r\nHost: node.4399.com\r\nConnection: close\r\n\r\n")
    buffered = bytearray()
    while b"\r\n\r\n" not in buffered:
        buffered.extend(stream.recv(1))
    while not buffered.endswith(b"first\n"):
        buffered.extend(stream.recv(1))
    manager.journal["next_renewal"] = 1
    manager.step()
    second = manager.status()["fingerprint"]
    assert first != second
    eventually(lambda: probe() == second)
    finish_stream.set()
    retained = bytearray()
    while data := stream.recv(1024): retained.extend(data)
    stream.close()
    assert bytes(retained) == b"second\n", retained
    manager.step()
    assert manager.status()["state"] == "ACTIVE", manager.status()
    (BASE/"control"/"rotation-complete").write_text("ok")
    eventually(lambda: (BASE/"control"/"nginx-recreated").exists(), seconds=60)
    eventually(lambda: probe() == second)
    manager.step()
    assert manager.status()["https_ready"]
    assert ca.account_creations == 1 and ca.validated_challenges == 2 and len(ca.orders) == 2
    report = {"bootstrap_business_closed": True, "late_nginx_no_ca_attempt": True,
              "actual_acme_http01": True, "nginx_trusted_tls": True,
              "renewal": True, "retained_stream": True, "nginx_recreated_restores_certificate": True,
              "account_creations": ca.account_creations, "validated_challenges": ca.validated_challenges,
              "first_fingerprint": first, "second_fingerprint": second}
    (BASE/"control"/"result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
finally:
    manager.close()
    ca.close()
    finish_stream.set()
    business.shutdown()
    business.server_close()
