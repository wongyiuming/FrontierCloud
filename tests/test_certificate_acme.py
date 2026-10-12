"""Real python-acme requests against a local protocol/verifier fixture."""
import socket
import tempfile
import unittest
from pathlib import Path

from app.services.certmanager import assets
from app.services.certmanager.issuer import ACMEIssuer
from app.services.certmanager.manager import Manager
from tests.acme_test_server import ACMETestServer


class ActualACMETests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.private, self.published, self.runtime = (self.base/name for name in ("private", "published", "runtime"))
        for path, mode in ((self.private, 0o700), (self.published, 0o750), (self.runtime, 0o710)):
            path.mkdir(mode=mode)
        with socket.socket() as stream:
            stream.bind(("127.0.0.1", 0))
            self.port = stream.getsockname()[1]
        self.ca = ACMETestServer("node.4399.com", self.port)
        self.addCleanup(self.ca.close)
        self.manager = Manager("node.4399.com", True, "direct", private=self.private, published=self.published,
                               runtime=self.runtime, roots=self.ca.ca.store, challenge_address=("127.0.0.1", self.port),
                               issuer_factory=lambda private, host, responder: ACMEIssuer(private, host, responder, directory_url=self.ca.url+"/directory"),
                               lookup=lambda *_args, **_kwargs: ["test-address"], probe=lambda: "")
        self.addCleanup(self.manager.close)

    def test_actual_jws_csr_http01_issuance_account_restore_and_renewal(self):
        self.manager.step()
        self.assertEqual(self.ca.failures, [])
        self.assertEqual(self.ca.account_creations, 1)
        self.assertEqual(self.ca.validated_challenges, 1)
        self.assertIn("fingerprint", self.manager.status(), self.manager.status())
        old = assets.pointer(self.published)
        self.assertFalse(self.manager.status()["job"]["port80_active"])
        with socket.socket() as stream:
            stream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            stream.bind(("127.0.0.1", self.port))
        # Restart issuer, restoring account key/registration from private persistent state.
        self.manager.issuer = ACMEIssuer(self.private, self.manager.host, self.manager.responder, directory_url=self.ca.url+"/directory")
        self.manager.journal["next_renewal"] = 1
        self.manager.step()
        self.assertEqual(self.ca.failures, [])
        self.assertEqual(self.ca.account_creations, 1)
        self.assertEqual(self.ca.validated_challenges, 2)
        self.assertEqual(assets.pointer(self.published)["previous"], old["generation"])

    def test_interrupted_order_resumes_same_csr_and_one_order(self):
        def interrupted(_phase): raise RuntimeError("simulated process interruption")
        with self.assertRaisesRegex(RuntimeError, "interruption"):
            self.manager.issuer.issue(interrupted)
        self.assertEqual(len(self.ca.orders), 1)
        self.assertTrue((self.private/"order.json").exists())
        restored = ACMEIssuer(self.private, self.manager.host, self.manager.responder, directory_url=self.ca.url+"/directory")
        certificate, key = restored.issue(lambda _phase: None)
        assets.validate("node.4399.com", certificate, key, roots=self.ca.ca.store)
        self.assertEqual(self.ca.failures, [])
        self.assertEqual(len(self.ca.orders), 1)
        self.assertEqual(self.ca.account_creations, 1)
        self.assertFalse((self.private/"order.json").exists())

    def test_actual_ca_rate_limit_classification_and_persisted_cooldown(self):
        import time
        self.ca.fail_order = ("rateLimited", 429)
        self.manager.step()
        self.assertEqual(self.ca.failures, [])
        self.assertEqual(self.manager.status()["last_error"], "ca_rate_limited")
        self.assertGreaterEqual(self.manager.journal["retry_at"], int(time.time())+3598)
        self.manager.step()
        self.assertEqual(len(self.ca.orders), 0)

    def test_actual_port_conflict_is_recoverable_and_does_not_crash_controller(self):
        import time
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", self.port))
            occupied.listen()
            self.manager.step()
        self.assertTrue(self.manager.status()["controller_running"])
        self.assertEqual(self.manager.status()["last_error"], "port80_unavailable")
        self.manager.journal["retry_at"] = 0
        self.manager.step()
        self.assertEqual(self.ca.failures, [])
        self.assertEqual(len(self.ca.orders), 1)
        self.assertIn("fingerprint", self.manager.status())

    def test_corrupt_account_key_preserved_before_new_account_attempt(self):
        (self.private/"account-key.pem").write_bytes(b"damaged private material")
        self.manager.step()
        self.assertEqual(self.manager.status()["last_error"], "account_key_recovered")
        self.assertEqual(self.ca.account_creations, 0)
        self.assertTrue(list(self.private.glob("account-key-invalid-*.pem")))
        self.manager.journal["retry_at"] = 0
        self.manager.step()
        self.assertEqual(self.ca.failures, [])
        self.assertEqual(self.ca.account_creations, 1)
        self.assertIn("fingerprint", self.manager.status())


if __name__ == "__main__":
    unittest.main()
