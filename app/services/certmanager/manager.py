"""Persistent node-local certificate state machine and safe publication."""
from __future__ import annotations

import base64
import fcntl
import http.client
import json
import logging
import os
import secrets
import socket
import ssl
import stat
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.x509.verification import VerificationError

from . import assets
from .issuer import ACMEIssuer

logger = logging.getLogger("frontiercloud.certificate")


class ChallengeBindError(OSError):
    """Public validation ingress could not be reserved; no serving fallback."""


class BusinessIngressPending(RuntimeError):
    """The local HTTP-01 proxy is not ready; no CA authorization was attempted."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def classify(exc):
    from acme import errors, messages
    from requests import exceptions as requests_errors
    if isinstance(exc, socket.gaierror):
        return "dns_unavailable"
    if isinstance(exc, ChallengeBindError):
        return "port80_unavailable"
    if isinstance(exc,(TimeoutError,requests_errors.Timeout)):
        return "ca_request_timeout"
    if isinstance(exc,requests_errors.SSLError):
        return "tls_verification_failed"
    if isinstance(exc,requests_errors.ConnectionError):
        return "ca_connection_failed"
    if isinstance(exc, OSError) and exc.errno in (13, 28, 30):
        return "certificate_storage_unavailable"
    if isinstance(exc, messages.Error):
        kind = exc.typ.rsplit(":", 1)[-1]
        return {"rateLimited": "ca_rate_limited", "caa": "caa_denied", "dns": "dns_unavailable",
                "connection": "challenge_unreachable", "unauthorized": "authorization_failed"}.get(kind, "ca_rejected")
    if isinstance(exc, errors.ValidationError):
        # CA authorization error details remain private; expose stable diagnostic classes.
        for authorization in exc.failed_authzrs:
            for challenge in authorization.body.challenges:
                if challenge.error:
                    return classify(challenge.error)
        return "authorization_failed"
    if isinstance(exc, ssl.SSLError):
        return "tls_verification_failed"
    if isinstance(exc, ValueError) and str(exc) in ("account_key_recovered", "account_record_recovered", "order_record_recovered"):
        return str(exc)
    return "ca_or_validation_unavailable"


class ChallengeServer(HTTPServer):
    allow_reuse_address = True
    request_queue_size = 4

    def get_request(self):
        stream, address = super().get_request()
        stream.settimeout(2)
        return stream, address


class IPv6ChallengeServer(ChallengeServer):
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


class Manager:
    def __init__(self, host, enabled, profile, relay_port=8443, *, private=assets.PRIVATE,
                 published=assets.PUBLISHED, runtime=assets.RUNTIME, roots=None, issuer_factory=None,
                 lookup=None, probe=None, challenge_address=None, now=None, ingress_address=None):
        self.host, self.enabled, self.profile, self.port = host, enabled, profile, relay_port if profile == "relay" else 443
        self.private, self.published, self.runtime = Path(private), Path(published), Path(runtime)
        self.roots = roots or assets.trust_store()
        self.clock = now or time.time
        self.lookup = lookup or socket.getaddrinfo
        self.probe = probe or self._probe
        self.challenge_address = challenge_address or (("0.0.0.0", 8001) if profile == "business" else ("", 80))
        # A fixed Compose-local route. Only in-process tests inject another address.
        self.ingress_address = ingress_address or ('nginx', 80)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.active_challenge = False
        self.ipv6_required = False
        self.prune_retry_at = 0
        self._leaf = None
        for directory, allowed in ((self.private, 0o700), (self.published, 0o750), (self.runtime, 0o710)):
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & ~allowed:
                raise ValueError("certificate controller requires isolated persistent volumes with restricted permissions")
        self.lease = os.open(self.private / "controller.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.lease)
            raise ValueError("certificate controller already running") from None
        self.journal = {"host": host, "state": "PENDING", "retry_at": 0, "failures": 0}
        if enabled:
            try:
                saved = json.loads(assets.bounded(self.private / "lifecycle.json", 8192))
                states = {"DISABLED", "PENDING", "VALIDATING", "ISSUING", "ACTIVE", "RENEWING", "DEGRADED", "FAILED"}
                if (not isinstance(saved, dict) or saved.get("state") not in states
                        or any(not isinstance(saved.get(name, 0), (int, float))
                               or isinstance(saved.get(name, 0), bool) or not 0 <= saved.get(name, 0) < 2**53
                               for name in ("retry_at", "last_clock", "next_renewal", "ari_recheck", "failures"))):
                    raise ValueError("invalid lifecycle journal")
                if saved.get("host") == host:
                    self.journal.update(saved)
                    if self.journal["state"] in ("ISSUING", "VALIDATING", "RENEWING"):
                        self.journal.update(state="PENDING", error="interrupted", retry_at=max(self.clock()+300, saved.get("retry_at", 0)))
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError, AttributeError):
                os.replace(self.private / "lifecycle.json", self.private / ("lifecycle-invalid-" + secrets.token_hex(8)))
                self.journal.update(error="journal_recovered", retry_at=self.clock()+300)
        else:
            self.journal["state"] = "DISABLED"
        self.issuer = (issuer_factory or ACMEIssuer)(self.private, host, self.responder)
        self.view = {}
        self.refresh()
        if enabled:
            if self._leaf is not None:
                self.journal.setdefault("ari_recheck", int(self.clock()+3600))
            self.save()

    def save(self):
        assets.atomic(self.private / "lifecycle.json", json.dumps(self.journal).encode(), 0o600)

    def phase(self, state):
        with self.lock:
            self.journal["state"] = state
            self.save()
            self.refresh()
        logger.info("automatic_certificate_phase", extra={"context": {"hostname": self.host, "state": state}})

    def _probe(self):
        # Public DNS ingress is the proof, not a private backend connection.
        context = ssl.create_default_context()
        with socket.create_connection((self.host, self.port), timeout=4) as raw:
            with context.wrap_socket(raw, server_hostname=self.host) as stream:
                return assets.fingerprint(x509.load_der_x509_certificate(stream.getpeercert(binary_form=True)))

    def refresh(self):
        now = self.clock()
        leaf = None
        invalid = ""
        if self.enabled:
            try:
                _generation, cert, key = assets.material(self.published)
                leaf = assets.validate(self.host, cert, key, roots=self.roots, now=datetime.fromtimestamp(now, timezone.utc))
            except (OSError, ValueError, TypeError, VerificationError):
                # No candidate is healthy until standard trust verification succeeds.
                invalid = "certificate_unavailable"
                try:
                    parsed = x509.load_pem_x509_certificates(cert)[0]
                    if parsed.not_valid_after_utc.timestamp() <= now:
                        invalid = "certificate_expired"
                except (UnboundLocalError, ValueError, IndexError):
                    pass
        self._leaf = leaf
        previous = self.view
        value = {
            "enabled": self.enabled, "controller_running": not self.stop_event.is_set(), "mode": "automatic",
            "managed": self.enabled, "auto_renew": self.enabled, "hostname": self.host,
            "state": self.journal["state"], "remaining_seconds": 0, "serving_matches": False,
            "https_ready": False, "validation": "HTTP-01", "last_error": self.journal.get("error", ""),
            "last_renewal": self.journal.get("last_renewal", ""), "next_renewal": self.journal.get("next_renewal", 0),
            "job": {"state": self.journal["state"], "code": self.journal.get("error", ""),
                    "started_at": self.journal.get("started_at", 0), "completed_at": self.journal.get("completed_at", 0),
                    "port80_active": self.active_challenge and self.profile != "business"},
        }
        if leaf:
            value.update(fingerprint=assets.fingerprint(leaf), issuer=leaf.issuer.rfc4514_string(),
                         san=leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName),
                         not_before=int(leaf.not_valid_before_utc.timestamp()), not_after=int(leaf.not_valid_after_utc.timestamp()),
                         remaining_seconds=max(0, int(leaf.not_valid_after_utc.timestamp() - now)))
            serving = previous.get("serving_fingerprint", "")
            value.update(serving_fingerprint=serving, serving_matches=serving == value["fingerprint"],
                         https_ready=serving == value["fingerprint"])
        elif self.enabled and self.journal["state"] == "ACTIVE":
            value.update(state="FAILED", last_error=invalid)
        self.view = value

    def status(self):
        with self.lock:
            self.refresh()
            return json.loads(json.dumps(self.view))

    def verify_business_ingress(self, resources):
        """Require an exact challenge response through Nginx, never follow redirects."""
        for path, value in resources.items():
            connection = http.client.HTTPConnection(*self.ingress_address, timeout=2)
            try:
                connection.request('GET', path, headers={'Host': self.host, 'Connection': 'close'})
                response = connection.getresponse()
                if 300 <= response.status < 400:
                    raise BusinessIngressPending('http_ingress_redirect')
                if response.status != 200 or response.read(8193) != value.encode('ascii'):
                    raise BusinessIngressPending('http_ingress_mismatch')
            except (OSError, http.client.HTTPException):
                raise BusinessIngressPending('http_ingress_unavailable') from None
            finally:
                connection.close()

    @contextmanager
    def responder(self, resources):
        manager = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            def log_message(self, *_args):
                pass
            def do_GET(self):
                expected = manager.host
                supplied = self.headers.get("Host", "").lower().split(":", 1)[0]
                body = resources.get(self.path)
                if supplied != expected or body is None or not self.path.startswith("/.well-known/acme-challenge/"):
                    self.send_error(404)
                    return
                data = body.encode("ascii")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        servers = []
        try:
            server = ChallengeServer(self.challenge_address, Handler)
            servers.append(server)
            if self.ipv6_required and self.profile != "business" and self.challenge_address[0] in ("", "0.0.0.0"):
                servers.append(IPv6ChallengeServer(("::", server.server_port), Handler))
        except OSError as exc:
            for server in servers:
                server.server_close()
            raise ChallengeBindError(exc.errno, "HTTP-01 validation ingress unavailable") from None
        threads = [threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .1}, daemon=True) for server in servers]
        with self.lock:
            self.active_challenge = True
            self.refresh()
        for thread in threads:
            thread.start()
        try:
            if self.profile == 'business':
                # This also guards the actual CA token immediately before answer_challenge.
                self.verify_business_ingress(resources)
            yield
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=3)
            with self.lock:
                self.active_challenge = False
                self.refresh()

    def step(self):
        if not self.enabled:
            return
        now = self.clock()
        with self.lock:
            self.refresh()
            leaf = self._leaf
        try:
            serving = self.probe() if leaf else ""
        except (OSError, ValueError):
            serving = ""
        with self.lock:
            self.view["serving_fingerprint"] = serving
            self.refresh()
            if leaf and self.view["serving_matches"] and self.journal.get("error", "") in (
                    "", "awaiting_serving_confirmation", "serving_certificate_mismatch", "interrupted", "journal_recovered"):
                self.journal.update(state="ACTIVE", error="")
                if now >= self.prune_retry_at:
                    try:
                        assets.prune(self.published)
                        self.prune_retry_at = 0
                    except (OSError, ValueError):
                        # Optional reclamation must not kill issuance or invalidate a trusted serving pair.
                        self.prune_retry_at = now + 300
                        logger.warning("automatic_certificate_reclamation_deferred", extra={"context": {
                            "hostname": self.host, "code": "certificate_storage_unavailable"}})
            default_renew = (int(leaf.not_valid_after_utc.timestamp()) - min(30 * 86400,
                            int((leaf.not_valid_after_utc - leaf.not_valid_before_utc).total_seconds() / 3))) if leaf else 0
            renew_at = self.journal.get("next_renewal") or default_renew
            due = not leaf or now >= renew_at
        if leaf and not due and now >= self.journal.get("ari_recheck", now+86400):
            try:
                _generation, current_cert, _key = assets.material(self.published)
                ari_renew, ari_recheck = self.issuer.renewal_time(current_cert)
                with self.lock:
                    self.journal.update(next_renewal=min(ari_renew or default_renew, default_renew),
                                        ari_recheck=max(now+3600, ari_recheck))
                    renew_at = self.journal["next_renewal"]
                    due = now >= renew_at
            except Exception:
                with self.lock:
                    self.journal["ari_recheck"] = now+3600
        with self.lock:
            if not due or now < self.journal.get("retry_at", 0):
                if leaf and not self.view["serving_matches"]:
                    self.journal.update(state="DEGRADED", error="serving_certificate_mismatch")
                self.refresh()
                self.save()
                return
            if self.profile != 'business':
                self.journal.update(state="RENEWING" if leaf else "VALIDATING", started_at=int(now), completed_at=0)
                self.save()
        try:
            # A clock rollback must not create a storm of new accounts or orders.
            if now < 1_700_000_000 or now + 300 < self.journal.get("last_clock", 0):
                raise ValueError("clock_unreliable")
            addresses = self.lookup(self.host, 80, type=socket.SOCK_STREAM)
            if not addresses:
                raise socket.gaierror("No A/AAAA addresses")
            self.ipv6_required = any(isinstance(address, tuple) and address[0] == socket.AF_INET6 for address in addresses)
            if self.profile == 'business':
                # Verify routing before opening an ACME account or creating any order.
                nonce = secrets.token_urlsafe(24)
                with self.responder({'/.well-known/acme-challenge/' + nonce: secrets.token_urlsafe(48)}):
                    pass
                with self.lock:
                    self.journal.update(state='RENEWING' if leaf else 'VALIDATING', started_at=int(now), completed_at=0)
                    self.save()
            certificate, key = self.issuer.issue(self.phase)
            issued = assets.publish(self.host, certificate, key, directory=self.published, roots=self.roots,
                                    now=datetime.fromtimestamp(now, timezone.utc))
            default_renew = int(issued.not_valid_after_utc.timestamp()) - min(30*86400,
                           int((issued.not_valid_after_utc - issued.not_valid_before_utc).total_seconds()/3))
            try:
                ari_renew, ari_recheck = self.issuer.renewal_time(certificate)
            except Exception:
                ari_renew, ari_recheck = None, now+86400
            with self.lock:
                self.journal.update(state="DEGRADED", error="awaiting_serving_confirmation", last_renewal="success",
                                    retry_at=0, failures=0, next_renewal=min(ari_renew or default_renew, default_renew),
                                    ari_recheck=ari_recheck, completed_at=int(self.clock()), last_clock=now)
                self.save()
                self.refresh()
            logger.info("automatic_certificate_published", extra={"context": {
                "hostname": self.host, "fingerprint": assets.fingerprint(issued), "state": "DEGRADED"}})
            # Nginx reload/Storage SNI hot load happen independently; next tick verifies actual service.
        except BusinessIngressPending as exc:
            with self.lock:
                self.journal.update(state='DEGRADED' if leaf else 'PENDING', error=exc.code,
                    retry_at=int(now + 5), completed_at=int(self.clock()), last_clock=now)
                self.save()
                self.refresh()
            logger.info('automatic_certificate_ingress_pending', extra={'context': {
                'hostname': self.host, 'code': exc.code}})
        except Exception as exc:
            code = "clock_unreliable" if str(exc) == "clock_unreliable" else classify(exc)
            with self.lock:
                failures = min(10, self.journal.get("failures", 0)+1)
                delay = min(86400, 300 * (2 ** (failures-1)))
                if code == "ca_rate_limited":
                    delay = max(delay, 3600)
                self.journal.update(state="DEGRADED" if leaf else "FAILED", error=code,
                                    last_renewal="failed", retry_at=int(now+delay+secrets.randbelow(31)), failures=failures,
                                    completed_at=int(self.clock()), last_clock=now)
                self.save()
                self.refresh()
            logger.warning("automatic_certificate_failed", extra={"context": {"hostname": self.host, "code": code}})

    def run(self):
        try:
            while not self.stop_event.is_set():
                self.step()
                delay = 5 if self.journal.get('error', '').startswith('http_ingress_') else 30
                self.stop_event.wait(delay)
        finally:
            self.stop_event.set()
            with self.lock:
                self.refresh()

    def close(self):
        self.stop_event.set()
        with self.lock:
            self.refresh()
        fcntl.flock(self.lease, fcntl.LOCK_UN)
        os.close(self.lease)

    def download(self, public_key, browser_nonce):
        def decode(value, size):
            if not isinstance(value, str) or len(value) > size or not value:
                raise ValueError("invalid browser download key")
            return base64.b64decode(value + "="*((-len(value)) % 4), altchars=b"-_", validate=True)
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decode(public_key, 90))
        nonce = decode(browser_nonce, 43)
        if len(nonce) != 32:
            raise ValueError("invalid browser download nonce")
        view = self.status()
        if not view["https_ready"] or not view["serving_matches"]:
            raise ValueError("certificate download requires verified active HTTPS")
        _generation, certificate, key = assets.material(self.published)
        leaf = assets.validate(self.host, certificate, key, roots=self.roots)
        if assets.fingerprint(leaf) != view["fingerprint"]:
            raise ValueError("certificate changed; query status and retry")
        private = ec.generate_private_key(ec.SECP256R1())
        shared = private.exchange(ec.ECDH(), peer)
        salt, iv = secrets.token_bytes(32), secrets.token_bytes(12)
        aad = "\n".join(("frontiercloud.certificate.download.v1", self.host, view["fingerprint"], browser_nonce)).encode()
        wrapping = HKDF(hashes.SHA256(), 32, salt, aad).derive(shared)
        plaintext = json.dumps({"certificate": certificate.decode(), "private_key": key.decode()}).encode()
        encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
        view["download"] = {"algorithm": "P256-HKDF-SHA256-AES256GCM",
                            "public_key": encode(private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)),
                            "salt": encode(salt), "iv": encode(iv), "ciphertext": encode(AESGCM(wrapping).encrypt(iv, plaintext, aad)),
                            "hostname": self.host, "fingerprint": view["fingerprint"], "browser_nonce": browser_nonce}
        return view
