from __future__ import annotations

import base64
import importlib.util
import json
import os
import socket
import ssl
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.services.certmanager import assets
from app.services.certmanager.contract import validate_settings
from app.services.certmanager.manager import Manager
from app.services.certmanager.migrate import adopt
from app.services.certmanager.server import HotTLS
from tests.certificate_fixtures import TestCA


class CertificateManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.private, self.published, self.runtime = (self.base/name for name in ("private", "published", "runtime"))
        for path, mode in ((self.private, 0o700), (self.published, 0o750), (self.runtime, 0o710)):
            path.mkdir(mode=mode)
        self.ca = TestCA()
        self.pair = self.ca.pair()
        self.current_time = time.time()
        self.calls = 0
        self.fail = None
        self.served = ""
        owner = self
        class Issuer:
            def __init__(self, *_args): pass
            def issue(self, phase):
                owner.calls += 1
                phase("VALIDATING")
                phase("ISSUING")
                if owner.fail:
                    raise owner.fail
                return owner.pair
            def renewal_time(self, _certificate):
                return None, int(owner.current_time+86400)
        self.issuer = Issuer

    def manager(self, host="node.4399.com", enabled=True, profile="direct", **kwargs):
        manager = Manager(host, enabled, profile, private=self.private, published=self.published, runtime=self.runtime,
                          roots=self.ca.store, issuer_factory=self.issuer, lookup=lambda *_args, **_kwargs: ["test-address"],
                          probe=lambda: self.served, now=lambda: self.current_time, **kwargs)
        self.addCleanup(manager.close)
        return manager

    def test_first_start_issue_and_actual_serving_confirmation(self):
        manager = self.manager()
        self.assertEqual(manager.status()["state"], "PENDING")
        manager.step()
        self.assertEqual(self.calls, 1)
        self.assertFalse(manager.status()["https_ready"])
        self.served = manager.status()["fingerprint"]
        manager.step()
        self.assertEqual(manager.status()["state"], "ACTIVE")
        self.assertTrue(manager.status()["https_ready"])
        self.assertEqual(self.calls, 1)

    def test_dns_failure_then_recovery_and_retry_cooldown(self):
        manager = self.manager()
        manager.lookup = lambda *_args, **_kwargs: (_ for _ in ()).throw(socket.gaierror("test DNS"))
        manager.step()
        self.assertEqual(manager.status()["last_error"], "dns_unavailable")
        self.assertEqual(self.calls, 0)
        manager.lookup = lambda *_args, **_kwargs: ["test"]
        manager.step()
        self.assertEqual(self.calls, 0)
        self.current_time = manager.journal["retry_at"]+1
        manager.step()
        self.assertEqual(self.calls, 1)

    def test_old_generation_cleanup_failure_preserves_active_controller(self):
        manager = self.manager()
        manager.step()
        self.served = manager.status()["fingerprint"]
        current = assets.pointer(self.published)
        with patch.object(assets, "prune", side_effect=PermissionError("test read-only old generation")) as prune:
            manager.step()
            manager.step()
            self.assertEqual(prune.call_count, 1)
        self.assertEqual(manager.status()["state"], "ACTIVE")
        self.assertTrue(manager.status()["https_ready"])
        self.assertTrue(manager.status()["controller_running"])
        self.assertEqual(assets.pointer(self.published), current)
        self.assertEqual(self.calls, 1)
        self.current_time += 301
        with patch.object(assets, "prune") as prune:
            manager.step()
            prune.assert_called_once_with(self.published)

    def test_renewal_failure_preserves_valid_previous_pair(self):
        manager = self.manager()
        manager.step()
        old = assets.pointer(self.published)
        self.served = manager.status()["fingerprint"]
        manager.step()
        manager.journal["next_renewal"] = self.current_time-1
        self.fail = OSError("CA offline")
        manager.step()
        self.assertEqual(assets.pointer(self.published), old)
        self.assertEqual(manager.status()["state"], "DEGRADED")
        self.assertTrue(manager.status()["https_ready"])
        self.assertEqual(manager.status()["last_renewal"], "failed")

    def test_renewal_switch_requires_new_serving_fingerprint(self):
        manager = self.manager()
        manager.step()
        self.served = manager.status()["fingerprint"]
        manager.step()
        old = assets.pointer(self.published)["generation"]
        manager.journal["next_renewal"] = self.current_time-1
        self.pair = self.ca.pair()
        manager.step()
        self.assertEqual(assets.pointer(self.published)["previous"], old)
        self.assertFalse(manager.status()["https_ready"])
        self.served = manager.status()["fingerprint"]
        manager.step()
        self.assertTrue(manager.status()["https_ready"])

    def test_invalid_candidate_never_changes_pointer(self):
        manager = self.manager()
        manager.step()
        old = assets.pointer(self.published)
        manager.journal["next_renewal"] = self.current_time-1
        self.pair = self.ca.pair("wrong.4399.com")
        manager.step()
        self.assertEqual(assets.pointer(self.published), old)

    def test_pair_mismatch_and_untrusted_root_rejected(self):
        certificate, key = self.pair
        _, another_key = self.ca.pair()
        with self.assertRaises(ValueError):
            assets.publish("node.4399.com", certificate, another_key, directory=self.published, roots=self.ca.store)
        from cryptography.x509.verification import VerificationError
        with self.assertRaises(VerificationError):
            assets.publish("node.4399.com", certificate, key, directory=self.published, roots=TestCA().store)
        self.assertFalse((self.published/"current.json").exists())

    def test_interrupted_publish_keeps_complete_old_generation(self):
        certificate, key = self.pair
        assets.publish("node.4399.com", certificate, key, directory=self.published, roots=self.ca.store)
        old = assets.pointer(self.published)
        real_atomic = assets.atomic
        def fail_key(path, data, mode=0o640):
            if path.name == "privkey.pem":
                raise OSError("disk full")
            real_atomic(path, data, mode)
        with patch.object(assets, "atomic", fail_key):
            with self.assertRaises(OSError):
                assets.publish("node.4399.com", certificate, key, directory=self.published, roots=self.ca.store)
        self.assertEqual(assets.pointer(self.published), old)
        self.assertEqual(len(list(self.published.iterdir())), 2)

    def test_socket_umask_cannot_remove_generation_traversal_permissions(self):
        previous = os.umask(0o117)
        try:
            assets.publish("node.4399.com", *self.pair, directory=self.published, roots=self.ca.store)
        finally:
            os.umask(previous)
        generation = assets.pointer(self.published)["generation"]
        self.assertEqual((self.published/generation).stat().st_mode & 0o777, 0o750)
        self.assertEqual((self.published/generation/"privkey.pem").stat().st_mode & 0o777, 0o640)

    def test_expired_certificate_is_never_healthy(self):
        manager = self.manager()
        manager.step()
        self.served = manager.status()["fingerprint"]
        manager.step()
        self.current_time += 100*86400
        manager.refresh()
        self.assertEqual(manager.status()["state"], "FAILED")
        self.assertEqual(manager.status()["last_error"], "certificate_expired")
        self.assertFalse(manager.status()["https_ready"])

    def test_single_controller_lease_and_private_permissions(self):
        self.manager()
        with self.assertRaisesRegex(ValueError, "already running"):
            self.manager()
        os.chmod(self.private, 0o755)
        with self.assertRaisesRegex(ValueError, "isolated persistent"):
            self.manager()

    def test_restart_preserves_certificate_and_retry_cooldown(self):
        manager = self.manager()
        manager.step()
        old = assets.pointer(self.published)
        manager.journal.update(state="FAILED", error="ca_rate_limited", retry_at=self.current_time+3600)
        manager.save()
        manager.close()
        self._cleanups.pop()
        restarted = self.manager()
        restarted.step()
        self.assertEqual(self.calls, 1)
        self.assertEqual(assets.pointer(self.published), old)
        self.assertGreater(restarted.journal["retry_at"], self.current_time)

    def test_corrupt_journal_preserves_original_and_imposes_cooldown(self):
        assets.atomic(self.private/"lifecycle.json", b'{"state":"ACTIVE","retry_at":"broken"}', 0o600)
        manager = self.manager()
        manager.step()
        self.assertEqual(self.calls, 0)
        self.assertTrue(list(self.private.glob("lifecycle-invalid-*")))
        self.assertEqual(manager.status()["last_error"], "journal_recovered")

    def test_crash_after_pair_publication_recovers_active_without_duplicate_order(self):
        manager = self.manager()
        manager.step()
        self.served = manager.status()["fingerprint"]
        manager.journal.update(state="ISSUING")
        manager.save()
        manager.close()
        self._cleanups.pop()
        restarted = self.manager()
        restarted.step()
        self.assertEqual(restarted.status()["state"], "ACTIVE")
        self.assertTrue(restarted.status()["https_ready"])
        self.assertEqual(self.calls, 1)
        self.assertGreater(restarted.journal["retry_at"], self.current_time)

    def test_domain_change_requests_new_identity(self):
        manager = self.manager()
        manager.step()
        manager.close()
        self._cleanups.pop()
        self.pair = self.ca.pair("new.4399.com")
        changed = self.manager("new.4399.com")
        self.assertFalse(changed.status()["https_ready"])
        changed.step()
        self.assertEqual(changed.status()["hostname"], "new.4399.com")
        self.assertEqual(self.calls, 2)

    def test_disabled_does_not_consume_corrupt_certificate_state(self):
        (self.published/"current.json").write_text("broken")
        (self.private/"lifecycle.json").write_text("retain damaged lifecycle for later recovery")
        manager = self.manager(enabled=False)
        manager.step()
        self.assertEqual(manager.status()["state"], "DISABLED")
        self.assertEqual(self.calls, 0)
        self.assertEqual((self.published/"current.json").read_text(), "broken")
        self.assertEqual((self.private/"lifecycle.json").read_text(), "retain damaged lifecycle for later recovery")

    def test_challenge_binds_and_releases_only_during_operation(self):
        manager = self.manager(challenge_address=("127.0.0.1", 0))
        with manager.responder({"/.well-known/acme-challenge/test": "value"}):
            self.assertTrue(manager.status()["job"]["port80_active"])
        self.assertFalse(manager.status()["job"]["port80_active"])

    @unittest.skipUnless(socket.has_ipv6, "kernel does not support IPv6")
    def test_aaaa_profile_serves_same_challenge_on_ipv4_and_ipv6_then_releases_both(self):
        import http.client
        with socket.socket() as free:
            free.bind(("127.0.0.1", 0))
            port = free.getsockname()[1]
        manager = self.manager(challenge_address=("", port))
        manager.ipv6_required = True
        with manager.responder({"/.well-known/acme-challenge/test": "proof"}):
            for host in ("127.0.0.1", "::1"):
                connection = http.client.HTTPConnection(host, port, timeout=2)
                try:
                    connection.request("GET", "/.well-known/acme-challenge/test", headers={"Host": "node.4399.com"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"proof")
                finally:
                    connection.close()
        for family, host in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
            with socket.socket(family) as stream:
                stream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    stream.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                stream.bind((host, port))

    def test_download_is_wrapped_for_browser_not_plaintext(self):
        manager = self.manager()
        manager.step()
        self.served = manager.status()["fingerprint"]
        manager.step()
        browser = ec.generate_private_key(ec.SECP256R1())
        encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
        decode = lambda value: base64.urlsafe_b64decode(value+"="*((-len(value))%4))
        nonce = encode(os.urandom(32))
        value = manager.download(encode(browser.public_key().public_bytes(serialization.Encoding.X962,
                                  serialization.PublicFormat.UncompressedPoint)), nonce)
        self.assertNotIn("PRIVATE KEY", json.dumps(value))
        envelope = value["download"]
        aad = "\n".join(("frontiercloud.certificate.download.v1", "node.4399.com", value["fingerprint"], nonce)).encode()
        shared = browser.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decode(envelope["public_key"])))
        wrapping = HKDF(hashes.SHA256(), 32, decode(envelope["salt"]), aad).derive(shared)
        plain = json.loads(AESGCM(wrapping).decrypt(decode(envelope["iv"]), decode(envelope["ciphertext"]), aad))
        self.assertEqual(plain["certificate"].encode(), self.pair[0])
        self.assertEqual(plain["private_key"].encode(), self.pair[1])
        with self.assertRaises(ValueError):
            manager.download("invalid", nonce)

    def test_download_requires_real_serving_verification(self):
        manager = self.manager()
        manager.step()
        browser = ec.generate_private_key(ec.SECP256R1())
        encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
        with self.assertRaisesRegex(ValueError, "verified active"):
            manager.download(encode(browser.public_key().public_bytes(serialization.Encoding.X962,
                             serialization.PublicFormat.UncompressedPoint)), encode(os.urandom(32)))

    def test_migration_validates_and_never_overwrites_other_identity(self):
        imported = self.base/"import"
        imported.mkdir()
        certificate, key = self.pair
        (imported/"fullchain.pem").write_bytes(certificate)
        (imported/"privkey.pem").write_bytes(key)
        first = adopt(host="node.4399.com", imported=imported, private=self.private, published=self.published, roots=self.ca.store)
        self.assertEqual(adopt(host="node.4399.com", imported=imported, private=self.private, published=self.published, roots=self.ca.store), first)
        other, other_key = self.ca.pair()
        (imported/"fullchain.pem").write_bytes(other)
        (imported/"privkey.pem").write_bytes(other_key)
        with self.assertRaisesRegex(ValueError, "overwrite"):
            adopt(host="node.4399.com", imported=imported, private=self.private, published=self.published, roots=self.ca.store)


class CertificateContractTests(unittest.TestCase):
    def test_only_public_standard_ports_and_verified_storage_endpoints(self):
        settings = SimpleNamespace(TLS_ENABLED=True, SERVER_NAME="node.4399.com", DEPLOYMENT_MODE="only_stroge",
                                   STORAGE_TRANSPORT="direct", STORAGE_PORT=443, STORAGE_ENDPOINT="https://node.4399.com")
        validate_settings(settings, {})
        for name in ("HTTP_PORT", "HTTPS_PORT"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "fixed at 80/443"):
                validate_settings(settings, {name: "8080"})
        for name in ("SSL_CERT_PATH", "SSL_KEY_PATH", "STORAGE_TLS_CERT", "STORAGE_TLS_KEY", "ACME_WEBROOT"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "retired"):
                validate_settings(settings, {name: "/legacy/path"})
        settings.STORAGE_PORT = 8443
        with self.assertRaisesRegex(ValueError, "Direct"):
            validate_settings(settings, {})
        settings.STORAGE_TRANSPORT = "relay"
        settings.STORAGE_ENDPOINT = "https://node.4399.com:8443"
        validate_settings(settings, {})
        settings.STORAGE_PORT = 443
        with self.assertRaisesRegex(ValueError, "Relay"):
            validate_settings(settings, {})

    def test_dns_names_no_ips_wildcards_or_urls(self):
        from app.services.certmanager.contract import hostname
        for host in ("localhost", "127.0.0.1", "*.4399.com", "https://4399.com", "4399.com:443", "bad..com", "-bad.com"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                hostname(host)


if __name__ == "__main__":
    unittest.main()
