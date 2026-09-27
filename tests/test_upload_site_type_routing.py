from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from app.api.v1 import admin_master_mutation_integrity as master_mutation
from app.services import upload_site_routing
from app.services.federation import protocol as p


ROOT = Path(__file__).resolve().parents[1]


def member(member_id: str, *, kind: str = "Follower", transport: str = "Direct",
           health: str = "online", writable: int = 1, enabled: int = 1,
           allocated: int = 1000, used: int = 0, reserved: int = 0,
           available: int = 1000) -> dict:
    return {
        "member_id": member_id,
        "member_kind": kind,
        "transport": transport,
        "health": health,
        "writable": writable,
        "storage_enabled": enabled,
        "allocated_bytes": allocated,
        "used_bytes": used,
        "reserved_bytes": reserved,
        "available_bytes": available,
    }


class UploadSiteRoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_site_type_is_derived_from_existing_member_metadata(self):
        self.assertEqual(
            upload_site_routing.site_type_for_member(
                member("master", kind="MasterLocal", transport="Local")
            ),
            "primary",
        )
        self.assertEqual(upload_site_routing.site_type_for_member(member("d", transport="Direct")), "direct")
        self.assertEqual(upload_site_routing.site_type_for_member(member("r", transport="Relay")), "relay")
        self.assertEqual(upload_site_routing.site_type_for_transport("Direct"), "direct")
        self.assertEqual(upload_site_routing.site_type_for_transport("Relay"), "relay")
        self.assertEqual(upload_site_routing.site_type_for_transport(None), "primary")

    async def test_direct_selection_only_uses_ready_direct_members(self):
        members = [
            member("direct-busy", transport="Direct", used=800, available=200),
            member("direct-ready", transport="Direct", used=100, available=900),
            member("direct-offline", transport="Direct", health="offline", available=1000),
            member("relay-ready", transport="Relay", used=0, available=1000),
        ]
        with patch.object(
            upload_site_routing.resource_pool,
            "list_members",
            new=AsyncMock(return_value=members),
        ):
            selected = await upload_site_routing.choose_member("direct", 100, object())
        self.assertEqual(selected["member_id"], "direct-ready")

    async def test_equal_type_load_is_spread_by_lowest_occupancy_ratio(self):
        members = [
            member("direct-a", transport="Direct", allocated=1000, used=300, available=700),
            member("direct-b", transport="Direct", allocated=2000, used=200, available=1800),
            member("direct-c", transport="Direct", allocated=1000, used=100, reserved=100, available=800),
        ]
        with patch.object(
            upload_site_routing.resource_pool,
            "list_members",
            new=AsyncMock(return_value=members),
        ):
            selected = await upload_site_routing.choose_member("direct", 50, object())
        self.assertEqual(selected["member_id"], "direct-b")

    async def test_site_type_without_ready_member_fails_closed(self):
        with patch.object(
            upload_site_routing.resource_pool,
            "list_members",
            new=AsyncMock(return_value=[member("relay", transport="Relay", health="offline")]),
        ):
            with self.assertRaises(p.ProtocolError):
                await upload_site_routing.choose_member("relay", 1, object())

    def test_master_upload_model_requires_site_type(self):
        with self.assertRaises(ValidationError):
            master_mutation.SiteTypeUploadReservation(
                target_dir="music/artist",
                filename="song.mp3",
                size_bytes=3,
            )
        payload = master_mutation.SiteTypeUploadReservation(
            site_type="direct",
            target_dir="music/artist",
            filename="song.mp3",
            size_bytes=3,
        )
        self.assertEqual(payload.site_type, "direct")


class _SharedLock:
    @asynccontextmanager
    async def shared(self):
        yield self


class MasterUploadSiteRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_resolves_type_to_member_before_existing_reservation_pipeline(self):
        payload = master_mutation.SiteTypeUploadReservation(
            site_type="relay",
            target_dir="music/artist",
            filename="song.mp3",
            size_bytes=10,
        )
        choose = AsyncMock(return_value=member("relay-2", transport="Relay"))
        reserve = AsyncMock(return_value={
            "upload_id": "u" * 32,
            "path": "music/artist/song.mp3",
            "transport": "Relay",
            "member_id": "relay-2",
            "upload_url": "/upload",
        })
        with (
            patch.object(master_mutation, "media_mutation_lock", _SharedLock()),
            patch.object(master_mutation, "ensure_media_mutations_ready"),
            patch.object(master_mutation, "node_state", SimpleNamespace(node={"role": "Master"}, database=object())),
            patch.object(master_mutation.upload_site_routing, "choose_member", new=choose),
            patch.object(master_mutation.masterlocal, "create_upload_session", new=reserve),
        ):
            result = await master_mutation.create_upload_session(payload, object(), "actor")

        choose.assert_awaited_once()
        legacy_payload = reserve.await_args.args[0]
        self.assertEqual(legacy_payload.storage_member_id, "relay-2")
        self.assertEqual(result["site_type"], "relay")
        self.assertEqual(result["site_label"], "中继站点")


class UploadSiteUiContractTests(unittest.TestCase):
    def test_ui_is_blank_until_site_type_is_selected_and_never_posts_member_id(self):
        source = (ROOT / "static/js/admin-upload-integrity.js").read_text(encoding="utf-8")
        self.assertIn("请选择上传站点类型", source)
        self.assertIn("!uploadSiteType?.value", source)
        self.assertIn("site_type: selectedSiteType", source)
        self.assertNotIn("storage_member_id: $('uploadStorageMember').value", source)
        self.assertIn("formData.append('site_type', selectedSiteType)", source)
        self.assertIn("counts.direct", source)
        self.assertIn("counts.relay", source)

    def test_historical_media_uses_transport_to_render_site_type(self):
        source = (ROOT / "static/js/admin-upload-integrity.js").read_text(encoding="utf-8")
        self.assertIn("item?.transport === 'Direct'", source)
        self.assertIn("item?.transport === 'Relay'", source)
        self.assertIn("return 'primary'", source)
        self.assertIn("mediaSiteTypes.set(item.path, siteTypeFromItem(item))", source)

    def test_site_colors_are_subtle_badges_not_row_highlights(self):
        css = (ROOT / "static/css/upload-site-types.css").read_text(encoding="utf-8")
        page = (ROOT / "app/api/v1/admin_page_integrity.py").read_text(encoding="utf-8")
        self.assertIn(".media-site-badge.site-primary", css)
        self.assertIn(".media-site-badge.site-direct", css)
        self.assertIn(".media-site-badge.site-relay", css)
        self.assertGreaterEqual(css.count(".13)"), 3)
        self.assertNotIn(".tree-row.site-", css)
        self.assertIn('static_asset_url("css/upload-site-types.css")', page)


if __name__ == "__main__":
    unittest.main()
