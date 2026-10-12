"""Test-only replacement entrypoint; production never accepts custom CA settings."""
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.verification import Store

from app.services.certmanager import agent
from app.services.certmanager.issuer import ACMEIssuer
from app.services.certmanager.manager import Manager
from app.services.certmanager.contract import profile
from tests.acme_test_server import ACMETestServer
from tests.certificate_fixtures import TestCA

ca = object.__new__(TestCA)
ca.key = serialization.load_pem_private_key(Path("/test-ca/private.pem").read_bytes(), password=None)
ca.root = x509.load_pem_x509_certificate(Path("/test-ca/root.crt").read_bytes())
ca.store = Store([ca.root])
mock = ACMETestServer(agent.settings.SERVER_NAME, 80,
                      challenge_host="nginx" if profile(agent.settings) == "business" else "127.0.0.1", ca=ca)


def fixture_manager(host, enabled, profile, relay_port):
    return Manager(host, enabled, profile, relay_port, lookup=lambda *_args, **_kwargs: ["test-address"],
                   issuer_factory=lambda private, name, responder: ACMEIssuer(private, name, responder, directory_url=mock.url+"/directory"))


agent.Manager = fixture_manager
try:
    agent.main()
finally:
    mock.close()
