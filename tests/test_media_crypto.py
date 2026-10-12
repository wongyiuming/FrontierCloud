import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.services import media_crypto as crypto


def browser_public(private):
    return base64.b64encode(private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)).decode()


def browser_wrapping(private, grant):
    server = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), base64.b64decode(grant["public_key"]))
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=base64.b64decode(grant["salt"]),
        info=("frontiercloud:browser-wrap:v1:" + grant["session_id"]).encode()).derive(private.exchange(ec.ECDH(), server))


class MediaCryptoTests(unittest.TestCase):
    def setUp(self):
        self.now = 1800000000.0
        self.manager = crypto.Manager(bytes(range(32)), now=lambda: self.now)
        self.private = ec.generate_private_key(ec.SECP256R1())

    def test_gin_wire_golden_key_nonce_and_aad(self):
        meta = crypto.Metadata(1, "AES-256-GCM", "000102030405060708090a0b0c0d0e0f", "EBESExQVFhc=", 1048579, 1048576, 1048611)
        expected = hmac.digest(bytes(range(32)), b"frontiercloud:file-key:v1:000102030405060708090a0b0c0d0e0f", "sha256")
        self.assertEqual(self.manager.file_key(meta), expected)
        self.assertEqual(self.manager.key_id(), hmac.digest(bytes(range(32)), b"frontiercloud:premaster-id:v1", "sha256").hex())
        self.assertEqual(meta.chunk_nonce(1).hex(), "101112131415161700000001")
        self.assertEqual(meta.chunk_aad(1), b"frontiercloud:chunk:v1:000102030405060708090a0b0c0d0e0f:1048579:1")
        encrypted = AESGCM(expected).encrypt(meta.chunk_nonce(1), b"ABC", meta.chunk_aad(1))
        self.assertEqual(len(encrypted), 19)
        self.assertEqual(AESGCM(expected).decrypt(meta.chunk_nonce(1), encrypted, meta.chunk_aad(1)), b"ABC")
        with self.assertRaises(InvalidTag):
            AESGCM(expected).decrypt(meta.chunk_nonce(1), encrypted[:-1] + bytes([encrypted[-1] ^ 1]), meta.chunk_aad(1))
        with self.assertRaises(InvalidTag):
            AESGCM(expected).decrypt(meta.chunk_nonce(1), encrypted, meta.chunk_aad(0))

    def test_multifile_fixed_deadline_browser_ecdh_hkdf_envelopes(self):
        grant = self.manager.new_session("browser:one", browser_public(self.private))
        wrapping = browser_wrapping(self.private, grant)
        self.assertEqual(grant["expires_at"], 1800000900)
        self.assertEqual(grant["expires_in"], 900)
        for size in (1, crypto.CHUNK_SIZE, crypto.CHUNK_SIZE + 1):
            meta = crypto.Metadata.new(size)
            envelope = self.manager.wrap("browser:one", grant["session_id"], meta)
            actual = AESGCM(wrapping).decrypt(base64.b64decode(envelope["iv"]), base64.b64decode(envelope["wrapped_key"]),
                f"frontiercloud:key-envelope:v1:{grant['session_id']}:{meta.file_id}".encode())
            self.assertEqual(actual, self.manager.file_key(meta))
            self.assertEqual(envelope["expires_at"], grant["expires_at"])
            self.now += 60
        with self.assertRaises(crypto.SessionError):
            self.manager.wrap("browser:other", grant["session_id"], meta)
        self.now = grant["expires_at"]
        with self.assertRaises(crypto.SessionError):
            self.manager.wrap("browser:one", grant["session_id"], meta)
        # A browser may retain this previously delivered key after expiry.
        self.assertEqual(actual, self.manager.file_key(meta))

    def test_revoke_does_not_extend_or_evict_other_binding(self):
        first = self.manager.new_session("browser:one", browser_public(self.private))
        other = self.manager.new_session("browser:two", browser_public(self.private))
        self.manager.revoke_binding("browser:one")
        meta = crypto.Metadata.new(10)
        with self.assertRaises(crypto.SessionError):
            self.manager.wrap("browser:one", first["session_id"], meta)
        self.manager.wrap("browser:two", other["session_id"], meta)

    def test_conservative_remaining_seconds_and_expired_handshake(self):
        for started, elapsed, expected in ((1800000000.3, 0, 899), (1800000000, 1.5, 898), (1800000000, -1, 900), (1800000000, 900, 0)):
            calls = 0
            def clock():
                nonlocal calls
                calls += 1
                return started if calls == 1 else started + elapsed
            manager = crypto.Manager(bytes(32), now=clock)
            if expected:
                grant = manager.new_session("binding", browser_public(self.private))
                self.assertEqual(grant["expires_in"], expected)
                self.assertEqual(grant["expires_at"], int(started + 900))
            else:
                with self.assertRaises(crypto.SessionError):
                    manager.new_session("binding", browser_public(self.private))
                self.assertEqual(len(manager._sessions), 0)

    def test_admission_before_ecdh_does_not_evict_live_grants(self):
        public = browser_public(self.private)
        grants = [self.manager.new_session("browser:one", public) for _ in range(16)]
        with patch.object(ec, "generate_private_key", side_effect=AssertionError("ECDH executed before admission")):
            with self.assertRaises(crypto.SessionLimitError):
                self.manager.new_session("browser:one", public)
        self.assertEqual(len(self.manager._sessions), 16)
        self.manager.wrap("browser:one", grants[0]["session_id"], crypto.Metadata.new(2))
        self.now += 901
        self.manager.new_session("browser:one", public)
        self.assertEqual(len(self.manager._sessions), 1)

    def test_reserved_admin_capacity_and_atomic_concurrent_binding_limit(self):
        public = browser_public(self.private)
        with patch.object(crypto, "MAX_PUBLIC_SESSIONS", 2), patch.object(crypto, "MAX_ADMIN_SESSIONS", 1):
            self.manager.new_session("browser:one", public)
            self.manager.new_session("browser:two", public)
            with self.assertRaises(crypto.SessionLimitError):
                self.manager.new_session("browser:three", public)
            self.manager.new_session("admin:one", public)
            with self.assertRaises(crypto.SessionLimitError):
                self.manager.new_session("admin:two", public)
        manager = crypto.Manager(bytes(32))
        def create(_):
            try:
                manager.new_session("browser:same", public)
                return True
            except crypto.SessionLimitError:
                return False
        with ThreadPoolExecutor(max_workers=20) as executor:
            self.assertEqual(sum(executor.map(create, range(40))), 16)

    def test_invalid_client_points_and_base64_are_rejected(self):
        for value in ("", "bad!", base64.b64encode(bytes(65)).decode(), base64.b64encode(b"\x04" + bytes(64)).decode(), browser_public(self.private).rstrip("=")):
            with self.assertRaises(crypto.SessionError):
                self.manager.new_session("binding", value)

    def test_descriptor_boundaries_and_noncanonical_values(self):
        for size in (1, crypto.CHUNK_SIZE, crypto.CHUNK_SIZE + 1, crypto.MAX_PLAINTEXT_SIZE):
            meta = crypto.Metadata.new(size)
            self.assertEqual(crypto.Metadata.from_dict(meta.to_dict()), meta)
            self.assertLessEqual(meta.ciphertext_size, crypto.MAX_CIPHERTEXT_SIZE)
        for size in (0, -1, True, 1.0, crypto.MAX_PLAINTEXT_SIZE + 1):
            with self.assertRaises(crypto.MetadataError):
                crypto.Metadata.new(size)
        valid = crypto.Metadata.new(2).to_dict()
        for key, value in (("version", True), ("plaintext_size", 2.0), ("file_id", "A" * 32), ("nonce_prefix", "AAAAAAAAAAB="), ("ciphertext_size", 2), ("chunk_size", 512)):
            with self.assertRaises(crypto.MetadataError):
                crypto.Metadata.from_dict({**valid, key: value})
        with self.assertRaises(crypto.MetadataError):
            crypto.Metadata.from_dict({**valid, "untrusted": True})

    def test_preparation_binding_descriptor_mac_expiration_and_upload_mode(self):
        meta = crypto.Metadata.new(10)
        token = self.manager.sign_preparation("admin:one", meta)
        self.manager.verify_preparation("admin:one", token, meta)
        for binding, supplied, descriptor in (("admin:two", token, meta), ("admin:one", token + "x", meta), ("admin:one", token, crypto.Metadata.new(10)), ("admin:one", "a" * 2049, meta)):
            with self.assertRaises(crypto.SessionError):
                self.manager.verify_preparation(binding, supplied, descriptor)
        with patch.object(crypto, "manager", self.manager):
            self.assertIsNone(crypto.upload_encryption("plain", "", "", "admin:one"))
            self.assertEqual(crypto.upload_encryption("encrypted", meta.to_dict(), token, "admin:one"), meta)
            for mode, encoded, supplied in (("", "", ""), ("plain", meta.to_dict(), ""), ("encrypted", "", ""), ("plain", "", token)):
                with self.assertRaises((crypto.MetadataError, crypto.SessionError)):
                    crypto.upload_encryption(mode, encoded, supplied, "admin:one")
        self.now += 901
        with self.assertRaises(crypto.SessionError):
            self.manager.verify_preparation("admin:one", token, meta)

    def test_premaster_restart_missing_corrupt_and_permissions_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "private"
            with self.assertRaises(crypto.PremasterError):
                crypto.open_premaster(directory, has_encrypted_data=True)
            first = crypto.open_premaster(directory)
            reopened = crypto.open_premaster(directory, has_encrypted_data=True)
            self.assertEqual(first.key_id(), reopened.key_id())
            key = directory / "media-premaster.key"
            key.write_bytes(b"bad")
            with self.assertRaises(crypto.PremasterError):
                crypto.open_premaster(directory)
            key.unlink()
            with self.assertRaises(crypto.PremasterError):
                crypto.open_premaster(directory, has_encrypted_data=True)
            crypto.open_premaster(directory)
            if os.name != "nt":
                key.chmod(0o644)
                with self.assertRaises(crypto.PremasterError):
                    crypto.open_premaster(directory)

    @unittest.skipIf(os.name == "nt", "Windows symlink creation needs privileges")
    def test_symlink_directory_and_key_cannot_expose_external_secret(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            target = root / "target"
            crypto.open_premaster(target)
            link = root / "alias"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(crypto.PremasterError):
                crypto.open_premaster(link)
            private = root / "private"
            private.mkdir(mode=0o700)
            (private / "media-premaster.key").symlink_to(target / "media-premaster.key")
            with self.assertRaises(crypto.PremasterError):
                crypto.open_premaster(private)

    def test_public_creation_throttle_fixed_window_and_bounded_cache(self):
        admission = crypto.CreationAdmission(now=lambda: self.now)
        for _ in range(8):
            self.assertTrue(admission.allow("1.2.3.4", "cookie"))
        self.assertFalse(admission.allow("1.2.3.4", "cookie"))
        for index in range(56):
            self.assertTrue(admission.allow("1.2.3.4", "cookie" + str(index)))
        self.assertFalse(admission.allow("1.2.3.4", "new-cookie"))
        self.now += 60
        self.assertTrue(admission.allow("1.2.3.4", "cookie"))
        self.assertEqual(len(admission._windows), 2)

    def test_paths_and_snapshot_source_identity(self):
        for value in ("music/a/song.mp3", "vido/a/b/video.mp4", "lyrics/a/b/song.lrc"):
            self.assertEqual(crypto.managed_path(value)[0], value)
        for value in ("music/song.mp3", "lyrics/default.lrc", "../music/a/song.mp3", "music/a/.song.mp3", "music/a//song.mp3", "music/a/song.jpg", "music/a/b/c/song.mp3"):
            with self.assertRaises(crypto.MetadataError):
                crypto.managed_path(value)
        first = crypto.download_snapshot("a" * 64, "music/a/a.mp3", 42, "revision")
        for identifier, path, size, revision in (("b" * 64, "music/a/a.mp3", 42, "revision"), ("a" * 64, "music/a/b.mp3", 42, "revision"), ("a" * 64, "music/a/a.mp3", 43, "revision"), ("a" * 64, "music/a/a.mp3", 42, "new")):
            self.assertNotEqual(first, crypto.download_snapshot(identifier, path, size, revision))


if __name__ == "__main__":
    unittest.main()
