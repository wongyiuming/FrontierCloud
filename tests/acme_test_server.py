"""Bounded local ACME fixture with real JWS, CSR and HTTP-01 verification.

This is a test CA, not part of the product certificate implementation. All
ACME protocol requests under test are made by the pinned python-acme library.
"""
import base64
import http.client
import json
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import josepy
from acme.jws import JWS
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from tests.certificate_fixtures import TestCA


class ACMETestServer:
    def __init__(self, host, challenge_port, *, challenge_host="127.0.0.1", ca=None):
        self.host, self.challenge_port, self.challenge_host = host, challenge_port, challenge_host
        self.ca = ca or TestCA()
        self.orders = []
        self.account = None
        self.account_creations = 0
        self.validated_challenges = 0
        self.failures = []
        self.nonces = set()
        self.fail_order = None
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def respond(self, value, *, code=200, location=None, up=None, content_type="application/json"):
                body = value if isinstance(value, bytes) else json.dumps(value).encode()
                nonce = secrets.token_urlsafe(24)
                owner.nonces.add(nonce)
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Replay-Nonce", nonce)
                if location:
                    self.send_header("Location", location)
                if up:
                    self.send_header("Link", f'<{up}>; rel="up"')
                self.end_headers()
                if self.command != "HEAD": self.wfile.write(body)
            def do_HEAD(self):
                self.respond(b"")
            def do_GET(self):
                if self.path == "/directory":
                    self.respond({"newNonce": owner.url+"/nonce", "newAccount": owner.url+"/account",
                                  "newOrder": owner.url+"/order", "revokeCert": owner.url+"/revoke", "keyChange": owner.url+"/key-change"})
                elif self.path == "/nonce": self.respond(b"")
                else: self.respond({"type": "urn:ietf:params:acme:error:malformed"}, code=404)
            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 131072: raise ValueError("bad length")
                    envelope = JWS.json_loads(self.rfile.read(length).decode())
                    header = envelope.signature.combined
                    key = header.jwk or owner.account
                    if not key or not envelope.verify(key): raise ValueError("JWS signature")
                    nonce = base64.urlsafe_b64encode(header.nonce).decode().rstrip("=")
                    if nonce not in owner.nonces: raise ValueError("JWS nonce")
                    owner.nonces.remove(nonce)
                    if header.url != owner.url+self.path: raise ValueError("JWS URL")
                    payload = json.loads(envelope.payload) if envelope.payload else {}
                    if self.path == "/account":
                        exists = owner.account is not None
                        if exists and owner.account.thumbprint() != key.thumbprint():
                            raise ValueError("unexpected replacement account identity")
                        owner.account = key
                        owner.account_creations += not exists
                        self.respond({"status": "valid", "orders": owner.url+"/orders"}, code=200 if exists else 201, location=owner.url+"/account/1")
                    elif self.path == "/account/1":
                        self.respond({"status": "valid", "orders": owner.url+"/orders"}, location=owner.url+"/account/1")
                    elif self.path == "/order":
                        if owner.fail_order:
                            kind, status = owner.fail_order
                            self.respond({"type": "urn:ietf:params:acme:error:"+kind, "detail": "test CA unavailable"}, code=status)
                            return
                        if payload.get("identifiers") != [{"type": "dns", "value": owner.host}]: raise ValueError("identifier")
                        identifier = len(owner.orders)+1
                        owner.orders.append({"id": identifier, "token": secrets.token_urlsafe(24), "valid": False, "certificate": None})
                        self.respond(owner.order(identifier), code=201, location=owner.url+f"/order/{identifier}")
                    elif self.path.startswith("/auth/"):
                        item = owner.orders[int(self.path.rsplit("/", 1)[-1])-1]
                        self.respond({"identifier": {"type": "dns", "value": owner.host}, "status": "valid" if item["valid"] else "pending",
                                      "challenges": [{"type": "http-01", "url": owner.url+f'/challenge/{item["id"]}',
                                                      "status": "valid" if item["valid"] else "pending", "token": item["token"]}]})
                    elif self.path.startswith("/challenge/"):
                        item = owner.orders[int(self.path.rsplit("/", 1)[-1])-1]
                        connection = http.client.HTTPConnection(owner.challenge_host, owner.challenge_port, timeout=3)
                        try:
                            connection.request("GET", "/.well-known/acme-challenge/"+item["token"], headers={"Host": owner.host})
                            response = connection.getresponse()
                            thumbprint = base64.urlsafe_b64encode(owner.account.thumbprint()).decode().rstrip("=")
                            if response.status != 200 or response.read() != (item["token"]+"."+thumbprint).encode():
                                raise ValueError("HTTP-01 did not prove account/domain")
                        finally: connection.close()
                        item["valid"] = True
                        owner.validated_challenges += 1
                        self.respond({"type": "http-01", "url": owner.url+self.path, "status": "valid", "token": item["token"]},
                                     up=owner.url+f'/auth/{item["id"]}')
                    elif self.path.startswith("/finalize/"):
                        item = owner.orders[int(self.path.rsplit("/", 1)[-1])-1]
                        if not item["valid"]: raise ValueError("unvalidated authorization")
                        csr = x509.load_der_x509_csr(base64.urlsafe_b64decode(payload["csr"]+"="*((-len(payload["csr"]))%4)))
                        if not csr.is_signature_valid: raise ValueError("CSR signature")
                        if csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) != [owner.host]:
                            raise ValueError("CSR SAN")
                        item["certificate"] = (owner.ca.sign(owner.host, csr.public_key()).public_bytes(serialization.Encoding.PEM)
                                               + owner.ca.root.public_bytes(serialization.Encoding.PEM))
                        self.respond(owner.order(item["id"]))
                    elif self.path.startswith("/order/"):
                        self.respond(owner.order(int(self.path.rsplit("/", 1)[-1])))
                    elif self.path.startswith("/certificate/"):
                        item = owner.orders[int(self.path.rsplit("/", 1)[-1])-1]
                        self.respond(item["certificate"], content_type="application/pem-certificate-chain")
                    else:
                        raise ValueError("unsupported ACME endpoint")
                except Exception as exc:
                    owner.failures.append(type(exc).__name__+": "+str(exc))
                    logging.getLogger('frontiercloud.test_acme').warning('test_acme_request_rejected',
                        extra={'context': {'error_class': type(exc).__name__,
                            'operation': self.path.split('/')[1], 'stage': 'challenge' if self.path.startswith('/challenge/') else 'protocol'}})
                    self.respond({"type": "urn:ietf:params:acme:error:malformed", "detail": "test CA rejected request"}, code=400)
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()

    def order(self, identifier):
        item = self.orders[identifier-1]
        result = {"status": "valid" if item["certificate"] else "ready" if item["valid"] else "pending",
                  "expires": (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                  "identifiers": [{"type": "dns", "value": self.host}], "authorizations": [self.url+f"/auth/{identifier}"],
                  "finalize": self.url+f"/finalize/{identifier}"}
        if item["certificate"]: result["certificate"] = self.url+f"/certificate/{identifier}"
        return result

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
