from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BrowserUIRegressionContractTests(unittest.TestCase):
    def test_round_one_covers_runtime_behavior_not_only_source_strings(self):
        browser = (ROOT / "tests" / "browser_ui_regression.py").read_text(encoding="utf-8")
        for contract in (
            "home_hit_area_check",
            "logo_cross_page_cache_check",
            "tesla_player_layout_check",
            "admin_focus_check",
            "maintenance_page_check",
        ):
            self.assertIn(f"def {contract}", browser)
        self.assertIn("new_cdp_session", browser)
        self.assertIn("fromDiskCache", browser)
        self.assertIn("PLAYER", browser.upper())
        self.assertIn("15750", browser)
        self.assertIn("sidebar-collapsed", browser)
        self.assertIn("expect_navigation", browser)
        self.assertIn("response.status == 503", browser)


if __name__ == "__main__":
    unittest.main()
