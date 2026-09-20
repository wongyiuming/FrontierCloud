import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SecurityUIContractTests(unittest.TestCase):
    def test_history_filters_pagination_and_reban_are_exposed(self):
        html = (ROOT / "static" / "media" / "admin.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "js" / "admin.js").read_text(encoding="utf-8")

        for element_id in (
            "securityIpFilter",
            "securityStatusFilter",
            "securityIpOrder",
            "securityPrev",
            "securityNext",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("/api/v1/media/admin/security/reban", script)
        self.assertIn("page_size", script)
        self.assertIn("event.reason", script)
        self.assertNotIn("securityScopeFilter", html)
        self.assertNotIn("securityDate", script)
        for value in ("ip_asc", "ip_desc", "last_attack_desc", "last_attack_asc"):
            self.assertIn(f'value="{value}"', html)
        for value in ("active", "observed", "history", "permanent", "whitelisted"):
            self.assertIn(f'value="{value}"', html)
        self.assertNotIn('value="expired"', html)
        self.assertNotIn('value="unbanned"', html)
        self.assertIn("event.last_attack_at", script)
        self.assertIn("event.attack_count", script)

    def test_single_expanded_module_and_permanent_ban_controls_are_exposed(self):
        html = (ROOT / "static" / "media" / "admin.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "js" / "admin.js").read_text(encoding="utf-8")
        style = (ROOT / "static" / "css" / "admin.css").read_text(encoding="utf-8")

        self.assertGreaterEqual(html.count('class="module-heading"'), 6)
        self.assertEqual(html.count('class="admin-module'), 7)
        self.assertIn("function expandAdminModule(target)", script)
        self.assertIn("module === target && shouldExpand", script)
        self.assertIn('id="permanentBanForm"', html)
        self.assertIn("/api/v1/media/admin/security/permanent-ban", script)
        self.assertIn("overflow-y: scroll", style)
        self.assertIn("scrollbar-width: auto", style)

    def test_temporary_key_and_both_grouped_network_views_are_exposed(self):
        html = (ROOT / "static" / "media" / "admin.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "js" / "admin.js").read_text(encoding="utf-8")
        style = (ROOT / "static" / "css" / "admin.css").read_text(encoding="utf-8")

        self.assertIn('id="temporaryKeyForm"', html)
        self.assertIn('value="15" selected', html)
        self.assertIn('value="120"', html)
        self.assertIn("/api/v1/media/admin/key/temporary", script)
        self.assertNotIn('data-network-view="pairs"', html)
        self.assertIn('data-network-view="public"', html)
        self.assertIn('data-network-view="webrtc"', html)
        self.assertIn("function renderNetworkGroups", script)
        self.assertNotIn("function renderNetworkPairList", script)
        self.assertIn(".network-branch::before", style)


if __name__ == "__main__":
    unittest.main()
