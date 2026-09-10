import unittest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from app.core.config import settings
from app.api.v1 import media
from app.services import network_observation


ROOT = Path(__file__).resolve().parents[1]


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _Connection:
    def __init__(self, rows=None, total=0):
        self.executed = []
        self.rows = rows or []
        self.total = total

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))
        return _Rows(self.rows)

    async def scalar(self, statement, params=None):
        self.executed.append((str(statement), params))
        return self.total


class _Context:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class _Engine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return _Context(self.connection)

    def connect(self):
        return _Context(self.connection)


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
        with patch.object(settings, "SERVER_NAME", "one.example"), patch.object(settings, "WEBRTC_STUN_PORT", 3478):
            self.assertEqual(settings.webrtc_stun_urls(), ["stun:one.example:3478"])
        self.assertNotIn("WEBRTC_STUN_URLS", type(settings).model_fields)

    def test_browser_probe_only_reports_srflx_and_closes_peer(self):
        script = (ROOT / "static" / "js" / "network-observation.js").read_text(encoding="utf-8")
        self.assertIn("candidate.type !== 'srflx'", script)
        self.assertIn("peer.close()", script)
        self.assertIn("createDataChannel", script)
        self.assertIn("iceGatheringState === 'complete'", script)
        self.assertIn("sawIceError = true", script)
        self.assertNotIn("if (!addresses.size) finish('ice_error')", script)
        self.assertNotIn("getUserMedia", script)
        self.assertIn("/api/v1/media/network-observation", script)

    def test_all_public_media_templates_load_probe(self):
        for name in ("index.html", "category.html", "audio-player.html", "video-player.html"):
            value = (ROOT / "static" / "media" / name).read_text(encoding="utf-8")
            self.assertIn("NETWORK_OBSERVATION_JS_URL", value)
            self.assertIn("STUN_URLS_JSON", value)

    def test_media_pages_render_content_versioned_assets_and_no_store_headers(self):
        response = asyncio.run(media.get_media_index_page())
        body = response.body.decode("utf-8")

        self.assertIn("/static/js/network-observation.js?v=", body)
        self.assertNotIn("NETWORK_OBSERVATION_JS_URL", body)
        self.assertEqual(response.headers["cache-control"], "no-store, no-cache, must-revalidate, max-age=0")

        refresh = asyncio.run(media.refresh_media_interface())
        self.assertEqual(refresh.status_code, 303)
        self.assertEqual(refresh.headers["clear-site-data"], '"cache"')

    def test_observation_is_attached_to_the_same_request_log_context(self):
        scope = {
            "type": "http",
            "client": ("172.18.0.10", 32000),
            "headers": [(b"x-real-ip", b"203.0.113.5")],
        }
        request = Request(scope)
        connection = _Connection()
        with (
            patch.object(network_observation.redis_client, "set", new=AsyncMock(return_value=True)),
            patch.object(network_observation, "engine", _Engine(connection)),
        ):
            result = asyncio.run(
                network_observation.record_observation(request, ["198.51.100.7"], None)
            )

        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(scope["webrtc_observation"]["addresses"], ["198.51.100.7"])
        self.assertFalse(scope["webrtc_observation"]["matches_verified"])
        self.assertIn("INSERT INTO webrtc_observation_events", connection.executed[0][0])
        self.assertEqual(connection.executed[0][1][0]["client_ip"], "203.0.113.5")
        self.assertEqual(connection.executed[0][1][0]["webrtc_ip"], "198.51.100.7")
        self.assertIn("INSERT INTO webrtc_observation_summary", connection.executed[1][0])

    def test_summary_can_filter_both_sides_of_the_relationship(self):
        connection = _Connection(rows=[{
            "client_ip": "203.0.113.5",
            "webrtc_ip": "198.51.100.7",
            "observation_count": 10,
            "first_seen": "2026-09-01",
            "last_seen": "2026-09-10",
            "matching_count": 0,
            "outcomes": "ok",
        }], total=1)
        with patch.object(network_observation, "engine", _Engine(connection)):
            result = asyncio.run(network_observation.list_observation_summary(
                public_ip="203.0.113.5",
                webrtc_ip="198.51.100.7",
            ))

        self.assertEqual(result["pagination"]["total"], 1)
        self.assertEqual(result["items"][0]["observation_count"], 10)
        sql = "\n".join(statement for statement, _params in connection.executed)
        self.assertIn("client_ip=:public_ip", sql)
        self.assertIn("webrtc_ip=:webrtc_ip", sql)

    def test_invalid_summary_ip_is_rejected_before_database_access(self):
        with self.assertRaisesRegex(ValueError, "IP 地址无效"):
            asyncio.run(network_observation.list_observation_summary(public_ip="invalid"))


if __name__ == "__main__":
    unittest.main()
