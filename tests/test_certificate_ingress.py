"""Real HTTP proxy and python-acme tests for delayed Business bootstrap."""
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.services.certmanager import assets
from app.services.certmanager.issuer import ACMEIssuer
from app.services.certmanager.manager import Manager
from tests.acme_test_server import ACMETestServer


def available_port():
    with socket.socket() as stream:
        stream.bind(('127.0.0.1', 0))
        return stream.getsockname()[1]


class BusinessIngressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.private, self.published, self.runtime = (self.root / name for name in ('private', 'published', 'runtime'))
        for directory, mode in ((self.private, 0o700), (self.published, 0o750), (self.runtime, 0o710)):
            directory.mkdir(mode=mode)
        self.backend_port, self.ingress_port = available_port(), available_port()
        self.ca = ACMETestServer('node.4399.com', self.ingress_port)
        self.addCleanup(self.ca.close)
        self.now, self.mode, self.requests = time.time(), 'proxy', []
        self.server = None
        self.manager = Manager('node.4399.com', True, 'business', private=self.private,
            published=self.published, runtime=self.runtime, roots=self.ca.ca.store,
            challenge_address=('127.0.0.1', self.backend_port), ingress_address=('127.0.0.1', self.ingress_port),
            lookup=lambda *_args, **_kwargs: ['test-address'], probe=lambda: '', now=lambda: self.now,
            issuer_factory=lambda private, host, responder: ACMEIssuer(private, host, responder, directory_url=self.ca.url + '/directory'))
        self.addCleanup(self.manager.close)

    def start_ingress(self):
        owner = self
        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def do_GET(self):
                owner.requests.append((self.path, self.headers.get('Host')))
                mode = owner.mode
                if mode == 'redirect':
                    self.send_response(302); self.send_header('Location', 'https://node.4399.com' + self.path)
                    self.send_header('Content-Length', '0'); self.end_headers(); return
                if mode == 'wrong' or mode == 'after_order' and owner.ca.orders:
                    self.send_response(200); self.send_header('Content-Length', '5')
                    self.end_headers(); self.wfile.write(b'wrong'); return
                upstream = http.client.HTTPConnection('127.0.0.1', owner.backend_port, timeout=2)
                try:
                    upstream.request('GET', self.path, headers={'Host': self.headers.get('Host')})
                    response = upstream.getresponse(); body = response.read(8193)
                    self.send_response(response.status); self.send_header('Content-Length', str(len(body)))
                    self.end_headers(); self.wfile.write(body)
                finally:
                    upstream.close()
        self.server = HTTPServer(('127.0.0.1', self.ingress_port), Proxy)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        thread.start()
        def close():
            self.server.shutdown(); self.server.server_close(); thread.join(timeout=3)
        self.addCleanup(close)

    def assert_no_ca(self, code):
        value = self.manager.status()
        self.assertEqual(value['state'], 'PENDING'); self.assertEqual(value['last_error'], code)
        self.assertTrue(value['controller_running']); self.assertFalse(value['https_ready'])
        self.assertEqual(self.manager.journal['failures'], 0)
        self.assertLessEqual(self.manager.journal['retry_at'], self.now + 5)
        self.assertEqual(self.ca.account_creations, 0); self.assertFalse(self.ca.orders)
        self.assertEqual(self.ca.validated_challenges, 0); self.assertFalse(self.ca.failures)
        self.assertFalse((self.private / 'account-key.pem').exists())
        self.assertFalse((self.private / 'order.json').exists()); self.assertFalse(self.manager.active_challenge)

    def test_delayed_ingress_creates_no_account_or_order_then_issues_when_ready(self):
        self.manager.step(); self.assert_no_ca('http_ingress_unavailable')
        self.start_ingress()
        self.manager.step(); self.assertEqual(self.ca.account_creations, 0)
        self.now += 6; self.manager.step()
        self.assertEqual(self.ca.account_creations, 1); self.assertEqual(len(self.ca.orders), 1)
        self.assertEqual(self.ca.validated_challenges, 1); self.assertFalse(self.ca.failures)
        self.assertIn('fingerprint', self.manager.status()); self.assertFalse(self.manager.active_challenge)
        self.assertTrue(all(host == 'node.4399.com' for _path, host in self.requests))
        self.assertEqual(len(set(path for path, _host in self.requests)), 2)

    def test_redirect_is_rejected_without_ca_consumption_then_recovers(self):
        self.mode = 'redirect'; self.start_ingress()
        self.manager.step(); self.assert_no_ca('http_ingress_redirect')
        self.mode = 'proxy'; self.now += 6; self.manager.step()
        self.assertEqual(self.ca.validated_challenges, 1); self.assertFalse(self.ca.failures)

    def test_wrong_body_is_rejected_without_ca_consumption_then_recovers(self):
        self.mode = 'wrong'; self.start_ingress()
        self.manager.step(); self.assert_no_ca('http_ingress_mismatch')
        self.mode = 'proxy'; self.now += 6; self.manager.step()
        self.assertEqual(self.ca.validated_challenges, 1); self.assertFalse(self.ca.failures)

    def test_ingress_loss_after_order_keeps_pending_order_and_never_answers_ca(self):
        self.mode = 'after_order'; self.start_ingress(); self.manager.step()
        self.assertEqual(self.manager.status()['last_error'], 'http_ingress_mismatch')
        self.assertEqual(len(self.ca.orders), 1); self.assertEqual(self.ca.validated_challenges, 0)
        self.assertEqual(self.manager.journal['failures'], 0); self.assertFalse(self.ca.failures)
        self.assertTrue((self.private / 'order.json').exists())
        self.mode = 'proxy'; self.now += 6; self.manager.step()
        self.assertEqual(len(self.ca.orders), 1); self.assertEqual(self.ca.validated_challenges, 1)
        self.assertFalse(self.ca.failures); self.assertIn('fingerprint', self.manager.status())

    def test_formal_ca_still_rejects_public_ingress_even_when_local_route_passes(self):
        self.start_ingress(); self.ca.challenge_port = available_port()
        self.manager.step()
        self.assertEqual(self.manager.status()['state'], 'FAILED')
        self.assertEqual(self.manager.journal['failures'], 1)
        self.assertEqual(self.ca.validated_challenges, 0); self.assertEqual(len(self.ca.failures), 1)
        self.assertFalse((self.published / 'current.json').exists())

    def test_unready_renewal_retains_valid_generation_then_renews_normally(self):
        self.start_ingress(); self.manager.step()
        original = assets.pointer(self.published)
        self.manager.journal['next_renewal'] = 1
        self.mode = 'wrong'; self.manager.step()
        self.assertEqual(self.manager.status()['state'], 'DEGRADED')
        self.assertEqual(assets.pointer(self.published), original)
        self.assertEqual(len(self.ca.orders), 1); self.assertEqual(self.manager.journal['failures'], 0)
        self.mode = 'proxy'; self.now += 6; self.manager.step()
        self.assertEqual(len(self.ca.orders), 2); self.assertEqual(self.ca.validated_challenges, 2)
        self.assertEqual(assets.pointer(self.published)['previous'], original['generation'])
        self.assertFalse(self.ca.failures)


if __name__ == '__main__':
    unittest.main()
