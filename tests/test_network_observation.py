import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import settings
from app.services import network_observation


ROOT = Path(__file__).resolve().parents[1]


class NetworkObservationTests(unittest.TestCase):
    def test_addresses_are_canonicalized_deduplicated_and_bounded(self):
        result = network_observation.normalize_observed_addresses([
            "203.0.113.5",
            "2001:0db8:0:0:0:0:0:1",
            "203.0.113.5",
        ])
        self.assertEqual(result, ["203.0.113.5", "2001:db8::1"])
        with self.assertRaises(ValueError):
            network_observation.normalize_observed_addresses(["not-an-ip"])
        with self.assertRaises(ValueError):
            network_observation.normalize_observed_addresses([f"203.0.113.{value}" for value in range(1, 10)])

    def test_stun_configuration_is_environment_driven_and_scheme_limited(self):
        with patch.object(settings, "WEBRTC_STUN_URLS", "stun:one.example:3478,stuns:two.example:5349"):
            self.assertEqual(
                settings.webrtc_stun_urls(),
                ["stun:one.example:3478", "stuns:two.example:5349"],
            )
        with patch.object(settings, "WEBRTC_STUN_URLS", "https://not-stun.example"):
            with self.assertRaises(ValueError):
                settings.webrtc_stun_urls()

    def test_browser_probe_only_reports_srflx_and_closes_peer(self):
        script = (ROOT / "static" / "js" / "network-observation.js").read_text(encoding="utf-8")
        self.assertIn("candidate.type !== 'srflx'", script)
        self.assertIn("peer.close()", script)
        self.assertIn("createDataChannel", script)
        self.assertNotIn("getUserMedia", script)
        self.assertIn("/api/v1/media/network-observation", script)

    def test_all_public_media_templates_load_probe(self):
        for name in ("index.html", "category.html", "player.html"):
            value = (ROOT / "static" / "media" / name).read_text(encoding="utf-8")
            self.assertIn("network-observation.js", value)
            self.assertIn("STUN_URLS_JSON", value)


if __name__ == "__main__":
    unittest.main()
