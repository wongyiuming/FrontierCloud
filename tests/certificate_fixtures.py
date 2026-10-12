"""In-process test CA. These keys and custom roots never enter product images."""
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from cryptography.x509.verification import Store


class TestCA:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(timezone.utc)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "FrontierCloud unit-test CA")])
        self.root = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                     .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
                     .not_valid_before(now-timedelta(days=365)).not_valid_after(now+timedelta(days=3650))
                     .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
                     .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), True)
                     .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()), False)
                     .sign(self.key, hashes.SHA256()))
        self.store = Store([self.root])

    def sign(self, host, public_key, *, days=90, offset=0):
        now = datetime.now(timezone.utc)+timedelta(days=offset)
        return (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
                .issuer_name(self.root.subject).public_key(public_key).serial_number(x509.random_serial_number())
                .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=days))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
                .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), True)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), False)
                .sign(self.key, hashes.SHA256()))

    def pair(self, host="node.4399.com", *, days=90, offset=0):
        key = ec.generate_private_key(ec.SECP256R1())
        certificate = self.sign(host, key.public_key(), days=days, offset=offset)
        return (certificate.public_bytes(serialization.Encoding.PEM)+self.root.public_bytes(serialization.Encoding.PEM),
                key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
