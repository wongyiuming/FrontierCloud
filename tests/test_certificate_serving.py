"""Real SSL handshakes, retained streams, and confined Nginx rollback controls."""
import hashlib
import importlib.util
import json
import socket
import socketserver
import ssl
import tempfile
import threading
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from app.services.certmanager import assets
from app.services.certmanager.server import HotTLS
from tests.certificate_fixtures import TestCA


class RealStorageTLSTests(unittest.TestCase):
    def test_native_tls_hot_rotation_preserves_existing_connection(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ca = TestCA()
            first_cert, first_key = ca.pair()
            first = assets.publish("node.4399.com", first_cert, first_key, directory=directory, roots=ca.store)
            tls = HotTLS("node.4399.com", directory, roots=ca.store)
            class Handler(socketserver.StreamRequestHandler):
                def handle(self):
                    try:
                        while data := self.rfile.readline(128):
                            self.wfile.write(data)
                            self.wfile.flush()
                    except (ConnectionResetError, BrokenPipeError, ssl.SSLEOFError):
                        pass
            class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
                daemon_threads = True
                def get_request(self):
                    raw, address = super().get_request()
                    try:
                        return tls.context.wrap_socket(raw, server_side=True), address
                    except BaseException:
                        raw.close()
                        raise
            server = Server(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
            thread.start()
            client_context = ssl.create_default_context(cadata=ca.root.public_bytes(serialization.Encoding.PEM).decode())
            def connect():
                return client_context.wrap_socket(socket.create_connection(server.server_address, timeout=3), server_hostname="node.4399.com")
            try:
                with connect() as old_connection:
                    self.assertEqual(hashlib.sha256(old_connection.getpeercert(binary_form=True)).hexdigest(), assets.fingerprint(first))
                    old_connection.sendall(b"before\n")
                    self.assertEqual(old_connection.recv(128), b"before\n")
                    second_cert, second_key = ca.pair()
                    second = assets.publish("node.4399.com", second_cert, second_key, directory=directory, roots=ca.store)
                    with connect() as new_connection:
                        self.assertEqual(hashlib.sha256(new_connection.getpeercert(binary_form=True)).hexdigest(), assets.fingerprint(second))
                    old_connection.sendall(b"after\n")
                    self.assertEqual(old_connection.recv(128), b"after\n")
                    self.assertEqual(hashlib.sha256(old_connection.getpeercert(binary_form=True)).hexdigest(), assets.fingerprint(first))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


class NginxTLSControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        spec = importlib.util.spec_from_file_location("test_tls_edge", Path(__file__).parents[1]/"nginx"/"tls_edge.py")
        self.edge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.edge)
        self.edge.RUNTIME = self.base/"runtime"
        self.edge.PUBLISHED = self.base/"published"
        self.edge.CONFIG = self.base/"nginx.conf"
        self.edge.RUNTIME.mkdir()
        self.edge.PUBLISHED.mkdir()
        self.ca = TestCA()
        certificate, key = self.ca.pair()
        assets.publish("node.4399.com", certificate, key, directory=self.edge.PUBLISHED, roots=self.ca.store)
        for file in self.edge.FILES:
            (self.edge.RUNTIME/file).write_bytes(b"old-config")
            (self.edge.RUNTIME/(file+".automatic-template")).write_bytes(b"/etc/nginx/certs/fullchain.pem /etc/nginx/certs/privkey.pem")
        self.edge.CONFIG.write_bytes(b"old-business")
        (self.edge.RUNTIME/"business.conf.automatic-template").write_bytes(b"new-business")
        self.calls = []
        self.edge.execute = lambda *args: self.calls.append(args)
        self.edge.serving = lambda *_args: None

    def test_generation_reload_and_idempotency(self):
        self.edge.apply("node.4399.com")
        self.assertEqual(self.calls, [("-t",), ("-s", "reload")])
        self.assertEqual(self.edge.CONFIG.read_bytes(), b"new-business")
        self.edge.apply("node.4399.com")
        self.assertEqual(len(self.calls), 2)

    def test_reload_failure_restores_previous_complete_configuration(self):
        def execute(*args):
            self.calls.append(args)
            if len(self.calls) == 2: raise RuntimeError("reload failed")
        self.edge.execute = execute
        with self.assertRaises(RuntimeError): self.edge.apply("node.4399.com")
        self.assertEqual(self.edge.CONFIG.read_bytes(), b"old-business")
        for file in self.edge.FILES:
            self.assertEqual((self.edge.RUNTIME/file).read_bytes(), b"old-config")
        self.assertFalse((self.edge.RUNTIME/"tls-applied").exists())

    def test_invalid_receipt_rejected_before_nginx_mutation(self):
        generation = assets.pointer(self.edge.PUBLISHED)["generation"]
        receipt = self.edge.PUBLISHED/generation/"verified.json"
        value = json.loads(receipt.read_text())
        value["hostname"] = "wrong.4399.com"
        receipt.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "receipt mismatch"):
            self.edge.apply("node.4399.com")
        self.assertEqual(self.calls, [])

    def test_bootstrap_has_only_challenge_public_traffic(self):
        configuration = self.edge.bootstrap("node.4399.com", True).decode()
        self.assertIn("listen 80", configuration)
        self.assertIn("listen [::]:80", configuration)
        self.assertIn("certificate-agent:8001", configuration)
        self.assertIn("location / { return 444; }", configuration)
        self.assertNotIn("web:8000", configuration)


if __name__ == "__main__":
    unittest.main()
