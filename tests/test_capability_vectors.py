"""Dependency-light capability oracle also run by the store interoperability image."""
import json
from pathlib import Path
import unittest

from app.services.federation import protocol


class CapabilityConformanceTests(unittest.TestCase):
    def test_shared_negotiation_vectors(self):
        path = Path(__file__).resolve().parents[1] / "protocol/v2/vectors/capabilities.json"
        for vector in json.loads(path.read_text(encoding="utf-8"))["cases"]:
            with self.subTest(vector["name"]):
                if not vector["valid"]:
                    with self.assertRaises(protocol.ProtocolError):
                        protocol.read_capabilities(vector["message"])
                    continue
                self.assertEqual(protocol.negotiate_capabilities(vector["message"]), vector["selected"])
                if vector["compatible"]:
                    protocol.negotiate_capabilities(vector["message"], *vector["required"])
                else:
                    with self.assertRaises(protocol.ProtocolError):
                        protocol.negotiate_capabilities(vector["message"], *vector["required"])

    def test_bounded_copy_and_no_implementation_metadata(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.read_capabilities({"capabilities": [f"feature-{i}" for i in range(65)]})
        copy = protocol.read_capabilities({})
        copy.clear()
        self.assertEqual(len(protocol.read_capabilities({})), 5)
        for private in ("go", "python", "sqlite", "mysql", "restore", "worker"):
            self.assertFalse(any(private in item for item in protocol.BASELINE_CAPABILITIES))


if __name__ == "__main__":
    unittest.main()
