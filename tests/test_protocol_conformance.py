"""Conformance checks shared by the Python and future Go runtimes."""
from __future__ import annotations

import hashlib
import json
import math
import unittest
from pathlib import Path

from app.services.federation import protocol as p


VECTOR_ROOT = Path(__file__).resolve().parents[1] / "protocol" / "v2" / "vectors"
SCHEMA_ROOT = VECTOR_ROOT.parent / "schemas"


def vector(name: str) -> dict:
    return json.loads((VECTOR_ROOT / name).read_text(encoding="utf-8"))


class ProtocolConformanceTests(unittest.TestCase):
    def test_search_alias_vectors(self):
        from app.services.media_search import compact_search_text, build_search_text
        from pypinyin import lazy_pinyin
        for case in vector("search.json")["cases"]:
            with self.subTest(value=case["value"]):
                self.assertEqual(compact_search_text(case["value"]), case["compact"])
                self.assertEqual("".join(lazy_pinyin(case["value"])), case["pinyin"])
                self.assertEqual(build_search_text(case["value"]), case["aliases"])

    def test_fernet_persistent_vault_vector(self):
        from cryptography.fernet import Fernet
        value = vector("fernet.json")
        self.assertEqual(Fernet(value["key"].encode()).decrypt(value["token"].encode()).decode(), value["plaintext"])

    def test_protocol_assets_are_valid_json_and_versioned(self):
        for path in sorted((*VECTOR_ROOT.glob("*.json"), *SCHEMA_ROOT.glob("*.json"))):
            value = json.loads(path.read_text(encoding="utf-8"))
            if path.parent == VECTOR_ROOT:
                self.assertEqual(value["format"], "frontiercloud-protocol-vector", path)
                self.assertEqual(value["version"], 1, path)
                self.assertEqual(value["protocol"], p.PROTOCOL_VERSION, path)

    def test_canonical_json_vectors(self):
        for case in vector("canonical-json.json")["cases"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(p.canonical(case["value"]), case["canonical"].encode("utf-8"))
        for invalid in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ValueError):
                p.canonical(invalid)

    def test_ed25519_vector(self):
        value = vector("ed25519.json")
        self.assertEqual(p.public_key(value["private_key"]), value["public_key"])
        self.assertEqual(p.canonical(value["payload"]).decode("utf-8"), value["canonical"])
        envelope = p.sign(value["private_key"], value["payload"])
        self.assertEqual(envelope["signature"], value["signature"])
        self.assertEqual(p.verify(value["public_key"], envelope), value["payload"])

    def test_node_auth_vector(self):
        value = vector("node-auth.json")
        body = value["body"].encode("utf-8")
        self.assertEqual(p.encode(body), value["body_base64url"])
        self.assertEqual(hashlib.sha256(body).hexdigest(), value["body_sha256"])
        headers = {
            "x-node-relationship": value["relationship"],
            "x-node-time": value["timestamp"],
            "x-node-nonce": value["nonce"],
            "x-node-signature": value["signature_hex"],
        }
        self.assertEqual(
            p.verify_auth(
                value["credential"], headers, value["method"], value["path"], body,
                int(value["timestamp"]),
            ),
            value["nonce"],
        )

    def test_media_capability_vector(self):
        value = vector("media-token.json")
        token = p.media_token(
            value["credential"], now=value["issued_at"], **value["arguments"],
        )
        self.assertEqual(token, value["token"])
        self.assertEqual(
            p.verify_media_token(value["credential"], token, value["verify_at"]),
            value["payload"],
        )

    def test_storage_capability_vector(self):
        value = vector("storage-token.json")
        token = p.storage_token(
            value["credential"], now=value["issued_at"], **value["arguments"],
        )
        self.assertEqual(token, value["token"])
        self.assertEqual(
            p.verify_storage_token(value["credential"], token, value["verify_at"]),
            value["payload"],
        )

    def test_recording_capability_vector(self):
        value = vector("recording-token.json")
        token = p.recording_token(
            value["credential"], now=value["issued_at"], **value["arguments"],
        )
        self.assertEqual(token, value["token"])
        self.assertEqual(
            p.verify_recording_token(value["credential"], token, value["verify_at"]),
            value["payload"],
        )


if __name__ == "__main__":
    unittest.main()
