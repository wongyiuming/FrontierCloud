import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AudioPlayerResponsiveLayoutTests(unittest.TestCase):
    def test_audio_player_uses_live_viewport_and_idle_sidebar_collapse(self):
        template = (ROOT / "static" / "media" / "audio-player.html").read_text(encoding="utf-8")

        self.assertIn('id="playerSidebar"', template)
        self.assertIn('id="sidebarToggle"', template)
        self.assertIn("const PLAYER_SIDEBAR_IDLE_MS = 15000", template)
        self.assertIn("window.visualViewport", template)
        self.assertIn("ResizeObserver", template)
        self.assertIn("--player-sidebar-width", template)
        self.assertIn("--player-viewport-height", template)
        self.assertIn("preferredSidebarWidth", template)
        self.assertIn("metrics.ratio >= 1.15", template)
        self.assertIn("body.classList.toggle('sidebar-collapsed'", template)
        self.assertIn("sidebar.toggleAttribute('inert', collapsed)", template)
        self.assertIn("window.setTimeout(() => setCollapsed(true, false), PLAYER_SIDEBAR_IDLE_MS)", template)

    def test_collapsed_sidebar_gives_layout_width_back_to_player(self):
        template = (ROOT / "static" / "media" / "audio-player.html").read_text(encoding="utf-8")

        self.assertIn(".audio-player-page .player-section {\n            flex: 1 1 auto;", template)
        self.assertIn(".audio-player-page #playerSidebar {\n            flex: 0 0 var(--player-sidebar-width);", template)
        self.assertIn(".audio-player-page.sidebar-collapsed #playerSidebar", template)
        self.assertIn("flex-basis: 0", template)
        self.assertIn("width: 0", template)
        self.assertIn("max-width: 0", template)
        self.assertIn("pointer-events: none", template)
        self.assertIn("position: absolute", template)
        self.assertIn("class=\"sidebar-toggle\"", template)

    def test_player_activity_does_not_force_a_collapsed_sidebar_open(self):
        template = (ROOT / "static" / "media" / "audio-player.html").read_text(encoding="utf-8")

        activity = template.split("const noteActivity = () =>", 1)[1].split("toggle.addEventListener", 1)[0]
        self.assertIn("if (wideLayout && !collapsed) scheduleCollapse();", activity)
        self.assertNotIn("setCollapsed(false", activity)
        self.assertIn("document.addEventListener('pointerdown'", template)
        self.assertIn("document.addEventListener('wheel'", template)
        self.assertIn("document.addEventListener('keydown'", template)

    def test_portrait_layout_keeps_existing_stacked_navigation(self):
        template = (ROOT / "static" / "media" / "audio-player.html").read_text(encoding="utf-8")

        self.assertIn("@media (max-width: 700px) and (orientation: portrait)", template)
        self.assertIn(".sidebar-toggle {\n                display: none;", template)
        self.assertIn("width: 100%", template)
        self.assertIn("pointer-events: auto", template)


if __name__ == "__main__":
    unittest.main()
