"""ACME v2 is provided by python-acme, never a local reimplementation."""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

import josepy as jose
from acme import challenges, client, errors, messages
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from .assets import atomic, bounded

CA = "https://acme-v02.api.letsencrypt.org/directory"


class ACMEIssuer:
    """One isolated controller owns the account, order and HTTP-01 responder."""

    def __init__(self, private, host, responder, *, network_factory=None, directory_url=CA):
        self.private, self.host, self.responder = private, host, responder
        # Only in-process tests can inject a CA. No CA URL/path/insecure setting is exposed.
        self.directory_url = directory_url
        self.network_factory = network_factory or client.ClientNetwork
        self.acme = None

    def open(self):
        if self.acme is not None:
            return self.acme
        path = self.private / "account-key.pem"
        try:
            key = serialization.load_pem_private_key(bounded(path, 16384), password=None)
            if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
                raise ValueError("unsupported ACME account key")
        except FileNotFoundError:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            atomic(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()), 0o600)
        except (ValueError, TypeError):
            # Preserve damaged account state. A later backoff-controlled attempt
            # registers a fresh account without rewriting any valid serving pair.
            suffix = secrets.token_hex(8)
            path.rename(self.private / ("account-key-invalid-"+suffix+".pem"))
            account = self.private/"account.json"
            if account.exists():
                account.rename(self.private/("account-invalid-"+suffix+".json"))
            raise ValueError("account_key_recovered") from None
        net = self.network_factory(jose.JWKRSA(key=key), user_agent="FrontierCloud-Python/automatic-TLS", timeout=15)
        net.session.trust_env = False
        directory = client.ClientV2.get_directory(self.directory_url, net)
        acme = client.ClientV2(directory, net)
        account_path = self.private / "account.json"
        try:
            net.account = messages.RegistrationResource.json_loads(bounded(account_path, 16384).decode())
            net.account = acme.query_registration(net.account)
        except FileNotFoundError:
            net.account = acme.new_account(messages.NewRegistration.from_data(terms_of_service_agreed=True))
        except (jose.DeserializationError, ValueError, TypeError):
            account_path.rename(self.private/("account-invalid-"+secrets.token_hex(8)+".json"))
            raise ValueError("account_record_recovered") from None
        atomic(account_path, net.account.json_dumps().encode(), 0o600)
        self.acme = acme
        return acme

    def issue(self, phase):
        acme = self.open()
        pending_path = self.private / "order.json"
        pending = None
        try:
            pending = json.loads(bounded(pending_path, 131072))
            if (not isinstance(pending, dict) or not isinstance(pending.get("host"), str)
                    or not isinstance(pending.get("created_at"), int)
                    or not isinstance(pending.get("key"), str) or not isinstance(pending.get("order"), str)):
                raise ValueError("invalid pending ACME order")
        except FileNotFoundError:
            pass
        except (ValueError, TypeError):
            pending_path.rename(self.private/("order-invalid-"+secrets.token_hex(8)+".json"))
            raise ValueError("order_record_recovered") from None
        if (pending and pending.get("host") == self.host
                and pending.get("created_at", 0) > int(datetime.now(timezone.utc).timestamp()) - 600):
            key_pem = pending["key"].encode()
            order = messages.OrderResource.json_loads(pending["order"])
        else:
            key = ec.generate_private_key(ec.SECP256R1())
            key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption())
            csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).add_extension(
                x509.SubjectAlternativeName([x509.DNSName(self.host)]), critical=False,
            ).sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
            order = acme.new_order(csr)
            atomic(pending_path, json.dumps({"host": self.host, "created_at": int(datetime.now(timezone.utc).timestamp()),
                    "key": key_pem.decode(), "order": order.json_dumps()}).encode(), 0o600)
        resources = []
        for authorization in order.authorizations:
            if authorization.body.status == messages.STATUS_VALID:
                continue
            challenge = next((item for item in authorization.body.challenges if isinstance(item.chall, challenges.HTTP01)), None)
            if challenge is None:
                raise ValueError("CA did not offer HTTP-01")
            response, validation = challenge.response_and_validation(acme.net.key)
            resources.append((challenge, response, challenge.chall.path, validation))
        phase("VALIDATING")
        try:
            with self.responder({path: validation for _challenge, _response, path, validation in resources}):
                for challenge, response, _path, _validation in resources:
                    acme.answer_challenge(challenge, response)
                phase("ISSUING")
                order = acme.poll_and_finalize(order, deadline=datetime.now() + timedelta(seconds=180))
        except errors.ValidationError:
            pending_path.unlink(missing_ok=True)
            raise
        pending_path.unlink(missing_ok=True)
        return order.fullchain_pem.encode(), key_pem

    def renewal_time(self, certificate):
        renew, recheck = self.open().renewal_time(certificate)
        return (int(renew.timestamp()) if renew else None, int(recheck.timestamp()))
